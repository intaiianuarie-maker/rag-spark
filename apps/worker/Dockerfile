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
    && apt-get install -y --no-install-recommends \
        curl \
        git \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-ron \
        tesseract-ocr-por \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir --no-deps --upgrade "pip @ https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/pip-26.2.2-py3-none-any.whl#sha256=fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"

COPY apps/worker/requirements.txt ./apps/worker/requirements.txt
RUN pip install --no-cache-dir -r apps/worker/requirements.txt

COPY packages ./packages
COPY apps/worker ./apps/worker

WORKDIR /app/apps/worker

CMD ["python", "-m", "worker.main"]
