# Heltec v3 MeshCore GPS Firmware Builder

A self-hosted web app that clones the latest [MeshCore](https://github.com/meshcore-dev/MeshCore) firmware from GitHub, compiles it with your custom settings using PlatformIO, and delivers the merged `.bin` directly to your browser — ready to download or flash via USB in one click.

---

## Features

- **4 firmware variants** — Companion Radio (BLE / WiFi / USB) and Repeater
- **Customisable build flags** — GPS pins, BLE PIN code, WiFi credentials, repeater name & admin password
- **In-browser USB flashing** — flash your Heltec v3 directly from the browser using the Web Serial API (Chrome / Edge)
- **One-click download** — download the compiled `.bin` for manual flashing
- **Build queue** — up to 2 concurrent builds; additional requests are queued with live position updates
- **Privacy first** — all build artefacts are deleted immediately after download or flash; nothing is stored

---

## Quick Start

```bash
docker run -d \
  -p 5000:5000 \
  -v pio-cache:/root/.platformio \
  --name meshcore-builder \
  christian45410/meshcore-firmware-builder
```

Then open **http://localhost:5000** in Chrome or Edge.

> **First run:** PlatformIO will download the ESP32-S3 toolchain (~400 MB) into the `pio-cache` volume. Subsequent starts reuse the cache and begin immediately.

---

## Docker Compose

```yaml
services:
  firmware-builder:
    image: christian45410/meshcore-firmware-builder
    ports:
      - "5000:5000"
    volumes:
      - pio-cache:/root/.platformio
    restart: unless-stopped

volumes:
  pio-cache:
```

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `HOST` | `0.0.0.0` | Address the server binds to |
| `PORT` | `5000` | Port the server listens on |

---

## Requirements

- Docker with Linux containers
- Chrome or Edge (for USB flashing via Web Serial API)
- Heltec WiFi LoRa 32 v3 connected via USB (for flashing)

---

## Source

[github.com/meshcore-dev/MeshCore](https://github.com/meshcore-dev/MeshCore)
