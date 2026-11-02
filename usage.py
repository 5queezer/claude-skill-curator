"""Skill usage telemetry + provenance (port of Hermes tools/skill_usage.py).

usage.json is keyed by skill name. Counters are recorded for every skill; lifecycle (stale/archived,
pin) only matters for curator-managed skills, i.e. records with created_by == "agent"."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from common import (ARCHIVE, LOCKS, SKILLS, USAGE_FILE, iso, load_json, locked, now, parse_iso,
                    read_frontmatter, save_json)

STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED = "active", "stale", "archived"


def empty_record() -> dict:
    return {"created_by": None, "use_count": 0, "view_count": 0, "patch_count": 0,
            "last_used_at": None, "last_viewed_at": None, "last_patched_at": None,
            "created_at": iso(), "first_seen_at": None, "state": STATE_ACTIVE, "pinned": False,
            "archived_at": None, "archived_path": None}


def load() -> dict[str, dict]:
    data = load_json(USAGE_FILE, {})
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


@contextmanager
def update():
    """Locked read-modify-write of the whole map."""
    with locked(LOCKS / "usage.lock"):
        data = load()
        yield data
        save_json(USAGE_FILE, data)


def record(data: dict, name: str, cwd: str | None = None) -> dict:
    """The record for *name* with defaults backfilled; creates it (with provenance) if missing."""
    rec = data.get(name)
    if rec is None:
        rec = data[name] = {**empty_record(), "created_by": provenance(name, cwd)}
    for k, v in empty_record().items():
        rec.setdefault(k, v)
    return rec


def provenance(name: str, cwd: str | None = None) -> str:
    """agent | user | external | plugin | project | bundled. Only an explicit `created_by: agent`
    marker makes a skill curator-managed (Hermes: provenance is declared, never inferred)."""
    if ":" in name:
        return "plugin"
    local = SKILLS / name
    if local.is_symlink():
        return "external"
    if (local / "SKILL.md").exists():
        return "agent" if read_frontmatter(local / "SKILL.md").get("metadata.created_by") == "agent" else "user"
    if cwd and (Path(cwd) / ".claude" / "skills" / name / "SKILL.md").exists():
        return "project"
    return "bundled"


def bump(name: str, kind: str, cwd: str | None = None) -> None:
    """kind: use | view | patch."""
    if not name:
        return
    with update() as data:
        rec = record(data, name, cwd)
        rec[f"{kind}_count"] = int(rec.get(f"{kind}_count") or 0) + 1
        rec[{"use": "last_used_at", "view": "last_viewed_at", "patch": "last_patched_at"}[kind]] = iso()


def record_created(name: str) -> None:
    """A skill created by the background reviewer or curator: a fresh, curator-managed record."""
    with update() as data:
        data[name] = {**empty_record(), "created_by": "agent", "first_seen_at": iso()}


def latest_activity_at(rec: dict):
    stamps = [d for d in (parse_iso(rec.get(k)) for k in ("last_used_at", "last_viewed_at", "last_patched_at")) if d]
    return max(stamps) if stamps else None


def is_managed(rec) -> bool:
    return isinstance(rec, dict) and rec.get("created_by") == "agent"


def managed_names() -> list[str]:
    """Curator-managed skills that still live in ~/.claude/skills as real directories."""
    return sorted(n for n, r in load().items()
                  if is_managed(r) and (SKILLS / n).is_dir() and not (SKILLS / n).is_symlink())


def archive_skill(name: str) -> tuple[bool, str]:
    """Move ~/.claude/skills/<name> to the curator archive (outside the dir Claude Code loads)."""
    src = SKILLS / name
    if src.is_symlink() or not src.is_dir():
        return False, f"{name}: not a local skill directory"
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    dest, n, stamp = ARCHIVE / name, 1, now().strftime("%Y%m%d%H%M%S")
    while dest.exists():  # <name>-<stamp>[N]: digits only, so restore_skill can find it
        dest = ARCHIVE / f"{name}-{stamp}{n if n > 1 else ''}"
        n += 1
    src.rename(dest)
    with update() as data:
        rec = record(data, name)
        rec.update(state=STATE_ARCHIVED, archived_at=iso(), archived_path=str(dest))
    return True, f"archived to {dest}"


def restore_skill(name: str) -> tuple[bool, str]:
    """Move the newest archived copy of *name* (the one its usage record points at) back."""
    if not ARCHIVE.exists():
        return False, "archive is empty"
    recorded = (load().get(name) or {}).get("archived_path")
    candidates = [p for p in ARCHIVE.iterdir() if p.is_dir() and (
        p.name == name or (p.name.startswith(f"{name}-") and p.name[len(name) + 1:].isdigit()))]
    if not candidates:
        return False, f"{name}: not in archive"
    if recorded and Path(recorded) in candidates:
        src = Path(recorded)
    else:  # the bare name is the oldest copy; suffixed copies sort by timestamp
        src = sorted(candidates, key=lambda p: (p.name != name, p.name))[-1]
    dest = SKILLS / name
    if dest.exists():
        return False, f"destination already exists: {dest}"
    src.rename(dest)
    with update() as data:
        record(data, name).update(state=STATE_ACTIVE, archived_at=None, archived_path=None)
    return True, f"restored {src} -> {dest}"
