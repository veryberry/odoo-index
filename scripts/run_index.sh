#!/usr/bin/env bash
# End-to-end: schema -> build rows from git -> embed -> stats.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck disable=SC1091
[ -f .venv/bin/activate ] && source .venv/bin/activate
export PYTHONPATH="src:${PYTHONPATH:-}"

python -m odoo_index.cli initdb
python -m odoo_index.cli build          # walk git + filters -> upsert rows
python -m odoo_index.cli stats          # shows est. token/cost BEFORE embedding
echo ">> review the estimate above; embedding starts in 5s (Ctrl-C to abort)"
sleep 5
python -m odoo_index.cli embed          # Voyage code-3 -> fills Postgres (paid ONCE)
python -m odoo_index.cli stats

echo ">> fanning the SAME vectors out to Qdrant (+ local BM25 sparse, no Voyage cost)"
python -m odoo_index.cli load-qdrant

echo
echo "Both stores are populated. Compare them:"
echo "  python -m odoo_index.cli search 'removed name_get' --store postgres"
echo "  python -m odoo_index.cli search 'removed name_get' --store qdrant"
echo "  python -m odoo_index.cli eval --queries eval/queries.yaml   # after labeling"
