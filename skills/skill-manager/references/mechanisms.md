# Platform mechanics behind the three-tier model

Facts verified against official documentation and live installs (July 2026).
Anything marked *unconfirmed* should be re-verified before relying on it — agent
hosts move fast.

## How assets cost context

- **Skills**: name + description load at session start for every installed skill;
  the body loads only on trigger. Claude Code budgets the listing (~1% of the
  context window; ~1,536 chars per description via `skillListingMaxDescChars`).
  Overflow means truncation — degraded triggering for every skill, silently.
- **Subagents**: names + descriptions also load every session (delegation
  decisions need them); full definitions load on invocation. Many installed agents
  create the same bloat problem as skills. [docs: sub-agents.md]
- **Workflows**: NOT listed in context; loaded only when invoked by name. Managing
  them is organizational, not a token optimization. [docs: workflows.md]
- **Commands** (`.claude/commands/`): still first-class, listed like skills.
- Invoked skill content persists in-session; compaction carries it with a ~5k/25k
  token budget. [docs: skills.md#skill-content-lifecycle]

## Scoping primitives (all confirmed in docs)

- Project assets (`<project>/.claude/{skills,agents,commands,workflows}/`) load
  only in that project (after workspace trust). Project overrides personal
  overrides bundled for same names; monorepos discover nested `.claude/` dirs,
  closest to cwd wins.
- Personal assets (`~/.claude/...`) load everywhere.
- Symlinked entries are documented as supported ("can be a symlink to a directory
  elsewhere"), BUT there are real regressions on record: user-level skills not
  loading when `~/.claude/skills` is *itself* a symlink (anthropics/claude-code
  #38051) and `/skills` not listing symlinked entries even when the model can use
  them (#14836). Consequences for this tool: link individual entries, never the
  whole tier directory; verify after linking; tracked copies are the reliable
  fallback (`link --copy`).
- The listing is normally fixed at session start; recent builds hot-reload watched
  directories. Verify rather than promise.

## Why the library is NOT ~/.agents/skills

`~/.agents/skills/` is the vercel-labs `skills` CLI convention — and by mid-2026
it is auto-scanned by Gemini CLI (documented alias of `~/.gemini/skills/`),
Cursor, Codex CLI (repo-tree scan), Zed, Warp, Amp and others. A "dormant"
library there is silently fully active in those agents. The default library
(`~/.agents/library/`) is scanned by nothing. Users can relocate it via
`~/.agents/skillmgr.json` (`library_root`) or the `SKILLMGR_LIBRARY_ROOT` env var.

Interop: `npx skills add` installs into `~/.agents/skills/` and symlinks into
agent dirs. Those installs can be `import`-ed into the library; the init workflow
covers replacing their links. SKILL.md itself is an open standard (agentskills.io,
Anthropic, Dec 2025) — keep frontmatter to core fields (name, description,
license, metadata) for cross-agent portability.

## Windows

Symlink creation needs Administrator or Developer Mode (WinError 1314 otherwise).
The engine's ladder: symlink → directory junction (`_winapi.CreateJunction`, no
elevation, dirs only, same volume) → tracked copy with content hash. Ecosystem
tools behave the same way (vercel CLI records `symlinkFailed: true` and copies).
Copies drift when the library updates — that's what the manifest + `sync` exist for.

## Plugin packs (marketplace installs)

- `enabledPlugins` (settings.json, keyed `plugin-name@marketplace`) merges across
  scopes: user < project (`.claude/settings.json`) < local
  (`.claude/settings.local.json`). Disabled globally + enabled in one project's
  local settings = the pack's skills, agents, hooks, and MCP servers exist only
  there. Confirmed in docs. This is the right lever for 50-skill packs — don't
  move their files.
- Plugin skills are namespaced `plugin-name:skill-name`. The plugin cache disk
  location is undocumented; discover it at runtime (look under `~/.claude/plugins/`)
  before attempting per-skill imports, and respect the pack's license.

## Managed / enterprise environments

Admin policy (managed settings, MDM, server-managed) can override or disable
local skills, agents, workflows, and permission rules; enterprise-scope assets
take precedence over everything local. On third-party providers (Bedrock/Vertex/
Foundry), server-managed settings don't apply but endpoint-managed ones do. If a
correctly-linked asset doesn't load, this is the first suspect — the tool cannot
and should not try to defeat policy.

## Surfaces

Filesystem assets load in Claude Code CLI, the desktop app, and IDE extensions.
They do NOT load on claude.ai web, mobile, or Slack. This tool manages the
filesystem surfaces only.

## Known gaps upstream

No built-in automatic per-project activation exists (anthropics/claude-code
#39749; vercel-labs/skills #634). Adjacent tools: `npx skills` (install/wiring),
skills-radar (MCP lazy search), two small skill-managers (move-to-backup;
CLAUDE.md injection). The tiering + drift-tracked activation model here fills the
gap none of them cover.
