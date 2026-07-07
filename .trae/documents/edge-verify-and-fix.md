# 边缘端工程框架 - 验证与修复计划

## 一、背景

上一轮会话已根据两份需求文档（`Cursor&Trae AI开发工具专属需求文档`、`中央空调AI寻优系统——边缘端开发设计文档（正式版）`）完成 Trae 负责的边缘端工程框架开发，共创建 68 个文件位于 `d:\project\AC-energy-saving\edge\`。

本轮目标：**对已生成的代码进行静态审计、修复缺陷、执行验证，确保框架可正常运行**。仍严格遵循 Trae 职责边界——不开发核心算法（PSO、能耗模型、数据清洗、熔断兜底），仅维护工程框架。

## 二、当前状态分析

### 已完成
- 完整目录结构（app/core、app/schemas、app/models、app/algorithms、app/services、app/middleware、app/api/v1、app/scheduler、tests、config）
- 四个算法 Protocol 接口 + 四个 stub 空实现
- FastAPI 应用入口（main.py）+ 中间件 + 路由 + 静态状态页
- SQLModel ORM 四张表（runtime_data、optimize_record、alarm_log、operation_log）
- APScheduler 三个定时任务（寻优、同步、清理）
- Docker 多阶段构建 + docker-compose 编排
- pytest 测试用例 3 个文件

### 静态审计发现的问题

#### 高严重度（影响功能或部署）

| # | 问题 | 文件 | 影响 |
|---|------|------|------|
| H1 | CORS 配置冲突：`allow_origins=["*"]` + `allow_credentials=True` | `app/middleware/cors.py` | Starlette 警告，浏览器拒绝带凭证跨域请求 |
| H2 | `main.py` 未暴露模块级 `app`，`uvicorn app.main:app` 失败 | `app/main.py` | 偏离 FastAPI 惯例，开发热重载不便 |
| H3 | `create_app()` 未调用 `init_db()`，uvicorn 直拉时数据库未建表 | `app/main.py` | 非 `python -m` 启动方式会运行异常 |
| H4 | `requirements.txt` 重复声明 `httpx==0.27.0`（第 19、27 行） | `requirements.txt` | pip 能容忍，但不规范 |
| H5 | `docker-compose.yml` 的 `version: "3.8"` 已被 Compose v2 弃用 | `docker-compose.yml` | 部署时打印警告 |

#### 中严重度（影响代码健壮性）

| # | 问题 | 文件 | 影响 |
|---|------|------|------|
| M1 | `models/database.py` 模块级创建 engine，`@lru_cache` 锁定配置后无法覆盖 | `app/models/database.py` | 测试隔离脆弱，环境变量须在导入前就绪 |
| M2 | `run_sync` 是同步函数但 `cloud_sync` 的方法是 async，缺少调用范式 | `app/scheduler/tasks/sync_task.py` | Cursor 接入时易踩坑 |
| M3 | `requirements.txt` 缺少 `python-dateutil`（APScheduler 依赖） | `requirements.txt` | 通常自动安装，显式声明更稳妥 |
| M4 | `test_api_system.py` 用 `TestClient(app)` 不进入 `with` 上下文 | `tests/test_api_system.py` | 新版 Starlette 行为变化时易出问题 |
| M5 | `app/api/deps.py` 的 `get_storage()` 是死代码，未被任何路由 `Depends` 引用 | `app/api/deps.py` | 冗余 |

#### 低严重度

| # | 问题 | 文件 | 影响 |
|---|------|------|------|
| L1 | `config/.env.example` 与根 `.env.example` 内容重复 | `config/.env.example` | 易产生不一致 |
| L2 | `OptimizeRequest.device_data` 是 `dict`，与 `optimize_task.py` 中 `DeviceData(**raw_data)` 解包方式耦合 | `app/schemas/optimize.py` | 类型不明确 |

## 三、修复方案

### 修复 1：CORS 配置（H1）
**文件**：[app/middleware/cors.py](file:///d:/project/AC-energy-saving/edge/app/middleware/cors.py)

将 `allow_credentials=True` 改为 `allow_credentials=False`。边缘端本地服务不需要跨域凭证。

### 修复 2：暴露模块级 app + lifespan 兜底 init_db（H2 + H3）
**文件**：[app/main.py](file:///d:/project/AC-energy-saving/edge/app/main.py)

- 在 `lifespan()` 启动阶段增加 `init_db()` 兜底调用（带 `try/except` 防止重复建表报错）
- 在模块末尾增加 `app = create_app()`，使 `uvicorn app.main:app` 可用
- `setup_logging()` 仍只在 `main()` 中调用（避免导入副作用）；uvicorn 启动时由日志配置默认 handler 即可

### 修复 3：requirements.txt 去重 + 补依赖（H4 + M3）
**文件**：[requirements.txt](file:///d:/project/AC-energy-saving/edge/requirements.txt)

- 删除第 27 行重复的 `httpx==0.27.0`
- 添加 `python-dateutil>=2.8.2`
- 考虑到本机 Python 3.14 较新，对部分锁版本的依赖放宽为兼容范围（如 `fastapi>=0.110.0`、`sqlmodel>=0.0.16`、`pydantic-settings>=2.2.1`），避免安装失败

### 修复 4：docker-compose 删除 version 字段（H5）
**文件**：[docker-compose.yml](file:///d:/project/AC-energy-saving/edge/docker-compose.yml)

删除 `version: "3.8"` 行。

### 修复 5：database engine 懒加载（M1）
**文件**：[app/models/database.py](file:///d:/project/AC-energy-saving/edge/app/models/database.py)

- 移除模块级 `engine = create_engine(**_get_engine_kwargs())`
- 增加 `get_engine()` 函数 + `@lru_cache` 单例
- `init_db()` 和 `get_session()` 改用 `get_engine()`

### 修复 6：run_sync 提供 async 调用范式（M2）
**文件**：[app/scheduler/tasks/sync_task.py](file:///d:/project/AC-energy-saving/edge/app/scheduler/tasks/sync_task.py)

保留 stub 框架，但增加一个 `_run_sync_async()` async 函数示范正确的 async 调用方式，`run_sync()` 用 `asyncio.run()` 包裹。这样 Cursor 接入时能直接参考。

### 修复 7：清理死代码（M5 + L1）
- 删除 `app/api/deps.py`（未被引用）
- 删除 `config/.env.example`（与根 `.env.example` 重复）

### 修复 8：测试用例改进（M4）
**文件**：[tests/test_api_system.py](file:///d:/project/AC-energy-saving/edge/tests/test_api_system.py)

`TestClient` 改为 `with TestClient(app) as client:` 形式，显式管理 lifespan。但因 lifespan 会启动调度器，需在 conftest 中通过环境变量禁用调度器，或修改 lifespan 在测试模式下不启动调度器。

更简洁的方案：保持现状（不进入 `with`），但添加注释说明。考虑到当前测试能通过，**此项标记为可选**。

## 四、验证步骤

按以下顺序执行验证（每步通过后再进行下一步）：

### 步骤 1：依赖安装
```powershell
cd D:\project\AC-energy-saving\edge
pip install -r requirements.txt
```
**预期**：所有依赖安装成功，无报错。
**风险**：Python 3.14 较新，若某依赖无对应 wheel，需放宽版本约束。

### 步骤 2：单元测试
```powershell
pytest tests/ -v
```
**预期**：所有测试用例通过（test_api_system、test_storage、test_simulator）。

### 步骤 3：服务启动
```powershell
python -m app.main
```
**预期**：服务启动，日志输出"边缘端服务已启动"，监听 0.0.0.0:8000。

### 步骤 4：健康检查
```powershell
curl http://localhost:8000/api/v1/system/health
```
**预期**：返回 `{"code":0,"message":"success","data":{"status":"ok","version":"0.1.0","uptime":"..."}}`

### 步骤 5：状态页
浏览器访问 `http://localhost:8000/`，应看到极简状态页，每 5 秒轮询。

### 步骤 6：触发寻优
```powershell
curl -X POST http://localhost:8000/api/v1/optimize/run -H "Content-Type: application/json" -d "{\"device_data\": {}}"
```
**预期**：返回 stub 寻优结果（chilled_water_temp=7.0 等）。

### 步骤 7：实时数据接口
```powershell
curl http://localhost:8000/api/v1/data/realtime
```
**预期**：返回一条模拟工况数据。

## 五、Assumptions & Decisions

1. **不做核心算法**：PSO 寻优、能耗模型、数据清洗、约束校验、熔断兜底均保持 stub，由 Cursor 后续实现。
2. **不做云端**：`cloud_sync` 仅保留客户端框架，`CLOUD_SYNC_ENABLED=false` 默认关闭。
3. **不修改需求文档**：两份需求文档作为只读输入。
4. **Python 版本**：本机 Python 3.14.6，需放宽依赖版本约束以适配。
5. **测试隔离**：当前测试用 `data/test.db` 共享 session，不强求改进，优先保证能通过。
6. **不验证 Docker 构建**：内网环境可能无法拉取 `python:3.11-slim` 镜像，Docker 构建验证标记为可选。

## 六、实施顺序

1. 修复 H1（CORS）→ 修复 H2+H3（main.py 暴露 app + lifespan init_db）→ 修复 H4+M3（requirements.txt）
2. 修复 H5（docker-compose version）→ 修复 M1（database 懒加载）→ 修复 M2（run_sync async 范式）
3. 清理 M5+L1（删除 deps.py 和 config/.env.example）
4. 执行验证步骤 1-7
5. 若验证失败，根据错误信息回溯修复

## 七、不修改的文件

以下文件经审计确认无问题，本轮不修改：
- `app/algorithms/*`（接口与 stub 完整）
- `app/schemas/*`（数据模型完整）
- `app/models/{base,runtime_data,optimize_record,alarm_log,operation_log}.py`（ORM 完整）
- `app/services/{storage,simulator,reconnect,sync,mqtt_client}.py`（业务服务完整）
- `app/api/v1/{router,system,optimize,data,status,control}.py`（路由完整）
- `app/middleware/{exception,request_log}.py`（中间件完整）
- `app/scheduler/{scheduler,tasks/optimize_task,tasks/cleanup_task}.py`（调度完整）
- `app/core/{config,constants,errors,logging}.py`（基础设施完整）
- `app/api/static/index.html`（状态页完整）
- `Dockerfile`、`run.sh`、`run.bat`、`README.md`、`DEVELOPMENT.md`（部署与文档完整）
- `config/settings.yaml`、`.env.example`（配置完整）
