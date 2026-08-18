"""Embedding backends, pluggable so the index build is model-agnostic.

What: a small interface plus two implementations - a real multilingual
      sentence-transformers model, and a dependency-free hashing fallback.
Inputs: lists of strings.
Outputs: L2-normalised float32 vectors.
Why: the model is the one part of this stack that needs a download. Keeping it
     behind an interface means the loader, chunker, index, BM25 and API can all
     be built and tested without it, and the model can be swapped later without
     touching anything else.

MODEL CHOICE (see MODEL_NOTES below): default is intfloat/multilingual-e5-small.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, asdict

import numpy as np

MODEL_NOTES = """
Candidates considered for a bilingual (ar/en) CPU-only corpus:

  intfloat/multilingual-e5-small            <- default
    384 dims, ~118M params. Trained contrastively *for retrieval*, which is
    this task. Requires asymmetric prefixes: queries are embedded as
    "query: ..." and passages as "passage: ...". Omitting the prefixes
    measurably degrades it, so they are applied automatically here.

  sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
    384 dims, ~118M params. Same size and speed class, but trained for
    paraphrase/STS similarity rather than query->document retrieval. Tends to
    favour texts that look like the query rather than texts that answer it.
    Reasonable fallback; select with --model.

Both are small enough to embed a few thousand chunks on CPU in minutes.
Larger multilingual models (e5-base/large, bge-m3) retrieve better but are
several times slower on CPU; worth trying only if quality is the bottleneck.
"""


@dataclass
class EmbedderConfig:
    """Recorded verbatim into the index metadata so a run is reproducible."""
    backend: str
    model_name: str
    dimension: int
    normalise: bool = True
    query_prefix: str = ""
    passage_prefix: str = ""
    max_seq_length: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Identity of this embedding space - cache keys hang off it."""
        return hashlib.sha1(
            f"{self.backend}|{self.model_name}|{self.dimension}|"
            f"{self.query_prefix}|{self.passage_prefix}".encode()).hexdigest()[:12]


class Embedder:
    """Interface: encode passages and queries, possibly differently."""

    config: EmbedderConfig

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        raise NotImplementedError


def _l2_normalise(v: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (v / norms).astype("float32")


# Prefix conventions per model family.
_PREFIXES = {
    "e5": ("query: ", "passage: "),
    "bge": ("Represent this sentence for searching relevant passages: ", ""),
}


class SentenceTransformerEmbedder(Embedder):
    """The real model. Requires `pip install sentence-transformers`."""

    def __init__(self, model_name: str = "intfloat/multilingual-e5-small",
                 batch_size: int = 32, device: str | None = None):
        try:
            from sentence_transformers import SentenceTransformer
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            raise SystemExit(
                f"cannot load sentence-transformers ({exc}).\n"
                "  pip install sentence-transformers\n"
                "  The model weights are downloaded from huggingface.co on first\n"
                "  use, so that host must be reachable once."
            ) from exc

        self.model = SentenceTransformer(model_name, device=device)
        self.batch_size = batch_size
        qp, pp = "", ""
        for family, (q, p) in _PREFIXES.items():
            if family in model_name.lower():
                qp, pp = q, p
                break
        self.config = EmbedderConfig(
            backend="sentence-transformers",
            model_name=model_name,
            dimension=int(self.model.get_sentence_embedding_dimension()),
            query_prefix=qp,
            passage_prefix=pp,
            max_seq_length=getattr(self.model, "max_seq_length", None),
        )

    def _encode(self, texts: list[str], prefix: str) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.config.dimension), dtype="float32")
        vecs = self.model.encode([prefix + t for t in texts],
                                 batch_size=self.batch_size,
                                 convert_to_numpy=True,
                                 show_progress_bar=len(texts) > 200)
        return _l2_normalise(np.asarray(vecs, dtype="float32"))

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.config.passage_prefix)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts, self.config.query_prefix)


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class HashingEmbedder(Embedder):
    """Deterministic hashed bag-of-words vectors. No model download.

    This is NOT a semantic model - it matches on shared tokens, not meaning,
    and will not bridge Arabic to English. It exists so the whole pipeline
    (chunking, cache, FAISS, API, CLI, tests) can be built and verified
    offline and in CI. Use a real model for actual retrieval quality.
    """

    def __init__(self, dimension: int = 384, seed: str = "bm-retrieval-v1"):
        self.seed = seed
        self.config = EmbedderConfig(
            backend="hashing", model_name=f"hashing-{dimension}d", dimension=dimension)

    def _encode(self, texts: list[str]) -> np.ndarray:
        dim = self.config.dimension
        out = np.zeros((len(texts), dim), dtype="float32")
        for row, text in enumerate(texts):
            for tok in _TOKEN_RE.findall(text.lower()):
                h = hashlib.md5(f"{self.seed}:{tok}".encode()).digest()
                idx = int.from_bytes(h[:4], "little") % dim
                sign = 1.0 if h[4] % 2 else -1.0
                out[row, idx] += sign
        return _l2_normalise(out)

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        return self._encode(texts)


def build_embedder(model_name: str, **kw) -> Embedder:
    """`hashing` selects the offline fallback; anything else loads the real model."""
    if model_name == "hashing":
        return HashingEmbedder(**kw)
    return SentenceTransformerEmbedder(model_name, **kw)
