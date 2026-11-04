#!/usr/bin/env python3
"""Claude Code hook entrypoint: `hook.py <stop|session-end|skill-used|prompt-expansion|session-start>`.

Never blocks the user: always exits 0, swallows errors into logs/hooks.log, and detaches anything
slow (reviews, curator runs) into its own session."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from common import (ENTRY_DELIMITER, LOCKS, LOGS, MEMORIES, MEMORY_FILES, NOTICES, ROOT, RUN_DIR,  # noqa: E402
                    SESSIONS, config, is_paused, iso, load_json, locked, log, log_exc, memory_mcp_server,
                    memory_mcp_tool, now, read_entries, save_json, state_update)

FIRST_SIGHT_MAX_BYTES = 8 * 1024 * 1024  # don't replay an enormous backlog on first sight
MEMORY_TITLES = {"memory": "MEMORY (your personal notes)", "user": "USER PROFILE (who the user is)"}


def log_payload_once(event: str, payload: dict) -> None:
    """Keep the first raw payload per event so the key names can be checked against reality."""
    path = LOGS / "hook-payloads" / f"{event}.json"
    if not path.exists():
        save_json(path, payload)


def detach(args: list[str], pass_fd: int | None = None) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k != "CURATOR_CHILD"}
    if pass_fd is not None:
        env["CURATOR_LOCK_FD"] = str(pass_fd)
    with open(LOGS / "runner.log", "a") as out:
        subprocess.Popen([sys.executable, "-I", *args], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         start_new_session=True, cwd=RUN_DIR, env=env,
                         pass_fds=(pass_fd,) if pass_fd is not None else ())


# --- review trigger (Hermes: _iters_since_skill >= creation_nudge_interval) ---

def spawn_review(st: dict, payload: dict, scope: str, start: int, final: bool) -> bool:
    cfg = config()["review"]
    day = now().strftime("%Y-%m-%d")
    trigger = "session-end" if final else "stop"
    args = [str(ROOT / "review.py"), "--transcript", payload["transcript_path"], "--session", payload["session_id"],
            "--cwd", payload.get("cwd", ""), "--start", str(start), "--end", str(st["cursor"]),
            "--scope", scope, "--trigger", trigger]
    with state_update() as state:
        today = int(state.get("reviews_by_day", {}).get(day, 0))
        if today >= int(cfg["daily_cap"]):
            log("hooks", f"review skipped: daily cap {cfg['daily_cap']} reached")
            return False
        fd = os.open(LOCKS / "review.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            if not final:
                log("hooks", "review skipped: another review is running (slice kept for the next trigger)")
                return False
            # An ended session has no next trigger: queue a reviewer that waits for the lock.
            fd = None
            args.append("--wait-lock")
        state["reviews_by_day"] = {day: today + 1}  # only today's count matters
    try:
        detach(args, pass_fd=fd)
    finally:
        if fd is not None:
            os.close(fd)  # the child keeps its inherited copy of the lock
    log("hooks", f"review {'queued' if fd is None else 'started'}: {payload['session_id'][:8]} scope={scope} "
                 f"trigger={trigger} slice={start}-{st['cursor']} iters={st['iters']} turns={st['turns']}")
    return True


def advance(payload: dict, final: bool) -> None:
    from transcript import EDIT_TOOLS, scan
    import usage

    sid, path = payload.get("session_id"), payload.get("transcript_path")
    if not sid or not path or not os.path.exists(path):
        return
    cfg = config()["review"]
    builtin = cfg["memory_backend"] == "builtin"
    # Foreground memory writes reset the memory nudge; the builtin store has no foreground tool.
    memory_write_tools = set() if builtin else {memory_mcp_tool(cfg, "retain")}
    with locked(LOCKS / "sessions.lock"):
        sf = SESSIONS / f"{sid}.json"
        st = load_json(sf, {})
        if "review_from" in st:  # older single-cursor state
            st["skills_from"] = st["memory_from"] = st.pop("review_from")
        size = os.path.getsize(path)
        if "cursor" not in st or st.get("transcript") != path or size < st["cursor"]:
            start = max(0, size - FIRST_SIGHT_MAX_BYTES) if "cursor" not in st else 0  # replaced file: rescan it
            st.update(transcript=path, cursor=start, skills_from=start, memory_from=start, iters=0, turns=0,
                      last_msg_id=None)
        events, st["cursor"] = scan(path, st["cursor"], session_id=sid)
        views, patches = [], []
        for e in events:
            if e["k"] == "user" and e.get("human"):
                st["turns"] += 1
            elif e["k"] == "tool":
                if e["msg_id"] != st["last_msg_id"]:  # one tool-loop iteration per assistant message
                    st["iters"] += 1
                    st["last_msg_id"] = e["msg_id"]
                if e.get("skill") and e["name"] in EDIT_TOOLS:
                    # Foreground already curated a skill: Hermes resets the counter (no duplicate review).
                    patches.append(e["skill"])
                    st["iters"], st["skills_from"] = 0, e["off"]
                elif e.get("skill") and e["name"] == "Read" and e.get("skill_file", "").endswith("SKILL.md"):
                    views.append(e["skill"])
                elif e["name"] in memory_write_tools:
                    st["turns"], st["memory_from"] = 0, e["off"]
        if views or patches:
            with usage.update() as data:
                for name, kind in [(v, "view") for v in views] + [(p, "patch") for p in patches]:
                    rec = usage.record(data, name, payload.get("cwd"))
                    rec[f"{kind}_count"] = int(rec[f"{kind}_count"] or 0) + 1
                    rec["last_viewed_at" if kind == "view" else "last_patched_at"] = iso()
        if cfg["enabled"] and not is_paused():
            need_iters = cfg["session_end_min_iterations"] if final else cfg["creation_nudge_interval"]
            need_turns = cfg["session_end_min_iterations"] if final else cfg["memory_nudge_interval"]
            skills = 0 < need_iters <= st["iters"]
            memory = cfg["memory"] and 0 < need_turns <= st["turns"] and (builtin or memory_mcp_server(cfg) is not None)
            # Each scope reviews from its own cursor, so a memory-only review doesn't eat the tool
            # work a later skill review needs (and vice versa).
            skills = skills and st["cursor"] > st["skills_from"]
            memory = memory and st["cursor"] > st["memory_from"]
            scope = "combined" if skills and memory else "skills" if skills else "memory" if memory else None
            start = min(st["skills_from"] if skills else st["cursor"], st["memory_from"] if memory else st["cursor"])
            if scope and spawn_review(st, payload, scope, start, final):
                if skills:
                    st["iters"], st["skills_from"] = 0, st["cursor"]
                if memory:
                    st["turns"], st["memory_from"] = 0, st["cursor"]
        st["updated_at"] = iso()
        save_json(sf, st)


# --- telemetry ---

def on_skill_used(payload: dict) -> None:
    import usage
    if payload.get("tool_name") != "Skill":
        return
    inp = payload.get("tool_input") or {}
    name = inp.get("skill") or inp.get("name") or inp.get("command")
    usage.bump(str(name).lstrip("/"), "use", payload.get("cwd"))


def on_prompt_expansion(payload: dict) -> None:
    import usage
    if payload.get("expansion_type", "slash_command") != "slash_command":
        return
    name = payload.get("command_name") or ""
    if name:
        usage.bump(name.lstrip("/"), "use", payload.get("cwd"))


# --- session start: curator poll + summary notice ---

def on_session_start(payload: dict) -> None:
    import curator_run
    cfg = config()
    if cfg["enabled"] and not is_paused() and curator_run.should_run_now():
        idle = curator_run.idle_hours(payload.get("session_id"))
        # Hermes' CLI treats startup as fully idle; with several concurrent sessions the idle gate
        # could postpone the pass forever, so it is dropped once the run is a full interval overdue.
        overdue = curator_run.hours_since_last_run() >= 2 * float(cfg["interval_hours"])
        if (idle >= float(cfg["min_idle_hours"]) or overdue) and curator_run.claim_run():
            detach([str(ROOT / "curator"), "run", "--scheduled"])
            log("hooks", f"curator run started (idle {idle:.1f}h, overdue={overdue})")
    context = "\n\n".join(p for p in (memory_snapshot(cfg["review"]), notice_text()) if p)
    if context:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


def memory_snapshot(cfg: dict) -> str:
    """Frozen MEMORY.md/USER.md blocks for the builtin backend, as Hermes MemoryStore._render_block."""
    if cfg["memory_backend"] != "builtin":
        return ""
    blocks, sep = [], "═" * 46
    for target, name in MEMORY_FILES.items():
        entries = list(dict.fromkeys(read_entries(MEMORIES / name)))  # Hermes dedupes on load
        if not entries:
            continue
        content, limit = ENTRY_DELIMITER.join(entries), int(cfg[f"{target}_char_limit"])
        pct = min(100, int(len(content) / limit * 100)) if limit > 0 else 0
        blocks.append(f"{sep}\n{MEMORY_TITLES[target]} [{pct}% — {len(content):,}/{limit:,} chars]\n{sep}\n{content}")
    return "\n\n".join(blocks)


def notice_text() -> str:
    lines = []
    with state_update() as state:
        shown, last = state.get("last_run_summary_shown_at"), state.get("last_run_at")
        if state.get("last_run_summary") and (not shown or (last and last > shown)):
            lines.append("curator: " + state["last_run_summary"].splitlines()[0][:300])
            state["last_run_summary_shown_at"] = iso()
    with locked(LOCKS / "notices.lock"):
        if NOTICES.exists():
            for raw in NOTICES.read_text().splitlines():
                try:
                    lines.append(json.loads(raw)["text"][:300])
                except (ValueError, KeyError):
                    continue
            NOTICES.unlink()
    if lines:
        out = lines if len(lines) <= 4 else lines[:3] + [f"... and {len(lines) - 3} more (`curator log`)"]
        return "\n".join(["[skill curator, since your last session; mention it to the user only if relevant]", *out])
    return ""


HANDLERS = {"stop": lambda p: advance(p, final=False), "session-end": lambda p: advance(p, final=True),
            "skill-used": on_skill_used, "prompt-expansion": on_prompt_expansion,
            "session-start": on_session_start}


def main() -> None:
    if os.environ.get("CURATOR_CHILD") or len(sys.argv) < 2 or sys.argv[1] not in HANDLERS:
        return
    try:
        payload = json.load(sys.stdin)
        log_payload_once(sys.argv[1], payload)
        HANDLERS[sys.argv[1]](payload)
    except Exception:
        log_exc("hooks")


if __name__ == "__main__":
    main()
    sys.exit(0)
