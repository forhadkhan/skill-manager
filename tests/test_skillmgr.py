"""Unit tests for skillmgr.py — stdlib unittest only (Python 3.8+).

Every test is hermetic: a fresh temp sandbox holds the library root, the
Claude home, the config file, and any project directory. The engine reads
the SKILLMGR_* environment variables at call time, so pointing them into
the sandbox in setUp (and restoring them in tearDown) fully isolates the
real home directories.
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

MOD_PATH = Path(__file__).resolve().parents[1] / "skills" / "skill-manager" / "scripts" / "skillmgr.py"
_spec = importlib.util.spec_from_file_location("skillmgr_under_test", str(MOD_PATH))
skillmgr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(skillmgr)

ENV_KEYS = ("SKILLMGR_LIBRARY_ROOT", "SKILLMGR_CLAUDE_HOME", "SKILLMGR_CONFIG")


class Base(unittest.TestCase):
    """Sandboxed environment + CLI helpers shared by all test classes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skillmgr-test-")
        self.lib = os.path.join(self.tmp, "library")
        self.claude = os.path.join(self.tmp, "claude-home")
        self.cfg = os.path.join(self.tmp, "skillmgr.json")
        self.proj = os.path.join(self.tmp, "project")
        os.makedirs(self.proj)
        self._saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ["SKILLMGR_LIBRARY_ROOT"] = self.lib
        os.environ["SKILLMGR_CLAUDE_HOME"] = self.claude
        os.environ["SKILLMGR_CONFIG"] = self.cfg

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- CLI runners -----------------------------------------------------

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = skillmgr.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def run_json(self, *argv):
        code, out, _ = self.run_cli(*(list(argv) + ["--json"]))
        return code, json.loads(out)

    # ---- fixture builders ------------------------------------------------

    def make_skill(self, name, desc="a test skill", body="content"):
        d = Path(self.lib) / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: %s\n---\n%s\n" % (name, desc, body),
            encoding="utf-8",
        )
        return d

    def make_agent(self, name="helper", body="do things"):
        d = Path(self.lib) / "agents"
        d.mkdir(parents=True, exist_ok=True)
        f = d / (name + ".md")
        f.write_text("---\ndescription: an agent\n---\n%s\n" % body, encoding="utf-8")
        return f

    def global_tier(self, kind="skills"):
        return Path(self.claude) / kind

    def project_tier(self, kind="skills"):
        return Path(self.proj) / ".claude" / kind

    def read_manifest(self, tier_dir):
        return json.loads(
            (Path(tier_dir) / skillmgr.MANIFEST_NAME).read_text(encoding="utf-8")
        )


# ---------------------------------------------------------------------------
# 1. validate_name
# ---------------------------------------------------------------------------


class TestValidateName(Base):
    def assert_rejected(self, name):
        with self.assertRaises(skillmgr.CmdError, msg="should reject %r" % name):
            skillmgr.validate_name(name)

    def test_traversal_variants_rejected(self):
        for bad in ("../x", "/abs", "a/b", ".", "..", ""):
            self.assert_rejected(bad)

    def test_backslash_rejected(self):
        self.assert_rejected("a\\b")
        self.assert_rejected("..\\x")

    def test_overlong_rejected(self):
        self.assert_rejected("a" * 65)

    def test_max_length_accepted(self):
        name = "a" * 64
        self.assertEqual(skillmgr.validate_name(name), name)

    def test_unicode_rejected(self):
        for bad in ("café", "скилл", "emoji\U0001f600"):
            self.assert_rejected(bad)

    def test_valid_edge_cases(self):
        self.assertEqual(skillmgr.validate_name("a"), "a")
        self.assertEqual(skillmgr.validate_name("A-1_b.c"), "A-1_b.c")

    def test_leading_punctuation_rejected(self):
        for bad in (".hidden", "-dash", "_under"):
            self.assert_rejected(bad)


# ---------------------------------------------------------------------------
# 2. scaffold + index
# ---------------------------------------------------------------------------


class TestScaffold(Base):
    def test_scaffold_creates_layout_and_config(self):
        code, data = self.run_json("scaffold")
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        for kind in ("skills", "agents", "commands", "workflows"):
            self.assertTrue((Path(self.lib) / kind).is_dir(), kind)
        cfg = json.loads(Path(self.cfg).read_text(encoding="utf-8"))
        self.assertEqual(cfg["library_root"], self.lib)

    def test_scaffold_idempotent(self):
        self.run_json("scaffold")
        code, data = self.run_json("scaffold")
        self.assertEqual(code, 0)
        self.assertEqual(data["created"], [])

    def test_scaffold_dry_run_creates_nothing(self):
        code, data = self.run_json("scaffold", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(Path(self.lib).exists())
        self.assertFalse(Path(self.cfg).exists())
        self.assertTrue(data["dry_run"])


class TestIndex(Base):
    def test_index_counts_and_valid_json(self):
        self.make_skill("alpha")
        self.make_skill("beta")
        self.make_agent("helper")
        code, data = self.run_json("index")
        self.assertEqual(code, 0)
        self.assertEqual(data["counts"]["skills"], 2)
        self.assertEqual(data["counts"]["agents"], 1)
        # index.json must be valid JSON with the expected shape after the run
        on_disk = json.loads((Path(self.lib) / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["version"], skillmgr.SCHEMA_VERSION)
        self.assertEqual(len(on_disk["kinds"]["skills"]), 2)
        names = {e["name"] for e in on_disk["kinds"]["skills"]}
        self.assertEqual(names, {"alpha", "beta"})

    def test_index_dry_run_writes_nothing(self):
        self.make_skill("alpha")
        code, _ = self.run_json("index", "--dry-run")
        self.assertEqual(code, 0)
        self.assertFalse((Path(self.lib) / "index.json").exists())

    def test_index_check_fresh_after_build(self):
        self.make_skill("alpha")
        self.run_json("index")
        code, data = self.run_json("index", "--check")
        self.assertEqual(code, 0)
        self.assertFalse(data["stale"])

    def test_index_check_stale_without_index(self):
        self.make_skill("alpha")
        code, data = self.run_json("index", "--check")
        self.assertEqual(code, 0)
        self.assertTrue(data["stale"])

    def test_index_check_stale_after_entry_mtime_change(self):
        d = self.make_skill("alpha")
        self.run_json("index")
        t = time.time() + 50
        os.utime(str(d), (t, t))
        code, data = self.run_json("index", "--check")
        self.assertEqual(code, 0)
        self.assertTrue(data["stale"])

    def test_index_check_stale_after_new_entry(self):
        self.make_skill("alpha")
        self.run_json("index")
        self.make_skill("beta")
        _, data = self.run_json("index", "--check")
        self.assertTrue(data["stale"])

    def test_index_check_stale_after_skill_md_edit(self):
        # Dir-kind staleness folds SKILL.md's mtime into the entry mtime, so an
        # in-place edit (which does not touch the directory mtime) is detected.
        d = self.make_skill("alpha")
        self.run_json("index")
        t = time.time() + 100
        os.utime(str(d / "SKILL.md"), (t, t))
        _, data = self.run_json("index", "--check")
        self.assertTrue(data["stale"])


# ---------------------------------------------------------------------------
# 3. link
# ---------------------------------------------------------------------------


class TestLink(Base):
    def test_link_dir_kind_global_symlink(self):
        self.make_skill("alpha")
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        dest = self.global_tier() / "alpha"
        self.assertTrue(dest.is_symlink())
        self.assertEqual(
            os.path.realpath(str(dest)),
            os.path.realpath(os.path.join(self.lib, "skills", "alpha")),
        )
        self.assertTrue((dest / "SKILL.md").is_file())
        self.assertEqual(data["results"][0]["mode"], "symlink")

    def test_link_file_kind_symlink(self):
        self.make_agent("helper")
        code, data = self.run_json("link", "helper", "--kind", "agents", "--tier", "global")
        self.assertEqual(code, 0)
        dest = self.global_tier("agents") / "helper.md"
        self.assertTrue(dest.is_symlink())
        self.assertTrue(dest.is_file())
        self.assertEqual(data["results"][0]["mode"], "symlink")

    def test_link_project_tier(self):
        self.make_skill("alpha")
        code, _ = self.run_json(
            "link", "alpha", "--tier", "project", "--project", self.proj
        )
        self.assertEqual(code, 0)
        dest = self.project_tier() / "alpha"
        self.assertTrue(dest.is_symlink())
        self.assertTrue((dest / "SKILL.md").is_file())

    def test_link_idempotent(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "existing")
        self.assertTrue((self.global_tier() / "alpha").is_symlink())

    def test_link_copy_creates_manifest_with_hash(self):
        src = self.make_skill("alpha")
        code, data = self.run_json("link", "alpha", "--tier", "global", "--copy")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "copy")
        dest = self.global_tier() / "alpha"
        self.assertTrue(dest.is_dir())
        self.assertFalse(dest.is_symlink())
        self.assertTrue((dest / "SKILL.md").is_file())
        manifest = self.read_manifest(self.global_tier())
        entry = manifest["entries"]["alpha"]
        self.assertEqual(entry["mode"], "copy")
        self.assertEqual(entry["hash"], skillmgr.sha256_of(src))

    def test_link_copy_idempotent(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        code, data = self.run_json("link", "alpha", "--tier", "global", "--copy")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "existing-copy")

    def test_link_case_insensitive_collision(self):
        self.make_skill("Alpha")
        self.make_skill("alpha")
        code, _ = self.run_json("link", "Alpha", "--tier", "global")
        self.assertEqual(code, 0)
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        self.assertIn("case-insensitive", data["error"])

    def test_link_nonexistent_exit3(self):
        code, data = self.run_json("link", "ghost", "--tier", "global")
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])

    def test_link_dest_exists_unmanaged_exit4(self):
        self.make_skill("alpha")
        squatter = self.global_tier() / "alpha"
        squatter.mkdir(parents=True)
        (squatter / "SKILL.md").write_text("---\nname: alpha\n---\n", encoding="utf-8")
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        # the unmanaged content is untouched
        self.assertTrue((squatter / "SKILL.md").is_file())
        self.assertFalse(squatter.is_symlink())

    def test_link_dry_run_mutates_nothing(self):
        self.make_skill("alpha")
        code, data = self.run_json("link", "alpha", "--tier", "global", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "dry-run")
        self.assertFalse(self.global_tier().exists())
        self.assertFalse((Path(self.lib) / ".skillmgr.lock").exists())


# ---------------------------------------------------------------------------
# 4. unlink
# ---------------------------------------------------------------------------


class TestUnlink(Base):
    def test_unlink_managed_link_removed(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("unlink", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "removed")
        dest = self.global_tier() / "alpha"
        self.assertFalse(dest.exists() or dest.is_symlink())
        # library never touched
        self.assertTrue((Path(self.lib) / "skills" / "alpha" / "SKILL.md").is_file())

    def test_unlink_foreign_symlink_refused_then_forced(self):
        outside = Path(self.tmp) / "outside-dir"
        outside.mkdir()
        tier = self.global_tier()
        tier.mkdir(parents=True)
        os.symlink(str(outside), str(tier / "rogue"))
        code, data = self.run_json("unlink", "rogue", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        self.assertTrue((tier / "rogue").is_symlink())
        code, data = self.run_json("unlink", "rogue", "--tier", "global", "--force")
        self.assertEqual(code, 0)
        self.assertFalse((tier / "rogue").is_symlink())
        self.assertTrue(outside.is_dir())  # only the link goes, never the target

    def test_unlink_modified_copy_refused_then_forced(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        copy_md = self.global_tier() / "alpha" / "SKILL.md"
        copy_md.write_text("locally edited\n", encoding="utf-8")
        code, data = self.run_json("unlink", "alpha", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertIn("local modifications", data["error"])
        self.assertTrue(copy_md.is_file())
        code, _ = self.run_json("unlink", "alpha", "--tier", "global", "--force")
        self.assertEqual(code, 0)
        self.assertFalse((self.global_tier() / "alpha").exists())

    def test_unlink_pristine_copy_removed_and_manifest_cleaned(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        code, _ = self.run_json("unlink", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertFalse((self.global_tier() / "alpha").exists())
        # manifest is deleted once its last entry is removed
        self.assertFalse((self.global_tier() / skillmgr.MANIFEST_NAME).exists())

    def test_unlink_unmanaged_real_dir_exit4(self):
        tier = self.global_tier()
        real = tier / "handmade"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: handmade\n---\n", encoding="utf-8")
        code, data = self.run_json("unlink", "handmade", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertIn("adopt", data["error"])
        self.assertTrue((real / "SKILL.md").is_file())

    def test_unlink_absent_is_noop_success(self):
        code, data = self.run_json("unlink", "nothere", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertEqual(data["results"][0]["mode"], "absent")
        self.assertTrue(any("not active" in n for n in data.get("notes", [])))


# ---------------------------------------------------------------------------
# 5. adopt
# ---------------------------------------------------------------------------


class TestAdopt(Base):
    def _real_project_skill(self, name="mytool"):
        d = self.project_tier() / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: adopted\n---\nbody\n" % name, encoding="utf-8"
        )
        return d

    def test_adopt_moves_real_dir_into_library(self):
        src = self._real_project_skill()
        code, data = self.run_json(
            "adopt", "mytool", "--tier", "project", "--project", self.proj
        )
        self.assertEqual(code, 0)
        lib_entry = Path(self.lib) / "skills" / "mytool"
        self.assertTrue((lib_entry / "SKILL.md").is_file())
        self.assertFalse(src.exists())
        self.assertFalse(data["results"][0]["relinked"])

    def test_adopt_relink_leaves_working_activation(self):
        src = self._real_project_skill()
        code, data = self.run_json(
            "adopt", "mytool", "--tier", "project", "--project", self.proj, "--relink"
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["results"][0]["relinked"])
        self.assertTrue(src.is_symlink())
        self.assertTrue((src / "SKILL.md").is_file())  # link works
        self.assertEqual(
            os.path.realpath(str(src)),
            os.path.realpath(os.path.join(self.lib, "skills", "mytool")),
        )

    def test_adopt_name_conflict_leaves_original_intact(self):
        self.make_skill("mytool", body="library version")
        src = self._real_project_skill()
        code, data = self.run_json(
            "adopt", "mytool", "--tier", "project", "--project", self.proj
        )
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        self.assertTrue((src / "SKILL.md").is_file())  # original untouched
        lib_md = Path(self.lib) / "skills" / "mytool" / "SKILL.md"
        self.assertIn("library version", lib_md.read_text(encoding="utf-8"))

    def test_adopt_file_kind_with_extension_resolution(self):
        tier = self.global_tier("agents")
        tier.mkdir(parents=True)
        (tier / "notes.md").write_text("---\ndescription: notes\n---\n", encoding="utf-8")
        code, _ = self.run_json("adopt", "notes", "--kind", "agents", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.lib) / "agents" / "notes.md").is_file())
        self.assertFalse((tier / "notes.md").exists())

    def test_adopt_symlink_refused_exit3(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("adopt", "alpha", "--tier", "global")
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])

    def test_adopt_dry_run_moves_nothing(self):
        src = self._real_project_skill()
        code, data = self.run_json(
            "adopt", "mytool", "--tier", "project", "--project", self.proj, "--dry-run"
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertTrue((src / "SKILL.md").is_file())
        self.assertFalse((Path(self.lib) / "skills" / "mytool").exists())


# ---------------------------------------------------------------------------
# 6. import
# ---------------------------------------------------------------------------


class TestImport(Base):
    def _external_skill(self, name="fetched", with_symlink=False):
        d = Path(self.tmp) / "downloads" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: %s\ndescription: imported\n---\n" % name, encoding="utf-8"
        )
        (d / "extra.txt").write_text("data\n", encoding="utf-8")
        if with_symlink:
            os.symlink("/etc/hostname", str(d / "evil"))
        return d

    def test_import_dir_with_skill_md(self):
        src = self._external_skill()
        code, data = self.run_json("import", str(src))
        self.assertEqual(code, 0)
        dest = Path(self.lib) / "skills" / "fetched"
        self.assertTrue((dest / "SKILL.md").is_file())
        self.assertTrue((dest / "extra.txt").is_file())
        self.assertTrue((src / "SKILL.md").is_file())  # source untouched
        # index refreshed as part of import
        idx = json.loads((Path(self.lib) / "index.json").read_text(encoding="utf-8"))
        self.assertEqual([e["name"] for e in idx["kinds"]["skills"]], ["fetched"])

    def test_import_embedded_symlink_not_copied(self):
        src = self._external_skill(with_symlink=True)
        code, data = self.run_json("import", str(src))
        self.assertEqual(code, 0)
        planted = Path(self.lib) / "skills" / "fetched" / "evil"
        self.assertFalse(planted.exists() or planted.is_symlink())
        self.assertTrue((Path(self.lib) / "skills" / "fetched" / "extra.txt").is_file())
        self.assertTrue(
            any("symlink" in n and "skipped" in n for n in data.get("notes", [])),
            "expected a note about the skipped symlink, got %r" % data.get("notes"),
        )

    def test_import_name_override(self):
        src = self._external_skill()
        code, _ = self.run_json("import", str(src), "--name", "renamed")
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.lib) / "skills" / "renamed" / "SKILL.md").is_file())
        self.assertFalse((Path(self.lib) / "skills" / "fetched").exists())

    def test_import_duplicate_exit4(self):
        src = self._external_skill()
        self.run_json("import", str(src))
        code, data = self.run_json("import", str(src))
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])

    def test_import_file_kind(self):
        f = Path(self.tmp) / "thing.md"
        f.write_text("---\ndescription: an agent\n---\n", encoding="utf-8")
        code, _ = self.run_json("import", str(f), "--kind", "agents")
        self.assertEqual(code, 0)
        self.assertTrue((Path(self.lib) / "agents" / "thing.md").is_file())

    def test_import_dir_without_skill_md_exit3(self):
        d = Path(self.tmp) / "not-a-skill"
        d.mkdir()
        code, data = self.run_json("import", str(d))
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])

    def test_import_invalid_override_name_rejected(self):
        src = self._external_skill()
        code, data = self.run_json("import", str(src), "--name", "../escape")
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertFalse((Path(self.tmp) / "escape").exists())


# ---------------------------------------------------------------------------
# 7. classify / status
# ---------------------------------------------------------------------------


class TestStatus(Base):
    def test_status_dormant_and_linked(self):
        self.make_skill("alpha")
        self.make_skill("beta")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("status", "--kind", "skills")
        self.assertEqual(code, 0)
        rows = {r["name"]: r["tier"] for r in data["report"]["skills"]["assets"]}
        self.assertEqual(rows["alpha"], "global")
        self.assertEqual(rows["beta"], "dormant")
        self.assertEqual(data["report"]["skills"]["attention"], [])

    def test_status_broken_managed_link_in_attention(self):
        self.make_skill("ephemeral")
        self.run_json("link", "ephemeral", "--tier", "global")
        shutil.rmtree(os.path.join(self.lib, "skills", "ephemeral"))
        code, data = self.run_json("status", "--kind", "skills")
        self.assertEqual(code, 0)
        attention = data["report"]["skills"]["attention"]
        self.assertEqual(len(attention), 1)
        self.assertEqual(attention[0]["state"], "broken-link")
        self.assertEqual(attention[0]["name"], "ephemeral")

    def test_status_foreign_link_reported(self):
        outside = Path(self.tmp) / "elsewhere"
        outside.mkdir()
        tier = self.global_tier()
        tier.mkdir(parents=True)
        os.symlink(str(outside), str(tier / "rogue"))
        _, data = self.run_json("status", "--kind", "skills")
        states = {e["name"]: e["state"] for e in data["report"]["skills"]["attention"]}
        self.assertEqual(states.get("rogue"), "foreign-link")

    def test_status_copied_drifted_after_library_edit(self):
        d = self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        (d / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: v2\n---\nnew\n", encoding="utf-8"
        )
        _, data = self.run_json("status", "--kind", "skills")
        rows = {r["name"]: r["tier"] for r in data["report"]["skills"]["assets"]}
        self.assertEqual(rows["alpha"], "global (copied-drifted)")

    def test_status_missing_project_exit3(self):
        code, data = self.run_json(
            "status", "--project", os.path.join(self.tmp, "no-such-project")
        )
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])

    def test_status_unmanaged_real_dir_in_attention(self):
        real = self.global_tier() / "handmade"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: handmade\n---\n", encoding="utf-8")
        _, data = self.run_json("status", "--kind", "skills")
        states = {e["name"]: e["state"] for e in data["report"]["skills"]["attention"]}
        self.assertEqual(states.get("handmade"), "unmanaged")


# ---------------------------------------------------------------------------
# 8. sync
# ---------------------------------------------------------------------------


class TestSync(Base):
    def test_sync_refreshes_drifted_copy_and_updates_hash(self):
        d = self.make_skill("alpha", body="v1")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        (d / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: updated\n---\nv2\n", encoding="utf-8"
        )
        code, data = self.run_json("sync", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertEqual(data["synced"], ["alpha"])
        copied = (self.global_tier() / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("v2", copied)
        manifest = self.read_manifest(self.global_tier())
        self.assertEqual(manifest["entries"]["alpha"]["hash"], skillmgr.sha256_of(d))

    def test_sync_skips_locally_modified_copy(self):
        d = self.make_skill("alpha", body="v1")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        local = self.global_tier() / "alpha" / "SKILL.md"
        local.write_text("my local edits\n", encoding="utf-8")
        code, data = self.run_json("sync", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertEqual(data["synced"], [])
        self.assertEqual(len(data["skipped"]), 1)
        self.assertIn("local modifications", data["skipped"][0]["reason"])
        self.assertIn("my local edits", local.read_text(encoding="utf-8"))

    def test_sync_force_overwrites_local_modifications(self):
        d = self.make_skill("alpha", body="library-truth")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        local = self.global_tier() / "alpha" / "SKILL.md"
        local.write_text("my local edits\n", encoding="utf-8")
        code, data = self.run_json("sync", "--tier", "global", "--force")
        self.assertEqual(code, 0)
        self.assertEqual(data["synced"], ["alpha"])
        self.assertIn("library-truth", local.read_text(encoding="utf-8"))

    def test_sync_dry_run_reports_but_does_not_write(self):
        d = self.make_skill("alpha", body="v1")
        self.run_json("link", "alpha", "--tier", "global", "--copy")
        (d / "SKILL.md").write_text("---\nname: alpha\n---\nv2\n", encoding="utf-8")
        code, data = self.run_json("sync", "--tier", "global", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(data["synced"], ["alpha"])
        copied = (self.global_tier() / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("v1", copied)


# ---------------------------------------------------------------------------
# 9. doctor
# ---------------------------------------------------------------------------


class TestDoctor(Base):
    def _broken_link_and_orphan(self):
        # broken managed link: link then delete the library source
        self.make_skill("gone")
        self.run_json("link", "gone", "--tier", "global")
        shutil.rmtree(os.path.join(self.lib, "skills", "gone"))
        # manifest orphan: tracked copy whose tier entry was deleted
        self.make_agent("helper")
        self.run_json("link", "helper", "--kind", "agents", "--tier", "global", "--copy")
        (self.global_tier("agents") / "helper.md").unlink()

    def test_doctor_finds_broken_links_and_manifest_orphans(self):
        self._broken_link_and_orphan()
        code, data = self.run_json("doctor")
        self.assertEqual(code, 0)
        states = {(f["entry"], f["state"]) for f in data["findings"]}
        self.assertIn(("gone", "broken-link"), states)
        self.assertIn(("helper.md", "manifest-orphan"), states)

    def test_doctor_fix_removes_and_prunes(self):
        self._broken_link_and_orphan()
        code, data = self.run_json("doctor", "--fix")
        self.assertEqual(code, 0)
        fixed = {f["entry"]: f.get("fixed") for f in data["findings"]}
        self.assertEqual(fixed["gone"], "removed")
        self.assertEqual(fixed["helper.md"], "pruned")
        broken = self.global_tier() / "gone"
        self.assertFalse(broken.exists() or broken.is_symlink())
        self.assertFalse(
            (self.global_tier("agents") / skillmgr.MANIFEST_NAME).exists()
        )

    def test_doctor_fix_dry_run_changes_nothing(self):
        self._broken_link_and_orphan()
        code, _ = self.run_json("doctor", "--fix", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue((self.global_tier() / "gone").is_symlink())
        manifest = self.read_manifest(self.global_tier("agents"))
        self.assertIn("helper.md", manifest["entries"])

    def test_doctor_clean_environment(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(data["findings"], [])


# ---------------------------------------------------------------------------
# 10. uninstall
# ---------------------------------------------------------------------------


class TestUninstall(Base):
    def test_uninstall_removes_managed_leaves_rest(self):
        self.make_skill("alpha")
        self.make_agent("helper")
        self.run_json("link", "alpha", "--tier", "global")
        self.run_json("link", "helper", "--kind", "agents", "--tier", "global", "--copy")
        self.run_json("link", "alpha", "--tier", "project", "--project", self.proj)
        # unmanaged real dir must survive
        real = self.global_tier() / "handmade"
        real.mkdir(parents=True)
        (real / "SKILL.md").write_text("---\nname: handmade\n---\n", encoding="utf-8")

        # --project alone scopes to that project's tier ONLY (least surprise)
        code, data = self.run_json("uninstall", "--project", self.proj)
        self.assertEqual(code, 0)
        removed = {(r["kind"], r["tier"], r["entry"]) for r in data["removed"]}
        self.assertEqual(removed, {("skills", "project", "alpha")})
        p_alpha = self.project_tier() / "alpha"
        self.assertFalse(p_alpha.exists() or p_alpha.is_symlink())
        self.assertTrue((self.global_tier() / "alpha").is_symlink())  # global survives

        # --all sweeps global too
        code, data = self.run_json("uninstall", "--all", "--project", self.proj)
        self.assertEqual(code, 0)
        removed = {(r["kind"], r["tier"], r["entry"]) for r in data["removed"]}
        self.assertEqual(
            removed,
            {("skills", "global", "alpha"), ("agents", "global", "helper.md")},
        )
        g_alpha = self.global_tier() / "alpha"
        self.assertFalse(g_alpha.exists() or g_alpha.is_symlink())
        self.assertFalse((self.global_tier("agents") / "helper.md").exists())
        self.assertFalse((self.global_tier("agents") / skillmgr.MANIFEST_NAME).exists())
        # unmanaged content and the library itself are untouched
        self.assertTrue((real / "SKILL.md").is_file())
        self.assertTrue((Path(self.lib) / "skills" / "alpha" / "SKILL.md").is_file())
        self.assertTrue((Path(self.lib) / "agents" / "helper.md").is_file())

    def test_uninstall_dry_run_removes_nothing(self):
        self.make_skill("alpha")
        self.run_json("link", "alpha", "--tier", "global")
        code, data = self.run_json("uninstall", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(len(data["removed"]), 1)
        self.assertTrue((self.global_tier() / "alpha").is_symlink())


# ---------------------------------------------------------------------------
# 11. parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter(Base):
    def fm_of(self, text, encoding="utf-8", raw=None):
        f = Path(self.tmp) / "fm-fixture.md"
        if raw is not None:
            f.write_bytes(raw)
        else:
            f.write_text(text, encoding=encoding)
        return skillmgr.parse_frontmatter(f)

    def test_utf8_bom(self):
        raw = b"\xef\xbb\xbf---\nname: bommed\ndescription: has a bom\n---\nbody\n"
        fm = self.fm_of("", raw=raw)
        self.assertEqual(fm["name"], "bommed")
        self.assertEqual(fm["description"], "has a bom")

    def test_crlf_line_endings(self):
        raw = b"---\r\nname: windows\r\ndescription: crlf file\r\n---\r\nbody\r\n"
        fm = self.fm_of("", raw=raw)
        self.assertEqual(fm["name"], "windows")
        self.assertEqual(fm["description"], "crlf file")

    def test_folded_block_with_colon_in_continuation(self):
        text = (
            "---\n"
            "description: >\n"
            "  Use this when: the user asks for X\n"
            "  or mentions Y.\n"
            "name: folded\n"
            "---\n"
        )
        fm = self.fm_of(text)
        self.assertEqual(
            fm["description"], "Use this when: the user asks for X or mentions Y."
        )
        self.assertEqual(fm["name"], "folded")

    def test_quoted_values(self):
        text = "---\nname: \"quoted name\"\ndescription: 'single quoted'\n---\n"
        fm = self.fm_of(text)
        self.assertEqual(fm["name"], "quoted name")
        self.assertEqual(fm["description"], "single quoted")

    def test_no_frontmatter(self):
        self.assertEqual(self.fm_of("# Just a title\n\nBody text.\n"), {})

    def test_empty_file(self):
        self.assertEqual(self.fm_of(""), {})

    def test_indented_continuation_without_block_marker(self):
        text = "---\ndescription: starts here\n  and wraps onto a second line\n---\n"
        fm = self.fm_of(text)
        self.assertEqual(fm["description"], "starts here and wraps onto a second line")


# ---------------------------------------------------------------------------
# 12. detect
# ---------------------------------------------------------------------------


class TestDetect(Base):
    def test_detect_stack_signals(self):
        p = Path(self.proj)
        (p / "package.json").write_text(
            json.dumps(
                {"dependencies": {"react": "^18.0.0"}, "devDependencies": {"jest": "^29"}}
            ),
            encoding="utf-8",
        )
        (p / "Dockerfile").write_text("FROM node:20\n", encoding="utf-8")
        (p / ".git").mkdir()
        (p / "app.js").write_text("console.log(1)\n", encoding="utf-8")
        (p / "tool.py").write_text("print(1)\n", encoding="utf-8")
        code, data = self.run_json("detect", "--project", self.proj)
        self.assertEqual(code, 0)
        self.assertTrue(data["is_git_repo"])
        self.assertIn("package.json", data["manifests"])
        self.assertIn("Dockerfile", data["manifests"])
        self.assertEqual(data["dependencies"]["js"], ["jest", "react"])
        self.assertEqual(data["languages"].get("javascript"), 1)
        self.assertEqual(data["languages"].get("python"), 1)
        self.assertIn("docker", data["infra"])

    def test_detect_missing_project_exit3(self):
        code, data = self.run_json(
            "detect", "--project", os.path.join(self.tmp, "does-not-exist")
        )
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])


# ---------------------------------------------------------------------------
# 13. --json envelope
# ---------------------------------------------------------------------------


class TestJsonEnvelope(Base):
    def test_success_envelope_ok_true_with_notes(self):
        self.make_skill("alpha")
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertIs(data["ok"], True)
        self.assertIsInstance(data.get("notes"), list)
        self.assertTrue(any("activated" in n for n in data["notes"]))

    def test_error_envelope_ok_false_with_code(self):
        code, data = self.run_json("link", "ghost", "--tier", "global")
        self.assertEqual(code, 3)
        self.assertIs(data["ok"], False)
        self.assertEqual(data["code"], 3)
        self.assertIn("ghost", data["error"])

    def test_non_json_error_goes_to_stderr(self):
        code, out, err = self.run_cli("link", "ghost", "--tier", "global")
        self.assertEqual(code, 3)
        self.assertIn("error:", err)


# ---------------------------------------------------------------------------
# 14. lock
# ---------------------------------------------------------------------------


class TestLock(Base):
    def _lock_path(self):
        return Path(self.lib) / ".skillmgr.lock"

    def test_fresh_lock_held_by_other_process_exit4(self):
        self.make_skill("alpha")
        lock = self._lock_path()
        lock.write_text("99999", encoding="utf-8")  # fresh mtime = now
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        self.assertIn("in progress", data["error"])
        self.assertTrue(lock.exists())  # a foreign fresh lock is never deleted
        dest = self.global_tier() / "alpha"
        self.assertFalse(dest.exists() or dest.is_symlink())

    def test_stale_lock_is_stolen(self):
        self.make_skill("alpha")
        lock = self._lock_path()
        lock.write_text("99999", encoding="utf-8")
        old = time.time() - 1200  # 20 minutes ago > 600s staleness threshold
        os.utime(str(lock), (old, old))
        code, data = self.run_json("link", "alpha", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertTrue((self.global_tier() / "alpha").is_symlink())
        self.assertFalse(lock.exists())  # released after the operation


# ---------------------------------------------------------------------------
# 15. Security regressions (post-audit hardening)
# ---------------------------------------------------------------------------


class TestSecurityRegressions(Base):
    def _copy_link(self, name="demo"):
        """Activate a tracked copy in the global tier and return its manifest path."""
        self.make_skill(name)
        self.run_json("link", name, "--tier", "global", "--copy")
        return self.global_tier() / skillmgr.MANIFEST_NAME

    def test_sync_rejects_traversal_manifest_key_delete(self):
        # CRITICAL regression: a committed/hand-edited manifest key '../../victim'
        # must never reach a filesystem delete/overwrite outside the tier.
        mpath = self._copy_link()
        victim = Path(self.tmp) / "victim.txt"
        victim.write_text("TOP SECRET", encoding="utf-8")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        manifest["entries"]["../../victim.txt"] = {"kind": "skills", "mode": "copy"}
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        code, data = self.run_json("sync", "--tier", "global")
        self.assertEqual(code, 0)
        self.assertTrue(victim.exists(), "victim outside tier must survive")
        self.assertEqual(victim.read_text(encoding="utf-8"), "TOP SECRET")

    def test_load_manifest_drops_unsafe_keys(self):
        mpath = self._copy_link()
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        for bad in ("../escape", "a/b", "/abs", ".."):
            manifest["entries"][bad] = {"kind": "skills", "mode": "copy"}
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        loaded = skillmgr.load_manifest(self.global_tier())
        for bad in ("../escape", "a/b", "/abs", ".."):
            self.assertNotIn(bad, loaded["entries"])
        self.assertIn("demo", loaded["entries"])  # legitimate key survives

    def test_uninstall_ignores_traversal_manifest_key(self):
        mpath = self._copy_link()
        victim = Path(self.tmp) / "victim.txt"
        victim.write_text("KEEP", encoding="utf-8")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        manifest["entries"]["../../victim.txt"] = {"kind": "skills", "mode": "copy"}
        mpath.write_text(json.dumps(manifest), encoding="utf-8")
        code, _ = self.run_json("uninstall")
        self.assertEqual(code, 0)
        self.assertTrue(victim.exists())

    def test_validate_name_rejects_trailing_dot(self):
        for bad in ("foo.", "foo. ", "bar..", "baz "):
            with self.assertRaises(skillmgr.CmdError, msg="should reject %r" % bad):
                skillmgr.validate_name(bad)

    def test_is_safe_component(self):
        for good in ("foo", "foo.md", "a-b_c.1", "nightly.js"):
            self.assertTrue(skillmgr.is_safe_component(good))
        for bad in ("", ".", "..", "a/b", "../x", "/abs", "a\x00b"):
            self.assertFalse(skillmgr.is_safe_component(bad))

    def test_status_neutralizes_terminal_escapes(self):
        # A foreign-link target containing an ANSI escape + newline must not
        # reach the terminal raw in human output.
        self.make_skill("real")
        tier = self.global_tier()
        tier.mkdir(parents=True, exist_ok=True)
        evil_target = Path(self.tmp) / "\x1b[2Jwiped\nINJECTED"
        os.symlink(str(evil_target), str(tier / "evil"))
        code, out, _ = self.run_cli("status", "--kind", "skills")
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("INJECTED\n", out.replace("INJECTED: ", ""))

    def test_safe_display_strips_controls(self):
        self.assertNotIn("\x1b", skillmgr.safe_display("\x1b[31mred"))
        self.assertNotIn("\n", skillmgr.safe_display("a\nb"))
        self.assertEqual(skillmgr.safe_display("normal-name.md"), "normal-name.md")

    def test_doctor_fix_takes_lock(self):
        # doctor --fix mutates, so a held fresh lock must block it (exit 4).
        self.make_skill("real")
        self.global_tier().mkdir(parents=True, exist_ok=True)
        os.symlink("/nonexistent/x", str(self.global_tier() / "dead"))
        lock = Path(self.lib) / ".skillmgr.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("12345", encoding="utf-8")
        code, data = self.run_json("doctor", "--fix")
        self.assertEqual(code, 4)
        self.assertFalse(data["ok"])
        self.assertTrue((self.global_tier() / "dead").is_symlink())  # not removed
        lock.unlink()

    def test_import_wrong_extension_refused(self):
        src = Path(self.tmp) / "thing.js"
        src.write_text("x", encoding="utf-8")
        code, data = self.run_json("import", str(src), "--kind", "agents")
        self.assertNotEqual(code, 0)
        self.assertFalse(data["ok"])
        self.assertFalse((Path(self.lib) / "agents" / "thing.js").exists())


if __name__ == "__main__":
    unittest.main()
