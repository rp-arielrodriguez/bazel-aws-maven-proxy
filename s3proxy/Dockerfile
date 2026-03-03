FROM python:3.11-slim

# Install AWS CLI v2 (multi-arch: x86_64 + aarch64)
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && ARCH=$(uname -m) \
    && if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then \
         CLI_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"; \
       else \
         CLI_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"; \
       fi \
    && curl "$CLI_URL" -o "awscliv2.zip" \
    && unzip awscliv2.zip \
    && ./aws/install \
    && rm -rf \
        awscliv2.zip \
        aws \
        /var/lib/apt/lists/* \
    && apt-get clean

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Create data directory for caching
RUN mkdir -p /data

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    CACHE_DIR=/data

# Expose the proxy port
EXPOSE ${PROXY_PORT:-8888}

# Health check using the application's health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PROXY_PORT:-8888}/healthz || exit 1

# Run the application with gunicorn (production WSGI server)
# Shell form so $PROXY_PORT is expanded at runtime
CMD gunicorn --bind "0.0.0.0:${PROXY_PORT:-8888}" --workers 2 --threads 4 --timeout 120 --access-logfile - app:app
