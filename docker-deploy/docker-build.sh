#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_REGISTRY="docker.cnb.cool/lfo.cc/llm-plug"
TAG="${1:-$(date +%Y%m%d-%H%M)}"
FULL_IMAGE="${IMAGE_REGISTRY}:${TAG}"

echo ">>> 构建镜像: ${FULL_IMAGE}"
docker build -f "${SCRIPT_DIR}/Dockerfile" -t "${FULL_IMAGE}" "${PROJECT_ROOT}"

echo ">>> 推送到 CNB: ${FULL_IMAGE}"
docker push "${FULL_IMAGE}"

docker tag "${FULL_IMAGE}" "${IMAGE_REGISTRY}:latest"
echo ">>> 推送到 CNB: ${IMAGE_REGISTRY}:latest"
docker push "${IMAGE_REGISTRY}:latest"

DIST_DIR="${SCRIPT_DIR}/dist"
mkdir -p "${DIST_DIR}"
ARCHIVE="${DIST_DIR}/llm-plug-${TAG}.tar.gz"
echo ">>> 保存镜像到: ${ARCHIVE}"
docker save "${FULL_IMAGE}" | gzip > "${ARCHIVE}"

IMAGE_SIZE=$(du -h "${ARCHIVE}" | cut -f1)
echo ">>> 完成! 镜像大小: ${IMAGE_SIZE}"
echo "    远程: ${FULL_IMAGE}"
echo "    本地: ${ARCHIVE}"
