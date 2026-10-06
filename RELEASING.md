# Releasing

Merging a version bump to `main` publishes it. `.github/workflows/publish.yml` sees a
`pyproject.toml` version with no `v<version>` tag, then:

1. runs the tests and builds the wheel and sdist;
2. checks that `CHANGELOG.md` has a section for the version and that `Formula/ace-sidecar.rb` pins this exact sdist;
3. publishes to PyPI through trusted publishing;
4. tags the merge commit `v<version>` and creates the GitHub release, using the changelog section as notes and attaching the wheel and sdist.

A version that is already tagged is skipped, so other merges to `main` never publish anything.

## Cutting a release

```bash
git switch -c release/vX.Y.Z origin/main
python scripts/release.py prepare patch   # or minor, major, or X.Y.Z
git commit -am "chore(release): X.Y.Z"
```

`prepare` bumps the version, moves the `[Unreleased]` notes under `[X.Y.Z] - <today>`, builds the sdist and pins the formula to its URL and sha256. It refuses an empty `[Unreleased]` section, so write the notes in the PRs that make the changes.

Open the PR and merge it. The release workflow does the rest.

## When `main` moves first

The sdist contains `src/` and `tests/`, so the formula's hash only matches a build of the same tree. If other PRs merge before the release PR, rebase it and re-pin:

```bash
git rebase origin/main
python scripts/release.py pin
git commit -am "chore(release): re-pin the formula"
```

If the hashes still differ when the workflow runs, it stops before uploading anything. Run `pin` on an up-to-date branch and merge again. The build is reproducible because `hatchling` is pinned in `pyproject.toml` and hatchling fixes timestamps and file order.

## Rerunning a failed release

Every step can be rerun. The PyPI upload skips files already there, and the version is not tagged until the GitHub release step, so rerunning the workflow from the Actions tab (`workflow_dispatch`) picks up where it stopped.
