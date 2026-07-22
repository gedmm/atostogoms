FROM python:3.12-slim

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY deal_monitor.py .
COPY config.yaml .

# seen_items.json lives in /app/data so it can be persisted via a volume
# (see docker-compose.yml) and survive container restarts/rebuilds.
RUN mkdir -p /app/data

ENTRYPOINT ["python", "deal_monitor.py"]
CMD ["--loop"]
