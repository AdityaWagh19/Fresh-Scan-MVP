#!/bin/bash
# Verification script to test the complete deployment

set -e

echo "=========================================="
echo "Deployment Verification"
echo "=========================================="
echo ""

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS="${GREEN}✓${NC}"
FAIL="${RED}✗${NC}"
WARN="${YELLOW}!${NC}"

# Read domain from .env.rpi if available
DOMAIN=""
if [ -f /home/pi/smart-fridge-camera/.env.rpi ]; then
    DOMAIN=$(grep CLOUDFLARE_DOMAIN /home/pi/smart-fridge-camera/.env.rpi | cut -d '=' -f2)
fi

echo "Test 1: Cloudflared Installation"
if command -v cloudflared &> /dev/null; then
    VERSION=$(cloudflared --version)
    echo -e "$PASS Cloudflared installed: $VERSION"
else
    echo -e "$FAIL Cloudflared not found"
    exit 1
fi

echo ""
echo "Test 2: Cloudflared Service Status"
if systemctl is-active --quiet cloudflared; then
    echo -e "$PASS Cloudflared service is running"
else
    echo -e "$FAIL Cloudflared service is not running"
    echo "   Try: sudo systemctl start cloudflared"
fi

echo ""
echo "Test 3: Camera Service Status"
if systemctl is-active --quiet camera-service; then
    echo -e "$PASS Camera service is running"
else
    echo -e "$FAIL Camera service is not running"
    echo "   Try: sudo systemctl start camera-service"
fi

echo ""
echo "Test 4: Local HTTP Endpoint"
if curl -s http://localhost:5000/test > /dev/null 2>&1; then
    RESPONSE=$(curl -s http://localhost:5000/test)
    echo -e "$PASS Local endpoint responding"
    echo "   Response: $RESPONSE"
else
    echo -e "$FAIL Local endpoint not responding"
    echo "   Check: sudo journalctl -u camera-service -n 50"
fi

echo ""
echo "Test 5: Health Check"
if curl -s http://localhost:5000/health > /dev/null 2>&1; then
    HEALTH=$(curl -s http://localhost:5000/health | python3 -m json.tool 2>/dev/null || echo "Invalid JSON")
    echo -e "$PASS Health endpoint responding"
    echo "$HEALTH"
else
    echo -e "$FAIL Health endpoint not responding"
fi

echo ""
echo "Test 6: Camera Device"
if [ -e /dev/video0 ]; then
    echo -e "$PASS Camera device found at /dev/video0"
else
    echo -e "$WARN Camera device not found"
    echo "   Enable camera: sudo raspi-config -> Interface Options -> Camera"
fi

echo ""
echo "Test 7: MongoDB Connection"
if curl -s http://localhost:5000/health | grep -q '"database": "ok"'; then
    echo -e "$PASS MongoDB connection successful"
else
    echo -e "$FAIL MongoDB connection failed"
    echo "   Check MONGO_URI in .env.rpi"
fi

echo ""
echo "Test 8: Cloudflare Tunnel Status"
TUNNEL_LIST=$(cloudflared tunnel list 2>/dev/null || echo "")
if echo "$TUNNEL_LIST" | grep -q "smart-fridge-camera"; then
    echo -e "$PASS Tunnel 'smart-fridge-camera' exists"
    echo "$TUNNEL_LIST"
else
    echo -e "$FAIL Tunnel 'smart-fridge-camera' not found"
    echo "   Create tunnel: cloudflared tunnel create smart-fridge-camera"
fi

if [ -n "$DOMAIN" ]; then
    echo ""
    echo "Test 9: Remote HTTPS Endpoint"
    echo "Testing: https://$DOMAIN/test"
    if curl -s -k "https://$DOMAIN/test" > /dev/null 2>&1; then
        REMOTE_RESPONSE=$(curl -s -k "https://$DOMAIN/test")
        echo -e "$PASS Remote endpoint responding"
        echo "   Response: $REMOTE_RESPONSE"
    else
        echo -e "$WARN Remote endpoint not responding (may need DNS propagation)"
        echo "   Wait up to 48 hours for DNS propagation"
        echo "   Check: nslookup $DOMAIN"
    fi
else
    echo ""
    echo -e "$WARN CLOUDFLARE_DOMAIN not set in .env.rpi"
    echo "   Skipping remote endpoint test"
fi

echo ""
echo "=========================================="
echo "Verification Complete"
echo "=========================================="
echo ""
echo "Logs:"
echo "  Cloudflared: sudo journalctl -u cloudflared -f"
echo "  Camera:      sudo journalctl -u camera-service -f"
echo ""
echo "Endpoints:"
echo "  Local:  http://localhost:5000/test"
if [ -n "$DOMAIN" ]; then
    echo "  Remote: https://$DOMAIN/test"
fi
