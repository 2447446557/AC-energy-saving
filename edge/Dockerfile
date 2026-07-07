# ============ 中央空调AI寻优系统 - 边缘端 Dockerfile ============
# 多阶段构建，适配各类边缘网关

# 阶段1：依赖安装
FROM python:3.11-slim AS builder

WORKDIR /build

# 仅复制依赖文件，利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# 阶段2：运行时
FROM python:3.11-slim

WORKDIR /app

# 复制已安装的依赖
COPY --from=builder /root/.local /root/.local

# 复制应用代码
COPY . .

# 创建数据/日志目录
RUN mkdir -p data logs

# 环境变量
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# 暴露端口
EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/system/health')" || exit 1

# 启动命令
CMD ["python", "-m", "app.main"]
