#!/usr/bin/env python3
"""Cut and check an ace-sidecar release.

A release is one pull request that changes three files together, and merging it is the
release: ``.github/workflows/publish.yml`` sees a version on main with no tag yet, then
publishes to PyPI, tags it and creates the GitHub release.

* ``pyproject.toml`` -- the version.
* ``CHANGELOG.md`` -- the ``[Unreleased]`` notes move under ``[X.Y.Z] - date``; the GitHub
  release body is that section.
* ``Formula/ace-sidecar.rb`` -- the PyPI sdist URL and its sha256. hatchling builds the
  sdist reproducibly (fixed timestamps, sorted entries), so the hash computed here is the
  hash of the file the workflow uploads; the workflow checks it before publishing. The
  sdist holds src/ and tests/, so a release PR rebased onto a newer main needs ``pin``.

Usage::

    python scripts/release.py prepare patch      # or minor, major, or 1.2.3
    python scripts/release.py pin                # re-pin the formula after rebasing
    python scripts/release.py check [--sdist dist/ace_sidecar-1.2.3.tar.gz]
    python scripts/release.py notes 1.2.3
    python scripts/release.py version
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"
FORMULA = ROOT / "Formula" / "ace-sidecar.rb"

UNRELEASED = "## [Unreleased]"
SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
SDIST_URL = "https://files.pythonhosted.org/packages/source/a/ace-sidecar/ace_sidecar-{v}.tar.gz"


class ReleaseError(Exception):
    pass


def current_version() -> str:
    return tomllib.loads(PYPROJECT.read_text())["project"]["version"]


def next_version(current: str, bump: str) -> str:
    m = SEMVER.match(current)
    if not m:
        raise ReleaseError(f"pyproject version {current!r} is not X.Y.Z")
    major, minor, patch = map(int, m.groups())
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    new = SEMVER.match(bump)
    if not new:
        raise ReleaseError(f"{bump!r} is not major, minor, patch or X.Y.Z")
    if tuple(map(int, new.groups())) <= (major, minor, patch):
        raise ReleaseError(f"{bump} is not newer than the current {current}")
    return bump


def _section_bounds(lines: list[str], heading_prefix: str) -> tuple[int, int]:
    """Line range of a changelog section's body: after its heading, up to the next
    ``---`` rule or ``## [`` heading."""
    start = next(
        (i for i, ln in enumerate(lines) if ln.startswith(heading_prefix)), None
    )
    if start is None:
        raise ReleaseError(f"CHANGELOG.md has no '{heading_prefix}' section")
    end = start + 1
    while (
        end < len(lines)
        and lines[end].strip() != "---"
        and not lines[end].startswith("## [")
    ):
        end += 1
    return start + 1, end


def release_notes(version: str) -> str:
    lines = CHANGELOG.read_text().splitlines()
    start, end = _section_bounds(lines, f"## [{version}] - ")
    notes = "\n".join(lines[start:end]).strip()
    if not notes:
        raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
    return notes + "\n"


def roll_changelog(text: str, version: str, date: str) -> str:
    lines = text.splitlines()
    start, end = _section_bounds(lines, UNRELEASED)
    body = "\n".join(lines[start:end]).strip()
    if not body:
        raise ReleaseError(
            "CHANGELOG.md [Unreleased] is empty: write the release notes first"
        )
    if any(ln.startswith(f"## [{version}]") for ln in lines):
        raise ReleaseError(f"CHANGELOG.md already has a [{version}] section")
    rolled = [UNRELEASED, "", "---", "", f"## [{version}] - {date}", "", body, ""]
    return "\n".join(lines[: start - 1] + rolled + lines[end:]) + "\n"


def set_pyproject_version(text: str, version: str) -> str:
    new, n = re.subn(
        r'(?m)^version = "[^"]*"$', f'version = "{version}"', text, count=1
    )
    if n != 1:
        raise ReleaseError("pyproject.toml has no top-level version line")
    return new


def set_formula(text: str, version: str, sha256: str) -> str:
    text, n_url = re.subn(
        r'(?m)^(\s*url )"[^"]*"$', rf'\1"{SDIST_URL.format(v=version)}"', text
    )
    text, n_sha = re.subn(r'(?m)^(\s*sha256 )"[0-9a-f]*"$', rf'\1"{sha256}"', text)
    if n_url != 1 or n_sha != 1:
        raise ReleaseError(
            "Formula/ace-sidecar.rb needs exactly one url and one sha256 line"
        )
    return text


def formula_pin() -> tuple[str, str]:
    text = FORMULA.read_text()
    url = re.search(r'(?m)^\s*url "([^"]*)"$', text)
    sha = re.search(r'(?m)^\s*sha256 "([0-9a-f]*)"$', text)
    if not url or not sha:
        raise ReleaseError("Formula/ace-sidecar.rb has no url or sha256 line")
    return url.group(1), sha.group(1)


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_sdist(outdir: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir), str(ROOT)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    (sdist,) = outdir.glob("*.tar.gz")
    return sdist


def pin_formula(version: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        sdist = build_sdist(Path(tmp))
        expected = f"ace_sidecar-{version}.tar.gz"
        if sdist.name != expected:
            raise ReleaseError(f"built {sdist.name}, expected {expected}")
        sha = sha256_of(sdist)
    FORMULA.write_text(set_formula(FORMULA.read_text(), version, sha))
    return sha


def cmd_prepare(args: argparse.Namespace) -> None:
    version = next_version(current_version(), args.bump)
    date = args.date or datetime.date.today().isoformat()
    changelog = roll_changelog(CHANGELOG.read_text(), version, date)
    pyproject = set_pyproject_version(PYPROJECT.read_text(), version)

    PYPROJECT.write_text(pyproject)
    CHANGELOG.write_text(changelog)
    pin_formula(version)

    print(f"Prepared {version}: pyproject.toml, CHANGELOG.md, Formula/ace-sidecar.rb.")
    print(
        f"Open a PR from a release/v{version} branch; merging it publishes the release."
    )


def cmd_pin(args: argparse.Namespace) -> None:
    version = current_version()
    print(f"Formula pinned to the {version} sdist, sha256 {pin_formula(version)}")


def cmd_check(args: argparse.Namespace) -> None:
    version = current_version()
    release_notes(version)
    url, sha = formula_pin()
    if url != SDIST_URL.format(v=version):
        raise ReleaseError(f"Formula url is {url}, expected the {version} sdist")
    if args.sdist:
        built = sha256_of(Path(args.sdist))
        if built != sha:
            raise ReleaseError(
                f"Formula sha256 {sha} does not match the built sdist ({built}); "
                "run `scripts/release.py pin` on the release branch, up to date with main"
            )
    print(
        f"{version}: changelog section, formula url"
        + (" and sdist hash" if args.sdist else "")
        + " agree"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser(
        "prepare", help="bump the version, roll the changelog, pin the formula"
    )
    p.add_argument("bump", help="major, minor, patch, or an explicit X.Y.Z")
    p.add_argument("--date", help="release date for the changelog (default: today)")
    p.set_defaults(func=cmd_prepare)
    pn = sub.add_parser("pin", help="re-pin the formula to this checkout's sdist")
    pn.set_defaults(func=cmd_pin)
    c = sub.add_parser("check", help="the version, changelog and formula agree")
    c.add_argument("--sdist", help="built sdist whose sha256 the formula must carry")
    c.set_defaults(func=cmd_check)
    n = sub.add_parser("notes", help="print a version's changelog section")
    n.add_argument("version")
    n.set_defaults(func=lambda a: sys.stdout.write(release_notes(a.version)))
    v = sub.add_parser("version", help="print the pyproject version")
    v.set_defaults(func=lambda a: print(current_version()))
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
