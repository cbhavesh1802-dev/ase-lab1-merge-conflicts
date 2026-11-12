"""Task 2 - RAG over the UPB Agentic SE Web Style Guide.

Loads every document in `style_guide/`, splits it into chunks, embeds them, and exposes
`search(query, k)` returning the most similar chunks with their source document id.

Chunking: recursive-ish, but tuned for this corpus. The style guide documents are already short
(most are a single coherent rule, 8-60 lines) and carry a title + "Status: ACTIVE/DEPRECATED" line
that matters for retrieval (an agent must be able to tell a deprecated rule from its replacement).
So:
  1. Split each document on markdown "## " subheadings (most documents have none -> one section).
  2. Any section still longer than `max_chunk_tokens` is split on blank-line paragraph breaks.
  3. Any paragraph still too long is split on sentence boundaries, with a small overlap so a rule
     split mid-thought still has its neighbour sentence for context.
Every chunk is prefixed with "[<doc_id>] <title> (status)" so the embedding - and the agent reading
the observation - always sees which document and status a chunk came from, even out of context.

Embeddings: the gateway embedding model via `llm.embed(...)` (free) if a gateway key is configured and
reachable, otherwise a local `sentence-transformers` model (`all-MiniLM-L6-v2`) so this module works
without the UPB AI Gateway / university network too (e.g. for development, CI, or this review).
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.llm_client import LLMClient, count_tokens  # noqa: E402

DEFAULT_STYLE_GUIDE_DIR = Path(__file__).resolve().parent / "style_guide"
HEADING_RE = re.compile(r"^##\s+.*$", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
FRONT_MATTER_RE = re.compile(r"^Status:\s*(\S+).*$", re.MULTILINE)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    status: str
    text: str


# ----------------------------------------------------------------- chunking

def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def _split_sentences(text: str, max_tokens: int, overlap_sentences: int = 1) -> list[str]:
    sentences = [s.strip() for s in SENTENCE_RE.split(text) if s.strip()]
    if not sentences:
        return [text]
    chunks, current = [], []
    for sentence in sentences:
        current.append(sentence)
        if count_tokens(" ".join(current)) >= max_tokens:
            chunks.append(" ".join(current))
            current = current[-overlap_sentences:] if overlap_sentences else []
    if current:
        chunks.append(" ".join(current))
    return chunks or [text]


def _split_section(text: str, max_tokens: int) -> list[str]:
    if count_tokens(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    for paragraph in _split_paragraphs(text):
        if count_tokens(paragraph) <= max_tokens:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_sentences(paragraph, max_tokens))
    return pieces or [text]


def chunk_document(doc_id: str, title: str, status: str, body: str, max_chunk_tokens: int = 220) -> list[Chunk]:
    boundaries = [m.start() for m in HEADING_RE.finditer(body)]
    if boundaries:
        starts = [0] + boundaries
        sections = [body[s:e].strip() for s, e in zip(starts, starts[1:] + [len(body)])]
    else:
        sections = [body.strip()]
    sections = [s for s in sections if s]

    chunks: list[Chunk] = []
    for section in sections:
        for i, piece in enumerate(_split_section(section, max_chunk_tokens)):
            prefixed = f"[{doc_id}] {title} ({status})\n{piece}"
            chunks.append(Chunk(chunk_id=f"{doc_id}#{len(chunks)}", doc_id=doc_id, title=title, status=status, text=prefixed))
    return chunks


def load_documents(style_guide_dir: Path) -> list[tuple[str, str, str, str]]:
    """Returns a list of (doc_id, title, status, body) for every SG-*.md document."""
    docs = []
    for path in sorted(style_guide_dir.glob("SG-*.md")):
        text = path.read_text(encoding="utf-8")
        doc_id = path.stem.split("_", 1)[0]  # "SG-13"
        first_line = text.splitlines()[0] if text else path.stem
        title = first_line.lstrip("#").strip() or path.stem
        status_match = FRONT_MATTER_RE.search(text)
        status = status_match.group(1) if status_match else "UNKNOWN"
        docs.append((doc_id, title, status, text))
    return docs


# ---------------------------------------------------------------- embedding

def gateway_embedder() -> tuple[Callable[[list[str]], np.ndarray], str] | None:
    try:
        llm = LLMClient()
        probe = llm.embed(["ok"])
        if probe.size == 0:
            raise RuntimeError("empty embedding")
        return llm.embed, f"gateway:{llm.embedding_model}"
    except Exception:
        return None


def local_embedder() -> tuple[Callable[[list[str]], np.ndarray], str] | None:
    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    except Exception:
        # Not installed, or (as in this review sandbox) no network to download the weights.
        return None

    def embed(texts: list[str]) -> np.ndarray:
        return np.asarray(model.encode(list(texts), show_progress_bar=False), dtype=np.float32)

    return embed, "local:sentence-transformers/all-MiniLM-L6-v2"


def hashing_embedder(dim: int = 512) -> tuple[Callable[[list[str]], np.ndarray], str]:
    """Pure-stdlib, no-network, last-resort bag-of-words embedder (hashed unigrams + bigrams, log-tf).

    This is NOT a design recommendation - it is here only so rag.py keeps working with zero network
    access at all (e.g. this offline review). For the real submission, use the gateway embedding
    model or sentence-transformers (both much stronger semantically); `get_embedder()` prefers those
    automatically whenever either is reachable.
    """
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


def get_embedder(prefer_gateway: bool = True) -> tuple[Callable[[list[str]], np.ndarray], str]:
    if prefer_gateway:
        result = gateway_embedder()
        if result is not None:
            return result
    result = local_embedder()
    if result is not None:
        return result
    return hashing_embedder()


# --------------------------------------------------------------------- RAG

class StyleGuideRAG:
    def __init__(
        self,
        style_guide_dir: Path = DEFAULT_STYLE_GUIDE_DIR,
        max_chunk_tokens: int = 220,
        embed_fn: Callable[[list[str]], np.ndarray] | None = None,
        embed_model_name: str | None = None,
        prefer_gateway: bool = True,
    ) -> None:
        self.style_guide_dir = Path(style_guide_dir)
        if embed_fn is None:
            embed_fn, embed_model_name = get_embedder(prefer_gateway=prefer_gateway)
        self.embed_fn = embed_fn
        self.embed_model_name = embed_model_name or "unknown"

        self.chunks: list[Chunk] = []
        for doc_id, title, status, body in load_documents(self.style_guide_dir):
            self.chunks.extend(chunk_document(doc_id, title, status, body, max_chunk_tokens))
        if not self.chunks:
            raise RuntimeError(f"no SG-*.md documents found in {self.style_guide_dir}")

        vectors = self.embed_fn([c.text for c in self.chunks])
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self.vectors = vectors / norms  # pre-normalize for cosine similarity via dot product

    def search(self, query: str, k: int = 4) -> list[Chunk]:
        qvec = self.embed_fn([query])[0]
        qnorm = np.linalg.norm(qvec)
        if qnorm > 0:
            qvec = qvec / qnorm
        sims = self.vectors @ qvec
        top = np.argsort(-sims)[:k]
        return [self.chunks[i] for i in top]


def format_observation(chunks: list[Chunk], max_tokens: int = 1000) -> str:
    """Render retrieved chunks as text for the agent, never exceeding max_tokens of style-guide text."""
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        piece = f"--- {chunk.chunk_id} ---\n{chunk.text}\n"
        piece_tokens = count_tokens(piece)
        if used + piece_tokens > max_tokens:
            if not parts:
                # Even a single chunk is too big: hard-truncate it rather than return nothing.
                parts.append(piece[: max(200, len(piece) * max_tokens // max(piece_tokens, 1))])
            break
        parts.append(piece)
        used += piece_tokens
    return "\n".join(parts) if parts else "(no results fit the token budget)"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Quick manual check of the style guide RAG.")
    parser.add_argument("query")
    parser.add_argument("-k", type=int, default=4)
    parser.add_argument("--local", action="store_true", help="force the local embedding model")
    args = parser.parse_args()

    rag = StyleGuideRAG(prefer_gateway=not args.local)
    print(f"embedding model: {rag.embed_model_name}, {len(rag.chunks)} chunks\n")
    results = rag.search(args.query, k=args.k)
    print(format_observation(results))
