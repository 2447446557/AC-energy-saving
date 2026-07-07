# 开发文档

## 目录结构

```
edge/
├── app/
│   ├── main.py                  # FastAPI 入口，算法注入接口
│   ├── core/                    # 核心基础设施
│   │   ├── config.py            # 配置加载（.env + settings.yaml）
│   │   ├── logging.py           # loguru 日志配置
│   │   ├── constants.py         # 常量定义
│   │   └── errors.py            # 错误码定义
│   ├── api/                     # API 路由层
│   │   ├── deps.py              # 依赖注入
│   │   ├── v1/                  # v1 接口
│   │   │   ├── router.py        # 路由聚合
│   │   │   ├── system.py        # 系统接口
│   │   │   ├── optimize.py      # 寻优接口
│   │   │   ├── data.py          # 数据接口
│   │   │   ├── control.py       # 控制接口
│   │   │   └── status.py        # 状态页接口
│   │   └── static/
│   │       └── index.html       # 极简状态页
│   ├── schemas/                 # Pydantic 数据模型
│   │   ├── common.py            # 统一返回体 Response[T]
│   │   ├── optimize.py          # 寻优请求/响应
│   │   ├── device.py            # 设备工况数据
│   │   ├── control.py           # 控制指令
│   │   └── status.py            # 状态页数据
│   ├── models/                  # SQLite ORM（SQLModel）
│   │   ├── database.py          # 引擎/会话/建表
│   │   ├── base.py              # 基类
│   │   ├── runtime_data.py      # 运行数据表
│   │   ├── optimize_record.py   # 寻优记录表
│   │   ├── alarm_log.py         # 告警日志表
│   │   └── operation_log.py     # 操作日志表
│   ├── services/                # 业务服务层
│   │   ├── storage.py           # 本地存储 CRUD
│   │   ├── sync.py              # 云端同步（httpx stub）
│   │   ├── simulator.py         # 模拟数据生成框架
│   │   ├── reconnect.py         # 指数退避重连
│   │   └── mqtt_client.py       # MQTT 客户端（备用）
│   ├── algorithms/              # 算法接入（接口 + stub）
│   │   ├── interfaces.py        # Protocol 接口定义
│   │   ├── optimizer_stub.py    # PSO 寻优 stub
│   │   ├── energy_model_stub.py # 能耗模型 stub
│   │   ├── data_cleaner_stub.py # 数据清洗 stub
│   │   └── constraints_stub.py  # 约束校验 stub
│   ├── scheduler/               # 定时任务基座
│   │   ├── scheduler.py         # APScheduler 实例
│   │   └── tasks/
│   │       ├── optimize_task.py # 寻优任务（10~15min）
│   │       ├── sync_task.py     # 云端同步任务
│   │       └── cleanup_task.py  # 数据清理任务
│   └── middleware/              # 中间件
│       ├── cors.py              # CORS 跨域
│       ├── exception.py         # 全局异常捕获
│       └── request_log.py       # 请求日志
├── config/
│   ├── .env.example             # 环境变量示例
│   └── settings.yaml            # 业务配置
├── logs/                        # 日志目录
├── data/                        # SQLite 数据目录
├── tests/                       # 测试
├── Dockerfile                   # Docker 构建
├── docker-compose.yml           # Docker 编排
├── requirements.txt             # Python 依赖
├── .env.example                 # 环境变量示例
├── run.sh / run.bat             # 启动脚本
└── README.md                    # 项目说明
```

## 分工边界（Trae vs Cursor）

### Trae 负责（已完成）

- 项目工程框架、目录结构、脚手架
- FastAPI 服务、中间件、路由、统一返回体
- SQLite ORM 模型、存储封装
- 定时任务基座（APScheduler）
- 模拟数据生成框架（默认 stub）
- 云端同步接口框架（httpx stub）
- Docker 部署脚本
- 算法接入接口定义（Protocol）+ 空实现 stub
- 极简状态页

### Cursor 负责（已实现）

| 能力 | 实现文件 | 接口 |
|---|---|---|
| PSO 寻优核心算法（scikit-opt，超时/收敛/异常兜底） | `app/algorithms/optimizer.py` → `PSOOptimizer` | `IOptimizer` |
| 精细化能耗数学模型（卡诺 COP + 相似定律 + 定点迭代） | `app/algorithms/energy_model.py` → `ACEnergyModel` | `IEnergyModel` |
| 高精度数据清洗（跳变过滤/缺失插值/EWMA 平滑/连续异常熔断） | `app/algorithms/data_cleaner.py` → `RobustDataCleaner` | `IDataCleaner` |
| 安全约束校验（硬约束/裁剪/惩罚） | `app/algorithms/constraints.py` → `SafetyConstraints` | `IConstraints` |
| 熔断兜底与参数阶梯平滑输出 | `app/algorithms/fallback.py` → `SafeOutputGuard` | — |
| 高仿真度医院空调时序模拟（含极端场景注入） | `app/services/hospital_simulator.py` → `HospitalDataGenerator` | `DataGenerator` |
| 算法装配与注入 | `app/algorithms/bootstrap.py` → `build_algorithms()` | — |

装配入口：`app/main.py` 的 `bootstrap_cursor_algorithms()` 在应用创建前调用
`build_algorithms()` 并通过 `set_*` / `simulator.set_generator` 注入真实实现，
`python -m app.main` 与 `uvicorn app.main:app` 两种启动方式均自动接管闭环。
算法模块测试见 `tests/test_algorithms.py`。

## 算法接入指南

### 1. 实现接口

```python
# my_optimizer.py
from app.algorithms.interfaces import IOptimizer
from app.schemas.optimize import OptimizeRequest, OptimizeResult

class MyPSOOptimizer:
    def optimize(self, request: OptimizeRequest) -> OptimizeResult:
        # 基于 scikit-opt 的 PSO 实现
        ...
```

### 2. 注入实现

```python
# 在 app/main.py 的 main() 函数中，create_app() 之前
from app.main import set_optimizer
from my_optimizer import MyPSOOptimizer

set_optimizer(MyPSOOptimizer())
```

### 3. 替换模拟器

```python
from app.services.simulator import simulator
from my_generator import HospitalDataGenerator

simulator.set_generator(HospitalDataGenerator())
```

## 部署说明

### 本地开发

```bash
cd edge
pip install -r requirements.txt
python -m app.main
```

### Docker 部署

```bash
cd edge
docker-compose up -d
```

### 边缘网关部署

1. 将 `edge/` 目录拷贝至网关
2. 执行 `./run.sh`（自动创建虚拟环境、安装依赖、启动）
3. 或构建 Docker 镜像后运行

## 数据库表结构

| 表名 | 说明 |
|---|---|
| runtime_data | 运行工况数据缓存 |
| optimize_record | 寻优历史记录 |
| alarm_log | 告警日志 |
| operation_log | 操作日志 |

所有表均含 `created_at`、`updated_at` 时间戳字段。
