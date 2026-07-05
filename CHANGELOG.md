# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-05

### Added

- Three-tier activation model (library / global / project) for four asset
  kinds: skills, agents, commands, and workflows.
- Activation strategy: symlink where possible, otherwise a tracked copy
  (hash-recorded in a manifest) as a universal, cross-platform fallback.
- Drift detection and `sync` repair for tracked copies that have diverged
  from their library source.
- `doctor` command that diagnoses (and with `--fix` repairs) dangling links,
  foreign links, drifted and locally-modified tracked copies, manifest orphans,
  and corrupt config/manifest files.
- Transactional `adopt`: moves real content into the library and relinks it,
  rolling back on failure so no state is lost midway.
- Hardened asset-name validation rejecting path traversal, reserved names,
  and unsafe characters.
- Hermetic test suite: no network, no subprocess, isolated temporary
  home and project directories.
