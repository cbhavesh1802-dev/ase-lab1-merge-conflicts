"""Pulls N concrete failure examples (lowest context_recall) out of a metrics_<config>.json file,
with the question, gold answer and expected doc groups, for the lab's failure-analysis deliverable.
Pure post-processing of run_all.py's output - no gateway needed.

Usage:
    uv run python task3_rag/analyze_failures.py --config r4_hybrid -n 5
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--questions", type=Path, default=HERE / "data" / "questions_dev.jsonl")
    parser.add_argument("-n", type=int, default=5)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    metrics = json.loads((HERE / "results" / f"metrics_{args.config}.json").read_text())
    questions = {q["id"]: q for q in (json.loads(l) for l in args.questions.open(encoding="utf-8"))}

    worst = sorted(metrics["per_question"], key=lambda pq: pq["context_recall"])
    cutoff = worst[min(len(worst), max(args.n * 4, 20)) - 1]["context_recall"]
    pool = [pq for pq in worst if pq["context_recall"] <= cutoff]
    random.Random(args.seed).shuffle(pool)
    sample = pool[: args.n]

    for pq in sample:
        q = questions[pq["id"]]
        print(f"=== {q['id']} ({q['category']}) recall={pq['context_recall']:.2f} precision={pq['context_precision']:.2f} ===")
        print("Q:   ", q["question"])
        print("gold:", q["answer"])
        print("needs:", q["relevant_doc_ids"])
        print()


if __name__ == "__main__":
    main()
