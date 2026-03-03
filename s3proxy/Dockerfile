FROM python:3.11-slim

# Install AWS CLI v2
RUN apt-get update && apt-get install -y \
    curl \
    unzip \
    && curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip" \
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

# Run the application
CMD ["python", "app.py"]
