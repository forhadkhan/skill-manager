#!/usr/bin/env python3
"""skillmgr — deterministic engine for the skill-manager skill.

Manages per-scope activation of agent assets (skills, agents, commands, workflows)
using a three-tier model:

  library   <library-root>/<kind>/<name>       canonical store, dormant, zero context cost
  global    ~/.claude/<kind>/<name>            active in every session
  project   <project>/.claude/<kind>/<name>    active only in that project

Activation is presence: a symlink (preferred) or, where symlinks are unavailable
(e.g. Windows without Developer Mode), a tracked copy recorded in a per-tier
manifest with a content hash so drift can be detected and synced.

SECURITY MODEL (invariants auditors can verify):
  * No network access of any kind (no urllib/socket/requests).
  * No subprocess, no os.system, no eval/exec, no dynamic imports.
  * No environment variables are read except SKILLMGR_* configuration overrides.
  * Never reads or writes outside: the library root, the Claude config dir
    (~/.claude or override), an explicitly supplied --project directory, and an
    explicitly supplied import source.
  * Every asset name is validated as a single safe path component, and every
    computed destination is verified to remain inside its intended root.
  * Mutating commands are guarded by a lockfile and support --dry-run.
  * The library is append-only from this tool's perspective: deactivation only
    ever removes links/tracked copies in tier directories, never library content.

Python 3.8+ (declared floor, guarded at runtime). Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

if sys.version_info < (3, 8):  # pragma: no cover
    sys.stderr.write("skillmgr requires Python 3.8 or newer\n")
    sys.exit(1)

SCHEMA_VERSION = 1
MANIFEST_NAME = ".skillmgr-manifest.json"

# Exit codes: 0 ok, 1 operational error, 2 usage error (argparse), 3 not found,
# 4 conflict (already exists / busy), 5 unsupported on this platform.
EXIT_OK, EXIT_ERROR, EXIT_NOTFOUND, EXIT_CONFLICT, EXIT_UNSUPPORTED = 0, 1, 3, 4, 5

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Windows reserved device names: harmless on POSIX but poison a library that is
# later synced to or used on Windows, so they are rejected everywhere.
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *("com%d" % i for i in range(1, 10)), *("lpt%d" % i for i in range(1, 10)),
}

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _env_path(var: str) -> "Path | None":
    val = os.environ.get(var, "").strip()
    return Path(val).expanduser() if val else None


def config_path() -> Path:
    return _env_path("SKILLMGR_CONFIG") or (Path.home() / ".agents" / "skillmgr.json")


def load_config() -> dict:
    path = config_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, OSError):
            pass  # a corrupt config falls back to defaults; `doctor` reports it
    return {}


def library_root() -> Path:
    env = _env_path("SKILLMGR_LIBRARY_ROOT")
    if env:
        return env
    cfg = load_config().get("library_root")
    if isinstance(cfg, str) and cfg.strip():
        return Path(cfg).expanduser()
    return Path.home() / ".agents" / "library"


def claude_home() -> Path:
    return _env_path("SKILLMGR_CLAUDE_HOME") or (Path.home() / ".claude")


# ---------------------------------------------------------------------------
# Asset kinds
# ---------------------------------------------------------------------------


class Kind:
    """One managed asset type. entity is 'dir' (skills) or 'file' (the rest)."""

    def __init__(self, name: str, entity: str, exts: tuple = ()) -> None:
        self.name = name
        self.entity = entity
        self.exts = exts

    def lib_dir(self) -> Path:
        return library_root() / self.name

    def tier_dir(self, tier: str, project: "Path | None") -> Path:
        if tier == "global":
            return claude_home() / self.name
        assert project is not None
        return project / ".claude" / self.name

    def is_valid_entry(self, path: Path) -> bool:
        if self.entity == "dir":
            return path.is_dir() and (path / "SKILL.md").is_file()
        return path.is_file() and (not self.exts or path.suffix in self.exts)


KINDS = {
    "skills": Kind("skills", "dir"),
    "agents": Kind("agents", "file", (".md",)),
    "commands": Kind("commands", "file", (".md",)),
    "workflows": Kind("workflows", "file", (".js", ".md")),
}


# ---------------------------------------------------------------------------
# Safety primitives
# ---------------------------------------------------------------------------


def safe_display(text: str) -> str:
    """Neutralize control characters (ANSI escapes, newlines, NUL) before they
    reach a terminal in human output. Symlink targets and asset descriptions are
    attacker-controllable, so a crafted target like '\\x1b[2J' must not execute."""
    return "".join(ch if ch == "\t" or (ch.isprintable() and ch not in "\x1b")
                   else "�" for ch in text)


class CmdError(Exception):
    def __init__(self, message: str, code: int = EXIT_ERROR) -> None:
        super().__init__(message)
        self.code = code


def validate_name(name: str) -> str:
    """Reject anything that is not a single, safe path component.

    This is the primary defense against path traversal (CWE-22): names are
    joined onto trusted roots, so '..'-style or absolute names would otherwise
    escape every tier and the library itself.
    """
    if not NAME_RE.match(name) or name in (".", "..") or name != Path(name).name:
        raise CmdError(
            "invalid name %r: use letters, digits, '.', '_', '-' (max 64 chars, "
            "no path separators)" % name,
            EXIT_ERROR,
        )
    # Windows silently strips trailing dots/spaces, so 'foo.' and 'foo' would
    # collide there; reject to keep a name portable and manifest-consistent.
    if name != name.rstrip(". "):
        raise CmdError("invalid name %r: trailing dot or space" % name, EXIT_ERROR)
    if name.split(".")[0].lower() in WINDOWS_RESERVED:
        raise CmdError("invalid name %r: reserved device name on Windows" % name, EXIT_ERROR)
    return name


def is_safe_component(name: str) -> bool:
    """True iff name is a single, traversal-free path component.

    Looser than validate_name (any filename characters allowed) but sufficient
    to prove a name cannot escape its directory when joined. Used to sanitize
    manifest keys, which are attacker-controllable when a tracked-copy tier
    directory (with its .skillmgr-manifest.json) is committed to a repo and
    later cloned and `sync`-ed.
    """
    if not name or name in (".", "..") or "\x00" in name:
        return False
    seps = [os.sep] + ([os.altsep] if os.altsep else [])
    return name == os.path.basename(name) and not any(s in name for s in seps)


def confine(child: Path, root: Path) -> Path:
    """Defense-in-depth: assert that child is a direct entry of root."""
    if child.parent != root:
        raise CmdError("refusing to operate outside %s" % root, EXIT_ERROR)
    return child


def resolve_entry(kind: Kind, name: str) -> Path:
    """Find a library entry by name; for file kinds the extension is optional.

    A bare stem that matches more than one file (e.g. nightly.js AND nightly.md
    in workflows) is ambiguous — refusing beats silently picking one.
    """
    base = kind.lib_dir()
    candidate = confine(base / name, base)
    if kind.entity == "dir":
        return candidate
    if candidate.exists() or candidate.is_symlink():
        return candidate
    matches = []
    for ext in kind.exts:
        alt = confine(base / (name + ext), base)
        if alt.exists():
            matches.append(alt)
    if len(matches) > 1:
        raise CmdError(
            "'%s' is ambiguous in %s: %s — use the full filename"
            % (name, kind.name, ", ".join(m.name for m in matches)),
            EXIT_CONFLICT,
        )
    return matches[0] if matches else candidate


def one_hop_target(link: Path) -> "Path | None":
    """The immediate (textual) symlink target, resolved against the link's dir.

    Classification is deliberately one-hop: an entry is 'managed' when it points
    at the library, even if the library entry is itself a symlink elsewhere
    (e.g. into a git checkout). Full resolve() would misclassify that chain.
    """
    try:
        raw = os.readlink(str(link))
    except OSError:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = link.parent / target
    try:
        return Path(os.path.normpath(str(target)))
    except (OSError, ValueError):
        return None


def is_managed_link(entry: Path, kind: Kind) -> bool:
    """True iff `entry` is a symlink that resolves to this tool's same-name
    library entry.

    Identity is decided with os.path.samefile (device + inode / Windows file
    index), not by comparing target path strings. String comparison is fragile
    on Windows — the same location can surface as an 8.3 short name
    (RUNNER~1), a `\\\\?\\`-prefixed extended path, or differing case — whereas
    samefile compares the underlying file object directly. It also transparently
    handles a library entry that is itself a symlink (both sides resolve to the
    same final inode). A broken link (target missing) raises inside samefile and
    is reported as unmanaged here; classify_entry routes it to `broken-link`
    based on existence before this flag matters."""
    if not entry.is_symlink():
        return False
    lib_entry = kind.lib_dir() / entry.name
    try:
        return entry.exists() and lib_entry.exists() and \
            os.path.samefile(str(entry), str(lib_entry))
    except OSError:
        return False


def remove_activation(path: Path) -> None:
    """Remove a tier activation (link or tracked copy) without ever deleting
    through it. A directory symlink needs `rmdir` on Windows (not `unlink`);
    only a real directory is tree-removed."""
    if path.is_symlink():
        try:
            path.unlink()
        except (OSError, ValueError):
            os.rmdir(str(path))  # Windows directory symlink
    elif path.is_dir():
        shutil.rmtree(str(path))
    else:
        path.unlink()


def _hash_file_into(h, path: Path) -> None:
    with open(str(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)


def sha256_of(path: Path) -> str:
    """Stable content hash of a file or directory tree (names + bytes), streamed."""
    h = hashlib.sha256()
    if path.is_file() and not path.is_symlink():
        h.update(path.name.encode("utf-8", "replace"))
        _hash_file_into(h, path)
        return h.hexdigest()
    for member in sorted(path.rglob("*")):
        rel = str(member.relative_to(path))
        h.update(rel.encode("utf-8", "replace"))
        if member.is_file() and not member.is_symlink():
            _hash_file_into(h, member)
    return h.hexdigest()


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=True)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Lock:
    """Best-effort cross-platform lockfile around mutating commands."""

    STALE_SECONDS = 600

    def __init__(self) -> None:
        self.path = library_root() / ".skillmgr.lock"
        self.acquired = False

    def __enter__(self) -> "Lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w") as fh:
                    fh.write(str(os.getpid()))
                self.acquired = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    continue  # lock vanished between attempts; retry
                if age > self.STALE_SECONDS:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                raise CmdError(
                    "another skillmgr operation is in progress (lock: %s)" % self.path,
                    EXIT_CONFLICT,
                )
        raise CmdError("could not acquire lock at %s" % self.path, EXIT_CONFLICT)

    def __exit__(self, *exc) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Manifest (tracked copies per tier directory)
# ---------------------------------------------------------------------------


def manifest_path(tier_dir: Path) -> Path:
    return tier_dir / MANIFEST_NAME


def load_manifest(tier_dir: Path) -> dict:
    path = manifest_path(tier_dir)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("entries"), dict):
                # SECURITY: manifest keys are joined onto tier/library roots by
                # sync/uninstall/doctor/classify. A committed, hand-edited
                # manifest could carry a '../../x' key, so any unsafe key is
                # dropped here — the single chokepoint every consumer flows
                # through — before it can reach a filesystem join.
                safe, dropped = {}, []
                for k, v in data["entries"].items():
                    if is_safe_component(k):
                        safe[k] = v
                    else:
                        dropped.append(k)
                if dropped:
                    note("ignored %d unsafe manifest key(s) in %s: %s"
                         % (len(dropped), path.name, dropped[:5]))
                data["entries"] = safe
                return data
        except (json.JSONDecodeError, OSError):
            pass
        # A manifest that exists but cannot be parsed holds real tracking data.
        # Preserve it under a .corrupt name instead of letting the next
        # save_manifest silently overwrite or delete it.
        backup = path.with_name(path.name + ".corrupt-%d" % int(time.time()))
        try:
            os.replace(str(path), str(backup))
            note("manifest %s was unreadable — preserved as %s" % (path, backup.name))
        except OSError:
            pass
    return {"version": SCHEMA_VERSION, "entries": {}}


def save_manifest(tier_dir: Path, manifest: dict) -> None:
    if manifest["entries"]:
        atomic_write_json(manifest_path(tier_dir), manifest)
    else:
        try:
            manifest_path(tier_dir).unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Frontmatter / metadata parsing (skills + agents/commands markdown)
# ---------------------------------------------------------------------------


def parse_frontmatter(md_file: Path) -> dict:
    """Tolerant YAML-frontmatter reader; no external YAML dependency.

    Handles: UTF-8 BOM, CRLF, quoted scalars, folded/literal blocks (>, |, >-,
    |-), and indented continuation lines. Inside a block scalar, lines are
    consumed as content until dedent to column 0 with a new 'key:' pattern is
    impossible — i.e. until the closing delimiter — so wrapped descriptions
    containing colons parse correctly.
    """
    try:
        # Only the leading frontmatter block is needed; cap the read so a huge
        # (or hostile) file cannot balloon memory during index/status.
        with open(str(md_file), "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read(65536)
    except OSError:
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fm: dict = {}
    key = None
    in_block = False
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        starts_new_key = (
            not in_block
            and line[:1] not in (" ", "\t")
            and ":" in line
            and NAME_RE.match(line.split(":", 1)[0].strip() or " ") is not None
        )
        if in_block and line[:1] not in (" ", "\t") and line.strip():
            # Block scalars end at dedent; a dedented 'key: value' starts a new key.
            in_block = False
            starts_new_key = ":" in line
        if starts_new_key:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            in_block = val in (">", ">-", ">+", "|", "|-", "|+")
            fm[key] = "" if in_block else val.strip("'\"")
        elif key and line.strip():
            fm[key] = (fm.get(key, "") + " " + line.strip()).strip()
    return fm


def describe_entry(kind: Kind, path: Path) -> dict:
    entry = {"name": path.stem if kind.entity == "file" else path.name, "file": path.name}
    meta_source = path / "SKILL.md" if kind.entity == "dir" else path
    if meta_source.suffix == ".md" or kind.entity == "dir":
        fm = parse_frontmatter(meta_source)
        entry["description"] = (fm.get("description") or "")[:500]
        if fm.get("name"):
            entry["name"] = fm["name"]
    elif path.suffix == ".js":
        try:
            with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(2000)
            m = re.search(r"description\s*:\s*['\"]([^'\"]{1,300})", head)
            entry["description"] = m.group(1) if m else ""
            n = re.search(r"\bname\s*:\s*['\"]([^'\"]{1,64})", head)
            if n:
                entry["name"] = n.group(1)  # meta name overrides stem, like .md frontmatter
        except OSError:
            entry["description"] = ""
    return entry


# ---------------------------------------------------------------------------
# Library enumeration + index
# ---------------------------------------------------------------------------


def library_entries(kind: Kind) -> list:
    base = kind.lib_dir()
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.iterdir()):
        if p.name.startswith("."):
            continue
        if any("\udc80" <= ch <= "\udcff" for ch in p.name):
            continue  # undecodable (non-UTF-8) name; doctor reports these
        if kind.is_valid_entry(p if not p.is_symlink() else p):
            out.append(p)
    return out


def index_path() -> Path:
    return library_root() / "index.json"


def entry_mtime(kind: Kind, p: Path) -> int:
    """Staleness signal. Editing <skill>/SKILL.md does not touch the directory
    mtime, so dir kinds fold the metadata file's mtime in."""
    try:
        m = p.stat().st_mtime
        if kind.entity == "dir":
            meta = p / "SKILL.md"
            if meta.is_file():
                m = max(m, meta.stat().st_mtime)
        return int(m)
    except OSError:
        return -1


def build_index() -> dict:
    kinds_out = {}
    for kname, kind in KINDS.items():
        entries = []
        for p in library_entries(kind):
            entry = describe_entry(kind, p)
            entry["mtime"] = entry_mtime(kind, p)
            entries.append(entry)
        kinds_out[kname] = entries
    return {
        "version": SCHEMA_VERSION,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "library_root": str(library_root()),
        "kinds": kinds_out,
    }


def index_is_stale(index: dict) -> bool:
    if index.get("version") != SCHEMA_VERSION or index.get("library_root") != str(library_root()):
        return True
    for kname, kind in KINDS.items():
        current = {p.name: entry_mtime(kind, p) for p in library_entries(kind)}
        cached = {e.get("file"): e.get("mtime") for e in index.get("kinds", {}).get(kname, [])}
        if current != cached:
            return True
    return False


# ---------------------------------------------------------------------------
# Link / copy creation
# ---------------------------------------------------------------------------


def _try_symlink(src: Path, dest: Path, tier: str) -> "str | None":
    if tier == "global":
        target = os.path.relpath(str(src), str(dest.parent))
    else:
        target = str(src)
    try:
        os.symlink(target, str(dest), target_is_directory=src.is_dir())
        return "symlink"
    except (OSError, NotImplementedError):
        return None


def _copy_entry(src: Path, dest: Path) -> None:
    """Copy src to dest atomically-ish (temp + rename), never preserving symlinks.

    Symlinks inside a source tree are skipped, not followed and not recreated:
    following them could read outside the tree, and recreating them replants
    the "SkillSlip" attack class into tier/library directories.
    """
    if src.is_file():
        fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=dest.name + ".")
        os.close(fd)
        try:
            shutil.copyfile(str(src), tmp)
            os.replace(tmp, str(dest))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return
    tmp_dir = tempfile.mkdtemp(dir=str(dest.parent), prefix=dest.name + ".")
    try:
        skipped = copy_tree_no_symlinks(src, Path(tmp_dir) / "x")
        os.rename(str(Path(tmp_dir) / "x"), str(dest))
        if skipped:
            note("skipped %d symlink(s) inside %s (not copied, by policy)" % (len(skipped), src))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def copy_tree_no_symlinks(src: Path, dest: Path) -> list:
    skipped = []
    dest.mkdir(parents=True, exist_ok=False)
    for root, dirnames, filenames in os.walk(str(src)):
        rel = os.path.relpath(root, str(src))
        # never descend into symlinked directories
        pruned = []
        for d in list(dirnames):
            if os.path.islink(os.path.join(root, d)):
                skipped.append(os.path.join(rel, d))
            else:
                pruned.append(d)
        dirnames[:] = pruned
        target_root = dest if rel == "." else dest / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for f in filenames:
            member = os.path.join(root, f)
            if os.path.islink(member):
                skipped.append(os.path.join(rel, f))
                continue
            shutil.copyfile(member, str(target_root / f))
    return skipped


# ---------------------------------------------------------------------------
# Output plumbing
# ---------------------------------------------------------------------------

_JSON_MODE = False
_NOTES: list = []


def note(msg: str) -> None:
    if _JSON_MODE:
        _NOTES.append(msg)
    else:
        print(msg)


def emit(result: dict) -> int:
    if _JSON_MODE:
        result.setdefault("ok", True)
        if _NOTES:
            result["notes"] = list(_NOTES)
        print(json.dumps(result, indent=2, ensure_ascii=True))
    return EXIT_OK


def fail(err: CmdError) -> int:
    if _JSON_MODE:
        print(json.dumps({"ok": False, "error": str(err), "code": err.code}, ensure_ascii=True))
    else:
        sys.stderr.write("error: %s\n" % err)
    return err.code


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def project_dir(args) -> Path:
    raw = Path(args.project).expanduser() if getattr(args, "project", None) else Path.cwd()
    if not raw.is_dir():
        raise CmdError("project directory does not exist: %s" % raw, EXIT_NOTFOUND)
    return raw


def get_kind(args) -> Kind:
    return KINDS[args.kind]


def cmd_scaffold(args) -> int:
    created = []
    for kind in KINDS.values():
        d = kind.lib_dir()
        if not d.is_dir():
            if not args.dry_run:
                d.mkdir(parents=True, exist_ok=True)
            created.append(str(d))
    cfg = config_path()
    if not cfg.is_file():
        if not args.dry_run:
            atomic_write_json(cfg, {"version": SCHEMA_VERSION, "library_root": str(library_root())})
        created.append(str(cfg))
    note("library ready at %s" % library_root() + (" (dry-run)" if args.dry_run else ""))
    return emit({"action": "scaffold", "created": created, "dry_run": args.dry_run})


def cmd_index(args) -> int:
    index = build_index()
    if getattr(args, "check", False):
        try:
            cached = json.loads(index_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            cached = {}
        stale = index_is_stale(cached)
        note("index is %s" % ("STALE" if stale else "fresh"))
        return emit({"action": "index-check", "stale": stale})
    if not args.dry_run:
        atomic_write_json(index_path(), index)
    counts = {k: len(v) for k, v in index["kinds"].items()}
    note("indexed %s -> %s" % (", ".join("%d %s" % (n, k) for k, n in counts.items()), index_path()))
    return emit({"action": "index", "counts": counts, "path": str(index_path())})


def classify_entry(entry: Path, kind: Kind, manifest: dict) -> dict:
    """Classify one tier-directory entry. Returns {name, file, state, ...}."""
    name = entry.stem if kind.entity == "file" and not entry.is_dir() else entry.name
    base = {"name": name, "file": entry.name}
    m = manifest["entries"].get(entry.name)
    if entry.is_symlink():
        target = one_hop_target(entry)
        base["target"] = str(target) if target else "?"
        if not entry.exists():
            # Dangling links can never load, wherever they point — all broken.
            base.update(state="broken-link", managed=is_managed_link(entry, kind))
            return base
        if is_managed_link(entry, kind):
            base.update(state="linked", mode="symlink")
            return base
        base.update(state="foreign-link")
        return base
    if m and m.get("mode") == "copy":
        state = "copied"
        lib_entry = kind.lib_dir() / entry.name  # manifest keys are full filenames
        if m.get("hash") and sha256_of(entry) != m["hash"]:
            state = "copied-modified"  # local edits trump library drift
        elif not lib_entry.exists():
            state = "copied-orphan"
        elif m.get("hash") and sha256_of(lib_entry) != m["hash"]:
            state = "copied-drifted"
        base.update(state=state, mode="copy")
        return base
    base.update(state="unmanaged", adoptable=kind.is_valid_entry(entry))
    return base


def tier_report(kind: Kind, tier_dir: Path) -> list:
    if not tier_dir.is_dir():
        return []
    manifest = load_manifest(tier_dir)
    out = []
    for p in sorted(tier_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_symlink() or kind.is_valid_entry(p) or p.is_dir() or p.is_file():
            out.append(classify_entry(p, kind, manifest))
    return out


ACTIVE_STATES = ("linked", "copied", "copied-drifted", "copied-modified")
ATTENTION_STATES = ("broken-link", "foreign-link", "unmanaged", "copied-orphan",
                    "copied-drifted", "copied-modified")


def default_project() -> "Path | None":
    """Use the cwd as the project when it visibly is one (has a .claude dir)."""
    cwd = Path.cwd()
    return cwd if (cwd / ".claude").is_dir() and cwd != Path.home() else None


def _attention_hint(e: dict) -> str:
    state = e["state"]
    if state == "broken-link":
        return "dangling link (target %s missing) — `doctor --fix` removes it" % e.get("target", "?")
    if state == "foreign-link":
        return "points outside the library (%s) — inspect manually" % e.get("target", "?")
    if state == "unmanaged":
        return ("real files, not library-backed — `adopt` moves them in"
                if e.get("adoptable")
                else "not a recognized asset for this kind — leave it or remove manually")
    if state == "copied-orphan":
        return "copy whose library source is gone — keep or unlink"
    if state == "copied-drifted":
        return "library changed since this copy — `sync` refreshes it"
    return "local edits in this copy — `sync --force` overwrites, `unlink --force` discards"


def cmd_status(args) -> int:
    kinds = list(KINDS) if args.kind == "all" else [args.kind]
    project = None
    if getattr(args, "project", None):
        project = project_dir(args)
    else:
        project = default_project()
        if project and not _JSON_MODE:
            print("(project tier: %s — pass --project to override)\n" % project)
    report = {}
    total_assets = 0
    for kname in kinds:
        kind = KINDS[kname]
        lib = library_entries(kind)
        stem_counts = {}
        for p in lib:
            stem_counts[p.stem] = stem_counts.get(p.stem, 0) + 1
        g = {e["file"]: e for e in tier_report(kind, kind.tier_dir("global", None))}
        pr = {e["file"]: e for e in tier_report(kind, kind.tier_dir("project", project))} if project else {}
        rows = []
        for entry in lib:
            # Join tiers by full filename (always unique); display the stem
            # unless two file-kind assets share it (nightly.js vs nightly.md).
            display = entry.name if kind.entity == "dir" or stem_counts[entry.stem] > 1 else entry.stem
            tiers = []
            for label, side in (("global", g), ("project", pr)):
                e = side.get(entry.name)
                if e and e["state"] in ACTIVE_STATES:
                    suffix = "" if e["state"] == "linked" else " (%s)" % e["state"]
                    tiers.append(label + suffix)
            rows.append({"name": display, "file": entry.name, "tier": " + ".join(tiers) or "dormant"})
        problems = [e for e in list(g.values()) + list(pr.values()) if e["state"] in ATTENTION_STATES]
        report[kname] = {"assets": rows, "attention": problems}
        total_assets += len(rows)
        if not _JSON_MODE and (rows or problems):
            print("[%s]" % kname)
            width = max([len(r["name"]) for r in rows] + [len(e["name"]) for e in problems] + [8]) + 2
            for r in rows:
                print("  %-*s%s" % (width, safe_display(r["name"]), r["tier"]))
            for e in problems:
                print("  ! %-*s%s: %s" % (width, safe_display(e["name"]), e["state"],
                                          safe_display(_attention_hint(e))))
            print()
    if total_assets == 0 and not _JSON_MODE:
        print("library is empty at %s — run `scaffold` then `import` assets to get started" % library_root())
    return emit({"action": "status", "library_root": str(library_root()),
                 "project": str(project) if project else None, "report": report})


def _activate(kind: Kind, name: str, tier: str, project: "Path | None", args) -> dict:
    src = resolve_entry(kind, name)
    if not kind.is_valid_entry(src if not src.is_symlink() else Path(os.path.realpath(str(src)))):
        raise CmdError("'%s' is not in the library (%s)" % (name, kind.lib_dir()), EXIT_NOTFOUND)
    tier_root = kind.tier_dir(tier, project)
    dest = confine(tier_root / src.name, tier_root)

    # Case-insensitive collision guard (macOS/Windows default filesystems).
    if tier_root.is_dir():
        lowered = {p.name.casefold(): p.name for p in tier_root.iterdir()}
        clash = lowered.get(src.name.casefold())
        if clash and clash != src.name:
            raise CmdError(
                "case-insensitive collision: '%s' already exists in %s" % (clash, tier_root),
                EXIT_CONFLICT,
            )

    if dest.is_symlink():
        if is_managed_link(dest, kind) and dest.exists():
            note("'%s' already active in %s tier" % (name, tier))
            return {"name": name, "mode": "existing", "dest": str(dest)}
        raise CmdError(
            "%s exists but is a %s — run `doctor` for repair options"
            % (dest, "broken link" if not dest.exists() else "foreign link"),
            EXIT_CONFLICT,
        )
    if dest.exists():
        manifest = load_manifest(tier_root)
        if dest.name in manifest["entries"]:
            note("'%s' already active in %s tier (tracked copy)" % (name, tier))
            return {"name": name, "mode": "existing-copy", "dest": str(dest)}
        raise CmdError("%s already exists and is not managed by skillmgr" % dest, EXIT_CONFLICT)

    if args.dry_run:
        note("would activate '%s' in %s tier at %s" % (name, tier, dest))
        return {"name": name, "mode": "dry-run", "dest": str(dest)}

    tier_root.mkdir(parents=True, exist_ok=True)
    mode = None
    if not args.copy:
        mode = _try_symlink(src, dest, tier)
    if mode is None:
        _copy_entry(src, dest)
        mode = "copy"
        manifest = load_manifest(tier_root)
        manifest["entries"][dest.name] = {
            "kind": kind.name,
            "mode": "copy",
            "hash": sha256_of(src),
            "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_manifest(tier_root, manifest)
        if not args.copy:
            note("symlinks unavailable here — copied instead (tracked; use `sync` to refresh)")
    note("activated '%s' [%s] in %s tier -> %s" % (name, mode, tier, dest))
    if tier == "project":
        g = kind.tier_dir("global", None) / src.name
        if g.is_symlink() or g.exists():
            note("note: '%s' is also active in the global tier (double activation)" % name)
    return {"name": name, "mode": mode, "dest": str(dest)}


def cmd_link(args) -> int:
    kind = get_kind(args)
    project = project_dir(args) if args.tier == "project" else None
    with Lock():
        results = [_activate(kind, validate_name(n), args.tier, project, args) for n in args.names]
    if not args.dry_run:
        note("reload or start a new session for the change to take effect")
    return emit({"action": "link", "tier": args.tier, "results": results, "dry_run": args.dry_run})


def _deactivate(kind: Kind, name: str, tier: str, project: "Path | None", args) -> dict:
    tier_root = kind.tier_dir(tier, project)
    dest = confine(tier_root / name, tier_root)
    if not dest.exists() and not dest.is_symlink():
        matches = []
        for ext in kind.exts:
            alt = confine(tier_root / (name + ext), tier_root)
            if alt.exists() or alt.is_symlink():
                matches.append(alt)
        if len(matches) > 1:
            raise CmdError(
                "'%s' is ambiguous here: %s — use the full filename"
                % (name, ", ".join(m.name for m in matches)),
                EXIT_CONFLICT,
            )
        if not matches:
            note("'%s' is not active in the %s tier" % (name, tier))
            return {"name": name, "mode": "absent"}
        dest = matches[0]
    manifest = load_manifest(tier_root)
    if dest.is_symlink():
        if not is_managed_link(dest, kind) and not args.force:
            raise CmdError(
                "%s is not a link into the library; re-run with --force to remove it anyway" % dest,
                EXIT_CONFLICT,
            )
        if not args.dry_run:
            remove_activation(dest)
    elif dest.name in manifest["entries"]:
        entry = manifest["entries"][dest.name]
        if entry.get("mode") == "copy" and entry.get("hash"):
            current = sha256_of(dest)
            lib_entry = resolve_entry(kind, name)
            lib_hash = sha256_of(lib_entry) if lib_entry.exists() else None
            if current not in (entry["hash"], lib_hash) and not args.force:
                raise CmdError(
                    "tracked copy %s has local modifications; --force discards them" % dest,
                    EXIT_CONFLICT,
                )
        if not args.dry_run:
            remove_activation(dest)
            del manifest["entries"][dest.name]
            save_manifest(tier_root, manifest)
    else:
        raise CmdError(
            "%s is real content not managed by skillmgr — `adopt` it into the library instead" % dest,
            EXIT_CONFLICT,
        )
    note(("would deactivate" if args.dry_run else "deactivated") + " '%s' from %s tier" % (name, tier))
    return {"name": name, "mode": "dry-run" if args.dry_run else "removed"}


def cmd_unlink(args) -> int:
    kind = get_kind(args)
    project = project_dir(args) if args.tier == "project" else None
    with Lock():
        results = [_deactivate(kind, validate_name(n), args.tier, project, args) for n in args.names]
    return emit({"action": "unlink", "tier": args.tier, "results": results, "dry_run": args.dry_run})


def _embedded_symlinks(path: Path) -> list:
    if path.is_file():
        return []
    return [str(p.relative_to(path)) for p in sorted(path.rglob("*")) if p.is_symlink()]


def _adopt_one(kind: Kind, name: str, tier: str, project: "Path | None", args) -> dict:
    """Move one real (unmanaged) tier entry into the library, transactionally.

    Order of operations is chosen so the user's only copy is never at risk:
    1. copy source -> library temp, 2. atomic-rename temp -> library entry,
    3. optionally create the tier link, 4. only then remove the original.
    Failure at any step before (4) leaves the original untouched.
    """
    tier_root = kind.tier_dir(tier, project)
    src = confine(tier_root / name, tier_root)
    if not src.exists() and kind.entity == "file":
        for ext in kind.exts:
            alt = confine(tier_root / (name + ext), tier_root)
            if alt.exists():
                src = alt
                break
    if src.is_symlink() or not src.exists():
        raise CmdError("%s is not real (non-link) content" % src, EXIT_NOTFOUND)
    if not kind.is_valid_entry(src):
        raise CmdError(
            "%s is not a valid %s asset (%s) — adopting it would make it invisible; "
            "move or remove it manually instead"
            % (src, kind.name,
               "needs a SKILL.md inside" if kind.entity == "dir"
               else "needs one of: %s" % ", ".join(kind.exts)),
            EXIT_ERROR,
        )
    links_inside = _embedded_symlinks(src)
    if links_inside and not args.force:
        raise CmdError(
            "%s contains symlink(s) %s which would NOT survive adoption (symlinks are "
            "never copied, and adopt removes the original) — re-run with --force to "
            "drop them deliberately" % (src, links_inside[:5]),
            EXIT_CONFLICT,
        )
    lib_dest = confine(kind.lib_dir() / src.name, kind.lib_dir())
    if lib_dest.exists():
        raise CmdError("library already has '%s' — resolve manually" % src.name, EXIT_CONFLICT)
    if args.dry_run:
        note("would adopt %s -> %s%s%s" % (
            src, lib_dest, " and relink" if args.relink else "",
            ", dropping symlinks: %s" % links_inside if links_inside else ""))
        return {"name": name, "dry_run": True}
    kind.lib_dir().mkdir(parents=True, exist_ok=True)
    _copy_entry(src, lib_dest)  # steps 1+2
    if not kind.is_valid_entry(lib_dest):  # e.g. SKILL.md was itself a symlink
        if lib_dest.is_dir():
            shutil.rmtree(str(lib_dest))
        else:
            lib_dest.unlink()
        raise CmdError(
            "adoption of '%s' aborted: the copy is not a valid %s asset (was a required "
            "file a symlink?); original left untouched" % (src.name, kind.name),
            EXIT_ERROR,
        )
    linked = False
    if args.relink:
        if src.is_dir():
            shutil.rmtree(str(src))
        else:
            src.unlink()
        mode = _try_symlink(lib_dest, src, tier)
        if mode is None:
            _copy_entry(lib_dest, src)
            manifest = load_manifest(tier_root)
            manifest["entries"][src.name] = {
                "kind": kind.name, "mode": "copy", "hash": sha256_of(lib_dest),
                "copied_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save_manifest(tier_root, manifest)
        linked = True
    else:
        if src.is_dir():
            shutil.rmtree(str(src))
        else:
            src.unlink()
    note("adopted '%s' into the library%s" % (src.name, " and relinked" if linked else ""))
    return {"name": name, "library_entry": str(lib_dest), "relinked": linked}


def cmd_adopt(args) -> int:
    kind = get_kind(args)
    project = project_dir(args) if args.tier == "project" else None
    with Lock():
        results = [_adopt_one(kind, validate_name(n), args.tier, project, args) for n in args.names]
        if not args.dry_run:
            atomic_write_json(index_path(), build_index())
    return emit({"action": "adopt", "results": results, "dry_run": args.dry_run})


def cmd_import(args) -> int:
    kind = get_kind(args)
    src = Path(args.src).expanduser()
    if kind.entity == "dir":
        if src.is_file():
            raise CmdError(
                "%s is a single file; skills are directories — for single-file assets "
                "pass --kind agents|commands|workflows" % src,
                EXIT_ERROR,
            )
        if not (src / "SKILL.md").is_file():
            raise CmdError("%s does not contain a SKILL.md" % src, EXIT_NOTFOUND)
        if (src / "SKILL.md").is_symlink():
            raise CmdError(
                "%s/SKILL.md is a symlink; symlinks are never imported, so the result "
                "would be invalid — materialize it first" % src,
                EXIT_ERROR,
            )
    else:
        if not src.is_file():
            raise CmdError("%s is not a file" % src, EXIT_NOTFOUND)
        if kind.exts and src.suffix not in kind.exts:
            raise CmdError(
                "%s assets require one of: %s (got %r)"
                % (kind.name, ", ".join(kind.exts), src.suffix or "no extension"),
                EXIT_ERROR,
            )
    name = validate_name(args.name or (src.stem if kind.entity == "file" else src.name))
    dest_name = name + src.suffix if kind.entity == "file" else name
    dest = confine(kind.lib_dir() / dest_name, kind.lib_dir())
    if dest.exists():
        raise CmdError("library already has '%s'" % dest_name, EXIT_CONFLICT)
    if args.dry_run:
        note("would import %s -> %s" % (src, dest))
        return emit({"action": "import", "name": name, "dry_run": True})
    with Lock():
        kind.lib_dir().mkdir(parents=True, exist_ok=True)
        _copy_entry(src, dest)
        if not kind.is_valid_entry(dest):
            if dest.is_dir():
                shutil.rmtree(str(dest))
            else:
                dest.unlink()
            raise CmdError(
                "import of %s aborted: the copy is not a valid %s asset" % (src, kind.name),
                EXIT_ERROR,
            )
        atomic_write_json(index_path(), build_index())
    note("imported '%s' into the library (index refreshed)%s"
         % (name, "" if kind.entity == "dir" else " — activate with: link %s --kind %s" % (name, kind.name)))
    return emit({"action": "import", "name": name, "library_entry": str(dest)})


def cmd_sync(args) -> int:
    """Refresh tracked copies that drifted from their library source."""
    kind = get_kind(args)
    project = project_dir(args) if args.tier == "project" else None
    tier_root = kind.tier_dir(args.tier, project)
    manifest = load_manifest(tier_root)
    synced, skipped = [], []
    with Lock():
        for entry_name, meta in sorted(manifest["entries"].items()):
            if meta.get("mode") != "copy":
                continue
            # confine() is defense-in-depth; load_manifest already dropped unsafe
            # keys, but the join sites assert containment regardless.
            lib_entry = confine(kind.lib_dir() / entry_name, kind.lib_dir())
            dest = confine(tier_root / entry_name, tier_root)
            if not lib_entry.exists():
                skipped.append({"name": entry_name, "reason": "library source missing"})
                continue
            lib_hash = sha256_of(lib_entry)
            if lib_hash == meta.get("hash") and dest.exists() and sha256_of(dest) == lib_hash:
                continue
            if dest.exists() and meta.get("hash") and sha256_of(dest) != meta["hash"] and not args.force:
                skipped.append({"name": entry_name, "reason": "local modifications (--force to overwrite)"})
                continue
            if not args.dry_run:
                if dest.exists() or dest.is_symlink():
                    remove_activation(dest)
                _copy_entry(lib_entry, dest)
                meta["hash"] = lib_hash
                meta["copied_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            synced.append(entry_name)
        if not args.dry_run:
            save_manifest(tier_root, manifest)
    verb = "would sync" if args.dry_run else "synced"
    note("%s %d cop%s, skipped %d" % (verb, len(synced), "y" if len(synced) == 1 else "ies", len(skipped)))
    return emit({"action": "sync", "synced": synced, "skipped": skipped, "dry_run": args.dry_run})


def _doctor_scan(args, project) -> list:
    findings = []
    for kname, kind in KINDS.items():
        tiers = [("global", kind.tier_dir("global", None))]
        if project:
            tiers.append(("project", kind.tier_dir("project", project)))
        for tier_label, tier_root in tiers:
            if not tier_root.is_dir():
                continue
            manifest = load_manifest(tier_root)
            for p in sorted(tier_root.iterdir()):
                if p.name.startswith("."):
                    continue
                info = classify_entry(p, kind, manifest)
                if info["state"] in ("broken-link", "foreign-link", "copied-orphan",
                                     "copied-drifted", "copied-modified"):
                    finding = {"kind": kname, "tier": tier_label, "entry": p.name,
                               "state": info["state"], "target": info.get("target")}
                    if info["state"] == "broken-link" and args.fix:
                        if not args.dry_run:
                            p.unlink()
                        finding["fixed"] = "removed"
                    findings.append(finding)
            for backup in sorted(tier_root.glob(MANIFEST_NAME + ".corrupt-*")):
                findings.append({"kind": kname, "tier": tier_label, "entry": backup.name,
                                 "state": "corrupt-manifest-backup"})
            for entry_name in list(manifest["entries"]):
                if not (tier_root / entry_name).exists():
                    finding = {"kind": kname, "tier": tier_label, "entry": entry_name,
                               "state": "manifest-orphan"}
                    if args.fix:
                        if not args.dry_run:
                            del manifest["entries"][entry_name]
                        finding["fixed"] = "pruned"
                    findings.append(finding)
            if args.fix and not args.dry_run:
                save_manifest(tier_root, manifest)
    cfg = config_path()
    if cfg.is_file():
        try:
            json.loads(cfg.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            findings.append({"kind": "-", "tier": "-", "entry": str(cfg), "state": "corrupt-config"})
    return findings


def cmd_doctor(args) -> int:
    project = project_dir(args) if getattr(args, "project", None) else default_project()
    if args.fix and not args.dry_run:
        with Lock():  # --fix mutates (removes dangling links, prunes manifests)
            findings = _doctor_scan(args, project)
    else:
        findings = _doctor_scan(args, project)
    if not findings:
        note("no problems found")
    for f in findings:
        note("%s: [%s/%s] %s" % (f["state"], f["kind"], f["tier"], safe_display(f["entry"]))
             + (" -> %s" % f["fixed"] if "fixed" in f else ""))
    return emit({"action": "doctor", "findings": findings, "fix": args.fix, "dry_run": args.dry_run})


def cmd_uninstall(args) -> int:
    """Remove managed activations (links + tracked copies). Library untouched.

    Scoping is least-surprise: --project removes ONLY that project's tier;
    no --project removes only the global tier; --all (with optional --project)
    removes both.
    """
    project = None
    if getattr(args, "project", None):
        project = project_dir(args)
    removed = []
    with Lock():
        for kname, kind in KINDS.items():
            tiers = []
            if args.all or not project:
                tiers.append(("global", kind.tier_dir("global", None)))
            if project:
                tiers.append(("project", kind.tier_dir("project", project)))
            for tier_label, tier_root in tiers:
                if not tier_root.is_dir():
                    continue
                manifest = load_manifest(tier_root)
                for p in sorted(tier_root.iterdir()):
                    if p.name.startswith("."):
                        continue
                    info = classify_entry(p, kind, manifest)
                    if info["state"] in ("linked", "broken-link") or (
                        info["state"].startswith("copied") and p.name in manifest["entries"]
                    ):
                        if not args.dry_run:
                            remove_activation(p)
                            manifest["entries"].pop(p.name, None)
                        removed.append({"kind": kname, "tier": tier_label, "entry": p.name})
                if not args.dry_run:
                    save_manifest(tier_root, manifest)
    by_tier = {}
    for r in removed:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    detail = ", ".join("%d %s" % (n, t) for t, n in sorted(by_tier.items())) or "0"
    note(("would remove" if args.dry_run else "removed")
         + " %d managed activation(s) [%s]; library untouched" % (len(removed), detail))
    return emit({"action": "uninstall", "removed": removed, "dry_run": args.dry_run})


# ---------------------------------------------------------------------------
# Project stack detection
# ---------------------------------------------------------------------------

MANIFEST_FILES = [
    "package.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod",
    "Cargo.toml", "composer.json", "Gemfile", "pubspec.yaml", "CMakeLists.txt",
    "Makefile", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
    "pom.xml", "build.gradle", "build.gradle.kts", "mix.exs", "deno.json",
    "wp-config.php", "kustomization.yaml", "manage.py",
]

EXT_LANGS = {
    ".py": "python", ".ts": "typescript", ".tsx": "typescript", ".js": "javascript",
    ".jsx": "javascript", ".go": "go", ".rs": "rust", ".php": "php", ".rb": "ruby",
    ".java": "java", ".kt": "kotlin", ".swift": "swift", ".cs": "csharp",
    ".cpp": "cpp", ".c": "c", ".dart": "dart", ".ex": "elixir", ".sql": "sql",
    ".vue": "vue", ".svelte": "svelte", ".liquid": "liquid", ".tf": "terraform",
}

SKIP_DIRS = {
    "node_modules", ".git", "vendor", "dist", "build", ".venv", "venv",
    "__pycache__", "target", ".next", ".nuxt", "coverage",
}

WALK_MAX_DEPTH = 4
WALK_MAX_FILES = 20000


def _dep_names(project: Path) -> dict:
    deps = {}
    pkg = project / "package.json"
    if pkg.is_file():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            deps["js"] = sorted(list(data.get("dependencies", {})) + list(data.get("devDependencies", {})))
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    req = project / "requirements.txt"
    if req.is_file():
        try:
            deps["python"] = sorted({
                re.split(r"[=<>\[;]", ln.strip())[0].lower()
                for ln in req.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "-"))
            })
        except OSError:
            pass
    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        try:
            names = re.findall(r'^\s*"([A-Za-z0-9_.-]+)', pyproject.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
            deps["python"] = sorted(set(deps.get("python", [])) | {n.lower() for n in names})
        except OSError:
            pass
    composer = project / "composer.json"
    if composer.is_file():
        try:
            data = json.loads(composer.read_text(encoding="utf-8"))
            deps["php"] = sorted(list(data.get("require", {})) + list(data.get("require-dev", {})))
        except (json.JSONDecodeError, OSError, AttributeError):
            pass
    gemfile = project / "Gemfile"
    if gemfile.is_file():
        try:
            deps["ruby"] = sorted(set(re.findall(r"gem\s+['\"]([^'\"]+)", gemfile.read_text(encoding="utf-8", errors="replace"))))
        except OSError:
            pass
    gomod = project / "go.mod"
    if gomod.is_file():
        try:
            deps["go"] = sorted({
                ln.split()[0].strip() for ln in gomod.read_text(encoding="utf-8", errors="replace").splitlines()
                if ln.strip().startswith(("\t", " ")) and "/" in ln
            })[:40]
        except OSError:
            pass
    return deps


def cmd_detect(args) -> int:
    project = project_dir(args)
    found = [m for m in MANIFEST_FILES if (project / m).is_file()]

    langs, infra_hits, count = {}, set(), 0
    for root, dirnames, filenames in os.walk(str(project)):
        rel_depth = len(Path(root).relative_to(project).parts)
        dirnames[:] = [] if rel_depth >= WALK_MAX_DEPTH else [
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        ]
        for f in filenames:
            if count >= WALK_MAX_FILES:
                break
            lang = EXT_LANGS.get(Path(f).suffix.lower())
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
            if f == "Chart.yaml":
                infra_hits.add("kubernetes-helm")
            count += 1
        if count >= WALK_MAX_FILES:
            note("stopped scanning after %d files" % WALK_MAX_FILES)
            break

    infra = sorted(infra_hits)
    if any((project / d).exists() for d in ("Dockerfile", "docker-compose.yml", "docker-compose.yaml")):
        infra.append("docker")
    if langs.get("terraform") or (project / "terraform").is_dir():
        infra.append("terraform")
    if (project / "kustomization.yaml").is_file() or (project / "k8s").is_dir():
        infra.append("kubernetes")
    if (project / ".github" / "workflows").is_dir():
        infra.append("github-actions")
    if (project / "wp-content").is_dir() or (project / "wp-config.php").is_file():
        infra.append("wordpress")

    result = {
        "action": "detect",
        "project": str(project),
        "is_git_repo": (project / ".git").exists(),
        "manifests": found,
        "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])),
        "dependencies": _dep_names(project),
        "infra": sorted(set(infra)),
    }
    if not _JSON_MODE:
        print(json.dumps(result, indent=2, ensure_ascii=True))
        return EXIT_OK
    return emit(result)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common(sp, kind=True, tier=False, dry=True) -> None:
    if kind:
        sp.add_argument("--kind", choices=list(KINDS) , default="skills")
    if tier:
        sp.add_argument("--tier", choices=["global", "project"], required=True)
        sp.add_argument("--project", help="project directory (default: cwd) when --tier project")
    if dry:
        sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--json", action="store_true", dest="json_mode")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="skillmgr",
        description="Three-tier activation manager for agent assets (skills/agents/commands/workflows).",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scaffold", help="create the library layout and config file")
    add_common(sp, kind=False)

    sp = sub.add_parser("index", help="rebuild (or --check) the library index")
    sp.add_argument("--check", action="store_true", help="report staleness without writing")
    add_common(sp, kind=False)

    sp = sub.add_parser("status", help="show every library asset and its tier")
    sp.add_argument("--kind", choices=list(KINDS) + ["all"], default="all")
    sp.add_argument("--project")
    sp.add_argument("--json", action="store_true", dest="json_mode")

    sp = sub.add_parser("detect", help="dump a project's stack signals as JSON")
    sp.add_argument("--project")
    sp.add_argument("--json", action="store_true", dest="json_mode")

    sp = sub.add_parser("link", help="activate library assets in a tier")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--copy", action="store_true", help="tracked copy instead of symlink")
    add_common(sp, tier=True)

    sp = sub.add_parser("unlink", help="deactivate assets (library never touched)")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--force", action="store_true",
                    help="also remove foreign links / locally-modified copies")
    add_common(sp, tier=True)

    sp = sub.add_parser("adopt", help="move real tier content into the library (transactional)")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--relink", action="store_true", help="leave an activation in place")
    sp.add_argument("--force", action="store_true",
                    help="proceed even when embedded symlinks would be dropped")
    add_common(sp, tier=True)

    sp = sub.add_parser("import", help="copy an external asset into the library")
    sp.add_argument("src")
    sp.add_argument("--name", help="override the library name (default: source basename)")
    add_common(sp)

    sp = sub.add_parser("sync", help="refresh tracked copies from the library")
    sp.add_argument("--force", action="store_true", help="overwrite locally-modified copies")
    add_common(sp, tier=True)

    sp = sub.add_parser("doctor", help="find (and --fix) broken/foreign/orphaned entries")
    sp.add_argument("--fix", action="store_true")
    sp.add_argument("--project")
    add_common(sp, kind=False)

    sp = sub.add_parser("uninstall",
                        help="remove managed activations (global tier, or --project's tier, "
                             "or both with --all); library untouched")
    sp.add_argument("--project")
    sp.add_argument("--all", action="store_true",
                    help="remove global AND project activations together")
    add_common(sp, kind=False)

    return ap


def main(argv=None) -> int:
    global _JSON_MODE
    args = build_parser().parse_args(argv)
    _JSON_MODE = bool(getattr(args, "json_mode", False))
    _NOTES.clear()
    handlers = {
        "scaffold": cmd_scaffold, "index": cmd_index, "status": cmd_status,
        "detect": cmd_detect, "link": cmd_link, "unlink": cmd_unlink,
        "adopt": cmd_adopt, "import": cmd_import, "sync": cmd_sync,
        "doctor": cmd_doctor, "uninstall": cmd_uninstall,
    }
    try:
        return handlers[args.cmd](args)
    except CmdError as err:
        return fail(err)
    except OSError as err:
        # Filesystem failures (read-only dirs, disk full, permissions) surface
        # as one-line errors / JSON error objects, never raw tracebacks.
        return fail(CmdError("filesystem error: %s" % err))
    except KeyboardInterrupt:
        return fail(CmdError("interrupted"))


if __name__ == "__main__":
    sys.exit(main())
