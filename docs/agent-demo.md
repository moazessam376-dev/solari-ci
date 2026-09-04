# Agent mode demo

Live dry-run output from:

```
solci agent moazessam376-dev/Gym-App --job typecheck --cpu 2 --brain codex --effort high --dry-run
```

```text
▮ SOLCI /// AGENT  measure, propose, and prepare a workflow change  DRY RUN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
◆ moazessam376-dev/Gym-App:typecheck
  ▸ 2 vCPU  created
  ▸ 2 vCPU  cpu online in 2.60s
  ▸ 2 vCPU  actions/setup-node@v6  started
  ▸ 2 vCPU  actions/setup-node@v6 5.0s ok
  ▸ 2 vCPU  npm ci  started
  ▸ 2 vCPU  npm ci 207.4s ok
  ▸ 2 vCPU  npm run typecheck  started
  ▸ 2 vCPU  npm run typecheck 16.1s ok
  ▸ 2 vCPU  done
  ◆ cloning repository
PROPOSED DIFF
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
index 2b05be7..41de1d0 100644
--- a/.github/workflows/ci.yml
+++ b/.github/workflows/ci.yml
@@ -40,6 +40,7 @@ jobs:
   # gate for those (security audit v2, section 11.1). tsc is green today.
   typecheck:
     runs-on: ubuntu-latest
+    timeout-minutes: 15
     steps:
       - uses: actions/checkout@v7
       - uses: actions/setup-node@v6

RATIONALE
Added `timeout-minutes: 15` to `typecheck`, bounding the measured 234.7-second
runtime (with `npm ci` taking 207.4 seconds). No other changes were justified.

```diff
diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml
@@ -40,6 +40,7 @@ jobs:
   # gate for those (security audit v2, section 11.1). tsc is green today.
   typecheck:
     runs-on: ubuntu-latest
+    timeout-minutes: 15
     steps:
```

YAML validation passed.
SUMMARY Added a 15-minute timeout to the typecheck job.
  dry run: no branch, commit, push, or PR created
Agent complete: proposal ready; no PR opened

[exited with code 0]
```
