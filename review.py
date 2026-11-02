#!/usr/bin/env python3
"""Background skill/memory review of one transcript slice (port of Hermes background_review).

Started detached by the Stop/SessionEnd hook (which already holds the review lock and passes it
via CURATOR_LOCK_FD), or in the foreground by `curator review <transcript>`."""
from __future__ import annotations

import argparse
import fcntl
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import usage  # noqa: E402
from childrun import (apply_stage, enforce_calls, manifest, permission_args, prepare_stage,  # noqa: E402
                      run_claude, snapshot_protected, write_mcp_config, write_report)
from common import (LOCKS, LOGS, PROMPTS, RUN_DIR, SESSIONS, SKILLS, commit_all, commit_paths,  # noqa: E402
                    config, ensure_git, hindsight_server, iso, load_json, locked, log, log_exc,
                    main_model, notice, now, read_frontmatter, repo_lock, save_json, skill_entries)
from transcript import build_digest, scan  # noqa: E402

SCHEMA = {
    "type": "object",
    "required": ["summary", "changes", "memory", "proposals", "overlaps"],
    "properties": {
        "summary": {"type": "string"},
        "changes": {"type": "array", "items": {"type": "object", "required": ["skill", "action", "description"], "properties": {
            "skill": {"type": "string"}, "action": {"type": "string", "enum": ["patch", "create", "write_file"]},
            "path": {"type": "string"}, "description": {"type": "string"}}}},
        "memory": {"type": "array", "items": {"type": "object", "required": ["tag", "content"], "properties": {
            "tag": {"type": "string", "enum": ["user", "environment"]}, "content": {"type": "string"}}}},
        "proposals": {"type": "array", "items": {"type": "object", "required": ["skill", "suggestion"], "properties": {
            "skill": {"type": "string"}, "path": {"type": "string"}, "suggestion": {"type": "string"}}}},
        "overlaps": {"type": "array", "items": {"type": "string"}},
    },
}
PROMPT_BY_SCOPE = {"combined": "review-combined.md", "skills": "review-skills.md", "memory": "review-memory.md"}


def scope_text(stage) -> str:
    rows = skill_entries()
    writable = [e["name"] for e in rows if e["kind"] == "writable"]
    readonly = [f"{e['name']} ({e['kind']})" for e in rows if e["kind"] != "writable"]
    return (f"- Writable skills (patch freely, in the working copy {stage}/skills/<name>/): "
            f"{', '.join(writable) or 'none'}\n"
            f"- New skills: any new directory name under {stage}/skills/\n"
            f"- Read-only skills (proposals only): {', '.join(readonly)}, plus plugin (`plugin:name`) and "
            f"built-in skills")


def ensure_agent_frontmatter(skill_md) -> bool:
    """Add metadata.created_by/created_at to a new skill's frontmatter if the reviewer left it out."""
    fm = read_frontmatter(skill_md)
    if fm.get("metadata.created_by") == "agent" or "metadata" in fm or not fm:
        return False
    text = skill_md.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    if end < 0:  # unterminated frontmatter: leave the file alone
        return False
    skill_md.write_text(text[:end] + f"\nmetadata:\n  created_by: agent\n  created_at: {iso()}" + text[end:], encoding="utf-8")
    return True


def _run_child(rid: str, digest: str, prompt: str, dry_run: bool, memory_on: bool,
               report: dict) -> tuple[dict, list[str], dict]:
    """Stage the writable skills, run the reviewer on the stage, then apply and commit its changes."""
    entries = skill_entries()
    existing = {e["name"] for e in entries}
    writable = sorted(e["name"] for e in entries if e["kind"] == "writable")
    if not dry_run:
        ensure_git()
        commit_all("curator: pre-review snapshot of manual edits")
    stage, before = prepare_stage(rid, writable)
    system = (PROMPTS / "review-system.md").read_text(encoding="utf-8")
    system = system.replace("{{SCOPE}}", scope_text(stage)).replace("{{STAGE}}", str(stage / "skills")).replace("{{NOW}}", iso())
    system_file = RUN_DIR / f"system-{rid}.md"
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    system_file.write_text(system, encoding="utf-8")
    snapshot = None if dry_run else snapshot_protected(RUN_DIR / f"protected-{rid}.tar")
    mcp = write_mcp_config() if memory_on else None
    cfg = config()["review"]
    try:
        meta = run_claude(user_message=f"{digest}\n\n{prompt}", system_file=system_file, schema=SCHEMA,
                          model=cfg["model"] or main_model(), budget=float(cfg["max_budget_usd"]),
                          timeout=int(cfg["timeout_seconds"]), mcp_config=mcp, stage=stage,
                          perm_args=permission_args(stage, writable, dry_run=dry_run, memory=memory_on))
        alerts, applied = [], {}
        report["created"] = []
        if not dry_run:
            alerts = enforce_calls(meta, snapshot, stage)
            for name in sorted({rel.split("/", 1)[0] for rel in manifest(stage / "skills")} - existing):
                md = stage / "skills" / name / "SKILL.md"
                try:
                    if md.exists() and ensure_agent_frontmatter(md):
                        report.setdefault("notes", []).append(f"added created_by metadata to {name}")
                except (OSError, UnicodeError) as e:
                    report.setdefault("notes", []).append(f"could not add metadata to {name}: {e}")
            applied, conflicts = apply_stage(stage, before, set(writable), existing, LOGS / "reviews" / f"{rid}.conflicts")
            alerts += conflicts
    finally:
        for p in (mcp, snapshot, system_file):
            if p:
                p.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
    meta.pop("_raw_calls", None)
    structured = meta.get("structured") or {}
    summary = (structured.get("summary") or meta.get("error") or "no result").strip()
    report.update(model=meta["model"], cost_usd=meta["cost_usd"], num_turns=meta["num_turns"],
                  error=meta["error"], summary=summary, structured=structured, alerts=alerts,
                  permission_denials=meta["permission_denials"], tool_calls=meta["tool_calls"],
                  child_init=meta["init"], child_hook_events=meta["hook_events"],
                  cmd=[a if not a.startswith('{"type"') else "<schema>" for a in meta["cmd"]])
    for a in alerts:
        log("alerts", f"{rid} {a}")
    if not dry_run:
        report["changed"] = {n: [str(p.relative_to(SKILLS)) for p in paths] for n, paths in applied.items()}
        for name in applied:
            if name in existing:
                usage.bump(name, "patch")
            else:
                usage.record_created(name)  # provenance comes from the reviewer's own create
                report["created"].append(name)
        paths = [p for ps in applied.values() for p in ps]
        report["commit"] = commit_paths(paths, f"curator(review): {report['session_id'][:8]} {summary[:72]}\n\n"
                                               f"report: {LOGS / 'reviews' / (rid + '.json')}")
    return meta, alerts, applied


def review(transcript: str, session_id: str, cwd: str, start: int, end: int | None, scope: str,
           trigger: str, dry_run: bool) -> dict:
    cfg = config()["review"]
    started, t0 = now(), time.monotonic()
    rid = f"{started.strftime('%Y%m%d-%H%M%S')}-{session_id[:8]}{'-dry' if dry_run else ''}"
    (LOGS / "reviews").mkdir(parents=True, exist_ok=True)
    base, n = rid, 1
    while (LOGS / "reviews" / f"{rid}.json").exists():  # two reviews of one session in the same second
        n += 1
        rid = f"{base}-{n}"
    report_path = LOGS / "reviews" / f"{rid}.json"
    report = {"id": rid, "started_at": iso(started), "trigger": trigger, "dry_run": dry_run,
              "session_id": session_id, "transcript": transcript, "slice": [start, end], "scope": scope}

    events, end_off = scan(transcript, start, end, session_id=session_id)
    if not events:  # manual review of a copied transcript whose lines carry another session id
        events, end_off = scan(transcript, start, end)
    report["slice"][1] = end_off
    report["events"] = {k: sum(1 for e in events if e["k"] == k) for k in ("user", "assistant", "tool", "error", "skill_load")}
    if not any(e["k"] in ("user", "tool") for e in events):
        report["skipped"] = "empty slice"
        write_report(report_path, report)
        return report

    if scope in ("memory", "combined") and not (cfg["memory"] and hindsight_server()):
        report["memory_skipped"] = "hindsight MCP server not configured" if cfg["memory"] else "review.memory=false"
        scope = "skills"
        report["scope"] = scope
    memory_on = scope in ("memory", "combined")
    sess = load_json(SESSIONS / f"{session_id}.json", {})
    digest = build_digest(events, session_id=session_id, cwd=cwd, prev_summary=sess.get("last_summary"),
                          max_chars=int(cfg["digest_max_chars"]))
    report["digest_chars"] = len(digest)
    (LOGS / "reviews" / f"{rid}.digest.txt").write_text(digest, encoding="utf-8")

    prompt = (PROMPTS / PROMPT_BY_SCOPE[scope]).read_text(encoding="utf-8")
    if dry_run:
        prompt = (PROMPTS / "dry-run-banner.md").read_text(encoding="utf-8") + "\n\n" + prompt
        meta, alerts, applied = _run_child(rid, digest, prompt, dry_run, memory_on, report)
    else:
        with repo_lock():  # one writer of ~/.claude/skills at a time (review, curator run, rollback)
            meta, alerts, applied = _run_child(rid, digest, prompt, dry_run, memory_on, report)
    summary = report["summary"]
    memory_writes = [c for c in meta["tool_calls"] if c["name"] == "mcp__hindsight__retain" and "error" not in c]
    report["memory_writes"] = len(memory_writes)
    if not dry_run:
        parts = [f"{'created' if n in report['created'] else 'patched'} {n}" for n in applied]
        if memory_writes:
            parts.append(f"saved {len(memory_writes)} memor{'y' if len(memory_writes) == 1 else 'ies'} to Hindsight")
        if parts or alerts:
            notice("review", f"background review: {', '.join(parts) or 'no changes'}"
                   + (f" ({sum(a.startswith(('ALERT', 'CONFLICT')) for a in alerts)} alerts/conflicts, see `curator log`)"
                      if alerts else "")
                   + (" -- undo: curator rollback" if report["commit"] else ""), report=str(report_path))
    report["duration_seconds"] = round(time.monotonic() - t0, 1)
    write_report(report_path, report)
    if not dry_run:
        with locked(LOCKS / "sessions.lock"):
            sf = SESSIONS / f"{session_id}.json"
            data = load_json(sf, {})
            data["last_summary"] = summary[:500]
            save_json(sf, data)
    log("reviews", f"{rid} scope={scope} dry={dry_run} cost={meta['cost_usd']} summary={summary[:160]!r} report={report_path}")
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transcript", required=True)
    ap.add_argument("--session")
    ap.add_argument("--cwd", default="")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int)
    ap.add_argument("--scope", default="combined", choices=sorted(PROMPT_BY_SCOPE))
    ap.add_argument("--trigger", default="manual")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--wait-lock", action="store_true", help="block until the running review finishes")
    a = ap.parse_args(argv)
    session = a.session or os.path.basename(a.transcript).removesuffix(".jsonl")
    if "CURATOR_LOCK_FD" in os.environ:
        lock_fd = int(os.environ.pop("CURATOR_LOCK_FD"))  # inherited, already locked by the hook
    else:
        LOCKS.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(LOCKS / "review.lock", os.O_RDWR | os.O_CREAT)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | (0 if a.wait_lock else fcntl.LOCK_NB))
        except BlockingIOError:
            print("another review is running; try again later", file=sys.stderr)
            return 1
    try:
        rep = review(a.transcript, session, a.cwd, a.start, a.end, a.scope, a.trigger, a.dry_run)
    except Exception:
        log_exc("reviews")
        raise
    finally:
        os.close(lock_fd)
    print(f"{rep.get('summary') or rep.get('skipped')}\nreport: {LOGS / 'reviews' / (rep['id'] + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
