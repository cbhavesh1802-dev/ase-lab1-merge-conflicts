"""Chunking strategies for Task 3 (R2: fixed-size, R3: recursive).

Every chunk is prefixed with its document's title, type and version, e.g.
    [d0132] ingress.interval_s (PaderQueue 2.x) (config, version 2.x)
This matters a lot for the "version" and "identifier" question categories: a question like "what
was X in PaderQueue 2.x" needs the version to be visible to BOTH the lexical (BM25) and the dense
retriever, not just sitting unused in metadata next to the chunk.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tokcount import count_tokens  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_CORPUS = HERE / "data" / "corpus.jsonl"

HEADING_RE = re.compile(r"^#{1,3}\s+.*$", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass
class Document:
    doc_id: str
    type: str
    title: str
    version: str
    text: str


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    text: str


def load_corpus(path: Path = DEFAULT_CORPUS) -> list[Document]:
    docs = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                d = json.loads(line)
                docs.append(Document(d["doc_id"], d["type"], d["title"], d.get("version", ""), d["text"]))
    return docs


def _prefix(doc: Document) -> str:
    return f"[{doc.doc_id}] {doc.title} ({doc.type}, version {doc.version})"


# --------------------------------------------------------------- R2: fixed size

_tiktoken_encoding = None
_tiktoken_tried = False


def _get_tiktoken_encoding():
    """Cached at module load - avoids re-attempting a (possibly network-blocked) download on every
    document. Returns None if tiktoken / its encoding file is unavailable, in which case
    fixed_size_chunks falls back to splitting on whitespace words instead of exact BPE tokens."""
    global _tiktoken_encoding, _tiktoken_tried
    if not _tiktoken_tried:
        _tiktoken_tried = True
        try:
            import tiktoken

            _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken_encoding = None
    return _tiktoken_encoding


def fixed_size_chunks(doc: Document, size_tokens: int = 200, overlap_tokens: int = 40) -> list[Chunk]:
    prefix = _prefix(doc)
    enc = _get_tiktoken_encoding()
    step = max(1, size_tokens - overlap_tokens)

    if enc is not None:
        units = enc.encode(doc.text)
        render = enc.decode
    else:
        # No network/tiktoken: approximate "tokens" with whitespace words instead of BPE tokens.
        units = doc.text.split(" ")
        render = " ".join

    if not units:
        return [Chunk(f"{doc.doc_id}#0", doc.doc_id, f"{prefix}\n{doc.text}")]

    chunks, start, idx = [], 0, 0
    while start < len(units):
        end = min(start + size_tokens, len(units))
        piece = render(units[start:end])
        chunks.append(Chunk(f"{doc.doc_id}#{idx}", doc.doc_id, f"{prefix}\n{piece}"))
        idx += 1
        if end == len(units):
            break
        start += step
    return chunks


# --------------------------------------------------------------- R3: recursive

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
    pieces = []
    for paragraph in _split_paragraphs(text):
        if count_tokens(paragraph) <= max_tokens:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_sentences(paragraph, max_tokens))
    return pieces or [text]


def recursive_chunks(doc: Document, max_tokens: int = 200) -> list[Chunk]:
    prefix = _prefix(doc)
    body = doc.text
    boundaries = [m.start() for m in HEADING_RE.finditer(body)]
    if boundaries:
        starts = [0] + boundaries
        sections = [body[s:e].strip() for s, e in zip(starts, starts[1:] + [len(body)])]
    else:
        sections = [body.strip()]
    sections = [s for s in sections if s]

    chunks = []
    for section in sections:
        for piece in _split_section(section, max_tokens):
            chunks.append(Chunk(f"{doc.doc_id}#{len(chunks)}", doc.doc_id, f"{prefix}\n{piece}"))
    return chunks or [Chunk(f"{doc.doc_id}#0", doc.doc_id, f"{prefix}\n{body}")]


def chunk_corpus(docs: list[Document], strategy: str, **kwargs) -> list[Chunk]:
    fn = fixed_size_chunks if strategy == "fixed" else recursive_chunks
    chunks = []
    for doc in docs:
        chunks.extend(fn(doc, **kwargs))
    return chunks
