# 中央空调AI寻优系统 - 边缘端

医院/政府中央空调 AI 寻优节能项目边缘端服务。基于 FastAPI 构建，支持断网自治、本地存储、定时寻优。

## 快速开始

### 环境要求

- Python 3.10+
- pip

### 安装与运行

```bash
# 1. 进入边缘端目录
cd edge

# 2. 复制环境配置
cp .env.example .env

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动服务
python -m app.main
```

服务启动后：
- API 服务：http://localhost:8000
- 状态页：http://localhost:8000/
- Swagger 文档：http://localhost:8000/docs

### 一键启动（自动创建虚拟环境）

**Linux:**
```bash
chmod +x run.sh && ./run.sh
```

**Windows:**
```cmd
run.bat
```

## Docker 部署

```bash
# 构建并启动
docker-compose up -d

# 查看日志
docker logs -f ac-edge

# 停止
docker-compose down
```

## 配置说明

### 环境变量（.env）

| 变量 | 默认值 | 说明 |
|---|---|---|
| APP_HOST | 0.0.0.0 | 服务监听地址 |
| APP_PORT | 8000 | 服务端口 |
| APP_DEBUG | true | 调试模式 |
| LOG_LEVEL | INFO | 日志级别 |
| SQLITE_PATH | data/edge.db | SQLite 路径 |
| CLOUD_SYNC_ENABLED | false | 是否启用云端同步 |
| MQTT_ENABLED | false | 是否启用 MQTT |

### 业务配置（config/settings.yaml）

- `optimize.interval_minutes`：寻优周期（分钟）
- `simulator.enabled`：是否启用模拟数据
- `constraints`：设备安全约束阈值
- `cleanup`：数据清理配置

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/v1/system/health | 健康检查 |
| GET | /api/v1/system/version | 版本信息 |
| POST | /api/v1/optimize/run | 触发寻优 |
| GET | /api/v1/optimize/latest | 最近寻优结果 |
| GET | /api/v1/optimize/history | 寻优历史 |
| GET | /api/v1/data/realtime | 实时工况数据 |
| POST | /api/v1/data/simulate | 触发模拟生成 |
| POST | /api/v1/control/send | 下发控制指令 |
| GET | /api/v1/status/ | 状态页数据 |

## 算法接入

当前使用 stub 空实现。Cursor 后续替换为真实算法：

```python
# 在 main.py 启动前注入
from app.main import set_optimizer, set_energy_model
from my_impl import MyOptimizer, MyEnergyModel

set_optimizer(MyOptimizer())
set_energy_model(MyEnergyModel())
```

接口定义见 `app/algorithms/interfaces.py`。

## 测试

```bash
pytest tests/ -v
```

## 目录结构

见 [DEVELOPMENT.md](DEVELOPMENT.md)
