# skill-manager

<!-- badges -->
[![CI](#)](#) [![License: MIT](#)](#) [![Python 3.8+](#)](#) [![Zero dependencies](#)](#)

**skill-manager** is a Claude Code skill plus a standard-library-only Python engine that gives agent assets — skills, subagents, commands, and workflows — three-tier activation: a dormant **library** (`~/.agents/library/`, zero context cost), an always-on **global** tier (`~/.claude/`), and a per-project tier (`<project>/.claude/`). You keep as many assets as you like in the library and activate only the ones each project actually needs, either conversationally ("set up skills for this project") or through an 11-command CLI.

> **Independent community tool.** skill-manager is not affiliated with or endorsed by Anthropic. It is built to the open SKILL.md specification ([agentskills.io](https://agentskills.io)).

## Why

Every skill and subagent you install globally adds its name and description to the context of **every** session, in every project. Hosts cap that listing to a fixed budget, and once you exceed it the descriptions are truncated to fit — auto-triggering silently degrades for *all* skills, and irrelevant entries become selection noise for the model. Claude Code has no built-in per-project activation to scope this (see [anthropics/claude-code#39749](https://github.com/anthropics/claude-code/issues/39749)). skill-manager fills that gap: an asset should cost context only where it is useful.

## Quickstart (30 seconds)

```bash
# 1. Install (see other channels below)
git clone https://github.com/OWNER/skill-manager
mkdir -p ~/.claude/skills
ln -s "$PWD/skill-manager/skills/skill-manager" ~/.claude/skills/skill-manager

# 2. In a Claude Code session, migrate what you already have:
#      "set up skill management" / "run skill-manager init"
#    ...then in any project:
#      "set up skills for this project"

# 3. Or drive the engine directly:
python3 skill-manager/skills/skill-manager/scripts/skillmgr.py scaffold
python3 skill-manager/skills/skill-manager/scripts/skillmgr.py status --kind all
```

New sessions in that project now see only the assets you activated; everything else stays dormant in the library, one `link` away.

## Installation

Replace `OWNER` with the repository owner's GitHub handle.

**1. Git clone (copy or symlink)**

```bash
git clone https://github.com/OWNER/skill-manager
mkdir -p ~/.claude/skills
# symlink (updates with git pull):
ln -s "$PWD/skill-manager/skills/skill-manager" ~/.claude/skills/skill-manager
# or copy:
cp -r skill-manager/skills/skill-manager ~/.claude/skills/
```

**2. Claude Code plugin**

```
/plugin marketplace add OWNER/skill-manager
/plugin install skill-manager
```

**3. skills CLI**

```bash
npx skills add OWNER/skill-manager
```

## The three-tier model

| Tier | Location | Context cost | Belongs here |
|------|----------|--------------|--------------|
| **Library** (dormant) | `~/.agents/library/<kind>/` | zero | Everything. Grows freely. |
| **Global** (always on) | `~/.claude/<kind>/` | every session | Universal tools only — keep it small (~8) |
| **Project** (scoped) | `<project>/.claude/<kind>/` | that project's sessions | Whatever the project's stack needs |

Four asset kinds are managed: `skills` (directories) and `agents`, `commands`, `workflows` (single files). Activation is presence: a link or tracked copy of a library asset placed in a tier directory.

### Why the library is not `~/.agents/skills/`

`~/.agents/skills/` is the vercel-labs `skills` CLI convention, and by mid-2026 it is auto-scanned by several agent CLIs — Gemini CLI, Cursor, Codex CLI, Zed, and others. A "dormant" library stored there would be silently fully active in those agents on a multi-agent machine. skill-manager's default library, `~/.agents/library/`, is scanned by nothing; it can be relocated via `~/.agents/skillmgr.json` (`library_root`) or the `SKILLMGR_LIBRARY_ROOT` environment variable.

## Usage

### Conversational (the skill drives the engine)

Once the skill is installed, ask Claude Code:

- "set up skills for this project" — detects the project's stack, proposes a matching set from your library in one exchange, links what you approve.
- "skill status" — every asset's tier, plus anything needing attention.
- "add / remove a skill here" — toggles a single asset in the current project.
- "run skill-manager init" — first-time migration: adopts your existing global assets into the library, then helps you choose a minimal always-on set.

### Direct CLI

```bash
python3 scripts/skillmgr.py <command> [--kind skills|agents|commands|workflows] [--json]
```

| Command | Purpose |
|---|---|
| `scaffold` | create the library layout and config file (idempotent) |
| `index [--check]` | rebuild the library index, or report staleness without writing |
| `status [--kind all] [--project DIR]` | show every asset's tier and problems needing attention |
| `detect [--project DIR]` | dump a project's stack signals (manifests, languages, deps, infra, git-ness) |
| `link NAME... --tier global\|project [--copy]` | activate assets (symlink → junction → tracked copy) |
| `unlink NAME... --tier ... [--force]` | deactivate; refuses foreign or modified content without `--force` |
| `adopt NAME --tier ... [--relink]` | move real tier content into the library, transactionally |
| `import SRC [--name N]` | copy an external asset into the library (strips embedded symlinks) |
| `sync --tier ... [--force]` | refresh drifted tracked copies from the library |
| `doctor [--fix] [--project DIR]` | find (and repair) broken links, orphans, corrupt config |
| `uninstall [--project DIR] [--all]` | remove managed activations — global tier by default, only `--project`'s tier with `--project`, both with `--all`; library untouched |

Every mutating command supports `--dry-run`.

### Link strategy ladder

Activation tries, in order:

1. **Symlink** — preferred everywhere it works.
2. **Windows directory junction** — no elevation or Developer Mode required; directories only, same volume.
3. **Tracked copy** — universal fallback, recorded in a per-tier manifest with a content hash so drift is detectable and repairable with `sync`.

On Windows without Developer Mode, symlink creation fails (WinError 1314); the engine falls back automatically — nothing to configure. Run `sync` occasionally to refresh tracked copies after library updates.

## Security posture

Claims below are invariants of the engine, verifiable by reading `scripts/skillmgr.py`:

- **Standard library only, zero dependencies** — Python 3.8+, nothing to `pip install`.
- **No network access** — no `urllib`, `socket`, or third-party HTTP.
- **No `subprocess`, `os.system`, `eval`/`exec`, or dynamic imports.**
- **No environment access** beyond `SKILLMGR_*` configuration overrides.
- **Path-traversal-validated names** — every asset name must be a single safe path component, and every computed destination is verified to remain inside its intended root.
- **Write scope is bounded** — only the library root, the Claude config dir, an explicitly supplied `--project` directory, and an explicit import source are ever touched.
- **Transactional `adopt`** — moving content into the library either completes or leaves the original in place.
- **Symlink-stripping `import`** — embedded symlinks in imported assets are removed.
- **Lockfile** guards concurrent mutating commands; **`--dry-run`** everywhere.
- **The library is append-only** from the tool's perspective: deactivation only removes links and tracked copies in tier directories, never library content.
- Unit tested; MIT licensed.

## Compatibility

- **Python**: 3.8+ (guarded at runtime).
- **OS**: Linux, macOS, Windows (with or without Developer Mode), WSL.
- **Surfaces**: filesystem assets load in Claude Code CLI, the desktop app, and IDE extensions. They do **not** load on claude.ai web or mobile — this tool manages the filesystem surfaces only.

## FAQ

**I already have a lot of skills installed. How do I migrate?**
Run the `init` workflow ("run skill-manager init"). Real directories and files in your global tier are moved into the library with `adopt --relink` (content moves, activation stays), assets installed elsewhere (e.g. by `npx skills`) are `import`-ed, and you pick a minimal always-on set. Nothing is deleted; everything demoted stays in the library, one `link` away.

**What about plugin packs from the marketplace?**
Don't move their files. Gate them per project with `enabledPlugins` in settings.json (keyed `plugin-name@marketplace`): disable globally in user settings, enable in one project's `.claude/settings.json` or `.claude/settings.local.json`. The pack's skills, agents, hooks, and MCP servers then exist only in that project.

**How do teams share project skills?**
Use `link --copy` so the assets are real files (not symlinks into one person's home directory), commit `.claude/` to the repository, and refresh copies from the library later with `sync`. The manifest's content hashes make local modifications visible before they are overwritten.

**What happens if I uninstall?**
`uninstall` removes managed activations — links and tracked copies in tier directories — and nothing else; the library and any unmanaged files are untouched. Scope is least-surprise: bare `uninstall` clears only the **global** tier, `uninstall --project DIR` clears only that project's tier, and `uninstall --all` (optionally with `--project`) clears both. Removing the skill-manager skill itself afterwards leaves your remaining activations working as plain files and symlinks.

**Does this work in enterprise / managed environments?**
With a caveat: admin policy (managed settings, MDM) can override or disable local skills, agents, and workflows entirely, and enterprise-scope assets take precedence over everything local. If a correctly linked asset does not load, managed policy is the first suspect — skill-manager cannot and should not try to defeat it.

## License

MIT
