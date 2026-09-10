import argparse
import sys

from .config import Config
from .gitwalk import iter_commits
from .filters import Filters

# NOTE: db / embed / qdrant_store are imported lazily inside each command so
# that `--help` and arg parsing work on a machine without psycopg/voyage/qdrant.


def cmd_initdb(cfg, args):
    from . import db
    db.init_schema(cfg, args.schema)
    print("schema initialized")


def cmd_build(cfg, args):
    """Walk git, apply filters, upsert commit rows (no embeddings yet)."""
    from . import db
    filters = Filters(cfg)
    conn = db.connect(cfg)
    seen = set()
    if not args.no_resume:
        seen = db.existing_shas(conn, cfg["source"])
        if seen:
            print(f"  resume: {len(seen)} rows already built, skipping them",
                  file=sys.stderr)
    batch, n, kept, skipped = [], 0, 0, 0
    for doc in iter_commits(cfg, filters):
        if doc["sha"] in seen:
            skipped += 1
            continue
        batch.append(doc)
        kept += 1
        if len(batch) >= args.batch:
            db.upsert_many(conn, cfg["source"], batch)
            batch = []
        n += 1
        if n % 500 == 0:
            print(f"  built {n} new commits...", file=sys.stderr)
        if args.limit and kept >= args.limit:
            break
    if batch:
        db.upsert_many(conn, cfg["source"], batch)
    conn.close()
    print(f"built {kept} new commit rows (skipped {skipped} existing) "
          f"source={cfg['source']}")


def cmd_embed(cfg, args):
    from . import db
    from .embed import Embedder
    emb = Embedder(cfg)
    conn = db.connect(cfg)
    done = 0
    while True:
        rows = db.fetch_unembedded(conn, args.batch)
        if not rows:
            break
        shas = [r[0] for r in rows]
        texts = [r[1] for r in rows]
        vectors = list(emb.embed_documents(texts))
        db.set_embeddings(conn, list(zip(shas, vectors)))
        done += len(shas)
        print(f"  embedded {done} | tokens={emb.total_tokens} "
              f"| est ${emb.cost_usd:.2f}", file=sys.stderr)
        if args.limit and done >= args.limit:
            break
    conn.close()
    print(f"embedded {done} commits | total tokens {emb.total_tokens} "
          f"| est cost ${emb.cost_usd:.2f}")


def cmd_load_qdrant(cfg, args):
    """Fan-out: read embedded rows from Postgres cache -> Qdrant (+ local BM25)."""
    from . import db
    from .embed import SparseEmbedder
    from .qdrant_store import QdrantStore
    conn = db.connect(cfg)
    n = db.count_embedded(conn, source=None)
    if n == 0:
        raise SystemExit("no embedded rows yet — run `embed` first")
    store = QdrantStore(cfg)
    store.ensure_collection(recreate=args.recreate)
    sparse = SparseEmbedder(cfg)
    # --recreate wipes the collection, so nothing to resume past
    store.load(conn, sparse, db, source=args.source,
               resume=not args.recreate and not args.no_resume)
    conn.close()


def _fmt(res):
    for sha, tag, subject, modules, when, snippet in res:
        w = (when if isinstance(when, str) else (when.date() if when else "?"))
        print(f"\n{sha[:12]}  [{tag or '-'}]  {w}  {', '.join(modules or [])}")
        print(f"  {subject}")


def cmd_search(cfg, args):
    from . import db
    from .embed import Embedder
    emb = Embedder(cfg)
    if args.store == "qdrant":
        from .embed import SparseEmbedder
        from .qdrant_store import QdrantStore
        store = QdrantStore(cfg)
        sparse = SparseEmbedder(cfg)
        res = store.search(emb, sparse, args.query, k=args.k,
                           module=args.module, tag=args.tag, source=args.source)
        _fmt(res)
    else:
        from .search import hybrid_search
        conn = db.connect(cfg)
        res = hybrid_search(conn, emb, args.query, k=args.k, module=args.module,
                            tag=args.tag, source=args.source)
        _fmt(res)
        conn.close()


def cmd_eval(cfg, args):
    from . import db
    import time
    from .embed import Embedder, SparseEmbedder
    from .search import hybrid_search
    from .qdrant_store import QdrantStore
    from .evaluate import load_queries, evaluate, print_report

    emb = Embedder(cfg)
    sparse = SparseEmbedder(cfg)
    conn = db.connect(cfg)
    store = QdrantStore(cfg)
    queries = load_queries(args.queries)

    def pg_fn(q, module, tag):
        t = time.perf_counter()
        rows = hybrid_search(conn, emb, q, k=args.k, module=module, tag=tag)
        return [r[0] for r in rows], time.perf_counter() - t

    def qd_fn(q, module, tag):
        t = time.perf_counter()
        rows = store.search(emb, sparse, q, k=args.k, module=module, tag=tag)
        return [r[0] for r in rows], time.perf_counter() - t

    rows, agg = evaluate(pg_fn, qd_fn, queries, k=args.k)
    if not rows:
        raise SystemExit("no labeled queries (fill `relevant:` in the queries file)")
    print_report(rows, agg, args.k)
    conn.close()


def cmd_stats(cfg, args):
    from . import db
    conn = db.connect(cfg)
    total, embedded, fix, chars = db.stats(conn)
    conn.close()
    price = cfg["embed"]["price_per_million_tokens"]
    est_tokens = chars / 4
    print(f"commits:        {total}")
    print(f"  embedded:     {embedded}")
    print(f"  [FIX]:        {fix}")
    print(f"doc_text chars: {chars:,}  (~{est_tokens/1e6:.1f}M tokens)")
    print(f"est. full-embed cost @ ${price}/M: ${est_tokens/1e6*price:.2f}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="odoo-index")
    p.add_argument("-c", "--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("initdb"); s.add_argument("--schema", default="db/schema.sql")
    s.set_defaults(fn=cmd_initdb)

    s = sub.add_parser("build")
    s.add_argument("--batch", type=int, default=200)
    s.add_argument("--limit", type=int, default=0)
    s.add_argument("--no-resume", action="store_true",
                   help="re-process every commit instead of skipping built ones")
    s.set_defaults(fn=cmd_build)

    s = sub.add_parser("embed")
    s.add_argument("--batch", type=int, default=128)
    s.add_argument("--limit", type=int, default=0)
    s.set_defaults(fn=cmd_embed)

    s = sub.add_parser("load-qdrant")
    s.add_argument("--source", default=None)
    s.add_argument("--recreate", action="store_true",
                   help="drop and rebuild the collection from scratch")
    s.add_argument("--no-resume", action="store_true",
                   help="re-upsert every point instead of skipping loaded ones")
    s.set_defaults(fn=cmd_load_qdrant)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-k", type=int, default=10)
    s.add_argument("--store", choices=["postgres", "qdrant"], default="postgres")
    s.add_argument("--module"); s.add_argument("--tag"); s.add_argument("--source")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("eval")
    s.add_argument("--queries", default="eval/queries.yaml")
    s.add_argument("-k", type=int, default=10)
    s.set_defaults(fn=cmd_eval)

    s = sub.add_parser("stats"); s.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    cfg = Config(args.config)
    args.fn(cfg, args)


if __name__ == "__main__":
    main()
