#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_REGISTRY="docker.cnb.cool/lfo.cc/llm-plug"
TAG="${1:-$(date +%Y%m%d-%H%M)}"
FULL_IMAGE="${IMAGE_REGISTRY}:${TAG}"

echo ">>> 构建多架构镜像 (amd64, arm64): ${FULL_IMAGE}"
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -f "${SCRIPT_DIR}/Dockerfile" \
  -t "${FULL_IMAGE}" \
  -t "${IMAGE_REGISTRY}:latest" \
  --push \
  --provenance=false \
  --sbom=false \
  "${PROJECT_ROOT}"

echo ">>> 镜像已推送到 CNB"

echo ">>> 完成!"
echo "    远程: ${FULL_IMAGE}"
echo "    远程: ${IMAGE_REGISTRY}:latest"
