"""Task 3 end-to-end generation: retrieve + generate an answer for each question, in the format
judge.py expects. NEEDS the gateway (real LLM calls) - this is the one Task 3 script that cannot be
run from this review sandbox; run it yourself on the university network/VPN.

Usage (from the repository root):
    uv run python task3_rag/generate_answers.py --config r4_hybrid --sample 50 \
        --out task3_rag/results/answers_r4_hybrid.jsonl
    uv run python task3_rag/judge.py task3_rag/results/answers_r4_hybrid.jsonl --sample 50 \
        --out task3_rag/results/judge_r4_hybrid.jsonl

Run this for your two best configurations (per the lab sheet), then eyeball/compare their
results/judge_*.jsonl summaries and hand-check 10 of the judge's decisions (see results/README.md).
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
from judge import ABSTENTION, stratified_sample  # noqa: E402

GENERATION_PROMPT = """Answer the question using ONLY the context below. If the context does not
contain the answer, reply with EXACTLY this sentence and nothing else:
"{abstention}"

Keep the answer short (one or two sentences), and prefer quoting exact identifiers/values from the
context (error codes, setting names, defaults) over paraphrasing them.

CONTEXT:
{context}

QUESTION:
{question}"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="r1_bm25 | r2_fixed | r3_recursive_<N>tok | r4_hybrid | r5_hybrid_rerank")
    parser.add_argument("--questions", type=Path, default=HERE / "data" / "questions_dev.jsonl")
    parser.add_argument("--sample", type=int, help="judge a stratified sample of N questions (same sample as judge.py)")
    parser.add_argument("--k", type=int, default=5, help="chunks passed to the generator")
    parser.add_argument("--model", help="override the chat model (default: $LAB_CHAT_MODEL)")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--local-embeddings", action="store_true")
    args = parser.parse_args()

    questions = load_jsonl(args.questions)
    if args.sample:
        questions = stratified_sample(questions, args.sample)

    docs = load_corpus()
    embed_fn, embed_model_name = get_embedder(prefer_gateway=not args.local_embeddings)
    print(f"config={args.config}  embedding backend={embed_model_name}")
    retriever = build_retriever(args.config, docs, embed_fn)

    llm = LLMClient(model=args.model, log_path=HERE / "logs" / f"generate_{args.config}.jsonl")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for i, q in enumerate(questions, start=1):
            scored = retriever.search(q["question"], args.k)
            contexts = [s.chunk.text for s in scored]
            prompt = GENERATION_PROMPT.format(
                abstention=ABSTENTION, context="\n\n---\n\n".join(contexts), question=q["question"]
            )
            answer = llm.chat([{"role": "user", "content": prompt}]).strip()
            f.write(json.dumps({"id": q["id"], "answer": answer, "contexts": contexts}, ensure_ascii=False) + "\n")
            print(f"[{i}/{len(questions)}] {q['id']} ({q['category']}): {answer[:100]}")

    print(llm.usage_summary())
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
