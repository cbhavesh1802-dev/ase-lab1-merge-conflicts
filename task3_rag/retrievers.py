"""R1-R5 retrievers for Task 3.

R1  BM25Retriever            - lexical, over the same chunks as R3
R2  DenseRetriever            - over fixed-size chunks
R3  DenseRetriever            - over recursive chunks (3 chunk-size variants compared separately)
R4  HybridRetriever           - normalized, weighted combination of R1 and R3
R5  CrossEncoderRerank(R4)    - R4 plus cross-encoder re-ranking of its top candidates
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from rank_bm25 import BM25Okapi

from chunking import Chunk
from embedding import Embedder, cosine_sim_matrix

IDENT_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
SPLIT_RE = re.compile(r"[._-]+")


def bm25_tokenize(text: str) -> list[str]:
    """Lowercase, keep dotted/underscored identifiers whole (`spool.buffer_bytes`, `PQ-E4663`)
    AND add their sub-parts as extra tokens, so both an exact-identifier query and a natural-language
    paraphrase of it can match."""
    text = text.lower()
    tokens = IDENT_RE.findall(text)
    extra = []
    for tok in tokens:
        if any(c in tok for c in "._-"):
            extra.extend(p for p in SPLIT_RE.split(tok) if p)
    return tokens + extra


@dataclass
class Scored:
    chunk: Chunk
    score: float


class BM25Retriever:
    name = "bm25"

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self._bm25 = BM25Okapi([bm25_tokenize(c.text) for c in chunks])

    def scores(self, query: str) -> np.ndarray:
        return np.asarray(self._bm25.get_scores(bm25_tokenize(query)), dtype=np.float32)

    def search(self, query: str, k: int) -> list[Scored]:
        scores = self.scores(query)
        top = np.argsort(-scores)[:k]
        return [Scored(self.chunks[i], float(scores[i])) for i in top]


class DenseRetriever:
    name = "dense"

    def __init__(self, chunks: list[Chunk], embed_fn: Embedder, batch_size: int = 64) -> None:
        self.chunks = chunks
        self.embed_fn = embed_fn
        vectors = []
        for i in range(0, len(chunks), batch_size):
            vectors.append(embed_fn([c.text for c in chunks[i : i + batch_size]]))
        matrix = np.concatenate(vectors, axis=0)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.matrix = matrix / norms

    def scores(self, query: str) -> np.ndarray:
        qvec = self.embed_fn([query])[0]
        return cosine_sim_matrix(qvec, self.matrix)  # self.matrix rows are already unit-normalized

    def search(self, query: str, k: int) -> list[Scored]:
        scores = self.scores(query)
        top = np.argsort(-scores)[:k]
        return [Scored(self.chunks[i], float(scores[i])) for i in top]


def _min_max(values: np.ndarray) -> np.ndarray:
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < 1e-9:
        return np.zeros_like(values)
    return (values - lo) / (hi - lo)


class HybridRetriever:
    """Normalized, weighted combination of a BM25Retriever and a DenseRetriever over (possibly)
    different chunk sets. Scores are combined per unique chunk_id after each side's own top-N0 is
    min-max normalized (BM25 scores and cosine similarities have incompatible ranges and cannot be
    combined raw)."""

    name = "hybrid"

    def __init__(self, bm25: BM25Retriever, dense: DenseRetriever, alpha: float = 0.5, pool: int = 50) -> None:
        self.bm25 = bm25
        self.dense = dense
        self.alpha = alpha  # weight on the dense side
        self.pool = pool

    def search(self, query: str, k: int) -> list[Scored]:
        bm25_scores = self.bm25.scores(query)
        dense_scores = self.dense.scores(query)

        bm25_norm = _min_max(bm25_scores)
        dense_norm = _min_max(dense_scores)

        by_id: dict[str, tuple[Chunk, float]] = {}
        bm25_top = np.argsort(-bm25_scores)[: self.pool]
        dense_top = np.argsort(-dense_scores)[: self.pool]
        for i in bm25_top:
            chunk = self.bm25.chunks[i]
            by_id[chunk.chunk_id] = (chunk, (1 - self.alpha) * float(bm25_norm[i]))
        for i in dense_top:
            chunk = self.dense.chunks[i]
            prev = by_id.get(chunk.chunk_id, (chunk, 0.0))[1]
            by_id[chunk.chunk_id] = (chunk, prev + self.alpha * float(dense_norm[i]))

        ranked = sorted(by_id.values(), key=lambda cs: -cs[1])[:k]
        return [Scored(chunk, score) for chunk, score in ranked]


class CrossEncoderRerank:
    """R5 = R4 plus cross-encoder re-ranking of the hybrid retriever's top `pool` candidates.

    Falls back to leaving the hybrid order unchanged (with a printed warning, once) if the
    cross-encoder model cannot be loaded - e.g. no internet to download the weights, which is the
    case in the sandbox this was developed in. On a normal machine with internet this just works;
    re-run there for the real R5 numbers.
    """

    name = "cross_encoder_rerank"
    _warned = False

    def __init__(self, base: HybridRetriever, pool: int = 20, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.base = base
        self.pool = pool
        self._model = None
        try:
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(model_name)
            self.model_name = model_name
        except Exception as exc:
            if not CrossEncoderRerank._warned:
                import sys

                print(f"[retrievers] cross-encoder unavailable ({exc}); R5 falls back to R4 order.", file=sys.stderr)
                CrossEncoderRerank._warned = True
            self.model_name = None

    def search(self, query: str, k: int) -> list[Scored]:
        candidates = self.base.search(query, k=self.pool)
        if self._model is None or not candidates:
            return candidates[:k]
        pairs = [(query, c.chunk.text) for c in candidates]
        rerank_scores = self._model.predict(pairs)
        order = np.argsort(-np.asarray(rerank_scores))[:k]
        return [Scored(candidates[i].chunk, float(rerank_scores[i])) for i in order]


# ---------------------------------------------------------------- utilities

def to_doc_ids(scored: list[Scored]) -> list[str]:
    """Chunks -> doc ids in rank order, as evaluate.py expects (duplicates are fine, it dedupes)."""
    return [s.chunk.doc_id for s in scored]
