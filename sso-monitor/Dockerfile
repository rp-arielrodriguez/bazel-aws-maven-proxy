FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy monitor script
COPY monitor.py .

# Run as non-root user
RUN useradd --no-create-home --system appuser
USER appuser

# Run monitor
CMD ["python", "-u", "monitor.py"]
