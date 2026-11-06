# syntax=docker/dockerfile:1.7
#
# emby-pulse 安全加固独立版 —— 基于本仓库完整源码自行构建。
# 不是 fork，不引用上游镜像，源码与构建流程全部自持。
#
# 相对上游 Dockerfile 的改动：
#   [问题 1/2] 默认注入 FORWARDED_ALLOW_IPS，让 uvicorn 采信 Cloudflare
#              Tunnel / 内网反代下发的 X-Forwarded-Proto，修复
#              「外网无法添加用户」「求片提交 CSRF Origin 不匹配」。
#   [问题 3/4/5] 默认 AUTH_GATE_MODE=enforce；WEBHOOK_ALLOW_QUERY_TOKEN=1
#              默认兼容 Emby Webhook 插件只能配置 ?token= URL 的限制，
#              后端仍不向前端回传明文 Token（凭证字段一律脱敏）。
#              Header 方式仍优先，可用 WEBHOOK_ALLOW_QUERY_TOKEN=0 切回严格模式。
#  [交付] OCI 元数据、构建期加固产物自检、显式 COPY 白名单。
#
# 说明：沿用上游的 compileall 字节码构建方式，是为了保持运行时行为与已验证
# 版本一致；本仓库源码公开，编译不构成任何额外保护，也不影响二次修改
# （改完 .py 重新 build 即可生效）。

FROM python:3.12-slim AS builder
WORKDIR /build
COPY app/ ./app/
RUN python -m compileall -b app/ && \
    find app/ -name "*.py" -delete && \
    test -f app/core/auth_gate_middleware.pyc && \
    test -f app/core/csrf_middleware.pyc

FROM python:3.12-slim
WORKDIR /workspace

ENV APP_VERSION=2.0.1-hardened \
    TZ=Asia/Shanghai \
    PATH="/workspace/.venv/bin:$PATH" \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    FORWARDED_ALLOW_IPS="127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16" \
    AUTH_GATE_MODE=enforce \
    WEBHOOK_ALLOW_QUERY_TOKEN=1

LABEL org.opencontainers.image.title="emby-pulse hardened" \
      org.opencontainers.image.description="Independent hardened build: Cloudflare Tunnel CSRF fix, default-deny API gate, Emby webhook ?token= compatibility, system-level Bark push" \
      org.opencontainers.image.source="https://github.com/Abson2/emby-pulse"

# 按锁文件安装依赖，利用层缓存
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv,sharing=locked \
    uv sync --locked --no-dev --no-install-project
RUN /workspace/.venv/bin/python -m uvicorn --version

# 显式白名单拷贝：任何运行期配置、数据库、备份、.env 都不会进入镜像
COPY --from=builder /build/app ./app
COPY run.py ./run.py
COPY templates/ ./templates/
COPY static/ ./static/

# 构建期自检：确认补丁产物确实进入镜像，且前端不再把 token 拼进 URL
RUN mkdir -p /workspace/config /workspace/data && \
    test -f /workspace/app/core/auth_gate_middleware.pyc && \
    test -f /workspace/app/core/csrf_middleware.pyc && \
    ! grep -q 'webhook?token=\${token}' /workspace/templates/settings.html && \
    ! grep -q 'webhook?token=' /workspace/templates/settings.html && \
    echo "hardening assets verified"

EXPOSE 10307 10308
CMD ["/workspace/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "10307"]
