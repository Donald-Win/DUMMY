FROM python:3.12-slim

LABEL org.opencontainers.image.title="DUMMY – Docker Update Made Manageable, Yay!"
LABEL org.opencontainers.image.description="Monitor, update, and rollback Docker containers via a lightweight web UI"
LABEL org.opencontainers.image.source="https://github.com/Donald-Win/dummy"
LABEL org.opencontainers.image.licenses="MIT"

# --------------------------------------------------------------------------
# System dependencies
# --------------------------------------------------------------------------
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# --------------------------------------------------------------------------
# Python dependencies
# --------------------------------------------------------------------------
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------
# Application
# --------------------------------------------------------------------------
COPY app.py .

# Persistent data volume (SQLite database)
VOLUME ["/data"]

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:5000/health || exit 1

CMD ["python3", "-u", "app.py"]
