FROM python:3.11-slim

# Set to true when behind a corporate proxy that intercepts HTTPS with custom certificates.
# Disables TLS verification for pip (PyPI packages).
ARG SKIP_TLS_VERIFY=false
RUN if [ "$SKIP_TLS_VERIFY" = "true" ]; then \
    printf '[global]\ntrusted-host =\n\tpypi.org\n\tfiles.pythonhosted.org\n\tpypi.python.org\n' \
        > /etc/pip.conf; \
fi

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
