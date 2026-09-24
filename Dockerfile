# ==========================================
# Grimmory to Komga API Bridge Dockerfile
# ==========================================
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Final runtime image
FROM python:3.12-slim

WORKDIR /app

# Install gosu for PUID/PGID user mapping step-down
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages globally from builder
COPY --from=builder /usr/local /usr/local
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Pre-create standard appuser (UID 1000) and directories
RUN useradd -u 1000 -U -m -s /bin/sh appuser \
    && mkdir -p /app/data /tmp \
    && chown -R appuser:appuser /app/data /tmp \
    && chmod 777 /app/data /tmp

# Copy application source code and entrypoint
COPY app/ ./app
COPY entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

VOLUME ["/app/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/actuator/info')" || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
