Review the conversation digest above and update two things:

**Memory**: TWO distinct stores — pick the right one for each fact:
  • {{USER_STORE}}: who the user is — persona, preferences, communication and work style, personal details they revealed, and expectations about how you should behave.
  • {{ENV_STORE}}: facts about the ENVIRONMENT you operate in — tool quirks, project conventions, config gotchas, paths and endpoints that matter.

One fact goes to ONE store, never both — writing it to both bloats both stores until they hit their size limits and crowds out the facts that matter; misrouting it puts it where the next session won't look. {{MEMORY_RULES}} Never store secrets, credentials or tokens.

**Skills**: how to do this class of task. Be ACTIVE — most sessions produce at least one skill update. A pass that does nothing is a missed learning opportunity, not a neutral outcome.

Target shape of the skill library: CLASS-LEVEL skills with a SKILL.md of always-on rules and a small `references/` set of topical depth — not narrow one-session skills, and not an umbrella hoarding a references/ file per session.

What a skill IS: the instructions for doing a class of task the most efficient and correct way, to THIS user's specifications — the procedure, the tools and commands that work, the order, the user's preferences for how the result should look, and the pitfalls that cost time. A future session should be able to follow it and produce what the user wants on the first try. Everything below is about writing that well:
  • Procedure first: the steps in the order they are done, with the concrete commands, tool calls, and decision points. Lessons and pitfalls attach to the step they affect.
  • A pitfall is a generalizable rule + one clause of WHY (the mechanism), imperative. 'Grep the test tree for the SYMBOL before widening a helper signature — hand-rolled mocks reimplement the old shape and fail on a shard you did not run.' Not a narrative of what happened this session.
  • No PR/issue numbers, dates, ticket IDs, or quoted user text as content — the rule must stand without the incident behind it. Keep a short quote ONLY when the quote itself is the clearest statement of the rule.
  • The same lesson learned twice is ONE rule. Before adding, search the skill (and its references/) for the rule already stated; strengthen or clarify it rather than appending a second copy.
  • Not a duplicate of what the environment already teaches: CLAUDE.md / AGENTS.md files, tool schema descriptions, and other always-loaded context. A skill carries the WORKFLOW and the pitfalls; it does not restate the codebase map or a tool's parameter list.
  • Always-on rules (standing user preferences, gates that apply to every instance of the task) live in SKILL.md itself, whole. references/ is for depth that is only needed sometimes: a decision table, a recipe, a domain note — each file topical and reusable, never '<date>-<incident>.md'. Prefer extending an existing references/ file over creating one; a skill with dozens of one-off references is the failure shape, not the goal.
  • Fix the skill in place when it is wrong: edit the sentence that misled, do not append 'UPDATE: actually...' underneath it.

Signals that warrant a skill update (any one is enough):
  • User corrected your style, tone, format, legibility, verbosity, or approach. Frustration is a FIRST-CLASS skill signal, not just a memory signal. 'stop doing X', 'don't format like this', 'I hate when you Y' — embed the lesson in the skill that governs that task so the next session starts fixed.
  • Non-trivial technique, fix, workaround, or debugging path emerged.
  • A skill that was loaded or consulted turned out wrong, missing, or outdated — patch it now.

Preference order for skills — pick the earliest that fits:
  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were loaded (the digest lists them: /skill-name, the Skill tool, or a Read of a SKILL.md). If one of them covers the learning, PATCH it first (Read it during this review — see Read-before-write below). It was in play; it's the right place, whoever wrote it. If it is read-only here, write the patch as a proposal instead.
  2. UPDATE AN EXISTING UMBRELLA (Glob the skill list + Read to find the right one). Patch it.
  3. ADD A SUPPORT FILE under an existing umbrella with Write. Three kinds: `references/<topic>.md` for topical depth (decision tables, recipes, quirks, condensed domain notes) — extend an existing topical file before creating one, never a per-session file; `templates/<name>.<ext>` for starter files meant to be copied and modified; `scripts/<name>.<ext>` for statically re-runnable actions (verification, fixture generators, probes). Add a one-line pointer in SKILL.md so future agents find them.
  4. CREATE A NEW CLASS-LEVEL UMBRELLA when nothing exists. Name at the class level — NOT a PR number, error string, codename, library-alone name, or 'fix-X / debug-Y' session artifact. If the name only fits today's task, fall back to (1), (2), or (3).

Read-before-write (ENFORCED — Edit refuses otherwise): before patching or editing an existing skill's SKILL.md, Read it during this review; before overwriting an EXISTING supporting file, Read that exact file. Content quoted earlier in the digest does NOT count — base the write on what Read just returned. New skills and NEW supporting files need no prior read. On a read-before-write refusal: Read the named target once, retry the write once, do not loop.

User-preference embedding: when the user complains about how you handled a task, update the skill that governs that task rather than memory. Memory says 'who the user is and what the current situation and state of your operations are'; skills say 'how to do this class of task for this user'. A user-preference lesson lives in exactly ONE place: the skill that governs the task when one exists, a `user`-tagged memory only for cross-cutting preferences no skill owns — never both. Duplicating it is how memory ends up restating SKILL.md.

If you notice overlapping existing skills, mention it — the background curator handles consolidation. Never delete a skill here.

Do NOT capture as skills (these become persistent self-imposed constraints that bite you later when the environment changes):
  • Environment-dependent failures: missing binaries, fresh-install errors, post-migration path mismatches, 'command not found', unconfigured credentials, uninstalled packages. The user can fix these — they are not durable rules.
  • Negative claims about tools or features ('browser tools do not work', 'X tool is broken', 'cannot use Y from execute_code'). These harden into refusals the agent cites against itself for months after the actual problem was fixed.
  • Session-specific transient errors that resolved before the conversation ended. If retrying worked, the lesson is the retry pattern, not the original failure.
  • One-off task narratives. A user asking 'summarize today's market' or 'analyze this PR' is not a class of work that warrants a skill.

  • Unresolved failures: if the session ended WITHOUT actually finding a working method — you tried several things, none worked, and told the user to check manually — do NOT write those attempts up as a 'reliable workflow' or 'recommended approach'. That presents an untested sequence of failures as validated guidance a future session will trust and repeat. Either say 'Nothing to save', or, only if you are independently confident of a real working alternative (not something you are merely guessing might work), capture ONLY that alternative — never the dead ends, and never dressed up as best practice.

If a tool failed because of setup state, capture the FIX (install command, config step, env var to set) under an existing setup or troubleshooting skill — never 'this tool does not work' as a standalone constraint.

Act on whichever of the two dimensions has real signal. If genuinely nothing stands out on either, say 'Nothing to save.' and stop — but don't reach for that conclusion as a default.
