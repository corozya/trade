"""RAG helpers for TA stack — wraps indicators.RAGManager with optional temp persist."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def make_rag(persist_dir: str | Path | None = None):
    """
    Zwraca RAGManager. Jeśli podano persist_dir — osobna baza (testy/smoke).
    """
    from indicators.RAGManager import RAGManager

    rag = RAGManager()
    if persist_dir is not None and rag.enabled:
        import chromadb
        from chromadb.utils import embedding_functions

        path = str(persist_dir)
        rag.client = chromadb.PersistentClient(path=path)
        rag.ef = embedding_functions.DefaultEmbeddingFunction()
        rag.collection = rag.client.get_or_create_collection(
            name=RAGManager.COLLECTION_NAME,
            embedding_function=rag.ef,
            metadata={"hnsw:space": "cosine"},
        )
    return rag
