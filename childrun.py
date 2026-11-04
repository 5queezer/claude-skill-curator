"""Run a headless `claude -p` child with a scoped toolset, then guard what it touched.

Shared by the background review (review.py) and the consolidation pass (curator_run.py)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

from common import (AGENTS_SKILLS, CLAUDE_BIN, ENTRY_DELIMITER, HOME, LOCKS, MEMORIES, MEMORY_FILES, PLUGINS, ROOT,
                    RUN_DIR, SKILLS, STAGE_ROOT, config, git, locked, memory_mcp_server, memory_mcp_tool, read_entries,
                    save_json, skill_entries)

PROTECTED_TREES = [AGENTS_SKILLS, SKILLS / "synced", SKILLS / "web-design"]
WRITE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def tilde(p: Path) -> str:
    return "~/" + str(p.relative_to(HOME))


def permission_args(stage: Path, editable: list[str], *, dry_run: bool, memory: bool) -> list[str]:
    """--tools/--allowedTools/--disallowedTools for the child.

    The child edits only the working copy *stage*/skills (copies of *editable* plus new skill dirs)
    and, with the builtin memory backend, *stage*/memories; the live library is read-only to it.
    Claude Code matches only Edit(path) rules for file writes (Write(path) rules are ignored), and an
    Edit rule covers Write too."""
    tools = ["Read", "Glob", "Grep"] + ([] if dry_run else ["Edit", "Write"])
    work = f"{tilde(stage)}/skills"
    allow = [f"Read({tilde(SKILLS)}/**)", f"Read({tilde(AGENTS_SKILLS)}/**)", f"Read({tilde(PLUGINS)}/**)",
             f"Read({tilde(stage)}/**)"]
    deny = ["Bash", "Agent", "WebFetch", "WebSearch", "NotebookEdit"]
    if not dry_run:
        allow.append(f"Edit({work}/**)")
        # Names that exist in the live library but are not editable must not be recreated in the stage.
        for name in sorted({e["name"] for e in skill_entries()} - set(editable)):
            deny += [f"Edit({work}/{name})", f"Edit({work}/{name}/**)"]
    cfg = config()["review"]
    if memory and cfg["memory_backend"] != "builtin":
        allow.append(memory_mcp_tool(cfg, "recall"))
        (deny if dry_run else allow).append(memory_mcp_tool(cfg, "retain"))
    elif memory and not dry_run:
        allow.append(f"Edit({tilde(stage)}/memories/**)")
    return ["--tools", ",".join(tools), "--allowedTools", *allow, "--disallowedTools", *deny]


def write_mcp_config() -> Path | None:
    """Minimal MCP config holding only the memory MCP server (0600; it may carry a bearer token)."""
    cfg = config()["review"]
    server = memory_mcp_server(cfg)
    if not server:
        return None
    path = ROOT / "mcp-memory.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"mcpServers": {cfg["memory_mcp"]["server"]: server}}, fh)
    return path


def run_claude(*, user_message: str, system_file: Path, schema: dict, model: str, budget: float,
               timeout: int, perm_args: list[str], mcp_config: Path | None, stage: Path) -> dict:
    """Run the child and return parsed metadata from its stream-json output."""
    cmd = [str(CLAUDE_BIN), "-p", "--model", model, "--setting-sources", "project",
           "--no-session-persistence", "--permission-mode", "dontAsk", "--strict-mcp-config",
           "--add-dir", str(SKILLS), str(AGENTS_SKILLS), str(stage),
           *perm_args,
           "--append-system-prompt-file", str(system_file), "--max-budget-usd", str(budget),
           "--output-format", "stream-json", "--verbose", "--include-hook-events",
           "--json-schema", json.dumps(schema)]
    if mcp_config:
        cmd += ["--mcp-config", str(mcp_config)]
    meta = {"cmd": cmd, "model": model, "tool_calls": [], "tool_errors": [], "structured": None,
            "result": None, "error": None, "cost_usd": None, "num_turns": None,
            "permission_denials": [], "init": {}, "hook_events": []}
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(cmd, input=user_message, capture_output=True, text=True, timeout=timeout,
                              cwd=RUN_DIR, env={**os.environ, "CURATOR_CHILD": "1"})
    except subprocess.TimeoutExpired as e:
        meta["error"] = f"timeout after {timeout}s"
        stdout = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        proc = None
    else:
        stdout = proc.stdout
        if proc.returncode != 0:
            meta["error"] = f"exit {proc.returncode}: {proc.stderr.strip()[-500:]}"
    calls = {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "system" and "hook" in str(ev.get("subtype")):
            meta["hook_events"].append({k: ev.get(k) for k in ("subtype", "hook_name", "hook_event", "name") if ev.get(k)})
        elif ev.get("type") == "system" and ev.get("subtype") == "init":
            meta["init"] = {k: ev.get(k) for k in ("model", "tools", "mcp_servers", "permissionMode", "plugins", "cwd")}
        elif ev.get("type") == "assistant":
            for b in ev.get("message", {}).get("content", []):
                if b.get("type") == "tool_use" and b.get("name") != "StructuredOutput":
                    calls[b["id"]] = {"name": b["name"], "input": b.get("input") or {}}
        elif ev.get("type") == "user":
            for b in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
                if b.get("type") == "tool_result" and b.get("is_error") and b.get("tool_use_id") in calls:
                    calls[b["tool_use_id"]]["error"] = str(b.get("content"))[:300]
        elif ev.get("type") == "result":
            meta.update(structured=ev.get("structured_output"), result=ev.get("result"),
                        cost_usd=ev.get("total_cost_usd"), num_turns=ev.get("num_turns"),
                        permission_denials=ev.get("permission_denials") or [])
            if ev.get("is_error") and not meta["error"]:
                meta["error"] = f"{ev.get('subtype')}: {str(ev.get('result'))[:300]}"
    for c in calls.values():
        short = {k: (v[:400] + "…" if isinstance(v, str) and len(v) > 400 else v) for k, v in c["input"].items()}
        meta["tool_calls"].append({"name": c["name"], "input": short, **({"error": c["error"]} if "error" in c else {})})
    meta["_raw_calls"] = list(calls.values())
    return meta


# --- working copy ---

def _sha(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def manifest(root: Path) -> dict[str, str]:
    """{relative file path: sha1} of regular files under *root* (symlinks skipped)."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            fp = Path(dirpath) / f
            if not fp.is_symlink():
                out[str(fp.relative_to(root))] = _sha(fp)
    return out


HEAVY = shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist")


def prepare_stage(run_id: str, names: list[str]) -> tuple[Path, dict[str, str]]:
    """Copy the editable skill dirs into a fresh per-run stage; returns (stage, manifest of stage/skills)."""
    stage = STAGE_ROOT / run_id
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "skills").mkdir(parents=True)
    for name in names:
        shutil.copytree(SKILLS / name, stage / "skills" / name, symlinks=True, ignore=HEAVY)
    return stage, manifest(stage / "skills")


def apply_stage(stage: Path, before: dict[str, str], editable: set[str], existing: set[str],
                conflict_dir: Path) -> tuple[dict[str, list[Path]], list[str]]:
    """Copy the child's changes from the stage back into ~/.claude/skills.

    Only files whose content changed in the stage are considered, so concurrent foreground edits of
    the live library are never attributed to the child. A live file that changed since the copy is
    not overwritten: the child's version is saved under *conflict_dir* and reported."""
    work = stage / "skills"
    applied: dict[str, list[Path]] = {}
    alerts = []
    for rel, digest in sorted(manifest(work).items()):
        if before.get(rel) == digest:
            continue
        top = rel.split("/", 1)[0]
        new_skill = top not in existing
        if "/" not in rel or top.startswith(".") or not (top in editable or new_skill):
            alerts.append(f"ALERT stage change outside scope ignored: {rel}")
            continue
        dest, live_top = SKILLS / rel, SKILLS / top
        if new_skill:  # someone else created the same name meanwhile
            moved = live_top.exists() and top not in applied
        else:  # archived, removed or replaced by a symlink meanwhile
            moved = live_top.is_symlink() or not live_top.is_dir()
        through_link = os.path.realpath(dest) != str(dest)
        drifted = dest.exists() and (rel not in before or _sha(dest) != before[rel])
        if moved or through_link or drifted:
            keep = conflict_dir / rel
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(work / rel, keep)
            why = "skill dir changed meanwhile" if moved else "path resolves through a symlink" if through_link \
                else "live file was edited meanwhile"
            alerts.append(f"CONFLICT {rel}: {why}; reviewer version kept at {keep}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(work / rel, dest)
        applied.setdefault(top, []).append(dest)
    return applied, alerts


def stage_memories(stage: Path) -> dict[str, list[str]]:
    """Copy MEMORY.md/USER.md into *stage*/memories (empty when missing); returns their entries per target."""
    (stage / "memories").mkdir(parents=True, exist_ok=True)
    before = {}
    for target, name in MEMORY_FILES.items():
        before[target] = read_entries(MEMORIES / name)
        (stage / "memories" / name).write_text(ENTRY_DELIMITER.join(before[target]), encoding="utf-8")
    return before


def apply_memories(stage: Path, before: dict[str, list[str]], cfg: dict) -> tuple[dict[str, int], list[str]]:
    """Append the reviewer's new memory entries to the live files; returns ({file: added}, alerts).

    Background reviews may only add (Hermes _background_delete_gate): a file whose old entries are not
    kept unchanged as a prefix, or that would exceed its char limit, is rejected whole. New entries are
    appended to the live file as it is now, so entries written meanwhile are kept."""
    added, alerts = {}, []
    with locked(LOCKS / "memories.lock"):
        for target, name in MEMORY_FILES.items():
            old, after = before[target], read_entries(stage / "memories" / name)
            if after == old:
                continue
            if after[:len(old)] != old:
                alerts.append(f"ALERT memory change rejected: {name}: existing entries were edited, reordered or removed")
                continue
            live = read_entries(MEMORIES / name)
            final = live + [e for e in dict.fromkeys(after[len(old):]) if e not in live]
            size, limit = len(ENTRY_DELIMITER.join(final)), int(cfg[f"{target}_char_limit"])
            if size > limit:
                alerts.append(f"ALERT memory change rejected: {name}: {size:,} chars exceeds the {limit:,} limit")
                continue
            MEMORIES.mkdir(parents=True, exist_ok=True)
            tmp = MEMORIES / f".{name}.{os.getpid()}.tmp"
            tmp.write_text(ENTRY_DELIMITER.join(final), encoding="utf-8")
            os.replace(tmp, MEMORIES / name)
            added[name] = len(final) - len(live)
    return added, alerts


# --- guards ---

def snapshot_protected(dest: Path) -> Path:
    """Uncompressed tar of the trees the child must not change, for targeted restores."""
    with tarfile.open(dest, "w") as tar:
        for tree in PROTECTED_TREES:
            if tree.exists():
                tar.add(tree, arcname=str(tree.relative_to(HOME)))
    return dest


def enforce_calls(meta: dict, snapshot: Path | None, stage: Path) -> list[str]:
    """Undo any successful child write outside its stage (permissions should already refuse them).

    Acts only on paths the child's own Edit/Write calls named, never on other changes."""
    alerts = []
    work = Path(os.path.realpath(stage))
    tar = tarfile.open(snapshot) if snapshot and snapshot.exists() else None
    for call in meta.get("_raw_calls", []):
        fp = call["input"].get("file_path") if call["name"] in WRITE_TOOLS else None
        if not fp or call.get("error"):
            continue
        real = Path(os.path.realpath(os.path.expanduser(fp)))
        if real.is_relative_to(work):
            continue
        alerts.append(f"ALERT out-of-scope {call['name']} {fp} (-> {real})")
        try:
            member = tar.getmember(str(real.relative_to(HOME))) if tar else None
        except (KeyError, ValueError):
            member = None
        if member is not None:
            tar.extract(member, path=HOME, filter="tar")
            alerts.append(f"  restored {real} from pre-run snapshot")
        elif real.is_relative_to(SKILLS) and git("ls-files", "--", str(real), check=False).strip():
            git("checkout", "HEAD", "--", str(real), check=False)
            alerts.append(f"  restored {real} from git")
        else:
            alerts.append("  could not restore automatically; check by hand")
    if tar:
        tar.close()
    return alerts


def write_report(path: Path, payload: dict) -> None:
    payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    save_json(path, payload)
