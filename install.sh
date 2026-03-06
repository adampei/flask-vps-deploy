#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this installer as root." >&2
  exit 1
fi

REPO_SLUG="${FLASK_VPS_DEPLOY_REPO:-adampei/flask-vps-deploy}"
REPO_REF="${FLASK_VPS_DEPLOY_REF:-main}"
RAW_BASE="https://raw.githubusercontent.com/${REPO_SLUG}/${REPO_REF}/scripts"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

curl -fsSL -o "${TMP_DIR}/flask_vps_deploy.py" "${RAW_BASE}/flask_vps_deploy.py"
curl -fsSL -o "${TMP_DIR}/install_flask_vps_deploy.sh" "${RAW_BASE}/install_flask_vps_deploy.sh"

bash "${TMP_DIR}/install_flask_vps_deploy.sh"
