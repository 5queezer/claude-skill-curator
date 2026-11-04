# claude-skill-curator

A port of the Hermes agent's self-improving skills system (`agent/background_review.py` and `agent/curator.py`) to Claude Code.

## How it works
- **Background review (auto-learning):** an async `Stop` hook counts tool-loop iterations in the transcript. Every 10 iterations (Hermes `creation_nudge_interval`), or every 10 user turns for memory, it starts a detached headless `claude -p` reviewer on a digest of the new transcript slice. `SessionEnd` reviews whatever is left. The reviewer works on a staging copy outside `.claude`, because Claude Code blocks writes to `.claude` paths. It patches the skills that were used, or creates new ones (`metadata.created_by: agent`), and saves memory (see Memory below). It never deletes. The changes are copied back and committed to git in `~/.claude/skills`, one commit per run.
- **Usage telemetry:** `PostToolUse(Skill)` and `UserPromptExpansion` update `usage.json`, which uses Hermes' `.usage.json` fields.
- **Curator:** `SessionStart` polls it. It runs every 168 h after 2 h idle, and skips the first run. Skills created by the agent go stale after 14 days and are archived after 30; pinned skills are exempt. LLM consolidation is off by default (`consolidate: false`). Each run writes a report to `logs/curator/<ts>/REPORT.md`.
- **Memory:** two backends, set by `review.memory_backend`.
  - `builtin` (default, like Hermes): `memories/MEMORY.md` (environment: tool quirks, conventions, paths) and `memories/USER.md` (who the user is), entries separated by `\n§\n`, limited to 2,200 and 1,375 chars (`review.memory_char_limit`, `review.user_char_limit`). `SessionStart` injects both files as additional context in Hermes' block format. The reviewer gets copies in its stage and may only append: a file whose existing entries changed, or that would go over its limit, is rejected and logged to `logs/alerts.log`. Edit or prune the files by hand.
  - `mcp`: an MCP memory server from `~/.claude.json`, set by `review.memory_mcp` (`server`, `recall_tool`, `retain_tool`; default `hindsight`, `recall`, `retain`). The reviewer recalls before it retains, and tags each fact `user` or `environment`. Nothing is injected at `SessionStart`.
- **Scope:** only real directories in `~/.claude/skills` are writable. Symlinks into `~/.agents/skills` (shared with Hermes), `synced/`, `web-design/`, and plugin or project skills are read-only. Out-of-scope writes are restored and logged to `logs/alerts.log`.

## CLI
`curator status | run [--dry-run] [--consolidate] | review <transcript> [--dry-run] | pause | resume | pin/unpin/adopt/restore <skill> | rollback [--list|--snapshot] | log`

## Install
Merge `settings-hooks.json` into `~/.claude/settings.json` and link `curator` into `~/.local/bin`. Defaults are in `common.py` (`DEFAULTS`). To override some, create `config.json` with only those keys, e.g. `{"review": {"memory_backend": "mcp"}}`. The file is optional and not tracked. To stop everything: `curator pause`.
