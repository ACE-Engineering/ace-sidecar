---
name: git-commit-and-push
description: Automates git working tree status check, file staging, commit creation, and branch pushing.
---
# Git Commit & Push Workflow

Automated git workflow mined from repeated user requests and transcript action loops.

## Step-by-Step Instructions
1. **Check Working Tree**:
   ```bash
   git status
   ```

2. **Stage Modified Files**:
   ```bash
   git add .
   ```

3. **Commit & Push**:
   ```bash
   git commit -m "$ARGUMENTS" && git push origin HEAD
   ```

## Verification
- Confirm working tree is clean and commits are safely pushed to remote branch.
