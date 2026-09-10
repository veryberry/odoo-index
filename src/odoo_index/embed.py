import time

import voyageai


class Embedder:
    def __init__(self, cfg):
        e = cfg["embed"]
        self.client = voyageai.Client(api_key=cfg.voyage_api_key)
        self.model = e["model"]
        self.dim = e["output_dimension"]
        self.batch_size = e["batch_size"]
        self.batch_max_chars = e["batch_max_chars"]
        self.price = e["price_per_million_tokens"]
        self.it_doc = e["input_type_document"]
        self.it_query = e["input_type_query"]
        self.total_tokens = 0

    def _batches(self, texts):
        batch, chars = [], 0
        for t in texts:
            if batch and (len(batch) >= self.batch_size or chars + len(t) > self.batch_max_chars):
                yield batch
                batch, chars = [], 0
            batch.append(t)
            chars += len(t)
        if batch:
            yield batch

    def embed_documents(self, texts):
        """Yield embeddings in input order, batching + retrying."""
        for batch in self._batches(texts):
            for attempt in range(6):
                try:
                    r = self.client.embed(
                        batch, model=self.model, input_type=self.it_doc,
                        output_dimension=self.dim, truncation=True,
                    )
                    self.total_tokens += r.total_tokens
                    yield from r.embeddings
                    break
                except Exception as exc:  # rate limit / transient
                    wait = 2 ** attempt
                    print(f"  embed retry {attempt+1} in {wait}s: {exc}")
                    time.sleep(wait)
            else:
                raise RuntimeError("embedding failed after retries")

    def embed_query(self, text):
        r = self.client.embed(
            [text], model=self.model, input_type=self.it_query,
            output_dimension=self.dim, truncation=True,
        )
        self.total_tokens += r.total_tokens
        return r.embeddings[0]

    @property
    def cost_usd(self):
        return self.total_tokens / 1_000_000 * self.price


class SparseEmbedder:
    """Local BM25 sparse vectors via fastembed. No API cost (CPU only)."""

    def __init__(self, cfg):
        from fastembed import SparseTextEmbedding

        self.model = SparseTextEmbedding(cfg["sparse"]["model"])

    def embed_documents(self, texts):
        for e in self.model.embed(list(texts)):
            yield e.indices.tolist(), e.values.tolist()

    def embed_query(self, text):
        e = next(iter(self.model.query_embed([text])))
        return e.indices.tolist(), e.values.tolist()
