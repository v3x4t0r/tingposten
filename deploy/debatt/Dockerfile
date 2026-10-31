# The comment server (tingposten debatt) for the Pi. Only the code and the
# `debatt` extra; no pipeline secrets, no deploy key, no Claude login.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY tingposten ./tingposten
RUN uv sync --frozen --no-dev --extra debatt && useradd --uid 1000 --no-create-home debatt
ENV PATH="/app/.venv/bin:$PATH" DEBATT_DIR=/data/debatt PYTHONUNBUFFERED=1
USER 1000:1000
CMD ["tingposten", "debatt", "--vert", "127.0.0.1", "--port", "8790"]
