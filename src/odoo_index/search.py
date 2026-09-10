"""Hybrid search: semantic (pgvector cosine) + lexical (FTS + trigram), RRF-merged.

For identifier-like queries (a bare token with `_`/`.`/camelCase, e.g.
`_action_assign`), literal-substring hits are treated as ground truth and
placed FIRST — otherwise RRF dilutes them among broad semantic "assign" matches.
"""
import re

_IDENT_RE = re.compile(r"^[A-Za-z0-9_.]+$")


def _looks_like_identifier(q):
    q = q.strip()
    if not q or " " in q or not _IDENT_RE.match(q):
        return False
    # code-ish shape: has an underscore, a dot, or an internal camelCase hump.
    return "_" in q or "." in q or bool(re.search(r"[a-z][A-Z]", q))


def _exact(conn, q, where, params, k):
    # Literal containment. Escape LIKE metachars so `_` matches a real
    # underscore (not "any char") and `%` is literal.
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    # Order by recency, not word_similarity: every hit contains the literal
    # already (so similarity ties at ~1.0), and for a 17->19 migration the
    # newest touch of an identifier is the most relevant. authored_at ordering
    # is also cheap, avoiding a per-row trigram recompute over 5KB docs.
    sql = (
        "SELECT sha FROM commits WHERE doc_text ILIKE %(pat)s" + where +
        " ORDER BY authored_at DESC LIMIT %(k)s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, {**params, "pat": pat, "k": k})
        return [r[0] for r in cur.fetchall()]


def _filter_sql(module, tag, source):
    clauses, params = [], {}
    if module:
        clauses.append("%(module)s = ANY(modules)")
        params["module"] = module
    if tag:
        clauses.append("tag = %(tag)s")
        params["tag"] = tag
    if source:
        clauses.append("source = %(source)s")
        params["source"] = source
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _semantic(conn, qvec, where, params, k):
    sql = (
        "SELECT sha FROM commits WHERE embedding IS NOT NULL" + where +
        " ORDER BY embedding <=> %(qv)s::vector LIMIT %(k)s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, {**params, "qv": qvec, "k": k})
        return [r[0] for r in cur.fetchall()]


def _lexical(conn, q, where, params, k):
    sql = (
        "SELECT sha FROM commits "
        "WHERE tsv @@ websearch_to_tsquery('simple', %(q)s)" + where +
        " ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('simple', %(q)s)) DESC "
        "LIMIT %(k)s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, {**params, "q": q, "k": k})
        return [r[0] for r in cur.fetchall()]


def _rrf(rank_lists, k=60):
    scores = {}
    for lst in rank_lists:
        for rank, sha in enumerate(lst):
            scores[sha] = scores.get(sha, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def hybrid_search(conn, embedder, query, *, k=10, pool=50,
                  module=None, tag=None, source=None):
    where, params = _filter_sql(module, tag, source)
    lex = _lexical(conn, query, where, params, pool)
    if _looks_like_identifier(query):
        # Pure-lexical fast path. A bare identifier gains ~nothing from a
        # semantic embedding, so we skip Voyage entirely (no network round-trip)
        # and pin literal-substring hits (ground truth, via the pg_trgm GIN
        # index) to the top, with FTS filling the tail.
        exact = _exact(conn, query, where, params, pool)
        fused = _rrf([exact, lex])
        seen = set(exact)
        order = (exact + [s for s in fused if s not in seen])[:k]
    else:
        qvec = embedder.embed_query(query)
        sem = _semantic(conn, qvec, where, params, pool)
        order = _rrf([sem, lex])[:k]
    if not order:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sha, tag, subject, modules, authored_at, "
            "left(diff_text, 400) FROM commits WHERE sha = ANY(%s)",
            (order,),
        )
        rows = {r[0]: r for r in cur.fetchall()}
    return [rows[s] for s in order if s in rows]
