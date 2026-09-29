#!/usr/bin/env bash

set -euo pipefail

IMAGE="localhost/ansible-lab-launcher:1.0"
DATA_DIR="$HOME/ansible-lab-launcher-data"

echo "========================================"
echo " Ansible Lab Launcher Installation"
echo "========================================"
echo

if [[ ! -f /etc/redhat-release ]]; then
    echo "ERROR: This installer requires a RHEL-compatible system."
    exit 1
fi

if [[ "$EUID" -eq 0 ]]; then
    echo "ERROR: Run this installer as a regular user, not root."
    exit 1
fi

if ! command -v sudo >/dev/null 2>&1; then
    echo "ERROR: sudo is required."
    exit 1
fi

echo "Operating system:"
cat /etc/redhat-release
echo

echo "Installing host prerequisites..."

sudo dnf install -y \
    git \
    podman \
    openssh-clients \
    curl \
    unzip \
    tar \
    gzip \
    ca-certificates

echo
echo "Host prerequisites: OK"

echo
echo "Checking AWS CLI..."

if ! command -v aws >/dev/null 2>&1; then
    echo "AWS CLI not found. Installing AWS CLI v2..."

    tmp_dir="$(mktemp -d)"
    trap 'rm -rf "$tmp_dir"' EXIT

    curl --fail --location --silent --show-error \
        "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" \
        --output "$tmp_dir/awscliv2.zip"

    unzip -q "$tmp_dir/awscliv2.zip" -d "$tmp_dir"

    sudo "$tmp_dir/aws/install"

    rm -rf "$tmp_dir"
    trap - EXIT
fi

if ! command -v aws >/dev/null 2>&1; then
    echo "ERROR: AWS CLI installation failed."
    exit 1
fi

echo "AWS CLI: OK"
aws --version

echo
echo "Preparing persistent Launcher data..."

mkdir -p "$DATA_DIR/keys"
chmod 700 "$DATA_DIR" "$DATA_DIR/keys"

echo "Persistent data: $DATA_DIR"

echo
echo "Building Launcher image..."
echo "Image: $IMAGE"

podman build \
    --tag "$IMAGE" \
    --file Containerfile \
    .

if ! podman image exists "$IMAGE"; then
    echo "ERROR: Launcher image build failed."
    exit 1
fi

echo
echo "========================================"
echo " Installation complete"
echo "========================================"
echo
echo "Next steps:"
echo "  1. Configure AWS credentials if required:"
echo "       aws configure"
echo
echo "  2. Verify AWS access:"
echo "       aws sts get-caller-identity"
echo
echo "  3. Start the Launcher:"
echo "       ./start.sh"
