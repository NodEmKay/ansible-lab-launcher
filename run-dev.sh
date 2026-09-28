#!/bin/bash

set -e

CONTAINER_NAME="ansible-lab-launcher"
IMAGE="localhost/ansible-lab-launcher:0.1"

# Remove an existing development container if present
if podman container exists "$CONTAINER_NAME"; then
    podman rm -f "$CONTAINER_NAME"
fi

podman run -d \
    --name "$CONTAINER_NAME" \
    -p 8000:8000 \
    -v "$PWD/backend:/app/backend:Z" \
    -v "$HOME/.aws:/root/.aws:ro,Z" \
    -v "$HOME/ansible-lab-launcher-data:/data:Z" \
    "$IMAGE" \
    uvicorn backend.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --reload

echo
echo "Ansible Lab Launcher development container started."
echo "URL: http://127.0.0.1:8000"
