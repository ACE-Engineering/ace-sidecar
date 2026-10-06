"""scripts/release.py: the version, changelog and Homebrew formula move together.

The release workflow publishes whatever pyproject says once main carries an untagged
version, so these edits are the release. Each one is pinned here against a copy of the
repo files, never the real ones.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "release", ROOT / "scripts" / "release.py"
)
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

CHANGELOG = """# Changelog

Intro.

---

## [Unreleased]

### Fixed
- A thing.

---

## [1.2.3] - 2026-01-01

### Added
- An older thing.

---
"""


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "Formula").mkdir()
    shutil.copy(
        ROOT / "Formula" / "ace-sidecar.rb", tmp_path / "Formula" / "ace-sidecar.rb"
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "ace-sidecar"\nversion = "1.2.3"\n'
    )
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG)
    monkeypatch.setattr(release, "PYPROJECT", tmp_path / "pyproject.toml")
    monkeypatch.setattr(release, "CHANGELOG", tmp_path / "CHANGELOG.md")
    monkeypatch.setattr(release, "FORMULA", tmp_path / "Formula" / "ace-sidecar.rb")
    return tmp_path


@pytest.mark.parametrize(
    "bump, expected",
    [("patch", "1.2.4"), ("minor", "1.3.0"), ("major", "2.0.0"), ("1.10.0", "1.10.0")],
)
def test_next_version(bump, expected):
    assert release.next_version("1.2.3", bump) == expected


@pytest.mark.parametrize("bump", ["1.2.3", "1.2.2", "v1.3.0", "next"])
def test_a_version_that_is_not_newer_or_not_semver_is_refused(bump):
    with pytest.raises(release.ReleaseError):
        release.next_version("1.2.3", bump)


def test_rolling_moves_unreleased_under_the_version_and_leaves_it_empty():
    rolled = release.roll_changelog(CHANGELOG, "1.3.0", "2026-10-05")
    assert (
        "## [Unreleased]\n\n---\n\n## [1.3.0] - 2026-10-05\n\n### Fixed\n- A thing.\n\n---"
        in rolled
    )
    assert rolled.index("## [1.3.0]") < rolled.index("## [1.2.3]")
    assert rolled.count("- A thing.") == 1


def test_an_empty_unreleased_section_is_refused():
    empty = CHANGELOG.replace("### Fixed\n- A thing.\n", "")
    with pytest.raises(release.ReleaseError, match="empty"):
        release.roll_changelog(empty, "1.3.0", "2026-10-05")


def test_notes_are_the_version_section_body(repo):
    assert release.release_notes("1.2.3") == "### Added\n- An older thing.\n"
    with pytest.raises(release.ReleaseError):
        release.release_notes("9.9.9")


def test_prepare_bumps_rolls_and_pins_the_formula(repo, monkeypatch):
    def fake_build(outdir):
        sdist = outdir / "ace_sidecar-1.3.0.tar.gz"
        sdist.write_bytes(b"sdist")
        return sdist

    monkeypatch.setattr(release, "build_sdist", fake_build)
    assert release.main(["prepare", "minor", "--date", "2026-10-05"]) == 0

    assert release.current_version() == "1.3.0"
    assert release.release_notes("1.3.0") == "### Fixed\n- A thing.\n"
    url, sha = release.formula_pin()
    assert url.endswith("/ace_sidecar-1.3.0.tar.gz")
    assert sha == release.hashlib.sha256(b"sdist").hexdigest()
    assert release.main(["check"]) == 0


def test_check_refuses_a_formula_that_does_not_match_the_build(
    repo, monkeypatch, tmp_path
):
    def fake_build(outdir):
        sdist = outdir / "ace_sidecar-1.3.0.tar.gz"
        sdist.write_bytes(b"a")
        return sdist

    monkeypatch.setattr(release, "build_sdist", fake_build)
    assert release.main(["prepare", "1.3.0"]) == 0
    other = tmp_path / "ace_sidecar-1.3.0.tar.gz"
    other.write_bytes(b"b")
    assert release.main(["check", "--sdist", str(other)]) == 1


def test_check_refuses_a_version_bump_without_the_formula(repo):
    # 1.2.3 has a changelog section, but the formula still pins the repo's own release.
    assert release.main(["check"]) == 1


def test_the_repo_itself_is_consistent():
    """The checked-in version has a changelog section and the formula pins its sdist."""
    assert release.main(["check"]) == 0
