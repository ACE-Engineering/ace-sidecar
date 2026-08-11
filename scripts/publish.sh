#!/usr/bin/env bash
set -euo pipefail

echo "==> Building and publishing ace-sidecar..."

# Ensure build and twine are installed
python3 -m pip install --quiet build twine

# Clean previous build artifacts
rm -rf dist/ build/ *.egg-info

# Build source distribution and wheel
python3 -m build

# Check distributions
twine check dist/*

# Upload to PyPI (requires TWINE_USERNAME/TWINE_PASSWORD or API token)
if [ "${1:-}" = "--publish" ]; then
    echo "==> Uploading dist/* to PyPI..."
    twine upload dist/*
else
    echo "==> Dry run complete. Use './scripts/publish.sh --publish' to upload to PyPI."
fi
