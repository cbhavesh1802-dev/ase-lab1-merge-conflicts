# Task 3 – RAG under the microscope

## How to run

```bash
# from the repository root
uv run python task3_rag/run_all.py                 # retrieval only, all 7 configs - no gateway needed for this part
uv run python task3_rag/run_all.py --local-embeddings   # force local sentence-transformers instead of the gateway
```

This writes `results/retrieval_<config>.jsonl`, `results/metrics_<config>.json` and
`results/run_all_summary.json` (embedding backend used, chosen hybrid weight + the grid search that
picked it, cost/latency table). See `results/README.md` for the full regeneration workflow,
including the end-to-end (`generate_answers.py` + `judge.py`) and test-prediction
(`predict_test.py`) steps, both of which need the gateway and are not run in this environment (see
below). `analyze_failures.py` pulls concrete low-recall examples out of a metrics file for the
failure-analysis write-up (`results/FAILURE_ANALYSIS.md`).

**This environment has no network access to the UPB gateway or to huggingface.co** (confirmed via
the proxy status endpoint — both return 403), so every number currently in `results/` was produced
with `embedding.py`'s last-resort, no-network hashed bag-of-words embedder, and the cross-encoder in
R5 fell back to leaving R4's order unchanged. The whole pipeline (chunking, all five retrievers,
`evaluate.py` integration, the per-category table, the hybrid weight search, cost/latency
measurement) is real and verified end to end — only the embedding/rerank *quality* is a stand-in.
Re-run `run_all.py` on the university network/VPN (or anywhere with normal internet) before
submitting; see `results/README.md` for the exact commands and `results/FAILURE_ANALYSIS.md` for
what's expected to change.

## Design decisions

**Chunking.** Every chunk (`chunking.py`) is prefixed with `[doc_id] title (type, version N)` before
embedding/indexing — not just carried as separate metadata. This corpus has a `version` question
category and plenty of config options with a page per major version, so the version has to be visible
to the retriever itself, not bolted on afterward.
- R2 (fixed-size): exact token-window slicing via `tiktoken` (cl100k_base), configurable
  `size_tokens`/`overlap_tokens` (default 200/40, i.e. 20% overlap). Falls back to whitespace-word
  windows if tiktoken's encoding file can't be downloaded (this sandbox; see `chunking.py`'s
  `_get_tiktoken_encoding`), with a one-time cached lookup rather than retrying per document — the
  very first version of this script tried to download the encoding file on every one of ~900
  documents and effectively hung on repeated proxy failures.
- R3 (recursive): split on markdown headings first, then blank-line paragraphs if a section is still
  over budget, then sentences (with a 1-sentence overlap) as the last resort — same structure as
  Task 2's style-guide chunker, generalized to a configurable `max_tokens` for the 3-way size
  comparison (120 / 200 / 320 tokens tested).

**BM25 tokenization (`retrievers.bm25_tokenize`).** Lowercases and keeps dotted/underscored
identifiers whole (`spool.buffer_bytes`, `PQ-E4663`) via a regex that treats `.`, `_`, `-` as
identifier characters, **and** adds each identifier's sub-parts as extra tokens
(`spool.buffer_bytes` → `spool`, `buffer`, `bytes` in addition to the whole identifier). This lets an
exact-identifier query and a natural-language paraphrase of the same setting both get some lexical
signal, without losing BM25's strength on exact matches (the whole-identifier token still gets its
own, much rarer, IDF weight).

**R4 hybrid weight.** `run_all.py` grid-searches `alpha` (the weight on the dense side) over
`{0.3, 0.4, 0.5, 0.6, 0.7}` and picks whichever maximizes overall `context_recall@5` on the dev set,
rather than a fixed 0.5 — the right balance depends on how much signal the dense side is actually
contributing, which varies enormously with embedding quality (see `FAILURE_ANALYSIS.md` for a
concrete case where a weak dense signal made the hybrid *worse* than BM25 alone, even at low alpha).
Both score vectors are min-max normalized to [0,1] per query before combining — raw BM25 scores and
raw cosine similarities are not on comparable scales.

**R5 choice: cross-encoder re-ranking** (`cross-encoder/ms-marco-MiniLM-L-6-v2`), reranking R4's top
20 candidates. We picked this over query rewriting/HyDE (needs an LLM call per query — extra gateway
cost/latency on top of the generator's own call) and semantic chunking (a chunking-time choice, not a
retrieval-time one — would have meant reindexing rather than reusing R4 as specified) because it's the
most direct way to improve precision on top of an already-reasonable candidate set, at the cost of one
extra model inference per query. `CrossEncoderRerank` fails soft: if the model can't be loaded (no
internet, as in this sandbox), it logs one warning and returns R4's own order unchanged rather than
crashing or raising — `results/run_all_summary.json`'s `cross_encoder_used` field says which happened
for a given run.

## Cost and latency (this environment's run — see the caveat above; re-measure for real numbers)

| config | index time (s) | mean query latency (ms) | p95 latency (ms) | mean context tokens (k=10) | recall@5 | precision@5 |
|---|---:|---:|---:|---:|---:|---:|
| r1_bm25 | 0.07 | 5.18 | 8.48 | 898.7 | 0.583 | 0.579 |
| r2_fixed | 0.33 | 1.29 | 1.40 | 1661.6 | 0.371 | 0.371 |
| r3_recursive_120tok | 0.47 | 2.70 | 2.94 | 860.5 | 0.392 | 0.385 |
| r3_recursive_200tok | 0.22 | 2.55 | 3.06 | 921.8 | 0.392 | 0.378 |
| r3_recursive_320tok | 0.16 | 2.30 | 2.74 | 988.4 | 0.392 | 0.382 |
| r4_hybrid (alpha=0.3) | ~0 (reuses R1+R3 indexes) | 6.48 | 8.62 | 938.8 | 0.517 | 0.512 |
| r5_hybrid_rerank | 0.36 (model load) | 9.95 | 16.06 | 938.8 | 0.517 | 0.512 |

These are in-process CPU timings with no network round-trip (local/hashed embeddings, no gateway
call) — real gateway latency will be dominated by the HTTP round trip per embedding/rerank call, not
by this table's numbers; re-measure once running against the gateway and report actual network
latency and (from `llm.usage_summary()` in `generate_answers.py`'s output) token cost per query for
your two end-to-end configs. One structural result that *will* hold regardless of embedder: R2
(fixed-size chunking) uses noticeably more context tokens per query (1661 vs. ~860-990 for the others)
for the same k=10 — fixed 200-token windows are consistently large, while recursive chunking produces
many short, single-paragraph chunks, so retrieving 10 of them costs less generator context (and
therefore less $ and latency downstream) for comparable or worse recall.

## Known limitations

- `embedding.py` and `tokcount.py` both have no-network fallbacks (hashed bag-of-words embeddings;
  whitespace-word "tokens" instead of BPE) purely so this module keeps working in a sandbox with zero
  internet access. Neither is a design recommendation — see the design decisions above for what the
  real submission should use, and re-run before submitting (results/README.md has the commands).
- `HybridRetriever`'s pooling (`pool=50` candidates from each side before combining) means a
  relevant chunk ranked below 50th by *both* BM25 and dense similarity can never surface in the
  hybrid result, even if their combined score would otherwise be competitive. Not an issue at this
  corpus size/k in practice (checked by comparing hybrid's recall against each side's individually),
  but worth knowing for a larger corpus.
- The `alpha` grid search optimizes overall `context_recall@5` on the *same* dev questions used for
  the final reported numbers — we are not tuning on the held-out test set, but we are tuning and
  reporting on the same dev split, which the lab sheet explicitly asks to flag ("don't tune on the
  questions you report results on without saying so"). A cleaner setup would split dev further into a
  tuning slice and a reporting slice; we didn't, given the dev set is already only 120 answerable
  questions (30/category).
- `CrossEncoderRerank` reranks a fixed pool of 20 hybrid candidates; it cannot recover a relevant
  chunk that hybrid search itself ranked below 20th.
