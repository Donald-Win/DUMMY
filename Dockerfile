FROM python:3.14-alpine

LABEL org.opencontainers.image.title="DUMMY – Docker Update Made Manageable, Yay!"
LABEL org.opencontainers.image.description="Monitor, update, and rollback Docker containers via a lightweight web UI"
LABEL org.opencontainers.image.source="https://github.com/Donald-Win/DUMMY"
LABEL org.opencontainers.image.licenses="MIT"

# --------------------------------------------------------------------------
# System dependencies
# alpine uses apk instead of apt-get
# docker-cli-compose is needed for the compose-file update strategy (Mode 2)
# --------------------------------------------------------------------------
RUN apk add --no-cache curl docker-cli-compose

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
