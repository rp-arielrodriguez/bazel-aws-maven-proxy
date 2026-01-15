FROM python:3.11-slim

# Install Docker CLI for service restart capability
RUN apt-get update && apt-get install -y \
    docker.io \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY monitor.py .

# Set environment variables
ENV PYTHONUNBUFFERED=1

# Run the monitor script
CMD ["python", "monitor.py"]

