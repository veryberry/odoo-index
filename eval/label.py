#!/usr/bin/env python
"""Interactive labeler for eval/queries.yaml (TREC-style pooling).

For each query it pools the top-k from BOTH stores (Postgres hybrid + Qdrant),
shows the union with a per-store provenance marker, you tick the real hits, and
it writes their SHAs into that query's `relevant:` list.

Run on the VM (needs the live DB, Qdrant, and VOYAGE_API_KEY):

    export PYTHONPATH=src
    python eval/label.py                 # only queries with relevant == []
    python eval/label.py --requery       # re-pool every query, even labeled ones
    python eval/label.py --store postgres # pool from one store only (skip Voyage duel)
    python eval/label.py -k 15           # deeper pool per store

At each prompt: `1 3 5`, `1,3-6`, `a`=all, Enter=none/skip, `u`=undo (clear this
query's labels), `q`=save+quit. Progress is saved after every query, so Ctrl-C
is safe — rerun to continue.
"""
import argparse
import os
import sys

# make `import odoo_index...` work whether run from repo root or eval/
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from odoo_index.config import Config  # noqa: E402


def _parse_selection(raw, n):
    """'1 3-5, 7' -> {1,3,4,5,7} (1-based, clamped to 1..n). '' -> set()."""
    raw = raw.strip().replace(",", " ")
    picked = set()
    for tok in raw.split():
        if "-" in tok and not tok.startswith("-"):
            a, _, b = tok.partition("-")
            if a.isdigit() and b.isdigit():
                for i in range(int(a), int(b) + 1):
                    if 1 <= i <= n:
                        picked.add(i)
            continue
        if tok.isdigit():
            i = int(tok)
            if 1 <= i <= n:
                picked.add(i)
    return picked


def _pool(query, module, tag, stores, k, deps):
    """Return (ordered_shas, meta) where meta[sha] = (marker, tag, subject, mods, when)."""
    order, seen = [], set()
    got = {}  # sha -> {"P"/"Q"}
    disp = {}
    results = []
    if "postgres" in stores:
        rows = deps["pg"](query, module, tag)
        results.append(("P", rows))
    if "qdrant" in stores:
        rows = deps["qd"](query, module, tag)
        results.append(("Q", rows))
    for marker, rows in results:
        for sha, t, subject, mods, when, _snip in rows:
            got.setdefault(sha, set()).add(marker)
            if sha not in seen:
                seen.add(sha)
                order.append(sha)
                disp[sha] = (t, subject, mods, when)
    meta = {}
    for sha in order:
        t, subject, mods, when = disp[sha]
        mk = "".join(m for m in ("P", "Q") if m in got[sha])
        meta[sha] = (mk, t, subject, mods, when)
    return order[: k * 2], meta


def _fmt_when(when):
    if when is None:
        return "?"
    if isinstance(when, str):
        return when[:10]
    return str(getattr(when, "date", lambda: when)())


def _label_loop(queries, stores, k, deps, requery, save):
    total = sum(1 for q in queries if requery or not q.get("relevant"))
    done = 0
    for q in queries:
        current = list(q.get("relevant") or [])
        if current and not requery:
            continue
        done += 1
        cls = q.get("class", "?")
        print(f"\n{'='*78}\n[{done}/{total}] ({cls})  {q['query']}")
        filt = []
        if q.get("module"):
            filt.append(f"module={q['module']}")
        if q.get("tag"):
            filt.append(f"tag={q['tag']}")
        if filt:
            print("  filter:", " ".join(filt))
        if current:
            print(f"  already labeled: {len(current)} — re-pooling (--requery)")
        order, meta = _pool(q["query"], q.get("module"), q.get("tag"),
                            stores, k, deps)
        if not order:
            print("  (no candidates from any store)")
            continue
        for i, sha in enumerate(order, 1):
            mk, t, subject, mods, when = meta[sha]
            star = "*" if sha in current else " "
            mods_s = ", ".join(mods or [])[:34]
            print(f" {star}{i:>2} [{mk:<2}] {sha[:12]} [{t or '-':<4}] "
                  f"{_fmt_when(when)}  {mods_s}\n       {(subject or '')[:70]}")
        try:
            raw = input("  relevant? (nums / a / u / Enter=skip / q): ").strip()
        except EOFError:
            raw = "q"
        if raw == "q":
            save(queries)
            print("saved, quitting.")
            return
        if raw == "u":
            q["relevant"] = []
            save(queries)
            print("  cleared.")
            continue
        if raw == "a":
            picked = set(range(1, len(order) + 1))
        else:
            picked = _parse_selection(raw, len(order))
        q["relevant"] = [order[i - 1] for i in sorted(picked)]
        save(queries)
        print(f"  -> {len(q['relevant'])} labeled.")
    print(f"\ndone. {total} queries visited.")


def _make_saver(path):
    """Return save(queries) that writes back, preserving comments if ruamel is present."""
    try:
        from ruamel.yaml import YAML
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        with open(path) as fh:
            doc = yaml_rt.load(fh)

        def save(queries):
            # queries are the same list objects loaded below, mutated in place
            with open(path, "w") as fh:
                yaml_rt.dump(doc, fh)
        return doc["queries"], save
    except ImportError:
        import yaml as pyyaml
        with open(path) as fh:
            text = fh.read()
        # capture the leading comment/blank header (up to the `queries:` key)
        header_lines = []
        for line in text.splitlines(keepends=True):
            if line.lstrip().startswith("#") or not line.strip():
                header_lines.append(line)
            else:
                break
        header = "".join(header_lines)
        data = pyyaml.safe_load(text)

        def save(queries):
            with open(path, "w") as fh:
                fh.write(header)
                pyyaml.safe_dump({"queries": queries}, fh, sort_keys=False,
                                 allow_unicode=True, default_flow_style=False)
        print("  (ruamel.yaml not installed — per-query inline comments will be "
              "dropped on save; header kept. `pip install ruamel.yaml` to keep all.)")
        return data["queries"], save


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--queries", default="eval/queries.yaml")
    ap.add_argument("-k", type=int, default=12, help="pool size per store")
    ap.add_argument("--store", choices=["postgres", "qdrant", "both"], default="both")
    ap.add_argument("--requery", action="store_true",
                    help="re-pool queries that are already labeled")
    args = ap.parse_args()

    cfg = Config(args.config)
    stores = ["postgres", "qdrant"] if args.store == "both" else [args.store]

    from odoo_index import db
    from odoo_index.embed import Embedder
    emb = Embedder(cfg)
    conn = db.connect(cfg)
    deps = {}
    from odoo_index.search import hybrid_search
    deps["pg"] = lambda q, m, t: hybrid_search(conn, emb, q, k=args.k,
                                               module=m, tag=t)
    if "qdrant" in stores:
        from odoo_index.embed import SparseEmbedder
        from odoo_index.qdrant_store import QdrantStore
        store = QdrantStore(cfg)
        sparse = SparseEmbedder(cfg)
        deps["qd"] = lambda q, m, t: store.search(emb, sparse, q, k=args.k,
                                                  module=m, tag=t)

    queries, save = _make_saver(args.queries)
    _label_loop(queries, stores, args.k, deps, args.requery, save)
    conn.close()


if __name__ == "__main__":
    main()
