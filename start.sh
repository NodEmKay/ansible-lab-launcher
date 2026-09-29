#!/usr/bin/env bash

set -euo pipefail

IMAGE="localhost/ansible-lab-launcher:1.0"
CONTAINER_NAME="ansible-lab-launcher"
DATA_DIR="$HOME/ansible-lab-launcher-data"
AWS_DIR="$HOME/.aws"
PORT="8000"

echo "Starting Ansible Lab Launcher..."

if ! podman image exists "$IMAGE"; then
    echo "ERROR: Launcher image $IMAGE is not installed."
    echo "Run ./install.sh first."
    exit 1
fi

if [[ ! -d "$AWS_DIR" ]]; then
    echo "ERROR: AWS configuration directory $AWS_DIR does not exist."
    echo "Configure AWS CLI credentials before starting the Launcher."
    exit 1
fi

mkdir -p "$DATA_DIR/keys"
chmod 700 "$DATA_DIR" "$DATA_DIR/keys"

if podman container exists "$CONTAINER_NAME"; then
    if [[ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" == "true" ]]; then
        echo "Ansible Lab Launcher is already running."
    else
        podman start "$CONTAINER_NAME" >/dev/null
        echo "Existing Launcher container started."
    fi
else
    podman run -d \
        --name "$CONTAINER_NAME" \
        --restart=unless-stopped \
        -p "${PORT}:8000" \
        -v "$AWS_DIR:/root/.aws:ro,Z" \
        -v "$DATA_DIR:/data:Z" \
        "$IMAGE" >/dev/null

    echo "Launcher container created."
fi

echo "Waiting for Launcher health check..."

for attempt in {1..30}; do
    if curl --fail --silent \
        "http://127.0.0.1:${PORT}/health" >/dev/null; then
        echo
        echo "Ansible Lab Launcher is ready."
        echo "Local URL: http://127.0.0.1:${PORT}"
        exit 0
    fi

    sleep 1
done

echo
echo "ERROR: Launcher did not become healthy."
echo "Check logs with:"
echo "  podman logs $CONTAINER_NAME"
exit 1
