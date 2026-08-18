"""FAISS vector index with a persisted metadata sidecar.

What: builds/loads a FAISS index over chunk embeddings, plus the records and
      build manifest needed to map a FAISS row back to a citable chunk.
Inputs: RetrievalRecords + an Embedder.
Outputs: data/retrieval/{index.faiss, records.jsonl, manifest.json}
Why: FAISS returns row numbers, nothing else. The sidecar is what turns row 417
     into "this section, on this page, at this URL" - without it the index is
     unusable for a system that has to cite its sources.

FAISS was chosen over Chroma: the need here is a flat cosine index over a few
thousand vectors, which is one FAISS call and one JSONL file. Chroma would add
a database, a client lifecycle and its own embedding-function abstraction for
no gain at this size.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .cache import EmbeddingCache
from .embedders import Embedder
from .intent import classifier_fingerprint
from .records import RetrievalRecord


@dataclass
class BuildStats:
    total_records: int
    html_records: int
    pdf_records: int
    embedded_now: int
    reused_from_cache: int
    dimension: int
    model_name: str
    backend: str

    def to_dict(self) -> dict:
        return self.__dict__


class VectorIndex:
    INDEX_FILE = "index.faiss"
    RECORDS_FILE = "records.jsonl"
    MANIFEST_FILE = "manifest.json"

    def __init__(self, index_dir: Path):
        self.dir = Path(index_dir)
        self.index = None
        self.records: list[dict] = []
        self.manifest: dict = {}

    # -- build ---------------------------------------------------------------
    def build(self, records: list[RetrievalRecord], embedder: Embedder,
              cache_dir: Path | None = None) -> BuildStats:
        import faiss

        texts = [r.text for r in records]
        dim = embedder.config.dimension

        cache = None
        vectors = np.zeros((len(texts), dim), dtype="float32")
        missing = list(range(len(texts)))
        if cache_dir is not None:
            cache = EmbeddingCache(Path(cache_dir), embedder.config.fingerprint(), dim)
            vectors, missing = cache.get_many(texts)

        if missing:
            todo = [texts[i] for i in missing]
            fresh = embedder.encode_passages(todo)
            for slot, i in enumerate(missing):
                vectors[i] = fresh[slot]
            if cache is not None:
                cache.put_many(todo, fresh)
                cache.save()

        # Inner product over L2-normalised vectors == cosine similarity, and
        # keeps scores in a readable [-1, 1] instead of raw L2 distances.
        index = faiss.IndexFlatIP(dim)
        if len(records):
            index.add(vectors)
        self.index = index
        self.records = [r.to_dict() for r in records]

        stats = BuildStats(
            total_records=len(records),
            html_records=sum(1 for r in records if r.document_type == "html"),
            pdf_records=sum(1 for r in records if r.document_type == "pdf"),
            embedded_now=len(missing),
            reused_from_cache=len(texts) - len(missing),
            dimension=dim,
            model_name=embedder.config.model_name,
            backend=embedder.config.backend,
        )
        self.manifest = {
            "embedder": embedder.config.to_dict(),
            "embedder_fingerprint": embedder.config.fingerprint(),
            "index_type": "faiss.IndexFlatIP (cosine over L2-normalised vectors)",
            # Metadata (page_type/segment/language) is regenerated on every
            # build; this records WHICH logic produced it, so a stale
            # records.jsonl is detectable instead of silently trusted.
            "classifier_fingerprint": classifier_fingerprint(),
            "metric": "cosine",
            "build": stats.to_dict(),
            "cache": cache.stats() if cache else None,
        }
        return stats

    # -- persistence ---------------------------------------------------------
    def save(self) -> None:
        import faiss
        self.dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.index, str(self.dir / self.INDEX_FILE))
        with (self.dir / self.RECORDS_FILE).open("w", encoding="utf-8") as fh:
            for rec in self.records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        (self.dir / self.MANIFEST_FILE).write_text(
            json.dumps(self.manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, index_dir: Path) -> "VectorIndex":
        import faiss
        self = cls(index_dir)
        idx_path = self.dir / cls.INDEX_FILE
        if not idx_path.exists():
            raise FileNotFoundError(
                f"no index at {idx_path}. Build it first:  python -m retrieval.build")
        self.index = faiss.read_index(str(idx_path))
        self.records = [json.loads(l) for l in
                        (self.dir / cls.RECORDS_FILE).read_text(encoding="utf-8").splitlines()
                        if l.strip()]
        self.manifest = json.loads(
            (self.dir / cls.MANIFEST_FILE).read_text(encoding="utf-8"))
        return self

    # -- search --------------------------------------------------------------
    def search(self, query_vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self.index is None or not len(self.records):
            return []
        k = min(top_k, len(self.records))
        scores, ids = self.index.search(query_vector.reshape(1, -1).astype("float32"), k)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]
