"""Builds a named retriever config (r1_bm25 / r2_fixed / r3_recursive_<N>tok / r4_hybrid /
r5_hybrid_rerank) the same way run_all.py does, so generate_answers.py and predict_test.py can
reuse the exact same retrievers for the end-to-end and test-set steps."""

from __future__ import annotations

import json
from pathlib import Path

from chunking import chunk_corpus
from retrievers import BM25Retriever, CrossEncoderRerank, DenseRetriever, HybridRetriever

DEFAULT_R3_SIZE = 200
RESULTS = Path(__file__).resolve().parent / "results"


def default_alpha() -> float:
    summary_path = RESULTS / "run_all_summary.json"
    if summary_path.exists():
        return json.loads(summary_path.read_text())["hybrid_alpha"]
    return 0.5


def build_retriever(name: str, docs, embed_fn, alpha: float | None = None):
    alpha = default_alpha() if alpha is None else alpha

    if name.startswith("r3_recursive_"):
        size = int(name.removeprefix("r3_recursive_").removesuffix("tok"))
        chunks = chunk_corpus(docs, "recursive", max_tokens=size)
        return DenseRetriever(chunks, embed_fn)
    if name == "r1_bm25":
        chunks = chunk_corpus(docs, "recursive", max_tokens=DEFAULT_R3_SIZE)
        return BM25Retriever(chunks)
    if name == "r2_fixed":
        chunks = chunk_corpus(docs, "fixed", size_tokens=DEFAULT_R3_SIZE, overlap_tokens=int(0.2 * DEFAULT_R3_SIZE))
        return DenseRetriever(chunks, embed_fn)
    if name == "r4_hybrid":
        chunks = chunk_corpus(docs, "recursive", max_tokens=DEFAULT_R3_SIZE)
        return HybridRetriever(BM25Retriever(chunks), DenseRetriever(chunks, embed_fn), alpha=alpha)
    if name == "r5_hybrid_rerank":
        return CrossEncoderRerank(build_retriever("r4_hybrid", docs, embed_fn, alpha))
    raise ValueError(f"unknown config: {name!r}")
