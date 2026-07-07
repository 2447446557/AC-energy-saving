#!/bin/bash
# ============ 边缘端启动脚本（Linux） ============

set -e

# 切换到脚本所在目录
cd "$(dirname "$0")"

# 检查 .env 是否存在
if [ ! -f ".env" ]; then
    echo "[INFO] 未找到 .env，从 .env.example 复制"
    cp .env.example .env
fi

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "[INFO] 创建虚拟环境"
    python3 -m venv venv
fi

# 激活虚拟环境
source venv/bin/activate

# 安装依赖
echo "[INFO] 检查依赖..."
pip install -r requirements.txt -q

# 启动服务
echo "[INFO] 启动边缘端服务..."
python -m app.main
