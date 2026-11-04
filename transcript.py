"""Incremental Claude Code transcript (JSONL) scanner and review digest builder.

Only user/assistant lines matter. Metadata lines start with '{"type":"' (api-request-shape/-blob
etc. carry tool schemas and would cause false matches) and attachment lines carry '"attachment":{'
near the start; both are skipped before json parsing, which keeps the Stop hook fast."""
from __future__ import annotations

import json

from common import skill_name_from_path

SKILL_LOAD_PREFIX = "Base directory for this skill:"
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _text_blocks(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def _short(value, limit: int) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ⏎ ")
    return s if len(s) <= limit else s[:limit] + f"…[+{len(s) - limit}]"


def scan(path: str, start: int = 0, end: int | None = None, session_id: str | None = None):
    """Parse complete lines in [start, end). Returns (events, offset_after_last_complete_line).

    Event kinds: user, assistant, tool, error, skill_load. Each carries 'off' (byte offset after
    its line) so callers can move cursors to an exact event."""
    events, seen_tools = [], set()
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read() if end is None else fh.read(max(0, end - start))
    offset = start
    for raw in data.splitlines(keepends=True):
        if not raw.endswith(b"\n"):
            break  # partial line still being written
        offset += len(raw)
        if raw.startswith(b'{"type":"') or b'"attachment":{' in raw[:120]:
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                events.extend(_line_events(obj, offset, session_id, seen_tools))
        except (ValueError, AttributeError, TypeError, KeyError):
            continue  # unexpected line shape: skip it, the cursor still advances
    return events, offset


def _line_events(obj: dict, offset: int, session_id: str | None, seen_tools: set) -> list[dict]:
    events = []
    kind = obj.get("type")
    if kind not in ("user", "assistant"):
        return events
    if session_id and obj.get("sessionId") not in (None, session_id):
        return events  # history copied in from another session (fork)
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return events
    content = msg.get("content")
    if kind == "assistant":
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and str(block.get("text") or "").strip():
                events.append({"k": "assistant", "text": str(block["text"]), "off": offset})
            elif btype == "tool_use" and block.get("id") not in seen_tools:
                seen_tools.add(block.get("id"))
                inp = block.get("input") or {}
                ev = {"k": "tool", "id": block.get("id"), "name": str(block.get("name") or "?"),
                      "input": inp, "msg_id": msg.get("id"), "off": offset}
                fp = (inp.get("file_path") or inp.get("notebook_path")) if isinstance(inp, dict) else None
                if isinstance(fp, str) and skill_name_from_path(fp):
                    ev["skill"] = skill_name_from_path(fp)
                    ev["skill_file"] = fp
                events.append(ev)
        return events
    # user line: tool results, skill bodies (isMeta), or a real prompt
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"):
                c = b.get("content")
                text = c if isinstance(c, str) else " ".join(_text_blocks(c if isinstance(c, list) else []))
                events.append({"k": "error", "id": b.get("tool_use_id"), "text": text, "off": offset})
        return events
    texts = _text_blocks(content)
    if obj.get("isMeta"):
        for t in texts:
            if t.startswith(SKILL_LOAD_PREFIX):
                p = t[len(SKILL_LOAD_PREFIX):].split("\n", 1)[0].strip()
                events.append({"k": "skill_load", "path": p, "name": skill_name_from_path(p + "/SKILL.md") or p.rsplit("/", 1)[-1], "off": offset})
        return events
    text = "\n".join(t for t in texts if t.strip())
    if text.strip():
        origin = obj.get("origin")
        human = (origin.get("kind") if isinstance(origin, dict) else None) in (None, "human")
        events.append({"k": "user", "text": text, "human": human, "off": offset})
    return events


def build_digest(events: list[dict], *, session_id: str, cwd: str, prev_summary: str | None, max_chars: int) -> str:
    """Compact, model-readable replay of a transcript slice (Hermes _digest_history analogue)."""
    loaded = sorted({f"{e['name']} ({e['path']})" for e in events if e["k"] == "skill_load"})
    loaded += sorted({f"{e['input'].get('skill')} (Skill tool)" for e in events
                      if e["k"] == "tool" and e["name"] == "Skill" and isinstance(e["input"], dict)})
    viewed = sorted({e["skill_file"] for e in events if e["k"] == "tool" and e.get("skill") and e["name"] == "Read"})
    edited = sorted({e["skill_file"] for e in events if e["k"] == "tool" and e.get("skill") and e["name"] in EDIT_TOOLS})
    tool_names = {e["id"]: e["name"] for e in events if e["k"] == "tool"}
    body = []
    for e in events:
        k = e["k"]
        if k == "user":
            body.append(("keep", f"USER: {_short(e['text'], 2000)}" if e.get("human") else f"USER (automated): {_short(e['text'], 400)}"))
        elif k == "assistant":
            body.append(("drop", f"ASSISTANT: {_short(e['text'], 800)}"))
        elif k == "tool":
            inp = e["input"]
            if isinstance(inp, dict):
                inp = {key: (_short(v, 300) if isinstance(v, str) else v) for key, v in inp.items()
                       if key not in ("content", "new_string", "old_string") or len(str(v)) < 300}
            body.append(("drop", f"TOOL {e['name']}: {_short(inp, 400)}"))
        elif k == "error":
            body.append(("keep", f"ERROR from {tool_names.get(e['id'], 'tool')}: {_short(e['text'], 500)}"))
        elif k == "skill_load":
            body.append(("keep", f"SKILL LOADED: {e['name']} ({e['path']})"))
    head = [
        "[Conversation digest of a Claude Code session slice, prepared for a background review. "
        "Long values are truncated (…[+N] marks the cut).]",
        f"Session: {session_id}   Working directory: {cwd}",
        f"Previous background review of this session: {prev_summary or 'none'}",
        f"Skills loaded in this slice: {', '.join(loaded) or 'none'}",
        f"Skill files read in this slice: {', '.join(viewed) or 'none'}",
        f"Skill files edited by the foreground session in this slice: {', '.join(edited) or 'none'}",
        "--- conversation ---",
    ]
    budget = max_chars - sum(len(h) + 1 for h in head)
    total = sum(len(t) + 1 for _, t in body)
    i, dropped = 0, 0
    while total > budget and i < len(body):  # drop the oldest assistant/tool lines first
        if body[i][0] == "drop":
            total -= len(body[i][1]) + 1
            body[i] = ("gone", "")
            dropped += 1
        i += 1
    lines = [t for tag, t in body if tag != "gone"]
    if dropped:
        lines.insert(0, f"[{dropped} older assistant/tool lines omitted for length]")
    text = "\n".join(head + lines)
    return text[-max_chars:] if len(text) > max_chars else text
