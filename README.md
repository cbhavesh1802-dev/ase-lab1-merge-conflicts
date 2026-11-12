# Lab 1 – Agentic Software Engineering (WS 2026/27) — Group [XX]

**Group members:**
- Bhavesh Mukesh Chaudhari, matriculation number [...]
- Pruthvi Vishwakarma, matriculation number [...]
- Mahmud Iftekhar Asef, matriculation number [...]
- Abu Sayem, matriculation number [...]
- Ayush Kushwaha, matriculation number [...]

See `CONTRIBUTIONS.md` for who did what.

## Status of this submission

This repository was prepared with substantial AI assistance (see each task's own notes and your
video's AI-usage statement). Two things do **not** come from this assistance and still need to be
done by the group before submitting:

1. **Task 1** needs to be built by actually talking to OpenCode (see `task1_opencode/` — content
   templates and a prompt plan are ready, the site itself has to come from a real OpenCode session,
   by design: "you write no code yourselves").
2. **Every step that calls the UPB AI Gateway** (Task 2's real agent run, Task 3's real-embedding
   retrieval numbers + end-to-end judge scoring + test predictions, Task 4's real-model run) needs to
   be run on the university network/VPN with your own `.env` key — this was prepared in an
   environment with no access to that gateway. Each task's README says exactly which command to run.

## How to run everything

```bash
# 0. Setup (once) - see the root README.md below this one... this IS the root README; see
#    "Access to the UPB AI Gateway" and "Setup" further down for the gateway key + uv sync steps.

# Task 1: see task1_opencode/README.md (setup) and the prompt plan you were given separately.
#   Deliverable: task1_opencode/site/index.html, built entirely through OpenCode.

# Task 2:
uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html

# Task 3:
uv run python task3_rag/run_all.py                      # retrieval eval, all configs
uv run python task3_rag/generate_answers.py --config <best> --sample 50 --out task3_rag/results/answers_<best>.jsonl
uv run python task3_rag/judge.py task3_rag/results/answers_<best>.jsonl --sample 50 --out task3_rag/results/judge_<best>.jsonl
uv run python task3_rag/predict_test.py --config <best>  # -> task3_rag/predictions_test.jsonl

# Task 4:
uv run pytest task4_tools/tests -v
uv run python task4_tools/reset_workspace.py && uv run python task4_tools/agent.py --mock
uv run python task4_tools/reset_workspace.py && uv run python task4_tools/agent.py
git -C task4_tools/workspace diff > task4_tools/final.diff
```

Each `task*/README.md` has the full design write-up required by its own grading rubric; this file is
just the entry point.

---

## Access to the UPB AI Gateway (do this first)

Every group member needs their own API key. Official guide: https://hilfe.uni-paderborn.de/AI-Gateway

1. **Activate the service** (once): log in to the Service Portal https://sp.upb.de/ with your university account →
   *Benutzerverwaltung* → *Weitere Dienste* → tab *Lehre und Studium* → *KI-Chat-Oberfläche* → *Optionen* → *Beantragen*.
   The request is approved automatically.
2. **Be in the university network:** the gateway only works from the university network (e.g. eduroam on campus)
   or via the university VPN. From home, connect the VPN first: https://hilfe.uni-paderborn.de/VPN_einrichten
3. **Create a key:** open https://ai-gateway.upb.de, log in with your university account, click *API-Key erstellen*
   and copy the key (`sk-...`). Store it safely; you can't see it again.
4. Keys **expire** after some time. If calls suddenly fail with `401`, create a new key and update your `.env`.

Never commit the key, never paste it into a prompt, never put it into a file you submit.

## Setup

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # once, if you don't have uv
uv sync                                            # or: python -m venv .venv && pip install -r requirements.txt
uv sync --extra local                              # optional: local embedding/cross-encoder models
cp .env.example .env                               # then put your key into .env
uv run python common/hello_gateway.py              # check, in the university network or VPN
```

## Rules

- Never commit or submit your `.env` / API keys.
- No agent/RAG frameworks (LangChain, LlamaIndex, ...) in Tasks 2-4.
- Every group member must be able to explain every part of the submission.
