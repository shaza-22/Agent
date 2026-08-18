"""Phase 2 retrieval layer over the frozen Phase 1 corpus.

Public API for Phase 3:

    from retrieval import retrieve, Retriever

    hits = retrieve("What documents do I need to open an account?", top_k=5)
    for h in hits:
        print(h["score"], h["citation"], h["section_heading"])

Nothing here writes to data/corpus/ - the Phase 1 corpus is read-only input.
"""
from .retriever import Retriever, retrieve          # noqa: F401
from .records import RetrievalRecord, load_all      # noqa: F401

__all__ = ["Retriever", "retrieve", "RetrievalRecord", "load_all"]
