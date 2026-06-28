# ===== Stage 1: Build dependencies =====
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .

RUN uv sync --no-dev --frozen

# ===== Stage 2: Runtime =====
FROM python:3.12-slim

# 安装时区数据库
RUN apt-get update && \
    apt-get install -y --no-install-recommends tzdata && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

# SQLite 资源限制（PRAGMA 由业务代码在创建连接时读取并应用）
# cache_size=-16000: 每连接 16MB cache,配合 mmap_size 共享 OS page cache,
# 在 1GB 内存设备上平衡内存占用与查询性能
ENV SQLITE_CACHE_SIZE=-16000
# temp_store=FILE: 临时表落盘而非占用内存,优先保障业务内存
ENV SQLITE_TEMP_STORE=FILE
ENV SQLITE_MMAP_SIZE=33554432
ENV SQLITE_JOURNAL_MODE=WAL
ENV SQLITE_SYNCHRONOUS=NORMAL

COPY . .

RUN mkdir -p data logs

EXPOSE 55555

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:55555/v1/models')" || exit 1

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "55555", \
     "--http", "httptools", \
     "--workers", "1", \
     "--timeout-keep-alive", "360", \
     "--log-level", "info", \
     "--no-use-colors", \
     "--no-server-header", \
     "--ws", "none", \
     "--loop", "uvloop", \
     "--backlog", "2048"]
