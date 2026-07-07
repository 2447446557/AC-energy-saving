"""中央空调 AI 寻优系统 - 边缘端入口

启动流程：
1. 加载配置（.env + settings.yaml）
2. 配置日志（loguru）
3. 初始化数据库（建表）
4. 创建 FastAPI app，注册中间件
5. 注册路由 + 挂载静态文件（状态页）
6. 启动定时任务（APScheduler）
7. uvicorn.run()

算法模块通过 set_* 注入，默认使用 stub。
Cursor 后续替换为真实实现。
"""

from __future__ import annotations

from pathlib import Path
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.algorithms import (
    ConstraintsStub,
    DataCleanerStub,
    EnergyModelStub,
    OptimizerStub,
)
from app.algorithms.interfaces import (
    IConstraints,
    IDataCleaner,
    IEnergyModel,
    IOptimizer,
)
from app.api.v1.router import create_v1_router
from app.core.config import get_business_config, get_settings
from app.core.logging import setup_logging
from app.middleware.cors import setup_cors
from app.middleware.exception import setup_exception_handler
from app.middleware.request_log import setup_request_log
from app.models.database import init_db
from app.scheduler.scheduler import shutdown_scheduler, start_scheduler
from app.services.mqtt_simulator import MqttSimulatorPublisher
from app.services.mqtt_subscriber import mqtt_subscriber
from app.services.simulator import simulator

# 全局 MQTT 模拟发布器实例（用于模拟真实设备上报）
_mqtt_simulator: MqttSimulatorPublisher | None = None


# ============ 算法模块注入（默认 stub，Cursor 替换） ============

_optimizer: IOptimizer = OptimizerStub()
_energy_model: IEnergyModel = EnergyModelStub()
_data_cleaner: IDataCleaner = DataCleanerStub()
_constraints: IConstraints = ConstraintsStub()


def get_optimizer() -> IOptimizer:
    """获取寻优算法实例"""
    return _optimizer


def get_energy_model() -> IEnergyModel:
    """获取能耗模型实例"""
    return _energy_model


def get_data_cleaner() -> IDataCleaner:
    """获取数据清洗实例"""
    return _data_cleaner


def get_constraints() -> IConstraints:
    """获取约束校验实例"""
    return _constraints


def set_optimizer(impl: IOptimizer) -> None:
    """注入寻优算法实现（Cursor 调用）"""
    global _optimizer
    _optimizer = impl
    logger.info("寻优算法已注入")


def set_energy_model(impl: IEnergyModel) -> None:
    """注入能耗模型实现（Cursor 调用）"""
    global _energy_model
    _energy_model = impl
    logger.info("能耗模型已注入")


def set_data_cleaner(impl: IDataCleaner) -> None:
    """注入数据清洗实现（Cursor 调用）"""
    global _data_cleaner
    _data_cleaner = impl
    logger.info("数据清洗已注入")


def set_constraints(impl: IConstraints) -> None:
    """注入约束校验实现（Cursor 调用）"""
    global _constraints
    _constraints = impl
    logger.info("约束校验已注入")


def bootstrap_cursor_algorithms() -> None:
    """装配并注入 Cursor 实现的核心算法与高仿真度模拟数据生成器。

    替换默认 stub：真实能耗模型、约束校验、数据清洗、PSO 寻优、熔断兜底，
    以及物理自洽的医院空调时序模拟数据生成器。装配后寻优器与数据清洗器
    共享同一实例，形成“清洗→熔断感知→兜底”的闭环鲁棒链路。
    """
    try:
        from app.algorithms.bootstrap import build_algorithms
        from app.services.simulator import simulator

        bundle = build_algorithms()
        set_optimizer(bundle.optimizer)
        set_energy_model(bundle.energy_model)
        set_data_cleaner(bundle.data_cleaner)
        set_constraints(bundle.constraints)
        simulator.set_generator(bundle.generator)
        logger.info("Cursor 核心算法已全部注入并接管闭环")
    except Exception as e:
        # 装配失败不影响服务启动：退化为 stub，保证边缘端可用
        logger.error(f"Cursor 算法装配失败，暂用默认 stub: {e}", exc_info=True)


# ============ FastAPI 应用 ============


def start_mqtt_components() -> None:
    """启动 MQTT 订阅与（可选的）模拟发布器。

    策略：
    - 只要 MQTT 启用（MQTT_ENABLED=true），就启动订阅器
    - 如果 settings.yaml 中 mqtt.simulator_publisher=true，则也启动虚拟发布器，
      用 HospitalDataGenerator 定时向主题播数据，形成 MQTT 端到端的闭环测试
    - 若 MQTT 未启用，维持原有 simulator 生成 SQLite 数据的方式不变
    """
    global _mqtt_simulator
    from app.core.config import get_business_config

    subscriber_started = mqtt_subscriber.start()

    yaml_cfg = get_business_config()
    mqtt_yaml = yaml_cfg.get("mqtt", {}) if isinstance(yaml_cfg, dict) else {}
    simulator_enabled = (
        isinstance(mqtt_yaml, dict) and
        mqtt_yaml.get("simulator_publisher", False) is True
    )
    if subscriber_started and simulator_enabled:
        _mqtt_simulator = MqttSimulatorPublisher(
            device_id=mqtt_yaml.get("simulator_device_id", "device-001"),
        )
        if _mqtt_simulator.start():
            _mqtt_simulator.run_loop()  # 启动独立发布线程（不阻塞主服务）


def stop_mqtt_components() -> None:
    """停止 MQTT 订阅与模拟发布器。"""
    global _mqtt_simulator
    try:
        mqtt_subscriber.stop()
    except Exception as e:
        logger.debug(f"停止 MQTT 订阅器异常（忽略）: {e}")
    if _mqtt_simulator is not None:
        try:
            _mqtt_simulator.stop()
        except Exception as e:
            logger.debug(f"停止 MQTT 模拟发布器异常（忽略）: {e}")
        _mqtt_simulator = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info("边缘端服务启动中...")

    # 兜底初始化数据库（防止 uvicorn 直拉时未建表）
    try:
        init_db()
    except Exception as e:
        logger.debug(f"init_db 跳过（可能已初始化）: {e}")

    # 启动 MQTT 组件（订阅 + 可选的模拟发布器）
    start_mqtt_components()

    # 生成首条本地模拟数据（仅当 MQTT 未启用时保持原有行为）
    from app.core.config import get_settings

    if not get_settings().mqtt_enabled and simulator.is_enabled():
        simulator.generate_once()

    start_scheduler()
    logger.info("边缘端服务已启动")

    yield

    # 关闭
    logger.info("边缘端服务关闭中...")
    stop_mqtt_components()
    shutdown_scheduler()
    logger.info("边缘端服务已关闭")


def create_app() -> FastAPI:
    """创建 FastAPI 应用"""
    settings = get_settings()

    app = FastAPI(
        title="中央空调AI寻优系统 - 边缘端",
        description="医院/政府中央空调 AI 寻优节能项目边缘端服务",
        version="0.1.0",
        debug=settings.app_debug,
        lifespan=lifespan,
    )

    # 中间件
    setup_cors(app)
    setup_exception_handler(app)
    setup_request_log(app)

    # 路由
    app.include_router(create_v1_router())

    # 静态文件（状态页）
    static_dir = Path(__file__).parent / "api" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def main() -> None:
    """主入口"""
    # 1. 配置日志
    setup_logging()

    # 2. 加载业务配置
    config = get_business_config()
    logger.info(f"业务配置已加载: {len(config)} 个模块")

    # 3. 初始化数据库
    init_db()
    logger.info("数据库已初始化")

    # 4. 创建应用
    app = create_app()

    # 5. 启动服务
    settings = get_settings()
    logger.info(
        f"启动边缘端服务: {settings.app_host}:{settings.app_port}"
    )

    uvicorn.run(
        app,
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()


# 模块级注入 Cursor 算法：无论通过 main() 还是 uvicorn app.main:app 直拉，
# 均在应用创建前完成真实算法接管，避免走 stub。
bootstrap_cursor_algorithms()

# 模块级 app，便于 uvicorn app.main:app 直拉启动
# 注意：通过 uvicorn 启动时不会执行 main()，setup_logging() 不会自动调用，
# 日志将使用 loguru 默认 handler；init_db() 由 lifespan 兜底。
app = create_app()
