# 配置目录说明

**运行时权威配置在 `edge/config/`**（以及 SQLite `edge/data/*.db` 中的覆盖）。

本目录（仓库根 `config/`）不是边缘服务启动路径，请勿在此改参数后期望生效。
请编辑：

- `edge/config/settings.yaml`
- `edge/config/equipment.json`

或通过管理页 `http://localhost:8000/` 保存（写入数据库 + 备份到 `edge/config/`）。
