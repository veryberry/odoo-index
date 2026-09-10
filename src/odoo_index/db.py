import psycopg
from pgvector.psycopg import register_vector


def connect(cfg):
    conn = psycopg.connect(cfg["database_url"], autocommit=False)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
        if cur.fetchone():
            register_vector(conn)
    return conn


def init_schema(cfg, schema_path="db/schema.sql"):
    sql = open(schema_path).read()
    conn = psycopg.connect(cfg["database_url"], autocommit=True)
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.close()


UPSERT = """
INSERT INTO commits
  (sha, source, authored_at, committed_at, author_name, author_email,
   tag, subject, body, modules, files, n_files, diff_text, doc_text)
VALUES
  (%(sha)s, %(source)s, %(authored_at)s, %(committed_at)s, %(author_name)s,
   %(author_email)s, %(tag)s, %(subject)s, %(body)s, %(modules)s, %(files)s,
   %(n_files)s, %(diff_text)s, %(doc_text)s)
ON CONFLICT (sha) DO UPDATE SET
   source=EXCLUDED.source, subject=EXCLUDED.subject, body=EXCLUDED.body,
   modules=EXCLUDED.modules, files=EXCLUDED.files, n_files=EXCLUDED.n_files,
   diff_text=EXCLUDED.diff_text, doc_text=EXCLUDED.doc_text
"""


def upsert_many(conn, source, docs):
    rows = [{**d, "source": source} for d in docs]
    with conn.cursor() as cur:
        cur.executemany(UPSERT, rows)
    conn.commit()


def fetch_unembedded(conn, limit):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sha, doc_text FROM commits WHERE embedding IS NULL "
            "ORDER BY committed_at NULLS LAST LIMIT %s",
            (limit,),
        )
        return cur.fetchall()


def set_embeddings(conn, pairs):
    with conn.cursor() as cur:
        cur.executemany(
            "UPDATE commits SET embedding=%s WHERE sha=%s",
            [(emb, sha) for sha, emb in pairs],
        )
    conn.commit()


def existing_shas(conn, source=None):
    q = "SELECT sha FROM commits"
    params = []
    if source:
        q += " WHERE source=%s"
        params.append(source)
    with conn.cursor() as cur:
        cur.execute(q, params)
        return {r[0] for r in cur.fetchall()}


def iter_embedded(conn, source=None, batch=500):
    """Stream (sha, dense_vector, payload, doc_text) for rows that are embedded.

    This is the durable vector cache Qdrant loads from — no re-embedding.
    """
    where = "WHERE embedding IS NOT NULL"
    params = []
    if source:
        where += " AND source=%s"
        params.append(source)
    with conn.cursor(name="emb_cur") as cur:  # server-side cursor
        cur.itersize = batch
        cur.execute(
            "SELECT sha, embedding, source, tag, subject, modules, "
            "authored_at, committed_at, n_files, doc_text "
            "FROM commits " + where,
            params,
        )
        for r in cur:
            sha, emb, src, tag, subject, modules, aat, cat, nfiles, doc = r
            payload = {
                "sha": sha, "source": src, "tag": tag, "subject": subject,
                "modules": modules, "n_files": nfiles,
                "authored_at": aat.isoformat() if aat else None,
                "committed_at": cat.isoformat() if cat else None,
            }
            yield sha, emb, payload, doc


def count_embedded(conn, source=None):
    q = "SELECT count(*) FROM commits WHERE embedding IS NOT NULL"
    params = []
    if source:
        q += " AND source=%s"
        params.append(source)
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchone()[0]


def stats(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*), count(embedding), "
            "count(*) FILTER (WHERE tag='FIX'), "
            "coalesce(sum(length(doc_text)),0) FROM commits"
        )
        return cur.fetchone()
