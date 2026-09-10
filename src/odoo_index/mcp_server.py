"""MCP server exposing the odoo commit index as agent tools.

Runs ON the index VM (where Postgres, Qdrant and VOYAGE_API_KEY live) and lets a
remote agent search the 17.0..19.0 commit corpus over the Model Context Protocol.

Two read-only tools:
  * search_commits(query, k, store, module, tag, source) -> ranked hits
  * get_commit(sha, max_chars)                            -> one commit's diff

Transport (pick at deploy time; the code is the same):

  stdio (default, recommended) — the agent VM spawns this over SSH, so nothing is
  exposed on the network and the Voyage key never leaves this host:

      # in the agent's MCP config:
      command: ssh
      args: ["index-vm",
             "cd /opt/odoo-commit-index && PYTHONPATH=src "
             "VOYAGE_API_KEY=... python -m odoo_index.mcp_server"]

  http (only if you need multiple clients / no SSH) — bind to a PRIVATE VPC ip and
  gate it with a firewall rule; do NOT expose it publicly:

      python -m odoo_index.mcp_server --http --host 10.0.0.5 --port 8000

Security posture: tools are SELECT-only; the agent's `query` is passed as a bound
SQL parameter (never string-formatted); no tool mutates the DB or returns secrets.
"""
import argparse

from mcp.server.fastmcp import FastMCP

from .config import Config

# odoo hosts community on github.com/odoo/odoo and licensed code on
# github.com/odoo/enterprise (private). We only link; we never fetch.
_REPO_BY_SOURCE = {"community": "odoo/odoo", "enterprise": "odoo/enterprise"}

mcp = FastMCP("odoo-commit-index")

# process-wide handles, initialized in main() before the server loop starts
_cfg = None
_conn = None
_embedder = None          # lazy: only built when a query actually needs Voyage
_qdrant = None            # lazy: (store, sparse_embedder)


def _get_embedder():
    global _embedder
    if _embedder is None:
        from .embed import Embedder
        _embedder = Embedder(_cfg)
    return _embedder


def _get_qdrant():
    global _qdrant
    if _qdrant is None:
        from .embed import SparseEmbedder
        from .qdrant_store import QdrantStore
        _qdrant = (QdrantStore(_cfg), SparseEmbedder(_cfg))
    return _qdrant


def _github_url(sha, source):
    repo = _REPO_BY_SOURCE.get(source or "community", "odoo/odoo")
    return f"https://github.com/{repo}/commit/{sha}"


def _iso(when):
    if when is None:
        return None
    return when if isinstance(when, str) else when.isoformat()


@mcp.tool()
def search_commits(query: str, k: int = 10, store: str = "postgres",
                   module: str | None = None, tag: str | None = None,
                   source: str | None = None) -> list[dict]:
    """Search the odoo 17->19 commit corpus for commits relevant to `query`.

    A bare code identifier (e.g. `_action_assign`, `stock.move`, `_compute_amount`)
    is auto-routed to a fast literal-match path; a natural-language question
    (e.g. "how did stock move reservation change") uses semantic search. Returns
    the top `k` hits, most relevant first.

    Args:
        query: identifier or natural-language question.
        k: number of results (1-50).
        store: "postgres" (default) or "qdrant" — the retrieval engine.
        module: restrict to commits touching this addon (e.g. "stock", "account").
        tag: restrict to a commit tag (e.g. "FIX", "REF", "IMP", "ADD").
        source: "community" (default corpus) or "enterprise".

    Returns:
        List of {rank, sha, tag, subject, modules, authored_at, github_url,
        snippet}. Use get_commit(sha) to fetch a hit's full diff.
    """
    k = max(1, min(int(k), 50))
    store = store if store in ("postgres", "qdrant") else "postgres"
    if store == "qdrant":
        qstore, sparse = _get_qdrant()
        rows = qstore.search(_get_embedder(), sparse, query, k=k,
                             module=module, tag=tag, source=source)
    else:
        from .search import hybrid_search
        # Voyage is only touched inside hybrid_search for NL queries; identifier
        # queries never call _get_embedder().embed_query, but we pass the lazy
        # embedder so it's there if the router needs it.
        rows = hybrid_search(_conn, _get_embedder(), query, k=k,
                             module=module, tag=tag, source=source)
    _conn.rollback()  # release the read snapshot (autocommit is off in db.connect)
    out = []
    for rank, (sha, tg, subject, modules, when, snippet) in enumerate(rows, 1):
        out.append({
            "rank": rank,
            "sha": sha,
            "tag": tg,
            "subject": subject,
            "modules": list(modules or []),
            "authored_at": _iso(when),
            "github_url": _github_url(sha, source),
            "snippet": (snippet or "").strip() or None,
        })
    return out


@mcp.tool()
def get_commit(sha: str, max_chars: int = 24000) -> dict:
    """Fetch one commit in full: metadata + its curated diff.

    Accepts a full 40-char sha or a unique prefix (as returned by search_commits).

    Args:
        sha: commit sha or unique prefix.
        max_chars: cap on the returned diff length (the diff is truncated with a
            marker if longer; raise this to see more).

    Returns:
        {sha, source, tag, subject, body, modules, files, n_files, authored_at,
        github_url, diff_text, truncated}. Returns {error: ...} if not found or
        the prefix is ambiguous.
    """
    sha = (sha or "").strip()
    if not sha or any(c not in "0123456789abcdefABCDEF" for c in sha):
        return {"error": "sha must be a hex commit id or prefix"}
    with _conn.cursor() as cur:
        if len(sha) >= 40:
            cur.execute(
                "SELECT sha, source, tag, subject, body, modules, files, "
                "n_files, authored_at, diff_text FROM commits WHERE sha=%s",
                (sha,),
            )
        else:
            cur.execute(
                "SELECT sha, source, tag, subject, body, modules, files, "
                "n_files, authored_at, diff_text FROM commits "
                "WHERE sha LIKE %s LIMIT 2",
                (sha + "%",),
            )
        found = cur.fetchall()
    _conn.rollback()
    if not found:
        return {"error": f"no commit matching {sha!r}"}
    if len(found) > 1:
        return {"error": f"prefix {sha!r} is ambiguous — give more characters"}
    (rsha, rsource, tag, subject, body, modules, files, n_files, when,
     diff_text) = found[0]
    diff = diff_text or ""
    truncated = len(diff) > max_chars
    if truncated:
        diff = diff[:max_chars] + f"\n... [truncated at {max_chars} chars]"
    return {
        "sha": rsha,
        "source": rsource,
        "tag": tag,
        "subject": subject,
        "body": body,
        "modules": list(modules or []),
        "files": list(files or []),
        "n_files": n_files,
        "authored_at": _iso(when),
        "github_url": _github_url(rsha, rsource),
        "diff_text": diff,
        "truncated": truncated,
    }


def main(argv=None):
    global _cfg, _conn
    ap = argparse.ArgumentParser(prog="odoo-index-mcp")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--http", action="store_true",
                    help="serve over streamable-http instead of stdio "
                         "(bind a PRIVATE ip + firewall; do not expose publicly)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)

    _cfg = Config(args.config)
    from . import db
    _conn = db.connect(_cfg)

    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
