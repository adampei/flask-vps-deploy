#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run this installer as root." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_PY="${SCRIPT_DIR}/flask_vps_deploy.py"
PREFIX="${PREFIX:-/usr/local}"
BIN_DIR="${PREFIX}/bin"
LIB_DIR="${PREFIX}/lib/flask-vps-deploy"
TARGET_PY="${LIB_DIR}/flask_vps_deploy.py"
TARGET_BIN="${BIN_DIR}/flask-vps-deploy"

if [[ ! -f "${SOURCE_PY}" ]]; then
  echo "Cannot find flask_vps_deploy.py next to this installer." >&2
  exit 1
fi

install -d "${BIN_DIR}" "${LIB_DIR}"
install -m 0644 "${SOURCE_PY}" "${TARGET_PY}"

cat > "${TARGET_BIN}" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec python3 "${TARGET_PY}" "\$@"
EOF

chmod +x "${TARGET_BIN}"

echo "Installed flask-vps-deploy to ${TARGET_BIN}"
echo "Run it inside your project directory:"
echo "  sudo flask-vps-deploy"
