FROM python:3.12-slim

# Build context is the repository root (see docker-compose.yml context: .)
# so all COPY paths are relative to the repo root.

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared package
COPY shared/ /app/shared/

# Copy worker source
COPY worker/ /app/worker/

ENV PYTHONPATH=/app

CMD ["python", "-m", "worker.main"]
