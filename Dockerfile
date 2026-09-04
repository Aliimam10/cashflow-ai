# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.12.0 AS uv
FROM python:3.12.13-slim-bookworm AS runtime

ENV HOME=/tmp/cashflow-home \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends --yes tesseract-ocr \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 cashflow \
    && useradd \
        --uid 10001 \
        --gid cashflow \
        --no-create-home \
        --home-dir /nonexistent \
        --shell /usr/sbin/nologin \
        cashflow

COPY --from=uv /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY alembic.ini ./
COPY migrations ./migrations
COPY configs ./configs
COPY src ./src

RUN uv sync --locked --no-dev --no-editable --no-cache \
    && mkdir -p /app/data /app/models \
    && chown -R cashflow:cashflow /app/data /app/models

USER cashflow

EXPOSE 8000 8501

CMD ["cashflow-api"]
