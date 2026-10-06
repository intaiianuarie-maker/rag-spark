# Knowledge OS worker image. Build context is the repository root:
#   docker build -f apps/worker/Dockerfile .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/packages:/app/apps/worker

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY apps/worker/requirements.txt ./apps/worker/requirements.txt
RUN pip install --no-cache-dir -r apps/worker/requirements.txt

COPY packages ./packages
COPY apps/worker ./apps/worker

WORKDIR /app/apps/worker

CMD ["python", "-m", "worker.main"]
