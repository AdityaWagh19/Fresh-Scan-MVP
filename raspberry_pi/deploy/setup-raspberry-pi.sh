#!/bin/bash
# Raspberry Pi Camera Service Setup Script
# This script automates the installation and configuration of the camera service

set -e  # Exit on error

echo "=========================================="
echo "Smart Fridge Camera Service Setup"
echo "=========================================="
echo ""

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Function to print colored output
print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running on Raspberry Pi
if [ ! -f /proc/device-tree/model ]; then
    print_warning "This doesn't appear to be a Raspberry Pi. Continuing anyway..."
else
    print_info "Detected: $(cat /proc/device-tree/model)"
fi

# Update system
print_info "Updating system packages..."
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
print_info "Installing Python and pip..."
sudo apt install -y python3 python3-pip python3-venv

# Install system dependencies for camera
print_info "Installing camera dependencies..."
sudo apt install -y python3-picamera2 libcamera-apps

# Create project directory
PROJECT_DIR="/home/pi/smart-fridge-camera"
print_info "Creating project directory: $PROJECT_DIR"
mkdir -p "$PROJECT_DIR"
cd "$PROJECT_DIR"

# Create virtual environment (optional but recommended)
print_info "Creating Python virtual environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python packages
print_info "Installing Python dependencies..."
pip3 install --upgrade pip
pip3 install flask flask-cors pymongo certifi picamera2 python-dotenv

# Create directories
print_info "Creating required directories..."
mkdir -p captured_images
mkdir -p logs

# Check if .env.rpi exists
if [ ! -f "$PROJECT_DIR/.env.rpi" ]; then
    print_warning ".env.rpi file not found!"
    print_info "Please create .env.rpi with your configuration before starting the service"
    print_info "Template available in .env.rpi.template"
fi

# Check if rpi_camera_server.py exists
if [ ! -f "$PROJECT_DIR/rpi_camera_server.py" ]; then
    print_error "rpi_camera_server.py not found in $PROJECT_DIR"
    print_info "Please copy rpi_camera_server.py to $PROJECT_DIR"
    exit 1
fi

# Set permissions
print_info "Setting file permissions..."
chmod +x "$PROJECT_DIR/rpi_camera_server.py"
chown -R pi:pi "$PROJECT_DIR"

# Test camera
print_info "Testing camera module..."
if [ -e /dev/video0 ]; then
    print_info "Camera device detected at /dev/video0"
else
    print_warning "Camera device not found. Make sure camera is enabled in raspi-config"
fi

# Install Cloudflared
print_info "Installing Cloudflared..."
if [ ! -f /usr/local/bin/cloudflared ]; then
    wget https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm
    chmod +x cloudflared-linux-arm
    sudo mv cloudflared-linux-arm /usr/local/bin/cloudflared
    print_info "Cloudflared installed successfully"
else
    print_info "Cloudflared already installed"
fi

# Verify cloudflared installation
cloudflared --version

print_info ""
print_info "=========================================="
print_info "Setup Complete!"
print_info "=========================================="
print_info ""
print_info "Next steps:"
print_info "1. Configure .env.rpi with your MongoDB URI and settings"
print_info "2. Authenticate Cloudflare: cloudflared tunnel login"
print_info "3. Create tunnel: cloudflared tunnel create smart-fridge-camera"
print_info "4. Configure tunnel in ~/.cloudflared/config.yml"
print_info "5. Install systemd services (see DEPLOYMENT_GUIDE.md)"
print_info ""
print_info "For detailed instructions, see CLOUDFLARE_SETUP_GUIDE.md"
