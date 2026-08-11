---
name: sync-proto-skills
description: Generates gRPC proto stubs and synchronizes frontend TypeScript skill definitions.
---
# Sync Proto Skills Workflow

Mined from repeated gRPC schema compilation & frontend TypeScript enum synchronization passes.

## Step-by-Step Instructions
1. **Regenerate Python Proto Stubs**:
   ```bash
   ./scripts/gen_proto.sh
   ```

2. **Sync Frontend Skills Enum**:
   ```bash
   bun run sync:skills
   ```

## Verification
- Confirm `skills.ts` matches `skills.proto` without schema drift.
