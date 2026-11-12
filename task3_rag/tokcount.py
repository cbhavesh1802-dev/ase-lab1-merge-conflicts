"""Token counting for Task 3, cached so a hot loop (chunking ~930 documents, then scoring thousands
of question/chunk pairs for the latency table) doesn't re-create the tiktoken encoding on every call.

Same approximation as common/llm_client.count_tokens (cl100k_base, falls back to len(text)//4 if
tiktoken/its encoding file is unavailable) - just memoized at module load instead of per call.
"""

from __future__ import annotations

_encoding = None
_tried = False


def count_tokens(text: str) -> int:
    global _encoding, _tried
    if not _tried:
        _tried = True
        try:
            import tiktoken

            _encoding = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _encoding = None
    if _encoding is None:
        return max(1, len(text) // 4)
    return len(_encoding.encode(text))
