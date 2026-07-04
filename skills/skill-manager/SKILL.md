---
name: skill-manager
description: >-
  Three-tier activation manager for agent assets (skills, subagents, commands,
  workflows): keep a large library dormant at zero context cost and activate only
  what each project needs. Use whenever the user wants to enable, disable, activate,
  or organize skills or agents; asks which skills are active or loaded; complains
  about context overhead, bloat, or token cost from installed skills; wants to
  install or import many skills or a skill pack without them loading everywhere;
  starts work in a project whose skills haven't been configured; or says things like
  "set up skills for this project", "skill status", "add/remove a skill here",
  "clean up my skills". Also use right after installing new skills to decide their tier.
license: MIT
metadata:
  version: "1.0.0"
---

# Skill Manager

Every skill and subagent installed globally adds its description to the context of
**every** session, in every project. The cost is bounded (agent hosts budget the
listing), but the bound is the trap: with many assets installed, descriptions get
truncated to fit, auto-triggering degrades for everything, and irrelevant entries
become selection noise. The fix is scoped activation — an asset should cost context
only where it's useful.

## The three-tier model

| Tier | Location | Context cost | Belongs here |
|------|----------|--------------|--------------|
| **Library** (dormant) | `~/.agents/library/<kind>/` | zero | Everything. Grows freely. |
| **Global** (always on) | `~/.claude/<kind>/` | every session | Universal tools only — keep under ~8 |
| **Project** (scoped) | `<project>/.claude/<kind>/` | that project's sessions | Whatever the project's stack needs |

Kinds: `skills` (directories), `agents`, `commands`, `workflows` (single files).
Activation = presence: a symlink where possible, a Windows junction, or a tracked
copy (hash-recorded in a manifest, so drift is detectable and `sync`-able).

The library deliberately lives at `~/.agents/library/`, **not** `~/.agents/skills/`
— several agent CLIs (Gemini, Cursor, Codex, Zed) now auto-scan `~/.agents/skills/`,
so assets stored there are not dormant on multi-agent machines.

## The engine

All mechanics go through `scripts/skillmgr.py` (Python 3.8+, stdlib only, no
network, no subprocess). Run commands with `--json` when you need to parse results:

```bash
python3 <skill-dir>/scripts/skillmgr.py <command> [--kind skills|agents|commands|workflows] [--json]
```

| Command | Purpose |
|---|---|
| `scaffold` | create library layout + config (idempotent) |
| `index` / `index --check` | rebuild the library index / report staleness |
| `status [--kind all] [--project DIR]` | every asset's tier + problems needing attention |
| `detect [--project DIR]` | stack signals: manifests, languages, deps, infra, git-ness |
| `link NAME... --tier global\|project [--copy]` | activate (symlink → junction → tracked copy) |
| `unlink NAME... --tier ... [--force]` | deactivate; refuses foreign/modified content without --force |
| `adopt NAME... --tier ... [--relink] [--force]` | move real content into the library, transactionally (batch OK; `--relink` keeps it active, plain adopt = adopt + demote in one step; refuses to drop embedded symlinks without --force) |
| `import SRC [--name N] [--kind ...]` | copy an external asset in (single files need a file kind; embedded symlinks are stripped) |
| `sync --tier ... [--force]` | refresh drifted tracked copies from the library |
| `doctor [--fix] [--project DIR]` | find/repair dangling links, orphans, drifted/modified copies, corrupt state |
| `uninstall [--project DIR] [--all]` | remove managed activations — global tier by default, only the project's with `--project`, both with `--all`; library untouched |

Every mutating command supports `--dry-run`. The engine handles mechanics; you
supply judgment: which assets matter here, and what to ask the user.

## Workflows

### `status` — what's active where

Run `status` (it auto-detects the project when the cwd has a `.claude/` dir;
`--project` overrides). Summarize by tier; surface the `attention` entries with
their built-in hints — including `copied-drifted` (library moved ahead of a copy;
`sync` fixes) and `copied-modified` (local edits; decide between `sync --force`
and keeping them). Offer `doctor --fix` for dangling links.

### `init` — first-time setup and migration

This is the critical path for users who already have many skills. Never lose data;
prefer `--dry-run` previews before mutating.

1. `scaffold`, then `status --kind all` to see the existing landscape.
2. **Existing global assets**: real directories/files get `adopt --tier global --relink`
   (content moves to the library, activation stays). Existing symlinks into some other
   store (e.g. `~/.agents/skills/` from the `npx skills` CLI): leave the store in place,
   `import` each asset into the library, then replace the old link via `unlink --force`
   + `link` — or, if the user prefers the npx-skills store as-is, manage only new
   assets and say so plainly.
3. **Interview**: propose a minimal always-on set (this skill, skill-creation and
   discovery tools, anything genuinely used in every project). One question, their
   context budget, their call.
4. Demote the rest (`unlink` from global). Remind: everything stays in the library,
   one `link` away; changes land in new sessions.
5. **Plugin packs** (marketplace installs): do not move their files. Gate them
   per-project with `enabledPlugins` in settings.json instead — see
   `references/mechanisms.md`.

### `setup` — configure a project

1. `index` (cheap — always refresh), `detect --project DIR`, then read
   `<library>/index.json`.
2. Match signals against asset descriptions across **all kinds**. Be honest: a
   Dockerfile alone doesn't justify kubernetes tooling. Drop candidates already
   active in the global tier. If the global tier is bloated (well over ~8), suggest
   `init`.
3. Bundle every decision into **one** exchange: a multi-select of proposed assets
   (include near-misses you rejected so the user can add them), plus — only if
   `detect` reported a git repo — personal vs team: personal = links + gitignore
   `.claude/` additions (say so before editing `.gitignore`); team = `link --copy`
   + commit, refreshed later with `sync`. Default personal.
4. `link` the chosen set with `--tier project`. Show final `status --project`.
5. Say when it lands: typically the next session; some builds hot-reload watched
   directories — check the live listing before promising either. Suggest verifying
   the assets appear (e.g. `/skills` or asking Claude); host loaders have had
   symlink-discovery regressions, and if one bites, `link --copy` is the fallback.

### `add` / `remove` — manual toggles

Map to `link` / `unlink`. Default tier: project when inside one, else global. On
name misses, check the index for near-matches; offer `import` for assets that exist
elsewhere on disk.

### `import` — grow the library

Local dir/file: `import SRC`. Repo: clone to a temp dir, `import` each asset,
delete the clone. Plugin packs: prefer per-project `enabledPlugins`; import
individual pieces only when the license permits.

## Safety rules

- The library is sacred: nothing in this skill ever deletes or edits library
  content. Deactivation removes links/tracked copies in tier directories only.
- Never `unlink --force` or `sync --force` without telling the user what will be
  discarded; prefer showing the `--dry-run` output first.
- Don't edit `.gitignore`, settings files, or anything outside tier directories
  without saying so first.
- If a command errors, read its message — the engine's errors are specific
  (traversal-safe names, case collisions, lock contention) and usually name the fix.
- In managed/enterprise environments, admin policy can override or block local
  assets entirely; if assets don't load despite correct linking, say so and point
  at `references/mechanisms.md`.

## Platform notes

Windows without Developer Mode can't create symlinks — the engine silently falls
back to junctions (directories) then tracked copies; nothing for you to do, but
mention `sync` for copy freshness. Filesystem assets only load in Claude Code
CLI/desktop/IDE surfaces, not claude.ai web or mobile. Deeper facts:
`references/mechanisms.md`.
