FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/pyproject.toml backend/uv.lock /app/backend/
COPY backend/src /app/backend/src

RUN pip install --no-cache-dir uv \
    && cd /app/backend \
    && uv sync --locked --no-dev

EXPOSE 8000

CMD ["uv", "run", "--project", "/app/backend", "uvicorn", "forge.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
