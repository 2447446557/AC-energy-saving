# 边缘端工程框架搭建计划（Trae 负责）

## Context（背景）

`AC-energy-saving` 是医院/政府中央空调 AI 寻优节能项目，目前项目目录为空，仅有两份需求文档：

1. `Cursor&Trae AI开发工具专属需求文档` —— 定义 Trae 与 Cursor 的分工边界
2. `中央空调AI寻优系统——边缘端开发设计文档（正式版）` —— 定义边缘端八大模块与技术栈

**本次开发范围**：按文档分工，**只做 Trae 负责的边缘端工程框架部分**，不做云端、不做核心算法。核心算法（PSO 寻优、能耗模型、数据清洗、熔断兜底）由 Cursor 后续实现，Trae 只做接口封装与参数透传。

**目标产出**：一个可直接 `pip install -r requirements.txt && python -m app.main` 运行的 FastAPI 边缘端工程骨架，包含完整脚手架、Docker 部署、算法接入 stub，Cursor 后续只需替换 stub 即可。

---

## 技术栈选型

| 组件 | 选型 | 理由 |
|---|---|---|
| Python | 3.10+ | 现代语法（match、type union） |
| Web 框架 | FastAPI 0.110+ | 文档指定，轻量高性能 |
| ASGI 服务器 | Uvicorn | FastAPI 标配 |
| ORM | SQLModel | 基于 SQLAlchemy+Pydantic，集成度高、轻量，适合边缘网关 |
| 定时任务 | APScheduler | Python 生态最成熟的调度库 |
| 日志 | loguru | 配置简单、功能完整 |
| HTTP 客户端 | httpx | 异步、用于云端同步接口 |
| 配置管理 | pydantic-settings | 类型安全的环境变量加载 |
| 测试 | pytest | 标准 |

---

## 目录结构

```
AC-energy-saving/
└── edge/                                    ← 边缘端根目录（用户确认）
    ├── app/
    │   ├── __init__.py
    │   ├── main.py                          ← FastAPI 入口，装配所有组件
    │   │
    │   ├── core/                            ← 核心基础设施
    │   │   ├── __init__.py
    │   │   ├── config.py                    ← Settings 类（pydantic-settings）
    │   │   ├── logging.py                   ← loguru 配置（控制台+文件轮转）
    │   │   ├── constants.py                 ← 常量（任务名、默认周期等）
    │   │   └── errors.py                    ← 错误码定义
    │   │
    │   ├── api/                             ← API 路由层
    │   │   ├── __init__.py
    │   │   ├── deps.py                      ← 依赖注入（DB 会话等）
    │   │   ├── v1/
    │   │   │   ├── __init__.py
    │   │   │   ├── router.py                ← 聚合所有 v1 路由
    │   │   │   ├── system.py                ← /system 健康检查、版本
    │   │   │   ├── optimize.py              ← /optimize 触发寻优、查询结果
    │   │   │   ├── data.py                  ← /data 实时/历史数据
    │   │   │   ├── control.py               ← /control 下发控制指令
    │   │   │   └── status.py                ← /status 状态页数据接口
    │   │   └── static/
    │   │       └── index.html               ← 极简状态页（单文件 HTML+JS）
    │   │
    │   ├── schemas/                         ← Pydantic 数据模型
    │   │   ├── __init__.py
    │   │   ├── common.py                    ← Response[T] 统一返回体
    │   │   ├── optimize.py                  ← OptimizeRequest/Result
    │   │   ├── device.py                    ← DeviceData 工况数据
    │   │   ├── control.py                   ← ControlCommand 控制指令
    │   │   └── status.py                    ← StatusInfo 状态页数据
    │   │
    │   ├── models/                          ← SQLite ORM（SQLModel）
    │   │   ├── __init__.py
    │   │   ├── database.py                  ← engine、get_session、init_db
    │   │   ├── base.py                      ← TimestampModel 基类
    │   │   ├── runtime_data.py              ← 运行工况缓存表
    │   │   ├── optimize_record.py           ← 寻优记录表
    │   │   ├── alarm_log.py                 ← 告警日志表
    │   │   └── operation_log.py             ← 操作日志表
    │   │
    │   ├── services/                        ← 业务服务层
    │   │   ├── __init__.py
    │   │   ├── storage.py                   ← 本地存储封装（CRUD）
    │   │   ├── sync.py                      ← 云端同步接口（httpx stub）
    │   │   ├── simulator.py                 ← 模拟数据生成框架（注入点）
    │   │   ├── reconnect.py                 ← 指数退避重连工具
    │   │   └── mqtt_client.py               ← MQTT 客户端封装（备用）
    │   │
    │   ├── algorithms/                      ← 算法接入（仅接口+stub）
    │   │   ├── __init__.py
    │   │   ├── interfaces.py                ← Protocol 接口定义
    │   │   ├── optimizer_stub.py            ← PSO 寻优空实现
    │   │   ├── energy_model_stub.py         ← 能耗模型空实现
    │   │   ├── data_cleaner_stub.py         ← 数据清洗空实现
    │   │   └── constraints_stub.py          ← 约束校验空实现
    │   │
    │   ├── scheduler/                       ← 定时任务基座
    │   │   ├── __init__.py
    │   │   ├── scheduler.py                 ← APScheduler 实例+生命周期
    │   │   └── tasks/
    │   │       ├── __init__.py
    │   │       ├── optimize_task.py         ← 10~15min 寻优（调 stub）
    │   │       ├── sync_task.py             ← 云端同步周期任务
    │   │       └── cleanup_task.py          ← 本地数据清理
    │   │
    │   └── middleware/                      ← 中间件
    │       ├── __init__.py
    │       ├── cors.py                      ← CORS 配置
    │       ├── exception.py                 ← 全局异常→统一返回体
    │       └── request_log.py               ← 请求耗时日志
    │
    ├── config/
    │   ├── .env.example                     ← 环境变量示例
    │   └── settings.yaml                    ← 业务配置（约束阈值、周期等）
    │
    ├── logs/                                ← 日志目录
    │   └── .gitkeep
    │
    ├── data/                                ← SQLite 数据目录
    │   └── .gitkeep
    │
    ├── tests/
    │   ├── __init__.py
    │   ├── conftest.py                      ← pytest fixtures
    │   ├── test_api_system.py               ← 健康检查接口测试
    │   ├── test_storage.py                  ← 存储封装测试
    │   └── test_simulator.py                ← 模拟器框架测试
    │
    ├── Dockerfile                           ← 多阶段构建
    ├── docker-compose.yml                   ← 编排+卷挂载
    ├── .dockerignore
    ├── requirements.txt                     ← Python 依赖
    ├── .env.example                         ← 根级环境变量示例
    ├── run.sh                               ← Linux 启动脚本
    ├── run.bat                              ← Windows 启动脚本
    ├── README.md                            ← 项目说明
    └── DEVELOPMENT.md                       ← 开发文档
```

---

## 关键设计点

### 1. 算法接入接口（`app/algorithms/interfaces.py`）

用 `typing.Protocol` 定义四个接口，stub 文件给出空实现（返回固定值或 `NotImplementedError`）。Cursor 后续只需新增 `optimizer_skopt.py` 等实现类，在 `main.py` 中替换注入即可。

```python
from typing import Protocol
from app.schemas.optimize import OptimizeRequest, OptimizeResult
from app.schemas.device import DeviceData

class IOptimizer(Protocol):
    def optimize(self, request: OptimizeRequest) -> OptimizeResult: ...

class IEnergyModel(Protocol):
    def calculate(self, data: DeviceData, params: dict) -> float: ...

class IDataCleaner(Protocol):
    def clean(self, raw: DeviceData) -> DeviceData: ...

class IConstraints(Protocol):
    def validate(self, params: dict) -> bool: ...
```

### 2. 配置分层

- `.env`：部署环境相关（端口、日志级别、DB 路径、云端地址）
- `config/settings.yaml`：业务配置（寻优周期、约束阈值、模拟器开关）

`Settings` 类用 `pydantic-settings` 加载 `.env`，业务配置用 `yaml` 单独加载。

### 3. 统一返回体（`schemas/common.py`）

```python
class Response(BaseModel, Generic[T]):
    code: int = 0
    message: str = "success"
    data: T | None = None
```

所有 API 返回 `Response[T]`，中间件把异常也转成这个结构。

### 4. 启动流程（`main.py`）

```
1. Settings 加载（.env + settings.yaml）
2. 配置 loguru（控制台 + logs/*.log 按天轮转）
3. init_db() 建表
4. 创建 FastAPI app，注册中间件（CORS / 异常 / 请求日志）
5. 注册 v1 路由 + 挂载静态文件（状态页）
6. 启动 APScheduler（寻优/同步/清理任务）
7. uvicorn.run(app, host, port)
```

### 5. 状态页（`api/static/index.html`）

单文件 HTML，用 fetch 轮询 `/api/v1/status` 接口，展示：
- 服务运行状态、版本
- 当前寻优结果（最近一次参数）
- 最近 5 条告警
- 设备在线状态

不引入任何前端框架，纯原生 JS。

### 6. Docker 部署

- `Dockerfile`：基于 `python:3.11-slim`，多阶段构建
- `docker-compose.yml`：挂载 `./data:/app/data`、`./logs:/app/logs`、`./config:/app/config`
- 端口暴露 8000

### 7. 模拟数据生成框架（`services/simulator.py`）

只做框架：定义 `DataGenerator` Protocol + 调度入口。具体生成逻辑（医院工况时序数据）由 Cursor 实现。Trae 提供 stub 返回合理的默认值，让闭环能跑通。

### 8. 云端同步（`services/sync.py`）

`httpx.AsyncClient` + 指数退避重连。仅定义接口和调用框架，实际云端地址在 `.env` 配置。用户明确不做云端，所以这边只做客户端侧的上报 stub。

---

## 实施步骤

| 步骤 | 内容 | 文件 |
|---|---|---|
| 1 | 创建目录骨架 + `.gitkeep` | `edge/` 全部目录 |
| 2 | 写依赖清单 | `requirements.txt` |
| 3 | 核心基础设施 | `core/{config,logging,constants,errors}.py` |
| 4 | 数据模型 | `schemas/{common,optimize,device,control,status}.py` |
| 5 | ORM 模型 | `models/{database,base,runtime_data,optimize_record,alarm_log,operation_log}.py` |
| 6 | 算法接口+stub | `algorithms/{interfaces,optimizer_stub,energy_model_stub,data_cleaner_stub,constraints_stub}.py` |
| 7 | 业务服务 | `services/{storage,sync,simulator,reconnect,mqtt_client}.py` |
| 8 | 中间件 | `middleware/{cors,exception,request_log}.py` |
| 9 | API 路由 | `api/v1/{router,system,optimize,data,control,status}.py` + `api/deps.py` |
| 10 | 状态页 | `api/static/index.html` |
| 11 | 定时任务 | `scheduler/{scheduler,tasks/*}.py` |
| 12 | 入口 | `main.py` |
| 13 | 配置文件 | `config/{.env.example,settings.yaml}` + `.env.example` |
| 14 | Docker | `Dockerfile`、`docker-compose.yml`、`.dockerignore` |
| 15 | 启动脚本 | `run.sh`、`run.bat` |
| 16 | 测试 | `tests/{conftest,test_api_system,test_storage,test_simulator}.py` |
| 17 | 文档 | `README.md`、`DEVELOPMENT.md` |

---

## 边界确认（Trae 不做）

- ❌ PSO 寻优核心算法（只做 stub）
- ❌ 精细化能耗数学模型（只做 stub）
- ❌ 高精度数据清洗/插值（只做 stub）
- ❌ 熔断兜底/参数平滑/安全约束校验（只做 stub）
- ❌ 云端服务（CRUD/前端/大屏）
- ❌ 高仿真度模拟数据生成逻辑（只做框架，stub 返回默认值）

---

## 验证方式

完成后执行以下验证：

1. **依赖安装**：`cd edge && pip install -r requirements.txt`
2. **启动服务**：`python -m app.main` 或 `python app/main.py`
3. **健康检查**：`curl http://localhost:8000/api/v1/system/health` → 返回 `{"code":0,"message":"success","data":{"status":"ok"}}`
4. **API 文档**：浏览器打开 `http://localhost:8000/docs` → Swagger UI 可见所有接口
5. **状态页**：浏览器打开 `http://localhost:8000/` → 极简状态页可显示
6. **寻优 stub**：`POST /api/v1/optimize/run` → 返回 stub 结果（固定值或默认参数）
7. **定时任务**：日志中可见寻优任务每 10 分钟触发一次（测试时改 30s）
8. **SQLite**：`data/edge.db` 文件生成，表结构正确
9. **Docker**：`docker-compose up -d` → 容器正常启动，端口可访问
10. **测试**：`pytest tests/` → 全部通过
