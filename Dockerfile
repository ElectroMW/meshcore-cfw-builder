# ── Stage: runtime ────────────────────────────────────────────────────────────
FROM python:3.11-slim

# System dependencies: git (for cloning MeshCore) + build tools required by
# PlatformIO's ESP32 toolchain download/extraction
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        libusb-1.0-0 \
        udev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies (Flask + PlatformIO)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app.py .
COPY templates/ templates/

# The .platformio cache (toolchain downloads ~400 MB) is kept in a Docker
# volume so it survives container restarts and is shared between rebuilds.
# Declaring it here makes Docker create the volume automatically.
VOLUME ["/root/.platformio"]

# Expose web UI port
EXPOSE 5000

# Bind to all interfaces inside the container (mapped to host via docker-compose)
ENV HOST=0.0.0.0
ENV PORT=5000

CMD ["python", "app.py"]
