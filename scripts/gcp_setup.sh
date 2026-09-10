#!/usr/bin/env bash
# Provision a fresh Debian/Ubuntu GCP VM to run the odoo commit index.
# Suggested VM: e2-standard-4 (4 vCPU / 16 GB), 60 GB balanced PD, Ubuntu 22.04.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/odoo/odoo}"
ODOO_REPO="${ODOO_REPO:-/opt/odoo}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo ">> installing system deps"
sudo apt-get update -y
sudo apt-get install -y git python3 python3-venv python3-pip ca-certificates curl gnupg

if ! command -v docker >/dev/null; then
  echo ">> installing docker"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER" || true
fi

echo ">> cloning odoo/odoo (full history required for the 17.0..19.0 range)"
if [ ! -d "$ODOO_REPO/.git" ]; then
  sudo mkdir -p "$ODOO_REPO"
  sudo chown "$USER:$USER" "$ODOO_REPO"
  # We need commit history + blobs for the range. A full clone is simplest.
  git clone "$REPO_URL" "$ODOO_REPO"
fi
git -C "$ODOO_REPO" fetch origin 17.0:17.0 19.0:19.0 || true

echo ">> starting postgres+pgvector"
cd "$PROJECT_DIR"
sudo docker compose up -d
until sudo docker exec odoo_index_db pg_isready -U odoo -d odoo_index >/dev/null 2>&1; do
  sleep 2; echo "  waiting for db..."
done

echo ">> python venv + deps"
python3 -m venv "$PROJECT_DIR/.venv"
# shellcheck disable=SC1091
source "$PROJECT_DIR/.venv/bin/activate"
pip install -q --upgrade pip
pip install -q -r requirements.txt

echo
echo "Setup done. Next:"
echo "  cp .env.example .env   # put your VOYAGE_API_KEY in it"
echo "  set -a; source .env; set +a"
echo "  ./scripts/run_index.sh"
