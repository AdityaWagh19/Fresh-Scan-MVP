#!/bin/bash
# Install and configure systemd services for Cloudflare Tunnel and Camera Service

set -e

echo "=========================================="
echo "Installing Systemd Services"
echo "=========================================="
echo ""

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Check if running as root or with sudo
if [ "$EUID" -ne 0 ]; then
    print_warning "This script needs sudo privileges. Rerunning with sudo..."
    sudo "$0" "$@"
    exit $?
fi

# Install Cloudflared service
print_info "Installing Cloudflared service..."
if [ -f /etc/systemd/system/cloudflared.service ]; then
    print_warning "Cloudflared service already exists. Backing up..."
    cp /etc/systemd/system/cloudflared.service /etc/systemd/system/cloudflared.service.backup
fi

# Use cloudflared's built-in service installer
cloudflared service install

# Install Camera Service
print_info "Installing Camera Service..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/../systemd/camera-service.service"

if [ ! -f "$SERVICE_FILE" ]; then
    print_warning "camera-service.service not found at $SERVICE_FILE"
    print_info "Please ensure the systemd directory contains camera-service.service"
    exit 1
fi

cp "$SERVICE_FILE" /etc/systemd/system/camera-service.service

# Reload systemd
print_info "Reloading systemd daemon..."
systemctl daemon-reload

# Enable services
print_info "Enabling services to start on boot..."
systemctl enable cloudflared
systemctl enable camera-service

# Start services
print_info "Starting services..."
systemctl start cloudflared
sleep 3
systemctl start camera-service

# Check status
print_info ""
print_info "=========================================="
print_info "Service Status"
print_info "=========================================="
print_info ""

print_info "Cloudflared Status:"
systemctl status cloudflared --no-pager || true

print_info ""
print_info "Camera Service Status:"
systemctl status camera-service --no-pager || true

print_info ""
print_info "=========================================="
print_info "Installation Complete!"
print_info "=========================================="
print_info ""
print_info "Useful commands:"
print_info "  - Check status: sudo systemctl status camera-service"
print_info "  - View logs: sudo journalctl -u camera-service -f"
print_info "  - Restart: sudo systemctl restart camera-service"
print_info "  - Stop: sudo systemctl stop camera-service"
