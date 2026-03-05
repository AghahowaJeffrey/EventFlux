FROM python:3.12-slim

# Build context is the repository root (see docker-compose.yml context: .)
# so all COPY paths are relative to the repo root.

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cache layer)
COPY ingestion/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared package — must be in PYTHONPATH alongside the app
COPY shared/ /app/shared/

# Copy ingestion application source
COPY ingestion/ /app/ingestion/

ENV PYTHONPATH=/app

EXPOSE 8000

CMD ["uvicorn", "ingestion.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
