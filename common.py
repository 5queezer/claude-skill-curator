"""Paths, config, state, locking and skill-scope helpers shared by the curator scripts."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import traceback
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

HOME = Path.home()
ROOT = HOME / ".claude" / "curator"
SKILLS = HOME / ".claude" / "skills"
AGENTS_SKILLS = HOME / ".agents" / "skills"
PLUGINS = HOME / ".claude" / "plugins"
PROJECTS = HOME / ".claude" / "projects"
PROMPTS = ROOT / "prompts"
LOGS = ROOT / "logs"
SESSIONS = ROOT / "sessions"
LOCKS = ROOT / "locks"
RUN_DIR = ROOT / "run"
ARCHIVE = ROOT / "archive"
BACKUPS = ROOT / "backups"
PAUSED = ROOT / "PAUSED"
STATE_FILE = ROOT / "state.json"
USAGE_FILE = ROOT / "usage.json"
CONFIG_FILE = ROOT / "config.json"
NOTICES = ROOT / "notices.jsonl"
# Builtin memory backend (Hermes ~/.hermes/memories): MEMORY.md = environment, USER.md = user profile.
MEMORIES = ROOT / "memories"
MEMORY_FILES = {"memory": "MEMORY.md", "user": "USER.md"}
ENTRY_DELIMITER = "\n§\n"
CLAUDE_BIN = HOME / ".local" / "bin" / "claude"
# Per-run working copies the child edits. Must be outside any `.claude` path: Claude Code treats those
# as sensitive and refuses Edit/Write there in dontAsk mode even with an explicit allow rule.
STAGE_ROOT = HOME / ".local" / "state" / "claude-curator" / "stage"

# Real dirs under ~/.claude/skills that are still read-only: synced/ is rewritten by the cloud sync,
# web-design/ is its own git repo.
READ_ONLY_NAMES = {"synced", "web-design"}

# Hermes defaults (curator.py, curator_backup.py, agent_init.py nudge intervals).
DEFAULTS = {
    "enabled": True,
    "interval_hours": 168,
    "min_idle_hours": 2,
    "stale_after_days": 14,
    "archive_after_days": 30,
    "consolidate": False,
    "backup_keep": 2,
    "review": {
        "enabled": True,
        "creation_nudge_interval": 10,  # tool-loop iterations between skill reviews
        "memory_nudge_interval": 10,  # user turns between memory reviews
        "session_end_min_iterations": 3,
        "memory": True,
        "daily_cap": 12,
        "model": None,  # None = the main model from ~/.claude/settings.json, like Hermes "auto"
        "max_budget_usd": 1.0,
        "timeout_seconds": 900,
        "digest_max_chars": 60000,
        "memory_backend": "builtin",  # "builtin" (MEMORY.md/USER.md, like Hermes) or "mcp"
        "memory_mcp": {"server": "hindsight", "recall_tool": "recall", "retain_tool": "retain"},
        "memory_char_limit": 2200,  # Hermes memory_char_limit / user_char_limit
        "user_char_limit": 1375,
    },
}

STATE_DEFAULTS = {
    "last_run_at": None, "last_run_duration_seconds": None, "last_run_summary": None,
    "last_run_summary_shown_at": None, "last_report_path": None, "paused": False, "run_count": 0,
    "reviews_by_day": {},
}


def now() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).isoformat(timespec="seconds")


def parse_iso(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None
    return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextmanager
def locked(path: Path):
    """Exclusive flock held across a read-modify-write cycle."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def config() -> dict:
    user = load_json(CONFIG_FILE, {})
    cfg = {**DEFAULTS, **{k: v for k, v in user.items() if k != "review"}}
    cfg["review"] = {**DEFAULTS["review"], **(user.get("review") or {})}
    cfg["review"]["memory_mcp"] = {**DEFAULTS["review"]["memory_mcp"], **(cfg["review"]["memory_mcp"] or {})}
    return cfg


def main_model() -> str:
    return load_json(HOME / ".claude" / "settings.json", {}).get("model") or "opus"


def load_state() -> dict:
    return {**STATE_DEFAULTS, **load_json(STATE_FILE, {})}


@contextmanager
def state_update():
    with locked(LOCKS / "state.lock"):
        state = load_state()
        yield state
        save_json(STATE_FILE, state)


def is_paused() -> bool:
    return PAUSED.exists() or bool(load_state().get("paused"))


def log(name: str, msg: str) -> None:
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / f"{name}.log", "a", encoding="utf-8") as fh:
        fh.write(f"{iso()} {msg}\n")


def log_exc(name: str) -> None:
    log(name, "ERROR " + traceback.format_exc().replace("\n", "\n    "))


def notice(kind: str, text: str, **extra) -> None:
    """Queue a line for the next SessionStart summary."""
    with locked(LOCKS / "notices.lock"), open(NOTICES, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": iso(), "kind": kind, "text": text, **extra}) + "\n")


# --- skills ---

def read_frontmatter(skill_md: Path) -> dict:
    """Top-level scalar keys plus metadata.created_by/created_at from a SKILL.md (no YAML lib)."""
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")[:6000]
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out, section = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip().strip("\"'")
        if not line.startswith((" ", "\t")):
            section = key.strip()
            if val:
                out[section] = val
        elif section == "metadata":
            out[f"metadata.{key.strip()}"] = val
    return out


def skill_entries() -> list[dict]:
    """Top-level entries of ~/.claude/skills with their write scope (Hermes: local vs external_dirs)."""
    rows = []
    for p in sorted(SKILLS.iterdir()) if SKILLS.exists() else []:
        if p.name.startswith(".") or not p.is_dir():
            continue
        if p.is_symlink():
            kind = "external"
        elif p.name in READ_ONLY_NAMES:
            kind = "readonly"
        else:
            kind = "writable"
        rows.append({"name": p.name, "path": p, "kind": kind})
    return rows


def writable_skill_names() -> list[str]:
    return [e["name"] for e in skill_entries() if e["kind"] == "writable"]


def skill_name_from_path(path: str) -> str | None:
    """Skill name for a file inside a Claude Code skills dir, else None.

    Recognised: <dir>/.claude/skills/<name>/… (user, project; synced/<bucket>/<name>/…),
    ~/.agents/skills/<name>/… and ~/.claude/plugins/**/skills/<name>/…. Any other `skills/` folder
    (e.g. a repo's own skills/ tree) is ordinary project code."""
    p = Path(os.path.abspath(os.path.expanduser(path)))
    parts = p.parts
    if p.is_relative_to(PLUGINS):
        idx = len(parts) - 1 - parts[::-1].index("skills") if "skills" in parts else None
    elif p.is_relative_to(AGENTS_SKILLS):
        idx = len(AGENTS_SKILLS.parts) - 1
    else:
        idx = next((i for i in range(1, len(parts)) if parts[i] == "skills" and parts[i - 1] == ".claude"), None)
    if idx is None:
        return None
    rest = parts[idx + 1:]
    if rest[:1] == ("synced",):  # synced/<account-bucket>/<name>/…
        rest = rest[2:]
    return rest[0] if len(rest) >= 2 else None


def git(*args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(SKILLS), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(res.stderr + res.stdout).strip()}")
    return res.stdout


def git_paths(cmd: str, *args: str) -> list[str]:
    """Path output of `git <cmd> -z ...` split on NUL; safe for spaces and non-ASCII names."""
    return [p for p in git(cmd, "-z", *args, check=False).split("\0") if p]


def repo_busy() -> str | None:
    """Why the skills repo must not be committed to right now (unfinished revert/merge), else None."""
    gitdir = SKILLS / ".git"
    for head in ("REVERT_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if (gitdir / head).exists():
            return f"{head} present (unfinished git operation)"
    if git_paths("diff", "--name-only", "--diff-filter=U"):
        return "unmerged paths"
    return None


@contextmanager
def repo_lock(blocking: bool = True):
    """One writer at a time for ~/.claude/skills (background review, curator run, rollback/restore).

    Yields True when held; with blocking=False it yields False if another writer holds it."""
    LOCKS.mkdir(parents=True, exist_ok=True)
    with open(LOCKS / "skills-repo.lock", "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def ensure_git() -> None:
    """git-init ~/.claude/skills once (baseline commit), ignoring the read-only real dirs."""
    if (SKILLS / ".git").exists():
        return
    git("init", "-q")
    ignore = SKILLS / ".gitignore"
    lines = ignore.read_text().splitlines() if ignore.exists() else []
    for name in sorted(READ_ONLY_NAMES):
        if f"{name}/" not in lines:
            lines.append(f"{name}/")
    ignore.write_text("\n".join(lines) + "\n")
    git("add", "-A")
    git("commit", "-q", "-m", "curator: baseline")


def commit_all(message: str) -> str | None:
    """Commit every change in the skills repo; returns the sha or None when clean or busy."""
    if busy := repo_busy():
        log("alerts", f"not committing {message.splitlines()[0]!r}: {busy}")
        return None
    git("add", "-A")
    if not git("status", "--porcelain").strip():
        return None
    git("commit", "-q", "-m", message)
    return git("rev-parse", "HEAD").strip()


def commit_paths(paths: list[Path], message: str) -> str | None:
    """Commit only *paths* (other dirty files stay for the next manual-edit snapshot commit)."""
    if busy := repo_busy():
        log("alerts", f"not committing {message.splitlines()[0]!r}: {busy}")
        return None
    rels = [str(p.relative_to(SKILLS)) for p in paths]
    if not rels:
        return None
    git("add", "--", *rels, check=False)  # gitignored paths fail here and are simply not committed
    staged = git_paths("diff", "--cached", "--name-only", "--", *rels)
    if not staged:
        return None
    git("commit", "-q", "-m", message, "--", *staged)
    return git("rev-parse", "HEAD").strip()


def memory_mcp_server(cfg: dict) -> dict | None:
    """The user-scope entry of the configured memory MCP server (review.memory_mcp.server) in ~/.claude.json."""
    servers = load_json(HOME / ".claude.json", {}).get("mcpServers") or {}
    return servers.get(cfg["memory_mcp"]["server"])


def memory_mcp_tool(cfg: dict, tool: str) -> str:
    """Full name of the memory MCP server's "recall" or "retain" tool: mcp__<server>__<tool>."""
    mcp = cfg["memory_mcp"]
    return f"mcp__{mcp['server']}__{mcp[tool + '_tool']}"


def read_entries(path: Path) -> list[str]:
    """Stripped, non-empty entries of a MEMORY.md/USER.md ([] when missing), Hermes _parse_entries.

    Other read errors propagate: callers rewrite the file and must not replace an unreadable one."""
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    return [e for e in (x.strip() for x in raw.split(ENTRY_DELIMITER)) if e]


def die_quietly_if_child() -> None:
    if os.environ.get("CURATOR_CHILD"):
        sys.exit(0)
