# solci report: `moazessam376-dev/solci-browser-demo` / `e2e`

## Results

| CPU | Mem MB | Boot | CPU online | Total | Solari/run | Solari/month | Speedup vs 1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 2,048 | 64.4 s | 1.6 s | 214.9 s | $0.0055 | - | - |

## Total time

| Size | Total | Cost/run |
| --- | --- | --- |
| 2 vCPU | 214.9 s | $0.0055 |

## Browser

| Size | Sessions | Seconds | Cost |
| --- | --- | --- | --- |
| 2 vCPU | 1 | 5.6 s | $0.0002 |

## Per-step timing

| Step | 2 vCPU |
| --- | --- |
| actions/checkout@v4 | 3.1s (checkout done by runner) |
| actions/setup-node@v4 | 5.3s |
| npm ci | 133.5s |
| npx playwright install --with-deps chromium | 0.0s (playwright install skipped: Solari cloud Chrome is already provisioned) |
| npx playwright test | 5.0s (cloud browser: 1 session, 5.6s, $0.0002) |

## Recommendation

> Only one size measured (2 vCPU: 215 s for $0.0055 per run); run with more --cpu sizes to compare.

## Findings

| Severity | Code | Step | Message | Suggestion |
| --- | --- | --- | --- | --- |
| low | NO_TIMEOUT | job | Job `e2e` has no job-level timeout. | Add `timeout-minutes: 15` under this job to bound its runtime. |
| low | NO_CONCURRENCY | job | This pull-request workflow has no concurrency group to cancel superseded runs. | Add `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` at the workflow top level. |

## How measured

_Measured on Solari microVMs; workflow steps run natively. Actions shimmed: actions/setup-python, astral-sh/setup-uv, actions/setup-node, pnpm/action-setup, oven-sh/setup-bun. Actions skipped/no-op: actions/checkout (runner clones the repository), actions/cache, actions/upload-artifact, actions/download-artifact, codecov/*, unsupported actions._
