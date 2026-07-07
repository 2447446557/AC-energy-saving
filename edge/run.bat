@echo off
REM ============ 边缘端启动脚本（Windows） ============

cd /d "%~dp0"

REM 检查 .env 是否存在
if not exist ".env" (
    echo [INFO] 未找到 .env，从 .env.example 复制
    copy .env.example .env
)

REM 检查虚拟环境
if not exist "venv" (
    echo [INFO] 创建虚拟环境
    python -m venv venv
)

REM 激活虚拟环境
call venv\Scripts\activate

REM 安装依赖
echo [INFO] 检查依赖...
pip install -r requirements.txt -q

REM 启动服务
echo [INFO] 启动边缘端服务...
python -m app.main

pause
