# 使用官方 Python 轻量镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 设置时区 (很少变动，放在最前面)
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 1. 先复制依赖文件并安装 (只要 requirements.txt 不变，这里就会完美命中缓存，瞬间跳过！)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 🔥 核心修复：把动态版本参数移到耗时的依赖安装之后！
ARG APP_VERSION=1.2.0.0
ENV APP_VERSION=${APP_VERSION}

# 2. 复制所有项目文件到容器 (这里改了 HTML 才会重新复制，但不会重新 pip install)
COPY . .

# 3. 创建配置和数据挂载点
RUN mkdir -p /app/config /emby-data && chmod -R 777 /app/config /emby-data

# 4. 启动命令
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10307"]