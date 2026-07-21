# === 后端 Dockerfile ===
FROM python:3.12-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

# 先安装 CPU 版 torch（容器无 GPU，避免下载 1.3GB CUDA 库，构建从 45min 降至 ~5min）
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu --timeout 120 --retries 10

# 安装其余 Python 依赖（torch 已满足，docling 不会重复拉 CUDA 版）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --timeout 120 --retries 10

# 复制源码
COPY . .

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
