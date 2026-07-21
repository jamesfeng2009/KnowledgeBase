# === Yjs 协作服务 Dockerfile ===
FROM node:20-slim

WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 make g++ && \
    rm -rf /var/lib/apt/lists/*

# 安装依赖（构建需要 devDependencies 中的 typescript，不能用 --production）
COPY package.json package-lock.json* ./
RUN npm ci || npm install

# 复制源码
COPY tsconfig.json ./
COPY src/ ./src/

# 构建
RUN npm run build

EXPOSE 8001

CMD ["node", "dist/index.js"]
