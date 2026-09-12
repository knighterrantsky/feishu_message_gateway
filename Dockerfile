FROM ghcr.io/astral-sh/uv:0.10.7 AS uv
FROM python:3.12-slim-bookworm AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.12-slim-bookworm AS runtime
ARG CODE_VERSION=dev
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" DATA_DIR=/data PORT=8080 \
    CODE_VERSION=$CODE_VERSION
LABEL org.opencontainers.image.source="https://github.com/knighterrantsky/feishu_message_gateway" \
      org.opencontainers.image.revision=$CODE_VERSION
RUN groupadd --gid 10001 gateway && useradd --uid 10001 --gid gateway --no-create-home gateway \
    && mkdir /data && chown gateway:gateway /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY gateway ./gateway
USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8080')+'/healthz',timeout=3)"
CMD ["python", "-m", "gateway"]
