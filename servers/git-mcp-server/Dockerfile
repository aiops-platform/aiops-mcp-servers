# git-mcp-server — 多阶段构建（builder 构建 wheel，runtime 精简运行）
#
# 构建：docker build -t git-mcp-server:0.1.0 -f servers/git-mcp-server/Dockerfile servers/git-mcp-server
# 运行：docker run --rm -p 8000:8000 \
#         -e ENVIRONMENT=production \
#         -e AUTH_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(32))')" \
#         -e GIT_ALLOWED_ROOTS=/srv/repos \
#         -v /absolute/path/to/repos:/srv/repos:ro \
#         git-mcp-server:0.1.0
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build
# 仅拷贝本 server 的构建上下文，避免引入整个 monorepo
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip wheel --no-deps --wheel-dir /wheels .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    ENVIRONMENT=production

WORKDIR /app

# git 运行时依赖：git-mcp-server 通过 subprocess 调用系统 git
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /wheels /wheels
RUN pip install /wheels/*.whl

# 非 root 运行
RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); sys.exit(0)" || exit 1

CMD ["python", "-m", "git_mcp_server"]
