# Security Policy

This document covers **skill-manager v1.0.0** and its deterministic engine,
`skills/skill-manager/scripts/skillmgr.py`. The claims below are written to be
audited: every one of them can be diffed against the code.

## Supported versions

| Version | Supported |
|---------|-----------|
| 1.x     | Yes       |
| < 1.0   | No        |

## Reporting a vulnerability

Please report vulnerabilities **privately** via GitHub Security Advisories:

> https://github.com/forhadkhan/skill-manager/security/advisories/new

- Do not open public issues for security reports.
- We follow **90-day coordinated disclosure**: we aim to acknowledge within a
  few days, fix within the window, and credit reporters in the advisory unless
  they prefer otherwise. If a fix ships earlier, disclosure can happen earlier
  by mutual agreement.
- There is **no bug bounty** for this project.

## Threat model

skill-manager moves and links filesystem assets (skills, agents, commands,
workflows) between a dormant library and active tier directories. The engine
defends against the following, specifically:

### Path traversal via asset names (CWE-22)

Asset names are attacker-influenced (they come from import sources and CLI
arguments) and are joined onto trusted roots. Two layers apply:

1. **Single-component validation** — `validate_name()` accepts only
   `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`, rejects `.` / `..`, and additionally
   requires `name == Path(name).name`, so path separators and absolute paths
   can never survive.
2. **Parent confinement** — `confine(child, root)` asserts, as
   defense-in-depth, that every computed destination is a *direct child* of
   its intended root before any operation touches it.
3. **Untrusted manifest keys** — a tracked-copy tier directory carries a
   `.skillmgr-manifest.json` whose keys are joined onto tier/library roots by
   `sync`, `uninstall`, and `doctor`. Because that file can be committed to a
   repository and reach another machine, `load_manifest()` drops any key that
   is not a safe single component (`is_safe_component()`) at the single load
   chokepoint, and the join sites additionally apply `confine()`. A hand-edited
   `../../x` key is discarded with a note, never dereferenced.

### SkillSlip-class symlink planting via `import` / copy

A malicious skill directory could embed symlinks so that copying it either
reads outside the source tree (when links are followed) or replants links into
the destination (when links are recreated). The engine does **neither**:
`copy_tree_no_symlinks()` skips symlinks entirely — they are not followed, not
recreated, and never descended into when they are directories. Skipped links
are reported to the user by count. Additionally, `adopt` refuses sources that
are themselves symlinks, and hashing (`sha256_of`) ignores symlinked members.

### Destructive races and partial writes

- **Lockfile** — every command that mutates activation state (`link`, `unlink`,
  `adopt`, `import`, `sync`, `uninstall`, and `doctor --fix`) runs under a
  best-effort cross-platform lockfile (`<library>/.skillmgr.lock`); a concurrent
  run fails with a conflict exit code rather than interleaving. Read-only
  commands (`status`, `detect`, `index --check`) and idempotent ones (`scaffold`,
  `index`, which writes atomically) do not take the lock.
- **Atomic writes** — file and tree copies go through a temp file / temp
  directory in the destination's parent followed by `os.rename`, and JSON state
  (index, manifests, config) via temp + `os.replace`, so a crash never leaves a
  half-written entry in place of a real one.
- **Transactional `adopt` ordering** — when moving real content into the
  library the source (potentially the user's only copy) is **never removed
  before the library copy exists and is validated in place**. The order is:
  (1) copy source into a library temp, (2) atomic-rename into the library entry,
  (3) validate it is a well-formed asset (aborting and removing the copy if not,
  leaving the original untouched), then (4) remove the original — and, for
  `--relink`, replace it with a link back to the library entry. Any failure
  before step 4 leaves the original intact.

### Drift in copy mode

Where symlinks are unavailable, activation falls back to a tracked
copy recorded in a per-tier manifest (`.skillmgr-manifest.json`) with a
**sha256 content hash**. `status` detects drift (a copy that no longer matches
its library entry), and `sync` refreshes it; `unlink` refuses to discard
modified copies without `--force`.

## Out of scope

- **The content of skills you import.** This tool manages *placement* of
  assets; it does not scan skill instructions, descriptions, or bundled
  scripts for prompt injection or malicious guidance. Vet sources before
  importing. Dedicated scanners exist for this layer — e.g. Snyk agent-scan,
  NVIDIA SkillSpector, Socket, and the Gen Agent Trust Hub.
- **Host loader vulnerabilities.** How Claude Code (or any other agent host)
  discovers, parses, and executes activated assets is the host's security
  boundary, not this tool's.
- **Managed-environment policy bypass.** In managed/enterprise environments,
  admin policy may override or block local assets. The tool does not attempt
  to circumvent such policy and refusing to do so is intended behavior, not a
  bug.

## Design invariants (auditor checklist)

Each invariant is verifiable by a short read of
`skills/skill-manager/scripts/skillmgr.py` (a single file). The CI
`lint-hygiene` job proves invariants 1–2 mechanically with an **AST scan**
(`.github/workflows/ci.yml`) rather than a text grep — a naive grep would match
this document's own prose and the engine's security notes, so do not rely on
one:

1. **No network access.** No imports of `urllib`, `socket`, `http`, `ftplib`,
   `requests`, or any other network module. Verified by the CI AST scan's
   `BANNED_MODULES` check.
2. **No process execution or dynamic code.** No `subprocess`, `os.system`,
   `os.popen`, `eval`, `exec`, `compile`, `__import__`, `pickle`, `marshal`, or
   `ctypes`. Verified by the CI AST scan's `BANNED_MODULES` / `BANNED_CALLS`
   checks. There are no lazy or conditional imports.
3. **Environment access limited to `SKILLMGR_*`.** The only environment
   variables read via `os.environ` are `SKILLMGR_CONFIG`,
   `SKILLMGR_LIBRARY_ROOT`, and `SKILLMGR_CLAUDE_HOME` (all through
   `_env_path()`); `HOME`/`USERPROFILE` are read only indirectly via
   `Path.home()`. `grep -n 'os.environ' skillmgr.py` → only `_env_path()`.
4. **The library is append-only.** No code path deletes or edits library
   content; deactivation (`unlink`, `uninstall`) removes only links and
   tracked copies inside tier directories. `adopt` refuses to overwrite an
   existing library entry (conflict exit).
5. **Every mutating command supports `--dry-run`** (`scaffold`, `link`,
   `unlink`, `adopt`, `import`, `sync`, `doctor --fix`, `uninstall`), and each
   checks `args.dry_run` before mutating.

Supporting facts: writes/reads are limited to the library root, the Claude
config dir (`~/.claude` or override), an explicitly supplied `--project`
directory, and an explicitly supplied import source; Python 3.8+ is a guarded
runtime floor.

## Supply chain

- **Zero dependencies** — Python standard library only; there is no
  `requirements.txt` and nothing to typosquat.
- **Single-file engine** — the entire mutating surface is one reviewable
  script, `skillmgr.py`.
- **No install hooks** — nothing runs at install time; the skill is inert
  files until a user invokes the script.
- **Tests and CI** — the test suite runs in CI on every change.
- **Releases** — releases are git-tagged; verify a checkout against the tag
  and hash the engine file (`sha256sum skillmgr.py`) against the release
  notes.
