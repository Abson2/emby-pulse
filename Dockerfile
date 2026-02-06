# 使用官方 Python 轻量镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置时区 (可选，防止日志时间不对)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 1. 先复制依赖文件 (利用 Docker 缓存层，加速构建)
COPY requirements.txt .

# 2. 安装依赖
RUN pip install --no-cache-dir -r requirements.txt

# 3. 复制所有项目文件到容器 (包括 app/, templates/, static/)
COPY . .

# 4. 创建配置和数据挂载点 (确保权限)
RUN mkdir -p /app/config /emby-data && chmod -R 777 /app/config /emby-data

# 5. 启动命令 (注意：这里变了！)
# 使用 uvicorn 直接启动 app 模块内的 main 实例
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10307"]