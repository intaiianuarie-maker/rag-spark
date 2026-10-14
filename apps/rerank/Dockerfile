# Knowledge OS Standalone MiniLM Rerank Sidecar (T-ARCH-06).
# Dedicated lightweight CPU-only cross-encoder sidecar (~100MB weights).
# ZERO bge-m3 weights. TORCH_NUM_THREADS=1-2. HARD 1G memory limit.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TORCH_NUM_THREADS=1 \
    OMP_NUM_THREADS=1 \
    MKL_NUM_THREADS=1 \
    PORT=7998 \
    RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L6-v2

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY apps/rerank/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple

# Copy service application
COPY apps/rerank/app ./app

# Pre-download MiniLM weights (~91MB safetensors) so container boots ready
# and assert NO bge-m3 or bge weights exist in the layer
RUN python -c "from sentence_transformers import CrossEncoder; m = CrossEncoder('cross-encoder/ms-marco-MiniLM-L6-v2', max_length=512); print('MiniLM pre-download verified')" \
    && python -c "import glob; bge = glob.glob('/root/.cache/huggingface/**/bge*', recursive=True); assert len(bge) == 0, f'Forbidden BGE weights found: {bge}'"

EXPOSE 7998

# Readiness-as-capability: /ready returns 200 only when model is loaded and capable of inference
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS http://localhost:7998/ready || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7998"]
