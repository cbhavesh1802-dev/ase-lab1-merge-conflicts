"""Generates task3_rag/predictions_test.jsonl for the 100 held-out test questions, using your best
retrieval config, in the exact submission format:
    {"id": "test001", "answer": "...", "retrieved_doc_ids": ["d0001", ..., "d0005"]}

NEEDS the gateway (real LLM calls for the answers) - run this yourself, after run_all.py has told you
which configuration to use (see results/run_all_summary.json and results/FAILURE_ANALYSIS.md).

Usage (from the repository root):
    uv run python task3_rag/predict_test.py --config r4_hybrid
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from chunking import load_corpus  # noqa: E402
from common.llm_client import LLMClient  # noqa: E402
from configs import build_retriever  # noqa: E402
from embedding import get_embedder  # noqa: E402
from evaluate import load_jsonl  # noqa: E402
from generate_answers import GENERATION_PROMPT  # noqa: E402
from judge import ABSTENTION  # noqa: E402
from retrievers import to_doc_ids  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--questions", type=Path, default=HERE / "data" / "questions_test.jsonl")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--model")
    parser.add_argument("--out", type=Path, default=HERE / "predictions_test.jsonl")
    parser.add_argument("--local-embeddings", action="store_true")
    args = parser.parse_args()

    questions = load_jsonl(args.questions)
    docs = load_corpus()
    embed_fn, embed_model_name = get_embedder(prefer_gateway=not args.local_embeddings)
    print(f"config={args.config}  embedding backend={embed_model_name}  n_questions={len(questions)}")
    retriever = build_retriever(args.config, docs, embed_fn)

    llm = LLMClient(model=args.model, log_path=HERE / "logs" / f"predict_test_{args.config}.jsonl")

    with args.out.open("w", encoding="utf-8") as f:
        for i, q in enumerate(questions, start=1):
            scored = retriever.search(q["question"], args.k)
            contexts = [s.chunk.text for s in scored]
            prompt = GENERATION_PROMPT.format(
                abstention=ABSTENTION, context="\n\n---\n\n".join(contexts), question=q["question"]
            )
            answer = llm.chat([{"role": "user", "content": prompt}]).strip()
            row = {"id": q["id"], "answer": answer, "retrieved_doc_ids": to_doc_ids(scored)}
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{i}/{len(questions)}] {q['id']}: {answer[:100]}")

    print(llm.usage_summary())
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
