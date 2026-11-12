# Task 3 – failure analysis & recommendation

**Status: retrieval-side analysis below is from a real run in this environment, but with the
no-network hashed bag-of-words embedder (`embedding.py`'s last-resort fallback) standing in for
dense embeddings — see `results/run_all_summary.json`'s `embedding_backend` field. Re-run
`uv run python task3_rag/run_all.py` on a machine with the gateway or internet access (for
sentence-transformers / the cross-encoder) before submitting, and re-run `analyze_failures.py` -
the specific numbers will change, but the two failure *mechanisms* identified below are structural
properties of this corpus and should still show up.**

## Overall picture (this environment's numbers, k=5, 120 answerable dev questions)

| Config | recall | precision | notes |
|---|---:|---:|---|
| R1 bm25 | 0.583 | 0.579 | strongest here - see why below |
| R2 fixed+dense | 0.371 | 0.371 | |
| R3 recursive+dense (120/200/320 tok) | 0.392 / 0.392 / 0.392 | 0.385 / 0.378 / 0.382 | chunk size barely moved recall with this embedder (see "What chunk size changed") |
| R4 hybrid (bm25+R3, alpha=0.3) | 0.517 | 0.512 | *below* R1 alone here - see why below |
| R5 hybrid+rerank | 0.517 | 0.512 | cross-encoder unavailable offline, falls back to R4's order unchanged |

Regenerate this table from `results/run_all_summary.json` after a real run; the shape worth
explaining either way is **why BM25 wins on `identifier`/`version` and loses on `paraphrase`**, which
is visible in the per-category breakdown printed by `run_all.py` / stored in `results/metrics_*.json`.

## Five concrete failures, with retrieval-vs-generation diagnosis

All five below have `context_recall = 0` against the best-performing config we could evaluate here
(R1 BM25) - the correct document never appears in the top-5 at all, which makes every one of them a
**retrieval failure by construction**: with no relevant chunk in the context, a generator has nothing
to be faithful to (true generation failures - the right context retrieved but the model still gets it
wrong, or gets it right but is marked unfaithful by the judge - can only be identified from a
`judge.py` run with the gateway; see `results/README.md` for exactly which command regenerates this
analysis for generation failures once you have that data).

**1. `dev085` (version): "In which release did the default of `spool.max_connections` change, and
what was it before?"** - gold: *In 3.0; before it was 128.* The correct document, `d0406`
(PaderQueue 3.0 release notes), literally contains the line `spool.max_connections now defaults to
1024 (previously 128)` - we verified this by grepping the corpus directly. BM25 still doesn't
retrieve it: two *dedicated* reference pages exist for this one setting, `d0088`
("spool.max_connections", 4.x) and `d0585` ("spool.max_connections", 2.x) - short documents whose
entire title and most of their text is that one identifier, so its term frequency there dwarfs its
frequency inside the 22-bullet "Changed defaults" list buried in the much longer changelog chunk.
BM25 ranks both dedicated pages (score ≈32 and ≈32) far above the changelog chunk that actually
answers the question (did not even make the top 10; a reference-page doc ranked higher purely
because of where else the same token appears). **Diagnosis: retrieval failure** - specifically, a
*document-type mismatch*: the query needs the changelog, BM25's term-frequency signal prefers the
per-setting reference page instead, because both legitimately contain the exact identifier and BM25
has no notion of "this page answers a change-over-time question" vs. "this page answers a
current-value question." The same pattern explains `dev088` and `dev100` (also `version`, also
needing a changelog doc that lost to a more topically-concentrated reference page).

**2. `dev002` (paraphrase): "Our logs say that the part that throws away expired messages has too
many waiting jobs and refuses more. What is the name of this error?"** - gold: `PQ-E1944
(RETENTION_WORK_QUEUE_FULL)`, in `d0356`. Here there is no lexical overlap at all between the
question and the document: the question describes the *behavior* ("throws away expired messages",
"too many waiting jobs") while the document uses the *vocabulary* ("retention", "work queue full").
**Diagnosis: retrieval failure**, and a different one from `dev085` - a pure vocabulary gap. This is
exactly the case semantic/dense retrieval exists for; our dense configs (R2/R3) use the no-network
hashed bag-of-words fallback here, which is still lexical under the hood (hashed token overlap, not
meaning), so it doesn't fix this either in this environment. We'd expect gateway or
sentence-transformers embeddings to do meaningfully better on exactly this question and this
category - that comparison is the main thing worth re-running for real before you finalize your
recommendation.

**3. `dev069` (multi-hop): "What is the current default value of the setting that the error
catalogue recommends changing for PQ-E4020?"** - gold: `spool.compression` defaults to `lz4` in
4.x, needing `d0506` (the PQ-E4020 error page, which names `spool.compression`) *and then* `d0679`
(the current config page for `spool.compression`) - two sequential lookups where the second query
term doesn't exist until the first document has been read. **Diagnosis: retrieval failure**, and
structurally so for any single-pass top-k retriever: one query embedding/BM25 call cannot "chain"
like this. A real fix is architectural, not a better embedding model - e.g. an agentic retrieval loop
that re-queries after reading the first hit (this is exactly what Task 2's `search_style_guide`-in-a-
loop pattern does, just not available to a one-shot `evaluate.py`-style retriever).

## What chunk size changed (R3, 120/200/320 tokens)

In this run, recall was identical (0.392) across all three sizes - with the hashed bag-of-words
embedder, chunk content barely matters because the "embedding" is just a token-overlap histogram, so
splitting a document differently mostly redistributes the same tokens rather than changing what gets
matched. **This is expected to change with real embeddings**: a semantic embedding model is sensitive
to a chunk's coherence, so (a) very small chunks (120 tok) should start losing context needed for
`multi-hop`/`paraphrase` questions, and (b) very large chunks (320 tok) should start diluting a
specific answer among unrelated neighboring content (visible above even here, structurally, in why
the changelog's 22-bullet chunk loses to a single-topic reference page). Re-run and report the real
per-size numbers; if they come out flat again, that itself is a finding worth stating plainly rather
than a reason to tune further.

## Why R4 (hybrid) underperforms R1 (BM25 alone) here, and what that implies about `alpha`

The grid search (`run_all.py`, `pick_alpha`) picked `alpha=0.3` (weight on the dense side) as best
among `{0.3, 0.4, 0.5, 0.6, 0.7}` - but even at the lowest tested alpha, hybrid (0.517) still trails
pure BM25 (0.583). That's the hashed embedder actively **hurting** the ranking: for `identifier`
questions BM25 alone gets recall 1.0, hybrid drops to 0.8, because the hybrid score blends in a weak,
noisy dense signal that outranks some of BM25's correct top picks. **This is a real, reportable
result** (not a bug): with a low-quality dense signal, hybrid search can lose to its lexical half
alone, and the "right" alpha is a function of how good your embeddings actually are - a finding worth
stating in the video even if your real-embeddings run tells a different (expected: hybrid beating
BM25 alone, especially on `paraphrase`) story. Re-run the alpha grid search with real embeddings and
report both numbers; the gap between them is itself evidence for "why hybrid search needs good
embeddings to pay off."

## Recommendation

**Preliminary, pending a real-embeddings re-run**: for this corpus, BM25-weighted hybrid (R4, low
alpha) looks like the right default - `identifier`/`version` questions depend on exact-match
precision BM25 is naturally good at, and even a decent dense signal should mostly help the
`paraphrase` category without hurting the others, provided alpha is tuned (not fixed at 0.5) as this
script already does automatically. **Concretely, before finalizing**: (1) re-run `run_all.py` with
real embeddings and confirm whether R4/R5 now beat R1 overall and specifically on `paraphrase`; (2) if
you have time, try chunking the changelog documents one bullet per chunk instead of one big section -
this directly targets failure #1 above and should be checkable in an afternoon; (3) multi-hop
(failure #3) will not be fixed by a better embedding model or chunking strategy alone - say so
explicitly rather than claiming a fix you haven't implemented.
