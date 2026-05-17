# ===== Stage 1: Build dependencies =====
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.6.14 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .

RUN uv sync --no-dev --frozen

# ===== Stage 2: Runtime =====
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH"

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
