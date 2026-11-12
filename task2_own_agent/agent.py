"""Task 2 - agent loop with a self-designed JSON action protocol (no native tool calling).

The model answers with exactly one JSON object per step:
    {"thought": "short reasoning", "action": "<action name>", "args": {}}

Actions: search_style_guide, read_profiles, write_site, finish (see ACTIONS below).

Usage (from the repository root):
    uv run python task2_own_agent/agent.py --content task1_opencode/content --out task2_own_agent/site/index.html
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from common.embed_images import embed_images  # noqa: E402
from common.llm_client import LLMClient, count_tokens  # noqa: E402
from common.site_checker import check_site, member_names_from_content  # noqa: E402
from rag import StyleGuideRAG, format_observation  # noqa: E402

ACTIONS = {"search_style_guide", "read_profiles", "write_site", "finish"}
MAX_STYLE_GUIDE_TOKENS = 1000
HTML_BLOCK_RE = re.compile(r"```html\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

SYSTEM_PROMPT = """You are building a single-file team website: task2_own_agent/site/index.html
(HTML + CSS + JavaScript, nothing loaded from the internet).

You cannot see the "UPB Agentic SE Web Style Guide" directly - you only know what you retrieve with
search_style_guide. The guide has deprecated documents; a deprecated document names the id that
replaces it. Never follow a rule from a document whose Status is DEPRECATED - look up its
replacement instead.

You must reply with EXACTLY ONE JSON object per turn, and nothing else outside it except, for
write_site, a fenced ```html code block directly after the JSON:

    {"thought": "short reasoning", "action": "<one of: search_style_guide, read_profiles, write_site, finish>", "args": {...}}

Actions:
  search_style_guide  args: {"query": "..."}            -> retrieved style guide chunks (<=1000 tokens)
  read_profiles       args: {}                           -> team member profiles + available image files
  write_site           args: {} (html goes in a ```html block right after the JSON, or args.html)
                        -> saves the file and runs the full checker; returns the report
  finish               args: {"summary": "..."}           -> accepted only if the last checker report passed

Rules:
  - Never write base64 image data yourself. Reference photos with placeholders like
    <img src="{{image:anna.jpg}}" alt="..."> - the surrounding program embeds the real file.
  - Call read_profiles before you write the site, and search_style_guide for each area of the page
    you are unsure about (colors, typography, header, member cards, footer, theme toggle, etc.)
    before relying on a rule you have not actually retrieved.
  - After write_site, read the checker report. If anything failed, fix the HTML and call write_site
    again. Only call finish once the report says every check passed.
  - The site also needs (independent of the style guide): a day/night toggle, mobile + desktop
    layout, every member introduced with their photo embedded, and a section with the team's shared
    view on AI and the future of software engineering. Make it funny and creative.
"""


@dataclass
class StepResult:
    observation: str
    report_passed: bool | None = None  # set by write_site/finish


@dataclass
class AgentState:
    content_dir: Path
    images_dir: Path
    out_path: Path
    rag: StyleGuideRAG
    k: int
    member_names: list[str] = field(default_factory=list)
    last_report_passed: bool = False
    last_report_summary: str = ""


# ------------------------------------------------------------ JSON parsing

def extract_json_object(text: str) -> tuple[dict | None, int]:
    """Find the first syntactically valid top-level JSON object in `text`.

    Returns (object, end_index) so callers can look for a fenced code block after it.
    Returns (None, -1) if no valid JSON object is found anywhere in the text.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                continue
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        obj = json.loads(candidate)
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj, i + 1
                    break
        start = text.find("{", start + 1)
    return None, -1


def extract_html(text: str, after: int, args: dict) -> str | None:
    if isinstance(args.get("html"), str) and args["html"].strip():
        return args["html"]
    match = HTML_BLOCK_RE.search(text, pos=max(after, 0))
    if not match:
        match = HTML_BLOCK_RE.search(text)
    return match.group(1) if match else None


# ------------------------------------------------------------------ actions

def do_search_style_guide(state: AgentState, args: dict) -> StepResult:
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return StepResult(observation='error: search_style_guide needs {"query": "..."} with a non-empty string')
    chunks = state.rag.search(query, k=state.k)
    text = format_observation(chunks, max_tokens=MAX_STYLE_GUIDE_TOKENS)
    assert count_tokens(text) <= MAX_STYLE_GUIDE_TOKENS + 50, "observation exceeded the token budget"
    return StepResult(observation=text)


def do_read_profiles(state: AgentState, args: dict) -> StepResult:
    profiles = []
    for path in sorted(state.content_dir.glob("*.md")):
        if path.name.startswith("_"):
            continue
        profiles.append(f"=== {path.stem} ===\n{path.read_text(encoding='utf-8')}")
    images = sorted(p.name for p in state.images_dir.glob("*")) if state.images_dir.is_dir() else []
    state.member_names = member_names_from_content(state.content_dir)
    observation = "\n\n".join(profiles) + f"\n\nAvailable images: {images}"
    return StepResult(observation=observation)


def do_write_site(state: AgentState, args: dict, raw_text: str, json_end: int) -> StepResult:
    html = extract_html(raw_text, json_end, args)
    if html is None:
        return StepResult(
            observation="error: write_site needs the HTML in a ```html code block right after the JSON, "
            'or as args.html'
        )
    try:
        resolved = embed_images(html, state.images_dir)
    except (FileNotFoundError, ValueError) as exc:
        return StepResult(observation=f"error embedding images: {exc}")

    state.out_path.parent.mkdir(parents=True, exist_ok=True)
    state.out_path.write_text(resolved, encoding="utf-8")

    if not state.member_names:
        state.member_names = member_names_from_content(state.content_dir)
    report = check_site(
        resolved, mode="full", member_names=state.member_names, file_size=len(resolved.encode("utf-8"))
    )
    state.last_report_passed = report.passed
    state.last_report_summary = report.summary()
    return StepResult(observation=f"checker report:\n{report.summary()}", report_passed=report.passed)


def do_finish(state: AgentState, args: dict) -> tuple[StepResult, bool]:
    summary = args.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return StepResult(observation='error: finish needs {"summary": "..."}'), False
    if not state.last_report_passed:
        return (
            StepResult(
                observation="finish rejected: the last checker report did not pass yet.\n" + state.last_report_summary
            ),
            False,
        )
    return StepResult(observation=f"finish accepted: {summary}"), True


# --------------------------------------------------------------------- loop

def run_agent(
    llm,
    state: AgentState,
    max_steps: int,
    interactive: bool = True,
    feedback_fn=None,
) -> None:
    """feedback_fn(prompt: str) -> str, defaults to input(); used to make this testable."""
    feedback_fn = feedback_fn or input
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Build the team website now, following the protocol exactly."},
    ]

    step = 0
    while step < max_steps:
        step += 1
        reply = llm.chat(messages, max_tokens=8000)
        messages.append({"role": "assistant", "content": reply})

        obj, end = extract_json_object(reply)
        if obj is None:
            observation = 'error: no valid JSON object found in your reply. Reply with {"thought": ..., "action": ..., "args": {...}}.'
            print(f"[step {step}] (invalid JSON)")
        elif "action" not in obj or obj["action"] not in ACTIONS:
            observation = f"error: unknown or missing action. Valid actions: {sorted(ACTIONS)}"
            print(f"[step {step}] invalid action: {obj.get('action')!r}")
        else:
            action = obj["action"]
            args = obj.get("args") if isinstance(obj.get("args"), dict) else {}
            print(f"[step {step}] {action} {json.dumps(args)[:120]}")

            if action == "search_style_guide":
                result = do_search_style_guide(state, args)
            elif action == "read_profiles":
                result = do_read_profiles(state, args)
            elif action == "write_site":
                result = do_write_site(state, args, reply, end)
            else:  # finish
                result, accepted = do_finish(state, args)
                if accepted:
                    print(f"[step {step}] finish accepted.")
                    llm.log_event({"step": step, "event": "finish_accepted", "summary": args.get("summary")})
                    messages.append({"role": "user", "content": result.observation})
                    if _human_in_the_loop(llm, messages, feedback_fn, interactive):
                        continue  # feedback given: keep going, consuming the remaining step budget
                    print(llm.usage_summary())
                    return
            observation = result.observation

        llm.log_event({"step": step, "event": "observation", "text": observation[:4000]})
        messages.append({"role": "user", "content": observation})

    else:
        print(f"Stopped: reached the {max_steps}-step budget without an accepted finish.")
        llm.log_event({"event": "stopped", "reason": "max_steps"})
        print(llm.usage_summary())
        return


def _human_in_the_loop(llm, messages: list[dict], feedback_fn, interactive: bool) -> bool:
    """Returns True if non-empty feedback was given (the caller should keep looping)."""
    if not interactive:
        return False
    try:
        feedback = feedback_fn("Feedback for the agent (empty to stop): ").strip()
    except EOFError:
        feedback = ""
    if not feedback:
        print("No feedback given, stopping.")
        return False
    llm.log_event({"event": "human_feedback", "text": feedback})
    messages.append({"role": "user", "content": feedback})
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--content", type=Path, default=ROOT / "task1_opencode" / "content")
    parser.add_argument("--images", type=Path, default=None, help="default: <content>/images")
    parser.add_argument("--out", type=Path, default=HERE / "site" / "index.html")
    parser.add_argument("--model", help="override the chat model (default: $LAB_CHAT_MODEL)")
    parser.add_argument("--max-steps", type=int, default=25)
    parser.add_argument("--k", type=int, default=4)
    parser.add_argument("--local-embeddings", action="store_true", help="force the local embedding model")
    parser.add_argument("--run-log", type=Path, default=None, help="default: logs/run_<NN>.jsonl (auto-numbered)")
    parser.add_argument("--non-interactive", action="store_true", help="skip the human-feedback prompt (for CI/testing)")
    args = parser.parse_args()

    images_dir = args.images or (args.content / "images")
    log_dir = HERE / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if args.run_log is None:
        existing = sorted(log_dir.glob("run_*.jsonl"))
        n = len(existing) + 1
        args.run_log = log_dir / f"run_{n:02d}.jsonl"

    print(f"Indexing the style guide ...")
    rag = StyleGuideRAG(prefer_gateway=not args.local_embeddings)
    print(f"  {len(rag.chunks)} chunks, embedding model: {rag.embed_model_name}")

    llm = LLMClient(model=args.model, log_path=args.run_log)
    state = AgentState(content_dir=args.content, images_dir=images_dir, out_path=args.out, rag=rag, k=args.k)

    run_agent(llm, state, max_steps=args.max_steps, interactive=not args.non_interactive)


if __name__ == "__main__":
    main()
