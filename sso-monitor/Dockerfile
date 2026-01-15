FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy monitor script
COPY monitor.py .

# Run monitor
CMD ["python", "-u", "monitor.py"]
