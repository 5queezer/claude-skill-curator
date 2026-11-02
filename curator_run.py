"""Curator pass (port of Hermes agent/curator.py + curator_backup.py).

Phase 1: deterministic active -> stale -> archived transitions for curator-managed skills.
Phase 2: optional LLM consolidation (off by default, like Hermes)."""
from __future__ import annotations

import json
import os
import shutil
import tarfile
import time
from datetime import timedelta
from pathlib import Path

import usage
from childrun import (apply_stage, enforce_calls, permission_args, prepare_stage, run_claude,
                      snapshot_protected, write_report)
from common import (ARCHIVE, BACKUPS, LOCKS, LOGS, PROJECTS, PROMPTS, RUN_DIR, SESSIONS, SKILLS,
                    STATE_FILE, USAGE_FILE, commit_all, config, ensure_git, iso, load_state, log,
                    main_model, now, parse_iso, repo_lock, skill_entries, state_update)

CLAIM = LOCKS / "curator-run"
CLAIM_STALE_SECONDS = 3600.0

CONSOLIDATION_SCHEMA = {
    "type": "object",
    "required": ["summary", "consolidations", "prunings", "added"],
    "properties": {
        "summary": {"type": "string"},
        "consolidations": {"type": "array", "items": {"type": "object", "required": ["from", "into", "reason"], "properties": {
            "from": {"type": "string"}, "into": {"type": "string"}, "reason": {"type": "string"}}}},
        "prunings": {"type": "array", "items": {"type": "object", "required": ["name", "reason"], "properties": {
            "name": {"type": "string"}, "reason": {"type": "string"}}}},
        "added": {"type": "array", "items": {"type": "string"}},
    },
}


# --- gates (Hermes should_run_now / maybe_run_curator) ---

def should_run_now() -> bool:
    cfg = config()
    if not cfg["enabled"] or load_state().get("paused"):
        return False
    with state_update() as state:
        last = parse_iso(state.get("last_run_at"))
        if last is None:  # first sight: seed and defer one interval
            state["last_run_at"] = iso()
            state["last_run_summary"] = ("deferred first run -- curator seeded, will run after one interval; "
                                         "use `curator run --dry-run` to preview now")
            return False
    return now() - last >= timedelta(hours=max(1, int(cfg["interval_hours"])))


def hours_since_last_run() -> float:
    last = parse_iso(load_state().get("last_run_at"))
    return (now() - last).total_seconds() / 3600 if last else 0.0


def idle_hours(exclude_session: str | None) -> float:
    """Hours since any OTHER Claude Code transcript was written (approximates Hermes' agent idle time)."""
    newest = 0.0
    for proj in os.scandir(PROJECTS):
        if not proj.is_dir():
            continue
        for entry in os.scandir(proj.path):
            if exclude_session and entry.name.startswith(exclude_session):
                continue
            if entry.name.endswith(".jsonl"):
                newest = max(newest, entry.stat().st_mtime)
    return (time.time() - newest) / 3600 if newest else float("inf")


def claim_run() -> bool:
    """O_EXCL claim so two sessions starting together don't both run; >1h old claims are stale."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(CLAIM, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - CLAIM.stat().st_mtime > CLAIM_STALE_SECONDS:
                CLAIM.unlink()
                return claim_run()
        except OSError:
            pass
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def release_claim() -> None:
    CLAIM.unlink(missing_ok=True)


# --- phase 1 ---

def apply_transitions(dry_run: bool) -> tuple[dict, list[dict]]:
    cfg = config()
    start = now()
    stale_cutoff = start - timedelta(days=max(1, int(cfg["stale_after_days"])))
    archive_cutoff = start - timedelta(days=max(1, int(cfg["archive_after_days"])))
    counts = {"checked": 0, "marked_stale": 0, "archived": 0, "reactivated": 0, "seeded": 0}
    transitions = []
    data = usage.load()
    # First sight of a local skill: seed its record (clock starts now) and defer it.
    unseen = [e["name"] for e in skill_entries() if e["kind"] == "writable" and e["name"] not in data]
    if unseen and not dry_run:
        with usage.update() as d:
            for name in unseen:
                usage.record(d, name)["first_seen_at"] = iso()
    counts["seeded"] = len([n for n in unseen if usage.provenance(n) == "agent"])

    for name in usage.managed_names():
        if name in unseen:
            continue
        rec = {**usage.empty_record(), **data[name]}
        counts["checked"] += 1
        if rec.get("pinned"):
            continue
        anchor = usage.latest_activity_at(rec) or parse_iso(rec.get("created_at")) or start
        state = rec.get("state", usage.STATE_ACTIVE)
        new = None
        if int(rec.get("use_count") or 0) == 0 and anchor > stale_cutoff:
            new = usage.STATE_ACTIVE if state == usage.STATE_STALE else None  # never-used grace floor
        elif anchor <= archive_cutoff and state != usage.STATE_ARCHIVED:
            new = usage.STATE_ARCHIVED
        elif anchor <= stale_cutoff and state == usage.STATE_ACTIVE:
            new = usage.STATE_STALE
        elif anchor > stale_cutoff and state == usage.STATE_STALE:
            new = usage.STATE_ACTIVE
        if not new:
            continue
        key = {"stale": "marked_stale", "archived": "archived", "active": "reactivated"}[new]
        transitions.append({"name": name, "from": state, "to": new, "last_activity": iso(anchor), "applied": not dry_run})
        counts[key] += 1
        if dry_run:
            continue
        if new == usage.STATE_ARCHIVED:
            ok, msg = usage.archive_skill(name)
            transitions[-1]["result"] = msg
            transitions[-1]["archived_path"] = usage.load().get(name, {}).get("archived_path")
            if not ok:
                counts[key] -= 1
        else:
            with usage.update() as d:
                usage.record(d, name)["state"] = new
    return counts, transitions


# --- backups (curator_backup.py) ---

def snapshot_skills(reason: str, protect: Path | None = None) -> Path:
    """tar.gz of the curator-writable skill dirs, usage.json, state.json and the archive; keep newest N."""
    base = now().strftime("%Y-%m-%dT%H-%M-%SZ")
    snap, n = BACKUPS / base, 1
    while snap.exists():  # same-second snapshots (e.g. pre-rollback right after a run)
        n += 1
        snap = BACKUPS / f"{base}-{n:02d}"
    snap.mkdir(parents=True)
    names = [e["name"] for e in skill_entries() if e["kind"] == "writable"]
    with tarfile.open(snap / "skills.tar.gz", "w:gz") as tar:
        for name in names:
            tar.add(SKILLS / name, arcname=f"skills/{name}")
        for f in (USAGE_FILE, STATE_FILE):
            if f.exists():
                tar.add(f, arcname=f.name)
        ARCHIVE.mkdir(parents=True, exist_ok=True)
        tar.add(ARCHIVE, arcname="archive")
    (snap / "manifest.json").write_text(json.dumps({"id": snap.name, "reason": reason, "created_at": iso(),
                                                    "skills": names, "has_archive": True}, indent=2))
    keep = max(1, int(config()["backup_keep"]))
    for old in sorted((p for p in BACKUPS.iterdir() if p.is_dir()), reverse=True)[keep:]:
        if old != protect:
            shutil.rmtree(old)
    return snap


def restore_snapshot(snap: Path) -> list[str]:
    """Restore the snapshot's skill dirs, usage.json and archive dir (after a safety snapshot).

    state.json is deliberately NOT restored (Hermes does): it would roll last_run_at back and the
    next startup would immediately redo the pass that was just rolled back."""
    manifest = json.loads((snap / "manifest.json").read_text())
    snapshot_skills(f"pre-rollback to {snap.name}", protect=snap)
    for name in manifest["skills"]:
        if (SKILLS / name).is_dir() and not (SKILLS / name).is_symlink():
            shutil.rmtree(SKILLS / name)
    if manifest.get("has_archive"):
        shutil.rmtree(ARCHIVE, ignore_errors=True)
        ARCHIVE.mkdir(parents=True)
    with tarfile.open(snap / "skills.tar.gz") as tar:
        members = []
        for m in tar.getmembers():
            if m.name.startswith("skills/"):
                m.name = m.name.removeprefix("skills/")
                members.append((SKILLS, m))
            elif m.name.startswith("archive/"):
                m.name = m.name.removeprefix("archive/")
                members.append((ARCHIVE, m))
            elif m.name == USAGE_FILE.name:
                members.append((USAGE_FILE.parent, m))
        for dest, m in members:
            tar.extract(m, dest, filter="tar")
    return manifest["skills"]


# --- phase 2 ---

def candidate_list(names: list[str]) -> str:
    data = usage.load()
    rows = []
    for n in names:
        r = {**usage.empty_record(), **data.get(n, {})}
        last = usage.latest_activity_at(r)
        rows.append(f"- {n}  state={r['state']}  pinned={'yes' if r['pinned'] else 'no'}  use={r['use_count']}  "
                    f"view={r['view_count']}  patches={r['patch_count']}  created={r['created_at']}  "
                    f"last_activity={iso(last) if last else 'never'}")
    return f"Agent-created skills ({len(rows)}):\n\n" + "\n".join(rows)


def consolidation_pass(dry_run: bool, stamp: str) -> dict:
    names = usage.managed_names()
    if not names:
        return {"summary": "skipped (no candidates)", "error": None, "tool_calls": []}
    pinned = {n for n, r in usage.load().items() if r.get("pinned")}
    editable = [n for n in names if n not in pinned]
    stage, before = prepare_stage(f"curator-{stamp}", editable)
    work = stage / "skills"
    prompt = (PROMPTS / "curator.md").read_text().replace("{{NOW}}", iso()).replace("{{STAGE}}", str(work))
    if dry_run:
        prompt = (PROMPTS / "dry-run-banner.md").read_text() + "\n\n" + prompt
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    system_file = RUN_DIR / f"curator-system-{stamp}.md"
    system_file.write_text("You are a headless background maintenance pass. Follow the curator prompt in the user message.")
    existing = {e["name"] for e in skill_entries()}
    snapshot = None if dry_run else snapshot_protected(RUN_DIR / f"protected-curator-{stamp}.tar")
    cfg = config()["review"]
    applied, alerts = {}, []
    try:
        meta = run_claude(user_message=f"{prompt}\n\n{candidate_list(names)}", system_file=system_file,
                          schema=CONSOLIDATION_SCHEMA, model=cfg["model"] or main_model(),
                          budget=float(cfg["max_budget_usd"]) * 3, timeout=int(cfg["timeout_seconds"]) * 2,
                          perm_args=permission_args(stage, editable, dry_run=dry_run, memory=False),
                          mcp_config=None, stage=stage)
        if not dry_run:
            alerts = enforce_calls(meta, snapshot, stage)
            applied, conflicts = apply_stage(stage, before, set(editable), existing,
                                             LOGS / "curator" / f"{stamp}.conflicts")
            alerts += conflicts
    finally:
        system_file.unlink(missing_ok=True)
        if snapshot:
            snapshot.unlink(missing_ok=True)
        shutil.rmtree(stage, ignore_errors=True)
    for a in alerts:
        log("alerts", f"curator-{stamp} {a}")
    meta.pop("_raw_calls", None)
    out = meta.get("structured") or {}
    result = {"summary": (out.get("summary") or meta.get("error") or "no change")[:2000], "error": meta["error"],
              "model": meta["model"], "cost_usd": meta["cost_usd"], "tool_calls": meta["tool_calls"],
              "alerts": alerts, "structured": out, "consolidated": [], "refused": [], "added": [], "patched": []}
    if dry_run:
        return result
    for name in applied:
        if name not in existing:
            usage.record_created(name)
            result["added"].append(name)
        else:
            usage.bump(name, "patch")
            result["patched"].append(name)
    # Archive only verified consolidations (Hermes: delete requires an existing absorbed_into umbrella).
    for c in out.get("consolidations") or []:
        src, into = c.get("from"), c.get("into")
        if src in editable and into and into != src and (SKILLS / into / "SKILL.md").exists():
            ok, msg = usage.archive_skill(src)
            (result["consolidated"] if ok else result["refused"]).append({**c, "result": msg})
        else:
            result["refused"].append({**c, "result": "refused: not a curator-managed unpinned skill, or umbrella missing"})
    return result


# --- run ---

def run(dry_run: bool = False, consolidate: bool | None = None, trigger: str = "manual") -> Path | None:
    """One curator pass. Writers of ~/.claude/skills are serialised; a scheduled pass skips (and is
    polled again at the next startup) when a background review or rollback holds the repo lock."""
    if dry_run:
        return _run(dry_run, consolidate, trigger)
    with repo_lock(blocking=False) as held:
        if held:
            return _run(dry_run, consolidate, trigger)
    if trigger == "scheduled":
        log("curator", "scheduled run skipped: skills repo busy (background review running)")
        return None
    print("waiting for the running background review to finish...", flush=True)
    with repo_lock():
        return _run(dry_run, consolidate, trigger)


def _run(dry_run: bool, consolidate: bool | None, trigger: str) -> Path:
    cfg = config()
    consolidate = cfg["consolidate"] if consolidate is None else consolidate
    start, t0 = now(), time.monotonic()
    stamp = start.strftime("%Y%m%d-%H%M%S")
    snapshot = None
    if not dry_run:
        ensure_git()
        commit_all("curator: pre-run snapshot of manual edits")
        if consolidate:
            snapshot = snapshot_skills("pre-curator-run")
    before = usage.managed_names()
    counts, transitions = apply_transitions(dry_run)
    auto = ", ".join(f"{counts[k]} {label}" for k, label in (("marked_stale", "marked stale"), ("archived", "archived"),
                                                            ("reactivated", "reactivated")) if counts[k]) or "no changes"
    prefix = "dry-run auto: " if dry_run else "auto: "
    with state_update() as state:
        state["last_run_summary"] = prefix + auto
        if not dry_run:
            state["last_run_at"] = iso(start)
            state["run_count"] = int(state.get("run_count") or 0) + 1
    llm = consolidation_pass(dry_run, stamp) if consolidate else {"summary": "skipped (consolidation off)", "error": None, "tool_calls": []}
    final = f"{prefix}{auto}; llm: {llm['summary'][:240]}"
    if llm.get("consolidated"):
        final += "\narchived " + ", ".join(f"{c['from']} -> {c['into']}" for c in llm["consolidated"])
    commit = None if dry_run else commit_all(f"curator(run): {auto}\n\nreport: {LOGS / 'curator' / stamp}")
    after = usage.managed_names()
    elapsed = time.monotonic() - t0
    run_dir = LOGS / "curator" / stamp
    while run_dir.exists():
        run_dir = run_dir.with_name(run_dir.name + "-2")
    run_dir.mkdir(parents=True)
    payload = {"started_at": iso(start), "duration_seconds": round(elapsed, 2), "trigger": trigger, "dry_run": dry_run,
               "consolidate": consolidate, "auto_transitions": counts, "state_transitions": transitions,
               "counts": {"before": len(before), "after": len(after)}, "llm": llm, "commit": commit,
               "snapshot": str(snapshot) if snapshot else None, "summary": final}
    write_report(run_dir / "run.json", payload)
    (run_dir / "REPORT.md").write_text(render_report(payload))
    with state_update() as state:
        state.update(last_run_duration_seconds=round(elapsed, 2), last_run_summary=final, last_report_path=str(run_dir))
        if not dry_run:
            state["last_run_summary_shown_at"] = None  # SessionStart shows the new summary once
    prune_session_files()
    log("curator", f"run trigger={trigger} dry={dry_run} consolidate={consolidate} {final!r} report={run_dir}")
    return run_dir


def render_report(p: dict) -> str:
    a = p["auto_transitions"]
    lines = [f"# Curator run — {p['started_at']}\n",
             f"Trigger: {p['trigger']}  ·  dry-run: {p['dry_run']}  ·  Duration: {p['duration_seconds']}s  ·  "
             f"Agent-created skills: {p['counts']['before']} → {p['counts']['after']}\n",
             "## Auto-transitions (pure, no LLM)\n", f"- checked: {a['checked']}", f"- seeded (deferred): {a['seeded']}",
             f"- marked stale: {a['marked_stale']}", f"- archived: {a['archived']}", f"- reactivated: {a['reactivated']}", ""]
    if p["state_transitions"]:
        lines += ["### State transitions" + (" (would apply)" if p["dry_run"] else "") + "\n"]
        lines += [f"- `{t['name']}`: {t['from']} → {t['to']} (last activity {t['last_activity']})" for t in p["state_transitions"]] + [""]
    llm = p["llm"]
    lines += ["## LLM consolidation pass\n", f"- {llm['summary']}"]
    for key, title in (("consolidated", "Consolidated into umbrella skills"), ("refused", "Refused archive requests")):
        if llm.get(key):
            lines += ["", f"### {title}\n"] + [f"- `{c['from']}` → `{c['into']}` — {c.get('reason', '')} ({c['result']})" for c in llm[key]]
    for key, title in (("added", "New skills"), ("patched", "Patched skills"), ("alerts", "ALERTS")):
        if llm.get(key):
            lines += ["", f"### {title}\n"] + [f"- {x}" for x in llm[key]]
    lines += ["", "## Recovery\n", "- Restore an archived skill: `curator restore <name>`",
              "- Undo the last curator commit: `curator rollback`; restore the pre-consolidation snapshot: `curator rollback --snapshot`",
              "- Archives live under `~/.claude/curator/archive/`; see `run.json` for the machine-readable record.", ""]
    return "\n".join(lines)


def prune_session_files(days: int = 30) -> None:
    cutoff = time.time() - days * 86400
    for f in SESSIONS.glob("*.json"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
