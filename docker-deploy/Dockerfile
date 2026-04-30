# ===== Stage 1: Build dependencies =====
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock .

RUN uv sync --no-dev --frozen

# ===== Stage 2: Runtime =====
FROM python:3.12-slim

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    HOST=0.0.0.0 \
    PORT=8000

COPY . .

RUN mkdir -p data logs

EXPOSE 8000

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--http", "httptools", \
     "--timeout-keep-alive", "360", \
     "--log-level", "info", \
     "--no-use-colors", \
     "--no-server-header"]
