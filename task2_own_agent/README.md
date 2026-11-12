# Task 2 – Build your own (small) coding agent

A Python program that builds `site/index.html` the same way Task 1 does, but on its own: it
retrieves the fictional style guide instead of knowing it, and drives itself through a JSON action
protocol we designed (no native tool calling, no agent framework).

## How to run

```bash
# from the repository root
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
```

Useful options: `--model` (default `$LAB_CHAT_MODEL`), `--max-steps` (default 25), `--k` (chunks per
retrieval, default 4), `--local-embeddings` (force the local `sentence-transformers` model instead of
the gateway embedding model), `--run-log` (default: auto-numbered `logs/run_NN.jsonl`),
`--non-interactive` (skip the human-feedback prompt, for CI/testing).

`rag.py` can also be run standalone to sanity-check retrieval before spending a model call on it:

```bash
uv run python task2_own_agent/rag.py "night mode background color" -k 4
```

## Design decisions

**Chunking (`rag.py`).** The 55 style guide documents are already short and each one is close to a
single coherent rule, so the chunker is recursive but rarely needs to split: a document is split on
`## ` subheadings first (most documents have none → one section), a section over 220 tokens is split
on blank-line paragraphs, and a paragraph still over budget is split on sentence boundaries with a
1-sentence overlap. Every chunk is prefixed with `[<doc_id>] <title> (<status>)` - this is the part
that matters most for this corpus: several rules exist in an ACTIVE and one or more DEPRECATED
versions (e.g. `SG-10` vs `SG-11` for the day palette), and the deprecated document's text always
names its replacement. Putting the id/title/status into the embedded text (not just as metadata)
means a retrieved chunk is self-describing even out of context, so the agent (and we, reading the
logs) can tell a stale rule from the current one without a second lookup.

**Embedding model.** `rag.py` prefers the gateway embedding model (`llm.embed`, via `get_embedder()`)
and falls back automatically, in order, to a local `sentence-transformers/all-MiniLM-L6-v2` model,
and finally to a pure-stdlib hashed bag-of-words embedder if neither the gateway nor the internet is
reachable at all. Only the first two are intended for the real submission; the hashing fallback
exists purely so the module keeps working with zero network access (useful for development/CI, and
is what this review environment actually used, since it cannot reach the university gateway or
download model weights). Report in the video which one you actually used for your submitted run
(`rag.embed_model_name`, also printed by `agent.py` at start-up).

**k.** 4 chunks per `search_style_guide` call, capped at 1,000 tokens total (`count_tokens` from
`common/llm_client.py`, enforced in `rag.format_observation`). With chunks this short, 4 chunks is
usually several whole documents - enough to cover one page area (e.g. "member card" pulls in the
card markup, the hot-take badge, and the photo rules together) without burning the whole budget on
one query, since a single page typically needs 5-8 different `search_style_guide` calls (colors,
fonts, header, cards, footer, toggle mechanism, breakpoints, sections).

**Prompt design.** The system prompt gives the exact JSON protocol, the four actions, and - this
matters more than it sounds - an explicit instruction to never follow a rule from a DEPRECATED
document and to look up its replacement instead, since nothing else would stop a model from quoting
whichever version it retrieves first. It also explicitly tells the model where the `write_site` HTML
goes (a fenced ```html block right after the JSON) and to never write base64 image data, only
`{{image:<file>}}` placeholders.

**Invalid output handling (`agent.py`).** `extract_json_object` scans the reply for every `{` and
tries to parse a balanced, string-aware brace span starting there, returning the first one that is
valid JSON - robust to the model wrapping its JSON in prose or a markdown fence. If nothing parses,
or the object has no recognized `action`, the loop sends back an error observation and continues
(counted as a step, matching the spec); it never raises or crashes the run. `write_site` with no
usable HTML (no code block, no `args.html`) is likewise an error observation, not a crash.
`extract_html` is tested against both supported variants in the self-test described below.

**Human in the loop.** After an accepted `finish`, `_human_in_the_loop` prompts on the terminal;
non-empty input becomes a new user message and the loop continues (`feedback_fn` is injectable, so
this is unit-testable without a real terminal - see Known limitations). Empty input (including EOF,
e.g. when stdin is not a terminal) ends the run.

## Known limitations

- `rag.py`'s chunker only splits on `## ` subheadings and paragraph/sentence boundaries; it has no
  special handling for markdown lists or tables, which is fine for this corpus (short rule lists)
  but would need more care on a corpus with large tables.
- The step budget (`--max-steps`, default 25) counts *every* step, including ones spent on invalid
  model output - a model that repeatedly produces broken JSON burns its own budget rather than
  hanging forever, but it also means a genuinely struggling model gets less "real" work done.
- `write_site` always re-runs the full style-guide checker (`check_site(mode="full")`), so every
  iteration costs the same regardless of how small the fix was; there is no incremental/partial
  check.
- No retry/backoff around a single `llm.chat()` call beyond what `LLMClient` already does
  internally; a model that times out mid-run stops the whole agent run rather than resuming.
- `extract_html`'s regex grabs the *last* ```html block in the reply if several are present with the
  search starting from the JSON's end position failing to match (falls back to searching the whole
  text) - in practice the model only ever emits one block per step.
