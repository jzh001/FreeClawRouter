FROM python:3.12-slim

# Install build dependencies needed by tiktoken (Rust wheel build)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/
COPY config.yaml .

# Optionally copy a local override if it exists (won't fail if absent)
# Users should volume-mount their config.local.yaml at runtime instead.

# Non-root user for security
RUN adduser --disabled-password --gecos "" freeclaw && \
    mkdir -p /app/data && \
    chown -R freeclaw:freeclaw /app/data

USER freeclaw

ENV FREECLAW_CONFIG_DIR=/app
ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "-m", "uvicorn", "src.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8765", \
     "--log-level", "info"]
