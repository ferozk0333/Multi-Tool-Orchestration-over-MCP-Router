"""Dense retrieval with a local sentence-transformer. No API key, embeddings cached to disk."""

from __future__ import annotations

import hashlib
import logging

import numpy as np

import config
from retrieval.base import Hit, IndexEntry, rank_hits

log = logging.getLogger(__name__)

_model = None


def _load_model():
    # This function loads the embedding model once per process; the import alone is slow.
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        log.info("loading embedding model %s", config.EMBEDDING_MODEL)
        _model = SentenceTransformer(config.EMBEDDING_MODEL)
    return _model


def _cache_key(texts: list[str]) -> str:
    # This function keys the cache on the model and the exact text that was embedded.
    digest = hashlib.sha256(config.EMBEDDING_MODEL.encode())
    for text in texts:
        digest.update(b"\x00")
        digest.update(text.encode())
    return digest.hexdigest()[:16]


def embed(texts: list[str], use_cache: bool = True) -> np.ndarray:
    # This function returns L2-normalised embeddings, loading from disk when the text is unchanged.
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)

    path = config.CACHE_DIR / f"emb-{_cache_key(texts)}.npy"
    if use_cache and path.exists():
        return np.load(path)

    vectors = _load_model().encode(texts, normalize_embeddings=True,
                                   convert_to_numpy=True, show_progress_bar=False)
    vectors = vectors.astype(np.float32)
    if use_cache:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.save(path, vectors)
    return vectors


class DenseRetriever:
    name = "dense"

    def __init__(self, use_cache: bool = True) -> None:
        self.entries: list[IndexEntry] = []
        self.use_cache = use_cache
        self._matrix: np.ndarray | None = None

    def index(self, entries: list[IndexEntry]) -> None:
        # This method embeds every entry once and keeps the matrix in memory.
        self.entries = entries
        self._matrix = embed([e.text for e in entries], self.use_cache)

    def warm(self) -> None:
        # This method loads the model during startup rather than on the first user query.
        # A cache hit means indexing never touches the model, so without this the ~6s load
        # lands on whoever asks the first question.
        _load_model()

    def search(self, query: str, k: int) -> list[Hit]:
        # This method returns the k nearest entries by cosine similarity.
        # 504 vectors, so exact search is instant and a vector database would be dead weight.
        if self._matrix is None or not len(self._matrix):
            return []
        vector = embed([query], use_cache=False)[0]
        scores = self._matrix @ vector
        return rank_hits([(e.key, float(s)) for e, s in zip(self.entries, scores)], k)
