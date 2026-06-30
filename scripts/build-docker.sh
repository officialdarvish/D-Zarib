#!/usr/bin/env bash
set -euo pipefail
IMAGE_NAME="${IMAGE_NAME:-darvish021/d-zarib:latest}"
docker build -t "$IMAGE_NAME" .
printf '\nBuilt image: %s\n' "$IMAGE_NAME"
