#!/bin/bash
set -e

echo "=== Telegram Bot Hosting Platform - VPS Setup ==="

# Update system
sudo apt update && sudo apt upgrade -y

# Install build essentials (needed for tgcrypto and other C extensions)
sudo apt install -y build-essential python3.14-dev

# Install Docker
if ! command -v docker &> /dev/null; then
    echo "Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
    sudo usermod -aG docker ubuntu
    echo "Docker installed. Log out and back in for group changes."
else
    echo "Docker already installed."
fi

# Install Python 3.14 and venv
if ! command -v python3.14 &> /dev/null; then
    echo "Installing Python 3.14..."
    sudo apt install -y software-properties-common
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt update
    sudo apt install -y python3.14 python3.14-venv python3.14-dev
else
    echo "Python 3.14 already installed."
fi

# Create project directory
PROJ_DIR="/home/ubuntu/host"
sudo mkdir -p "$PROJ_DIR"
sudo chown ubuntu:ubuntu "$PROJ_DIR"

# Generate encryption key for environment variables
echo ""
echo "Generating ENV_ENCRYPTION_KEY..."
ENCRYPTION_KEY=$(python3.14 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>/dev/null || echo "")
if [ -z "$ENCRYPTION_KEY" ]; then
    echo "WARNING: Could not generate encryption key."
    echo "Run this manually after setup: python3.14 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    echo "Then add it to your .env file as ENV_ENCRYPTION_KEY=<key>"
else
    echo "Generated key: $ENCRYPTION_KEY"
fi

echo ""
echo "=== Next Steps ==="
echo ""
echo "1. Clone or copy your hosting-bot project to $PROJ_DIR"
echo ""
echo "2. Create and activate virtual environment:"
echo "   cd $PROJ_DIR"
echo "   python3.14 -m venv venv"
echo "   source venv/bin/activate"
echo ""
echo "3. Install dependencies:"
echo "   pip install -r requirements.txt"
echo ""
echo "4. Create your .env file:"
echo "   cp .env.example .env"
echo "   nano .env"
if [ -n "$ENCRYPTION_KEY" ]; then
    echo ""
    echo "   Add this to your .env:"
    echo "   ENV_ENCRYPTION_KEY=$ENCRYPTION_KEY"
fi
echo ""
echo "5. Install the systemd service:"
echo "   sudo cp hosting-bot.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable hosting-bot"
echo "   sudo systemctl start hosting-bot"
echo ""
echo "6. Install the web dashboard service:"
echo "   sudo cp dashboard.service /etc/systemd/system/"
echo "   sudo systemctl daemon-reload"
echo "   sudo systemctl enable dashboard"
echo "   sudo systemctl start dashboard"
echo ""
echo "   And set WEB_URL in your .env to point to your dashboard:"
echo "   WEB_URL=http://13.60.45.38:9090"
echo "   (then restart both services to pick it up)"
echo ""
echo "7. Check status:"
echo "   sudo systemctl status hosting-bot dashboard"
echo ""
echo "8. View logs:"
echo "   sudo journalctl -u hosting-bot -f"
echo "   sudo journalctl -u dashboard -f"
echo ""
echo "=== Setup Complete ==="
