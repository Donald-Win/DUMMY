FROM python:3.14-alpine

LABEL org.opencontainers.image.title="DUMMY – Docker Update Made Manageable, Yay!"
LABEL org.opencontainers.image.description="Monitor, update, and rollback Docker containers via a lightweight web UI"
LABEL org.opencontainers.image.source="https://github.com/Donald-Win/DUMMY"
LABEL org.opencontainers.image.licenses="MIT"

# --------------------------------------------------------------------------
# System dependencies
#
# apk upgrade first — ensures all bundled packages (including the Go binaries
# inside docker-cli-compose such as containerd, golang.org/x/crypto, and
# golang.org/x/net) are at their latest patched versions rather than whatever
# was cached in the base image layer at the time python:alpine was built.
#
# docker-cli-compose is needed for the compose-file update strategy (Mode 2).
# curl is used by the HEALTHCHECK.
# --------------------------------------------------------------------------
RUN apk upgrade --no-cache \
 && apk add --no-cache curl docker-cli-compose

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

# /data is where the SQLite database lives.
# Do NOT use VOLUME here — the VOLUME instruction creates an anonymous Docker
# volume that can shadow the bind mount declared in compose.yaml, causing the
# database to be written to Docker-managed storage instead of the host path.
# Instead, always mount /data explicitly in your compose file:
#   volumes:
#     - /stacks/data/dummy:/data
RUN mkdir -p /data

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:5000/health || exit 1

CMD ["python3", "-u", "app.py"]
