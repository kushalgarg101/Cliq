FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev
ENV PYTHONPATH=/app/src
ENV PORT=8000
CMD ["sh", "-c", ".venv/bin/uvicorn parcelpilot.main:app --host 0.0.0.0 --port ${PORT}"]
