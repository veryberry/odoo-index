"""Qdrant store: dense (Voyage) + sparse (local BM25), server-side fusion.

Dense vectors are read from the Postgres cache — Voyage is never called again.
"""
import uuid

from qdrant_client import QdrantClient, models

_NS = uuid.UUID("6f1e7c4a-0000-4000-8000-000000000001")


def _point_id(sha: str) -> str:
    return str(uuid.uuid5(_NS, sha))


def _vec(v):
    """pgvector may return a Vector object / numpy array / list — normalize."""
    if hasattr(v, "to_list"):
        return v.to_list()
    if hasattr(v, "tolist"):
        return v.tolist()
    return list(v)


class QdrantStore:
    def __init__(self, cfg):
        q = cfg["qdrant"]
        self.client = QdrantClient(url=q["url"])
        self.collection = q["collection"]
        self.dense = q["dense_name"]
        self.sparse = q["sparse_name"]
        self.dim = cfg["embed"]["output_dimension"]
        self.fusion = (
            models.Fusion.DBSF if q.get("fusion") == "dbsf" else models.Fusion.RRF
        )

    def ensure_collection(self, recreate=False):
        exists = self.client.collection_exists(self.collection)
        if exists and recreate:
            self.client.delete_collection(self.collection)
            exists = False
        if not exists:
            self.client.create_collection(
                self.collection,
                vectors_config={
                    self.dense: models.VectorParams(
                        size=self.dim, distance=models.Distance.COSINE
                    )
                },
                sparse_vectors_config={
                    self.sparse: models.SparseVectorParams(
                        modifier=models.Modifier.IDF  # BM25 needs IDF
                    )
                },
            )
            # payload indexes for fast filtering
            for field, schema in [
                ("source", models.PayloadSchemaType.KEYWORD),
                ("tag", models.PayloadSchemaType.KEYWORD),
                ("modules", models.PayloadSchemaType.KEYWORD),
            ]:
                self.client.create_payload_index(self.collection, field, schema)

    def existing_ids(self):
        """Set of point ids already stored (for resume)."""
        ids, offset = set(), None
        while True:
            pts, offset = self.client.scroll(
                self.collection, limit=10000, offset=offset,
                with_payload=False, with_vectors=False,
            )
            ids.update(p.id for p in pts)
            if offset is None:
                break
        return ids

    def load(self, conn, sparse_embedder, db_module, source=None, batch=256,
             resume=True):
        """Fan-out: read embedded rows from Postgres, add local sparse, upsert.

        With resume=True, points already present in Qdrant are skipped so an
        interrupted load continues from where it stopped.
        """
        from itertools import islice

        done_ids = self.existing_ids() if resume else set()
        if done_ids:
            print(f"  resume: {len(done_ids)} points already in Qdrant, skipping")
        gen = db_module.iter_embedded(conn, source=source)
        total, skipped = 0, 0
        while True:
            chunk = list(islice(gen, batch))
            if not chunk:
                break
            chunk = [c for c in chunk if _point_id(c[0]) not in done_ids]
            if not chunk:
                skipped += batch
                continue
            docs = [c[3] for c in chunk]
            sparses = list(sparse_embedder.embed_documents(docs))
            points = []
            for (sha, dense, payload, _), (idx, val) in zip(chunk, sparses):
                points.append(
                    models.PointStruct(
                        id=_point_id(sha),
                        vector={
                            self.dense: _vec(dense),
                            self.sparse: models.SparseVector(indices=idx, values=val),
                        },
                        payload=payload,
                    )
                )
            self.client.upsert(self.collection, points=points)
            total += len(points)
            print(f"  qdrant loaded {total}", end="\r")
        print(f"\n  qdrant loaded {total} new points (skipped ~{skipped} existing)")
        return total

    def _filter(self, module, tag, source):
        must = []
        if module:
            must.append(models.FieldCondition(key="modules", match=models.MatchValue(value=module)))
        if tag:
            must.append(models.FieldCondition(key="tag", match=models.MatchValue(value=tag)))
        if source:
            must.append(models.FieldCondition(key="source", match=models.MatchValue(value=source)))
        return models.Filter(must=must) if must else None

    def search(self, dense_embedder, sparse_embedder, query, *, k=10, pool=50,
               module=None, tag=None, source=None):
        qdense = dense_embedder.embed_query(query)
        idx, val = sparse_embedder.embed_query(query)
        flt = self._filter(module, tag, source)
        res = self.client.query_points(
            self.collection,
            prefetch=[
                models.Prefetch(query=list(qdense), using=self.dense, limit=pool, filter=flt),
                models.Prefetch(
                    query=models.SparseVector(indices=idx, values=val),
                    using=self.sparse, limit=pool, filter=flt,
                ),
            ],
            query=models.FusionQuery(fusion=self.fusion),
            limit=k,
            with_payload=True,
        )
        out = []
        for p in res.points:
            pl = p.payload
            out.append((pl["sha"], pl.get("tag"), pl.get("subject"),
                        pl.get("modules"), pl.get("authored_at"), ""))
        return out
