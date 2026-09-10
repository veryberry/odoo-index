-- odoo-commit-index schema (Postgres 16 + pgvector)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS commits (
    sha          text PRIMARY KEY,
    source       text NOT NULL DEFAULT 'community',
    authored_at  timestamptz,
    committed_at timestamptz,
    author_name  text,
    author_email text,
    tag          text,                 -- FIX / IMP / REF / REM / MOV / ADD ...
    subject      text,
    body         text,
    modules      text[],               -- addons touched (after filtering)
    files        text[],               -- files kept in curated diff
    n_files      int,
    diff_text    text,                 -- curated, capped unified diff
    doc_text     text,                 -- what we embed + lexically index
    embedding    vector(1024),         -- NULL until the embed pass fills it
    tsv          tsvector GENERATED ALWAYS AS (
                     to_tsvector('simple',
                        coalesce(subject,'') || ' ' ||
                        coalesce(body,'')    || ' ' ||
                        coalesce(diff_text,'')
                     )
                 ) STORED,
    indexed_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS commits_tsv_idx     ON commits USING gin (tsv);
CREATE INDEX IF NOT EXISTS commits_doc_trgm_idx ON commits USING gin (doc_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS commits_modules_idx  ON commits USING gin (modules);
CREATE INDEX IF NOT EXISTS commits_tag_idx      ON commits (tag);
CREATE INDEX IF NOT EXISTS commits_source_idx   ON commits (source);
-- HNSW index for cosine similarity. Built now; fine to (re)build after load too.
CREATE INDEX IF NOT EXISTS commits_embedding_idx
    ON commits USING hnsw (embedding vector_cosine_ops);
