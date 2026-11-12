# Task 3 – results directory

## What's already here (generated in this environment, offline embedding fallback)

- `retrieval_<config>.jsonl` / `metrics_<config>.json` — one pair per configuration (`r1_bm25`,
  `r2_fixed`, `r3_recursive_120tok`/`200tok`/`320tok`, `r4_hybrid`, `r5_hybrid_rerank`), produced by
  `run_all.py`.
- `run_all_summary.json` — which embedding backend was used, the chosen hybrid `alpha` and the grid
  search that picked it, whether the cross-encoder loaded, and a cost/latency table (indexing time,
  mean/p95 query latency, mean context tokens per query) per configuration.
- `FAILURE_ANALYSIS.md` — 5 concrete failure examples with retrieval-vs-generation diagnosis, the
  recommendation, and what to re-check once you have real embeddings.

**Important:** `run_all_summary.json`'s `embedding_backend` field currently reads
`local:hashing-bow-768` - this sandbox has no network access to either the UPB gateway or
huggingface.co (confirmed via the proxy status, both return 403), so `embedding.py` fell all the way
back to its last-resort, no-network bag-of-words embedder. That's enough to prove the whole pipeline
end-to-end correctly (chunking → indexing → BM25/dense/hybrid/rerank → evaluate.py → per-category
table), and the retrieval-side failure analysis it produced is genuinely explainable and real (see
`FAILURE_ANALYSIS.md`) - but it is **not** what you should submit. Re-run on the university
network/VPN (or anywhere with normal internet) before submitting.

## Commands to regenerate everything for real

```bash
# from the repository root

# 1. Retrieval evaluation, all 7 configs, real embeddings (gateway if .env is set, else downloads
#    sentence-transformers locally):
uv run python task3_rag/run_all.py

# 2. Pick your two best configs from the printed/metrics tables, then end-to-end generation + judge
#    for each (needs the gateway for both generation and the judge itself):
uv run python task3_rag/generate_answers.py --config r4_hybrid --sample 50 \
    --out task3_rag/results/answers_r4_hybrid.jsonl
uv run python task3_rag/judge.py task3_rag/results/answers_r4_hybrid.jsonl --sample 50 \
    --out task3_rag/results/judge_r4_hybrid.jsonl
# repeat for your second-best config

# 3. Hand-check 10 of the judge's decisions: open judge_<config>.jsonl, read 10 rows' "claims" /
#    "correctness_reason", and note in your video whether you agree.

# 4. Re-pull failure examples against your real best config:
uv run python task3_rag/analyze_failures.py --config r4_hybrid -n 5
#    update FAILURE_ANALYSIS.md's examples and "why" with the real ones (the two failure
#    *mechanisms* described there - document-type mismatch, vocabulary gap, multi-hop chaining - are
#    structural to this corpus and worth checking whether they still show up, not just replaced).

# 5. Test-set predictions with your best config:
uv run python task3_rag/predict_test.py --config r4_hybrid
#    -> task3_rag/predictions_test.jsonl
```

`run_all.py --local-embeddings` forces the local `sentence-transformers` model instead of the
gateway (useful if your gateway budget is tight — embeddings are free on the gateway too, so there's
rarely a reason to).
