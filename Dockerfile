FROM python:3.11-slim

WORKDIR /app

# Install dependencies first — cached layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY main.py tracker_loop.py ./
COPY core/       ./core/
COPY data/       ./data/
COPY strategies/ ./strategies/
COPY scoring/    ./scoring/
COPY alerts/     ./alerts/
COPY dashboard/  ./dashboard/
COPY wallet_tracker/ ./wallet_tracker/

# Create logs and config_versions directories
RUN mkdir -p /app/logs /app/config_versions && \
    chmod 777 /app/logs /app/config_versions

# config/ is NOT copied — it is mounted via ConfigMap
# This allows threshold changes without rebuilding the image

CMD ["python", "main.py"]
