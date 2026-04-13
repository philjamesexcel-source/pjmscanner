FROM python:3.11-slim

WORKDIR /app

# Dependencies layer — cached unless requirements.txt changes
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code — all baked in, no permission issues
COPY screener.py filters.py tracker.py db.py alerts.py dashboard.py ./

# Create logs directory with correct permissions
RUN mkdir -p /app/logs && chmod 777 /app/logs

# config.yaml is NOT copied here — it is mounted via ConfigMap
# so thresholds can be tuned without rebuilding the image.

# Default: run screener
# Override with: command: ["python", "dashboard.py"]
CMD ["python", "screener.py"]
