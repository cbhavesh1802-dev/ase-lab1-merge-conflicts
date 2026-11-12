"""Builds and evaluates all retrieval configurations (R1-R5) for Task 3.

    uv run python task3_rag/run_all.py                      # gateway embeddings if configured, else local fallback
    uv run python task3_rag/run_all.py --local-embeddings    # force the local sentence-transformers model

Writes, per configuration: results/retrieval_<name>.jsonl (evaluate.py's input format) and
results/metrics_<name>.json (its output). Writes results/REPORT.md with the overall comparison
table, the per-category breakdown, the R3 chunk-size comparison, the R4 weight justification, and a
cost/latency table (indexing time, mean query latency, mean context tokens per query).

This script only touches retrieval (no LLM calls) - safe to run with zero gateway access. The
end-to-end step (answer generation + judge.py) is in generate_answers.py and does need the gateway.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from chunking import chunk_corpus, load_corpus  # noqa: E402
from tokcount import count_tokens  # noqa: E402
from embedding import get_embedder  # noqa: E402
from evaluate import evaluate, format_table, load_jsonl  # noqa: E402
from retrievers import (  # noqa: E402
    BM25Retriever,
    CrossEncoderRerank,
    DenseRetriever,
    HybridRetriever,
    to_doc_ids,
)

RESULTS = HERE / "results"
CHUNK_SIZE_VARIANTS = [120, 200, 320]  # tokens, same overlap ratio (20%)
DEFAULT_R3_SIZE = 200
K_EVAL = 5
K_RETRIEVE = 10  # retrieve more than k_eval so context-precision@k has something to rank


def write_retrieval(path: Path, questions: list[dict], retriever, k: int) -> dict:
    """Runs `retriever.search(q, k)` for every question, writes the JSONL, and returns timing stats."""
    path.parent.mkdir(parents=True, exist_ok=True)
    latencies = []
    context_tokens = []
    with path.open("w", encoding="utf-8") as f:
        for q in questions:
            start = time.perf_counter()
            scored = retriever.search(q["question"], k)
            latencies.append(time.perf_counter() - start)
            context_tokens.append(sum(count_tokens(s.chunk.text) for s in scored))
            f.write(json.dumps({"id": q["id"], "retrieved_doc_ids": to_doc_ids(scored)}) + "\n")
    return {
        "mean_latency_ms": round(1000 * sum(latencies) / len(latencies), 2),
        "p95_latency_ms": round(1000 * sorted(latencies)[int(0.95 * len(latencies))], 2),
        "mean_context_tokens": round(sum(context_tokens) / len(context_tokens), 1),
    }


def run_config(name: str, retriever, questions: list[dict], index_time_s: float) -> dict:
    out_path = RESULTS / f"retrieval_{name}.jsonl"
    timing = write_retrieval(out_path, questions, retriever, K_RETRIEVE)
    report = evaluate(load_jsonl(out_path), questions, K_EVAL)
    (RESULTS / f"metrics_{name}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(format_table(report["summary"], name))
    print()
    return {"name": name, "summary": report["summary"], "timing": timing, "index_time_s": round(index_time_s, 2)}


def pick_alpha(bm25: BM25Retriever, dense: DenseRetriever, questions: list[dict]) -> tuple[float, dict]:
    """Small grid search over the hybrid weight, selecting the alpha with the best overall
    context_recall@K_EVAL on the dev set. This is the "justify your weight" deliverable."""
    candidates = [0.3, 0.4, 0.5, 0.6, 0.7]
    scored = {}
    for alpha in candidates:
        hybrid = HybridRetriever(bm25, dense, alpha=alpha)
        tmp_path = RESULTS / "_alpha_search.jsonl"
        write_retrieval(tmp_path, questions, hybrid, K_RETRIEVE)
        report = evaluate(load_jsonl(tmp_path), questions, K_EVAL)
        scored[alpha] = report["summary"]["overall"]["context_recall"]
    tmp_path = RESULTS / "_alpha_search.jsonl"
    if tmp_path.exists():
        tmp_path.unlink()
    best_alpha = max(scored, key=scored.get)
    return best_alpha, scored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-embeddings", action="store_true")
    parser.add_argument("--questions", type=Path, default=HERE / "data" / "questions_dev.jsonl")
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    questions = [q for q in load_jsonl(args.questions) if q["relevant_doc_ids"]]  # unanswerable excluded, per evaluate.py
    print(f"{len(questions)} answerable dev questions (unanswerable ones are scored by the judge, not here)\n")

    docs = load_corpus()
    embed_fn, embed_model_name = get_embedder(prefer_gateway=not args.local_embeddings)
    print(f"embedding backend: {embed_model_name}\n")

    configs: list[dict] = []

    # ---- R3 chunk-size comparison (also gives us the default R3 index used by R1/R4/R5) ----
    r3_indexes = {}
    for size in CHUNK_SIZE_VARIANTS:
        t0 = time.perf_counter()
        chunks = chunk_corpus(docs, "recursive", max_tokens=size)
        dense = DenseRetriever(chunks, embed_fn)
        index_time = time.perf_counter() - t0
        r3_indexes[size] = (chunks, dense)
        configs.append(run_config(f"r3_recursive_{size}tok", dense, questions, index_time))

    chunks_r3, dense_r3 = r3_indexes[DEFAULT_R3_SIZE]

    # ---- R1: BM25 on the same chunks as R3 (default size) ----
    t0 = time.perf_counter()
    bm25 = BM25Retriever(chunks_r3)
    configs.append(run_config("r1_bm25", bm25, questions, time.perf_counter() - t0))

    # ---- R2: fixed-size chunking + dense ----
    t0 = time.perf_counter()
    chunks_fixed = chunk_corpus(docs, "fixed", size_tokens=DEFAULT_R3_SIZE, overlap_tokens=int(0.2 * DEFAULT_R3_SIZE))
    dense_r2 = DenseRetriever(chunks_fixed, embed_fn)
    configs.append(run_config("r2_fixed", dense_r2, questions, time.perf_counter() - t0))

    # ---- R4: hybrid R1 + R3(default), weight chosen by grid search on the dev set ----
    print("Grid-searching the hybrid weight (alpha = weight on the dense side) ...")
    best_alpha, alpha_scores = pick_alpha(bm25, dense_r3, questions)
    print(f"alpha search (context_recall@{K_EVAL}): " + ", ".join(f"{a}={s:.3f}" for a, s in alpha_scores.items()))
    print(f"-> chosen alpha = {best_alpha}\n")
    t0 = time.perf_counter()
    hybrid = HybridRetriever(bm25, dense_r3, alpha=best_alpha)
    configs.append(run_config("r4_hybrid", hybrid, questions, time.perf_counter() - t0))

    # ---- R5: R4 + cross-encoder re-ranking ----
    t0 = time.perf_counter()
    reranked = CrossEncoderRerank(hybrid)
    configs.append(run_config("r5_hybrid_rerank", reranked, questions, time.perf_counter() - t0))

    report = {
        "embedding_backend": embed_model_name,
        "hybrid_alpha": best_alpha,
        "hybrid_alpha_search": alpha_scores,
        "cross_encoder_used": reranked.model_name is not None,
        "chunk_size_variants": CHUNK_SIZE_VARIANTS,
        "k_eval": K_EVAL,
        "configs": configs,
    }
    (RESULTS / "run_all_summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {RESULTS / 'run_all_summary.json'} and per-config retrieval_*.jsonl / metrics_*.json")


if __name__ == "__main__":
    main()
