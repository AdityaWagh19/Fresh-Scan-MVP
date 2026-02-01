# Fresh-Scan MVP: IoT Hardware Module
![Platform](https://img.shields.io/badge/platform-Raspberry_Pi-red.svg?logo=raspberrypi&logoColor=white) ![Python](https://img.shields.io/badge/python-3.11+-yellow.svg) ![Status](https://img.shields.io/badge/module-active-success)

This directory contains the **Edge Computing Layer** for the Fresh-Scan MVP system. It transforms a standard Raspberry Pi into a secure, improved IoT camera node capable of high-resolution capture and secure, encrypted transmission.

---

## Overview

The system is designed to run indefinitely on a Raspberry Pi (Zero 2 W, 3B+, 4, or 5). It operates as a RESTful API server that the main application (running on your PC/Cloud) polls for images.

**Key Capabilities:**
*   **High-Res Capture**: Supports standard (v1/v2) and HQ Pi Cameras with autofocus control.
*   **Secure Tunneling**: Utilizes **Cloudflare Tunnels** to expose the camera securely to the internet without opening router ports (Zero Trust).
*   **Token Authentication**: Rejects any request without a valid `CAMERA_API_KEY`.
*   **Auto-Healing**: Systemd services ensure the camera server restarts automatically if it crashes or the Pi reboots.

---

## Hardware Bill of Materials (BOM)

| Component | Recommendation | Notes |
| :--- | :--- | :--- |
| **SBC** | Raspberry Pi 4 Model B (2GB+) | Pi Zero 2W is acceptable for low-res. |
| **Camera** | Raspberry Pi Camera Module 3 | Autofocus support is highly recommended. |
| **Case** | Official Pi Case / Custom Mount | Need a mounting point inside the fridge. |
| **Power** | Official USB-C Power Supply | Stable voltage is critical for long uptimes. |
| **SD Card** | 32GB High Endurance | High endurance cards resist corruption better. |

---

## Deployment Guide

### Phase 1: OS Preparation
1.  Flash **Raspberry Pi OS Lite (64-bit)** to your SD card. *Desktop version is unnecessary overhead.*
2.  Enable SSH and setup Wi-Fi in the Raspberry Pi Imager settings before flashing.
3.  Boot the Pi and SSH into it:
    ```bash
    ssh pi@raspberrypi.local
    ```

### Phase 2: Transfer Codebase
From your main computer, transfer the `raspberry_pi` folder:
```bash
# Run this from the root of the Fresh-Scan MVP project
scp -r raspberry_pi pi@raspberrypi.local:~/fresh-scan-camera/
```

### Phase 3: Dependencies & Environment
On the Raspberry Pi:
```bash
# Update system
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip libcamera-apps

# Install Python deps
cd ~/fresh-scan-camera
pip3 install -r requirements.txt --break-system-packages
# Note: --break-system-packages is needed on new Debian Bookworm releases
# Alternatively, use a venv (Recommended).
```

**Configuration:**
```bash
cp .env.rpi.template .env
nano .env
```
*   Set `CAMERA_API_KEY` to a strong random string.
*   Set `SERVER_PORT` (Default: 5000).

### Phase 4: Service Installation (Autostart)
We use `systemd` to make the camera server a robust background service.

```bash
# 1. Copy service file
sudo cp smart-fridge.service /etc/systemd/system/

# 2. Reload daemon
sudo systemctl daemon-reload

# 3. Enable and Start
sudo systemctl enable smart-fridge
sudo systemctl start smart-fridge

# 4. Check Status
systemctl status smart-fridge
```

---

## Remote Access (Cloudflare Tunnel)

To access your fridge inventory while at the grocery store, we need a public URL. We use Cloudflare Tunnels for this (Free & Secure).

1.  **Install Cloudflared on Pi:**
    ```bash
    curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
    sudo dpkg -i cloudflared.deb
    ```

2.  **Authenticate:**
    ```bash
    cloudflared tunnel login
    # Follow the URL to login with your domain
    ```

3.  **Create Tunnel:**
    ```bash
    cloudflared tunnel create fridge-cam
    ```

4.  **Configure Routing:**
    Update `config.yml` with your Tunnel UUID:
    ```yaml
    tunnel: <Tunnel-UUID>
    credentials-file: /home/pi/.cloudflared/<Tunnel-UUID>.json

    ingress:
      - hostname: camera.yourdomain.com
        service: http://localhost:5000
      - service: http_status:404
    ```

5.  **Run Tunnel:**
    ```bash
    cloudflared tunnel run fridge-cam
    ```
    *(Ideally, install this as a service too using `sudo cloudflared service install`)*

---

## Troubleshooting

### Camera Issues
**Error: `mmal: No data received from sensor`**
*   **Fix**: Check ribbon cable orientation. The blue pull tab should face towards the ethernet port (on Pi 4).
*   **Fix**: Ensure `libcamera` is enabled or legacy camera stack is enabled via `sudo raspi-config`.

**Error: `Camera not detected`**
*   **Fix**: Run `vcgencmd get_camera`. If `supported=0 detected=0`, your hardware connection is loose or broken.

### Network Issues
**Error: `Connection Refused` on port 5000**
*   **Fix**: Ensure the service is running (`systemctl status smart-fridge`).
*   **Fix**: Check `config.yml` if using Cloudflare. Ensure `ingress` points to `http://localhost:5000`.

### Logs
The system logs are your best friend.
```bash
# Watch live logs
journalctl -u smart-fridge -f

# Check Cloudflare logs (if running as service)
journalctl -u cloudflared -f
```

---

## API Reference

The Camera Server exposes a simple REST API.

**GET `/health`**
> Checks if the server is running and camera is initialized.
> **Response**: `200 OK` `{"status": "online", "uptime": 1340}`

**GET `/capture`**
> Takes a photo and returns it.
> **Headers**: `X-API-Key: <YOUR_KEY>`
> **Response**: `200 OK` (Image/JPEG binary)

**POST `/settings/focus`**
> Adjusts focus (for motorized focus cameras).
> **Body**: `{"value": 1.5}`

---
**Maintained by Team FreshScan**
