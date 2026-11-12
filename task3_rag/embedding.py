"""Shared embedding backend for Task 3 retrievers.

Same fallback chain as task2_own_agent/rag.py (duplicated here on purpose: each task folder in the
submission is self-contained, per the lab's structure): prefer the gateway embedding model, then a
local sentence-transformers model, then a pure-stdlib hashed bag-of-words embedder so this module
never hard-fails even with zero network (useful for development, and what this review environment
itself had to fall back to - see README for what to re-run with real embeddings).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.llm_client import LLMClient  # noqa: E402

Embedder = Callable[[list[str]], np.ndarray]


def gateway_embedder() -> tuple[Embedder, str] | None:
    try:
        llm = LLMClient()
        probe = llm.embed(["ok"])
        if probe.size == 0:
            raise RuntimeError("empty embedding")
        return llm.embed, f"gateway:{llm.embedding_model}"
    except Exception:
        return None


def local_embedder() -> tuple[Embedder, str] | None:
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception:
        return None

    def embed(texts: list[str]) -> np.ndarray:
        return np.asarray(model.encode(list(texts), show_progress_bar=False, batch_size=64), dtype=np.float32)

    return embed, "local:sentence-transformers/all-MiniLM-L6-v2"


def hashing_embedder(dim: int = 768) -> tuple[Embedder, str]:
    """No-network last-resort fallback. See task2_own_agent/rag.py for the same function, documented."""
    import hashlib

    token_re = re.compile(r"[a-z0-9]+")

    def tokens(text: str) -> list[str]:
        words = token_re.findall(text.lower())
        return words + [f"{a}_{b}" for a, b in zip(words, words[1:])]

    def embed(texts: list[str]) -> np.ndarray:
        vectors = np.zeros((len(texts), dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in tokens(text):
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                vectors[i, h % dim] += 1.0
        return np.log1p(vectors)

    return embed, f"local:hashing-bow-{dim}"


def get_embedder(prefer_gateway: bool = True) -> tuple[Embedder, str]:
    if prefer_gateway:
        result = gateway_embedder()
        if result is not None:
            return result
    result = local_embedder()
    if result is not None:
        return result
    print("[embedding] WARNING: no gateway and no downloadable local model - falling back to a crude "
          "hashed bag-of-words embedder. Dense/hybrid/rerank numbers from this run are a pipeline "
          "sanity check only, not representative - re-run with the gateway or sentence-transformers "
          "for real numbers.", file=sys.stderr)
    return hashing_embedder()


def cosine_sim_matrix(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    qn = query_vec / (np.linalg.norm(query_vec) + 1e-9)
    mn = matrix / (np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9)
    return mn @ qn
