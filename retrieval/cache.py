"""On-disk embedding cache.

What: maps (embedding space, chunk text) -> vector, so a rebuild only embeds
      what actually changed.
Inputs/Outputs: data/retrieval/cache/<fingerprint>.npz
Why: embedding a few thousand chunks on CPU takes minutes. Re-running the build
     after a code change should cost seconds, not minutes, or you stop
     iterating. Keyed by content hash rather than position, so inserting one
     new document does not invalidate the other few thousand vectors.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np


def text_key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


class EmbeddingCache:
    def __init__(self, cache_dir: Path, fingerprint: str, dimension: int):
        self.path = Path(cache_dir) / f"{fingerprint}.npz"
        self.dimension = dimension
        self.keys: list[str] = []
        self.vectors: dict[str, np.ndarray] = {}
        self.hits = 0
        self.misses = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = np.load(self.path, allow_pickle=False)
            keys = [str(k) for k in data["keys"]]
            vecs = data["vectors"]
            if vecs.shape[1] != self.dimension:
                return  # different embedding space; ignore rather than corrupt
            self.vectors = {k: vecs[i] for i, k in enumerate(keys)}
        except (OSError, ValueError, KeyError):
            self.vectors = {}   # unreadable cache is a miss, never a crash

    def get_many(self, texts: list[str]) -> tuple[np.ndarray, list[int]]:
        """Return (matrix with cached rows filled, indices still needing work)."""
        out = np.zeros((len(texts), self.dimension), dtype="float32")
        missing: list[int] = []
        for i, t in enumerate(texts):
            v = self.vectors.get(text_key(t))
            if v is None:
                missing.append(i)
            else:
                out[i] = v
                self.hits += 1
        self.misses += len(missing)
        return out, missing

    def put_many(self, texts: list[str], vectors: np.ndarray) -> None:
        for t, v in zip(texts, vectors):
            self.vectors[text_key(t)] = v.astype("float32")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.vectors:
            return
        keys = list(self.vectors.keys())
        mat = np.stack([self.vectors[k] for k in keys]).astype("float32")
        np.savez_compressed(self.path, keys=np.array(keys), vectors=mat)

    def stats(self) -> dict:
        return {"path": str(self.path), "entries": len(self.vectors),
                "hits": self.hits, "misses": self.misses}
