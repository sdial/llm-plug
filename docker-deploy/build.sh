#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_REGISTRY="docker.cnb.cool/lfo.cc/llm-plug"
TAG="${1:-$(date +%Y%m%d-%H%M)}"
FULL_IMAGE="${IMAGE_REGISTRY}:${TAG}"
BUILDER_NAME="llm-plug-builder"

# ---------- 颜色输出 ----------
if [[ -t 1 ]]; then
  C_RESET=$'\033[0m'
  C_INFO=$'\033[1;36m'
  C_OK=$'\033[1;32m'
  C_WARN=$'\033[1;33m'
  C_ERR=$'\033[1;31m'
else
  C_RESET=""; C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""
fi

log()   { echo "${C_INFO}>>> $*${C_RESET}"; }
ok()    { echo "${C_OK}>>> $*${C_RESET}"; }
warn()  { echo "${C_WARN}>>> $*${C_RESET}"; }
die()   { echo "${C_ERR}>>> $*${C_RESET}" >&2; exit 1; }

# ---------- 前置检查 ----------
command -v docker >/dev/null 2>&1 || die "未找到 docker 命令"

if ! docker info >/dev/null 2>&1; then
  die "docker daemon 未运行或无权限访问"
fi

# 检查 CNB 登录态（不强制，CI 环境可能用其他方式认证）
if ! docker system info 2>/dev/null | grep -q "docker.cnb.cool"; then
  if [[ -z "${DOCKER_CONFIG:-}" && ! -f "${HOME}/.docker/config.json" ]]; then
    warn "未检测到 docker.cnb.cool 登录态，如推送失败请执行: docker login docker.cnb.cool"
  fi
fi

# ---------- 确保 buildx 可用 ----------
if ! docker buildx version >/dev/null 2>&1; then
  die "docker buildx 不可用，请安装或升级 Docker"
fi

# 确保 builder 存在。
# 优先使用默认 docker 驱动（直接用宿主 buildkit，无需额外 mount，兼容受限容器环境）。
# docker 驱动不支持多架构同时 push，但支持通过 --platform 单次构建；
# 真正的多架构清单由 buildx 在 push 阶段合成，需要 buildkit >= 0.10。
# 如默认驱动不可用，再尝试创建 docker-container 驱动 builder。
BUILDER_FLAG=""

if docker buildx inspect default >/dev/null 2>&1; then
  # 默认 builder 存在，直接使用
  BUILDER_FLAG="--builder default"
  log "使用默认 buildx builder (docker 驱动)"
else
  # 回退：创建 docker-container 驱动 builder
  if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
    warn "默认 builder 不可用，创建 docker-container builder: ${BUILDER_NAME}"
    docker buildx create \
      --name "${BUILDER_NAME}" \
      --driver docker-container \
      --bootstrap \
      >/dev/null 2>&1 || true
  fi
  docker buildx inspect "${BUILDER_NAME}" --bootstrap >/dev/null 2>&1 || true
  BUILDER_FLAG="--builder ${BUILDER_NAME}"
fi

# ---------- 构建信息 ----------
log "项目根目录: ${PROJECT_ROOT}"
log "Dockerfile : ${SCRIPT_DIR}/Dockerfile"
log "目标平台  : linux/amd64, linux/arm64"
log "镜像标签  :"
echo "    ${C_OK}${FULL_IMAGE}${C_RESET}"
echo "    ${C_OK}${IMAGE_REGISTRY}:latest${C_RESET}"

# ---------- 多架构构建 + 推送 ----------
log "开始构建并推送多架构镜像..."
# shellcheck disable=SC2086
docker buildx build \
  ${BUILDER_FLAG} \
  --platform linux/amd64,linux/arm64 \
  -f "${SCRIPT_DIR}/Dockerfile" \
  -t "${FULL_IMAGE}" \
  -t "${IMAGE_REGISTRY}:latest" \
  --push \
  --provenance=false \
  --sbom=false \
  --pull \
  "${PROJECT_ROOT}"

# ---------- 验证推送结果 ----------
log "验证远程 manifest..."
if docker buildx imagetools inspect "${FULL_IMAGE}" >/dev/null 2>&1; then
  ok "tag ${TAG} 推送成功"
else
  die "tag ${TAG} 远程验证失败"
fi

if docker buildx imagetools inspect "${IMAGE_REGISTRY}:latest" >/dev/null 2>&1; then
  ok "tag latest 推送成功"
else
  die "tag latest 远程验证失败"
fi

echo
ok "构建并推送完成!"
echo "    ${C_OK}${FULL_IMAGE}${C_RESET}"
echo "    ${C_OK}${IMAGE_REGISTRY}:latest${C_RESET}"
