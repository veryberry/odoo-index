"""Head-to-head eval: Postgres vs Qdrant on quality (recall@k, nDCG) + latency."""
import math
import time

import yaml


def load_queries(path):
    with open(path) as fh:
        return yaml.safe_load(fh)["queries"]


def _dcg(rels):
    return sum(r / math.log2(i + 2) for i, r in enumerate(rels))


def ndcg_at_k(ranked_shas, relevant, k):
    rels = [1.0 if s in relevant else 0.0 for s in ranked_shas[:k]]
    ideal = _dcg(sorted([1.0] * min(len(relevant), k), reverse=True))
    return (_dcg(rels) / ideal) if ideal else 0.0


def recall_at_k(ranked_shas, relevant, k):
    if not relevant:
        return None
    hit = len(set(ranked_shas[:k]) & set(relevant))
    return hit / len(relevant)


def evaluate(pg_search_fn, qd_search_fn, queries, k=10):
    """Each *_search_fn(query, module, tag) -> (list_of_shas, latency_seconds)."""
    agg = {"postgres": {"recall": [], "ndcg": [], "ms": []},
           "qdrant": {"recall": [], "ndcg": [], "ms": []}}
    rows = []
    for q in queries:
        relevant = set(q.get("relevant", []))
        if not relevant:
            continue  # unlabeled query — skip in scoring
        line = {"query": q["query"]}
        for name, fn in (("postgres", pg_search_fn), ("qdrant", qd_search_fn)):
            shas, dt = fn(q["query"], q.get("module"), q.get("tag"))
            r = recall_at_k(shas, relevant, k)
            n = ndcg_at_k(shas, relevant, k)
            agg[name]["recall"].append(r)
            agg[name]["ndcg"].append(n)
            agg[name]["ms"].append(dt * 1000)
            line[name] = (r, n, dt * 1000)
        rows.append(line)
    return rows, agg


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")


def print_report(rows, agg, k):
    print(f"\n{'query':<44} {'PG r@k':>7} {'PG ndcg':>8} {'PG ms':>7} "
          f"{'QD r@k':>7} {'QD ndcg':>8} {'QD ms':>7}")
    print("-" * 92)
    for r in rows:
        p, q = r["postgres"], r["qdrant"]
        print(f"{r['query'][:44]:<44} {p[0]:>7.2f} {p[1]:>8.2f} {p[2]:>7.1f} "
              f"{q[0]:>7.2f} {q[1]:>8.2f} {q[2]:>7.1f}")
    print("-" * 92)
    for name in ("postgres", "qdrant"):
        a = agg[name]
        print(f"{name:<10} mean recall@{k}={_avg(a['recall']):.3f}  "
              f"nDCG@{k}={_avg(a['ndcg']):.3f}  "
              f"p50 latency={sorted(a['ms'])[len(a['ms'])//2]:.1f}ms  "
              f"mean={_avg(a['ms']):.1f}ms")
