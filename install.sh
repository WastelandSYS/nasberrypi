#!/bin/bash

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root"
    exit
fi

clear

echo "Installing Nasberry NAS System..."

apt-get update

apt-get install -y \
    python3 \
    python3-pip \
    samba \
    cifs-utils \
    net-tools

mkdir -p /mnt/nasberry

chmod 755 /mnt/nasberry

ln -sf "$(pwd)/nasberry.py" /usr/local/bin/nasberry

chmod +x nasberry.py

clear

echo "======================================"
echo " Nasberry installation complete!"
echo " Launch using: nasberry"
echo "======================================"