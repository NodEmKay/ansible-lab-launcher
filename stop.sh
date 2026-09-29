#!/usr/bin/env bash

set -euo pipefail

CONTAINER_NAME="ansible-lab-launcher"

if ! podman container exists "$CONTAINER_NAME"; then
    echo "Ansible Lab Launcher is not installed as a container."
    exit 0
fi

if [[ "$(podman inspect -f '{{.State.Running}}' "$CONTAINER_NAME")" != "true" ]]; then
    echo "Ansible Lab Launcher is already stopped."
    exit 0
fi

podman stop "$CONTAINER_NAME" >/dev/null

echo "Ansible Lab Launcher stopped."
