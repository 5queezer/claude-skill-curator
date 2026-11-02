Review the conversation digest above and consider saving to memory if appropriate.

Memory (Hindsight) has TWO distinct kinds of fact — pick the right one for each fact and pass it as the retain `tags`:
  • tag `user` (Hermes USER.md): who the user is — persona, preferences, communication and work style, personal details they revealed, and expectations about how you should behave.
  • tag `environment` (Hermes MEMORY.md): facts about the ENVIRONMENT you operate in — tool quirks, project conventions, config gotchas, paths and endpoints that matter.

One fact goes to ONE kind, never both — writing it twice bloats memory and crowds out the facts that matter; misrouting it puts it where the next session won't look. Before each retain, run mcp__hindsight__recall with a short query for that fact; if it is already stored, skip it. One concept per retain call, `context` = "curator background review". Never store secrets, credentials or tokens.

If something stands out, save it once, with the matching tag, using mcp__hindsight__retain. If nothing is worth saving, just say 'Nothing to save.' and stop.
