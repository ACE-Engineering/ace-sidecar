---
name: verify-test-and-lint
description: Runs ruff linter auto-fixes, pytest unit/integration test suite, and coverage ratchet check.
---
# Verify Test & Lint Workflow

Automated test and code quality verification mined from repeated transcript sessions.

## Step-by-Step Instructions
1. **Ruff Linter & Import Ordering Pass**:
   ```bash
   .venv/bin/ruff check --fix .
   ```

2. **Run Pytest Suite**:
   ```bash
   .venv/bin/pytest
   ```

3. **Coverage Ratchet Verification**:
   ```bash
   .venv/bin/python scripts/coverage_report.py --check
   ```

## Verification
- Confirm 100% test pass rate and coverage ratchet approval.
