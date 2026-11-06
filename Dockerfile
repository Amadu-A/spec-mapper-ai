# Dockerfile
# CPU-only приложение; существующий shared-embedding используется по HTTP без локальных весов.
FROM ghcr.io/astral-sh/uv:0.12.23 AS uv
FROM python:3.12-slim AS runtime
COPY --from=uv /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY src ./src
COPY docs/INTEGRATION_1C.md docs/INTEGRATION_1C.md
COPY alembic ./alembic
COPY alembic.ini .env.example ./
RUN useradd --create-home --uid 10001 mapper && mkdir /app/staging && chown mapper:mapper /app/staging && chmod 700 /app/staging
USER mapper
EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--no-proxy-headers"]

FROM runtime AS test
USER root
RUN uv sync --locked
COPY tests ./tests
COPY docker-compose.yml ./
CMD ["/app/.venv/bin/pytest", "-m", "not live and not e2e", "-q"]
