FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy TZ=Asia/Kolkata DATA_DIR=/data TUYA_TRANSPORT=cloud ENERGY_POLL_SECONDS=300
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY butler ./butler
RUN uv sync --frozen --no-dev
VOLUME /data
CMD ["uv", "run", "--no-dev", "butler", "serve"]
