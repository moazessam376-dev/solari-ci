# solci report: `moazessam376-dev/solci-browser-demo` / `e2e`

## Results

| CPU | Mem MB | Boot | CPU online | Total | Solari/run | Solari/month | Speedup vs 1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 2,048 | 0.8 s | 2.6 s | 438.2 s | $0.0112 | - | - |

## Total time

| Size | Total | Cost/run |
| --- | --- | --- |
| 2 vCPU | 438.2 s | $0.0112 |

## Browser

| Size | Sessions | Seconds | Cost |
| --- | --- | --- | --- |
| 2 vCPU | 1 | 5.8 s | $0.0002 |

## Per-step timing

| Step | 2 vCPU |
| --- | --- |
| actions/checkout@v4 | 2.8s (checkout done by runner) |
| actions/setup-node@v4 | 3.3s |
| npm ci | 421.5s |
| npx playwright install --with-deps chromium | 0.0s (playwright install skipped: Solari cloud Chrome is already provisioned) |
| npx playwright test | 5.1s (cloud browser: 1 session, 5.8s, $0.0002) |

## Recommendation

> Only one size measured (2 vCPU: 438 s for $0.0112 per run); run with more --cpu sizes to compare.

## Findings

| Severity | Code | Step | Message | Suggestion |
| --- | --- | --- | --- | --- |
| low | NO_TIMEOUT | job | Job `e2e` has no job-level timeout. | Add `timeout-minutes: 15` under this job to bound its runtime. |
| low | NO_CONCURRENCY | job | This pull-request workflow has no concurrency group to cancel superseded runs. | Add `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` at the workflow top level. |

## How measured

_Measured on Solari microVMs; workflow steps run natively. Actions shimmed: actions/setup-python, astral-sh/setup-uv, actions/setup-node, pnpm/action-setup, oven-sh/setup-bun. Actions skipped/no-op: actions/checkout (runner clones the repository), actions/cache, actions/upload-artifact, actions/download-artifact, codecov/*, unsupported actions._

## Test output

The `npx playwright test` step ran the two specs in [solci-browser-demo](https://github.com/moazessam376-dev/solci-browser-demo): one against the repo's own `http-server` on port 4173, reached through the sandbox preview URL with the auth token preserved, and one against an external site.

```
Running 2 tests using 1 worker
··
  2 passed (2.9s)
```

The sandbox list was empty after the run and the single browser session was released in the step's `finally` block.
