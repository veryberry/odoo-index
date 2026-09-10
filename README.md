# odoo-commit-index

Semantic + lexical search over **odoo/odoo** commit history, scoped to the
`17.0..19.0` range, to power an agentic 17→19 migration of your custom modules.

Each commit becomes one searchable document = **commit message + a curated,
filtered diff**. Documents are stored in Postgres and searched three ways,
merged with Reciprocal Rank Fusion:

- **Semantic** — Voyage `voyage-code-4` embeddings (pgvector, cosine / HNSW)
- **Lexical (FTS)** — Postgres `tsvector` full-text
- **Lexical (trigram)** — `pg_trgm` for exact identifier fragments
  (a removed method name, a renamed field)

## ⚠️ Data privacy — read before indexing anything private

Text sent to Voyage's embeddings API is, **by default, stored and may be used
to train/improve their models** (perpetual license) unless you explicitly opt
out. Opt-out gives **zero-day retention** but requires: a payment method on
file **+** org **Admin**, then Dashboard → *Terms of Service* → toggle
*"Opted In"* → *"Opted Out"*. Opt-out is **not reversible** in the dashboard
(reinstate only via `legal@voyageai.com`). See
<https://docs.voyageai.com/docs/faq> and <https://www.voyageai.com/privacy>.

What we actually send: each commit's **message + curated diff** (the `doc_text`).

Checklist by corpus:

| Corpus (`source`) | Contents | Sensitivity | Action before embedding |
|---|---|---|---|
| `community` (odoo/odoo) | **public** GitHub code | none — already open | none; index freely |
| `enterprise` (odoo/enterprise) | Odoo's **licensed** source | 3rd-party proprietary | **opt out first** |
| your `custom-addons` / `veryberry` modules | **your** proprietary source | high | **opt out first** |

Rule of thumb: **public code → fine as-is; any private/licensed code → do the
Voyage opt-out BEFORE the first `embed` on that `source`.** The free 200M-token
allowance is unaffected by opting out. If zero-retention still isn't acceptable
for private code, swap in a self-hosted open code-embedding model (quality
trade-off) — only `embed.py` changes; the rest of the pipeline is unchanged.

## What gets excluded (all configured in `config.yaml`)

| Rule | Where |
|---|---|
| Everything outside `git log 17.0..19.0` | `rev_range` |
| `[MERGE]` / merge commits | `skip_merges`, `skip_subject_tags` |
| `l10n_*` **except** `l10n_de*`, `l10n_ch*` | `module_exclude_globs` / `module_allow_globs` |
| `point_of_sale`, `pos_*` | `module_exclude_globs` |
| `spreadsheet*` | `module_exclude_globs` |
| `payment_*` **except** installed providers (custom, paypal, novalnet, novalnet_ext, sepa_direct_debit) | allow list |
| per-file: `*/i18n/*.po`, `*.pot`, `*/static/lib/**`, `*.min.*`, lockfiles | `skip_*`, `lockfiles` |
| per-file asset cap for `js/css/scss/less` diffs over N chars | `asset_diff_max_chars` |
| global per-commit diff cap | `commit_diff_max_chars` |

A commit whose diff is *entirely* excluded (e.g. a POS-only or `l10n_fr`-only
commit) is dropped completely.

> **Decisions flagged for you** (see comments in `config.yaml`):
> - `l10n_at` (Austria) and `l10n_din5008*` (German DIN document layouts) are
>   **installed on prod** but excluded by the literal "only de/ch" rule.
>   Uncomment their allow entries if you customize them.
> - Several `spreadsheet*` modules are **installed** but excluded per your spec.
> - `only_installed: false` → indexes *all* addons minus the excludes above
>   (broader recall). Set `true` to also drop anything not in
>   `installed_modules.txt`.

## Two stores, one Voyage bill

Voyage is paid **once** at embed time. The dense vectors land in Postgres, which
doubles as the durable cache that Qdrant loads from — so both engines are
populated for a single spend. Qdrant's lexical side (BM25 sparse vectors) is
generated **locally by fastembed at $0 API cost**.

```
git → build ─→ embed (Voyage, paid once) ─→ Postgres (rows + dense vectors)
                                              ├─ search --store postgres   (pgvector + tsvector + pg_trgm)
                                              └─ load-qdrant ─→ Qdrant      (dense + local BM25 sparse)
                                                                 └─ search --store qdrant
                                    eval  → scores BOTH on recall@k, nDCG, latency
```

Decide the winner empirically with `eval` (see below), then keep one or both.

## Pipeline (all stages resumable)

1. `build` — walk git, apply filters, upsert commit rows (offline, fast).
2. `embed` — embed rows where `embedding IS NULL` (Voyage; cost-tracked; resumable).
3. `load-qdrant` — fan the cached vectors + local BM25 into Qdrant (no Voyage).
4. `search --store postgres|qdrant` — hybrid query against either engine.
5. `eval --queries eval/queries.yaml` — head-to-head quality + latency.

### Resuming after a stop/crash

Every stage continues from where it stopped — just re-run the same command:

| Stage | Resume behavior |
|---|---|
| `build` | skips commits already in Postgres (`--no-resume` to force full re-walk) |
| `embed` | only embeds rows `WHERE embedding IS NULL` — **never re-pays Voyage** for a done commit; commits per batch |
| `load-qdrant` | skips point ids already in Qdrant (`--recreate` to wipe, `--no-resume` to force) |

So a Voyage/API interruption at commit 12,000 of 27,664 costs nothing extra:
`embed` resumes at 12,001. Safe to Ctrl-C any stage and rerun.

## Run on a GCP VM

Suggested: `e2-standard-4` (4 vCPU / 16 GB), 60 GB disk, Ubuntu 22.04.

```bash
git clone <this project> && cd odoo-commit-index
./scripts/gcp_setup.sh            # installs docker+pg+pgvector, clones odoo, venv
cp .env.example .env              # add your VOYAGE_API_KEY
set -a; source .env; set +a
./scripts/run_index.sh            # initdb -> build -> stats -> embed -> stats
```

`run_index.sh` prints a token/cost estimate from `stats` **before** the embed
stage and pauses 5s so you can abort.

### Manual / partial runs

```bash
export PYTHONPATH=src
python -m odoo_index.cli build --limit 500      # try a slice first
python -m odoo_index.cli stats                  # see est. cost
python -m odoo_index.cli embed                  # embed everything unembedded
python -m odoo_index.cli search "how did stock move reservation change" --tag REF
python -m odoo_index.cli search "removed _get_default_journal" --module account
```

## Indexing enterprise too

Roughly half your installed Odoo modules live in the separate `odoo/enterprise`
repo. Index it as a second source into the same DB:

```bash
ODOO_REPO=/opt/enterprise python -m odoo_index.cli build   # after editing
# config.yaml: set source: enterprise  (and point repo/ODOO_REPO at the clone)
```
Then filter searches with `--source community|enterprise`, or search across both.

## Serve to a remote agent (MCP)

The index runs on one VM; your migration agent usually runs on another. Expose
search over the **Model Context Protocol** so the agent calls it as a tool.
`odoo_index.mcp_server` publishes two **read-only** tools:

| Tool | Purpose |
|---|---|
| `search_commits(query, k, store, module, tag, source)` | ranked hits; each carries a `github_url` |
| `get_commit(sha, max_chars)` | one commit's metadata + curated diff (sha or unique prefix) |

`query` routing is automatic: a bare identifier (`_action_assign`) takes the
literal fast-path (no Voyage), a question ("how did reservation change") goes
semantic. Tools are SELECT-only and `query` is a bound SQL parameter — no
mutations, no injection. `pip install -r requirements.txt` pulls in `mcp`.

### Transport: stdio over SSH (recommended)

Nothing is exposed on the network — SSH provides auth + encryption, and the
Voyage key never leaves the index VM. The agent VM spawns the server per session.

On the **index VM**, smoke-test the wiring without a client:

```bash
set -a; source .env; set +a; PYTHONPATH=src .venv/bin/python - <<'PY'
from odoo_index import mcp_server as m, db
from odoo_index.config import Config
m._cfg = Config('config.yaml'); m._conn = db.connect(m._cfg)
import json; print(json.dumps(m.search_commits('_action_assign', k=3, module='stock'),
                              indent=2, ensure_ascii=False))
PY
```

On the **agent VM**, add an SSH alias (key auth, non-interactive) to `~/.ssh/config`:

```
Host index-vm
    HostName 10.0.0.5
    User <you>
    IdentityFile ~/.ssh/id_ed25519
    BatchMode yes
```

then point the agent's MCP config at the wrapper (it encapsulates venv/`.env`/PYTHONPATH):

```json
{
  "mcpServers": {
    "odoo-commits": {
      "command": "ssh",
      "args": ["index-vm", "/path/to/odoo-commit-index/scripts/mcp_stdio.sh"]
    }
  }
}
```

`BatchMode yes` is required (no interactive prompts); do **not** pass `ssh -t` —
the stdio channel must stay raw, with no pseudo-tty. One MCP session = one SSH =
one server process holding one DB connection + one embedder for its lifetime.
Inspect it interactively with `npx @modelcontextprotocol/inspector` pointed at
the same command.

### Transport: streamable-http (only if you need it)

For multiple clients or a non-SSH host, serve HTTP — but bind a **private** VPC
address and gate it with a firewall rule; never expose it publicly. The file has
no built-in auth, so put a token-checking reverse proxy in front if you go this way.

```bash
python -m odoo_index.mcp_server --http --host 10.0.0.5 --port 8000   # endpoint: /mcp
```

## Expected scale & cost (`17.0..19.0`, community, current filters)

~27.7k commits survive filtering (all addons minus excludes; ~20k with
`only_installed: true`). `.po` noise upstream is ~1% (Odoo keeps translations
out of git). Full embed of message+capped diff ≈ **~60–70M tokens**, which is
**free** under `voyage-code-4`'s first-200M-tokens allowance ($0.12/M after).
`stats` gives the real token count for your actual corpus before you run.

## Tests

```bash
python -m tests.test_filters      # 29 filter-rule assertions, no DB needed
```
