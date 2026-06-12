#!/usr/bin/env bash
# Run this once on a fresh Ubuntu 22.04 / 24.04 droplet as root.
set -euo pipefail

echo "==> Installing Docker..."
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

echo "==> Cloning repo..."
# Replace with your actual repo URL
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git /opt/football-automation
cd /opt/football-automation

echo "==> Creating .env..."
cp .env.example .env
echo ""
echo "  >> Edit /opt/football-automation/.env and fill in your API keys, then run:"
echo "  >> cd /opt/football-automation && docker compose up -d --build"
echo ""
echo "Done. Server will be live on http://YOUR_DROPLET_IP once you start compose."
