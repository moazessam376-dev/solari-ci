# solci report: `moazessam376-dev/Gym-App` / `typecheck`

## Results

| CPU | Mem MB | Boot | CPU online | Total | Solari/run | Solari/month | Speedup vs 1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2,048 | 1.1 s | 0.0 s | 67.1 s | $0.0011 | $0.0182 | 1.00x |
| 2 | 2,048 | 0.8 s | 0.4 s | 48.5 s | $0.0012 | $0.0212 | 1.38x |
| 4 | 4,096 | 0.3 s | 2.6 s | 45.5 s | $0.0023 | $0.0399 | 1.47x |

GitHub baseline: median 39.0 s, p90 57.0 s, 20 runs, $0.0100/run, 17.1 runs/month.

## Total time

| Size | Total | Cost/run |
| --- | --- | --- |
| 1 vCPU | 67.1 s | $0.0011 |
| 2 vCPU | 48.5 s | $0.0012 |
| 4 vCPU | 45.5 s | $0.0023 |
| GitHub ubuntu-latest | 39.0 s | $0.0100 |

## Per-step timing

| Step | 1 vCPU | 2 vCPU | 4 vCPU |
| --- | --- | --- | --- |
| actions/checkout@v7 | 3.0s (checkout done by runner) | 2.9s (checkout done by runner) | 2.8s (checkout done by runner) |
| actions/setup-node@v6 | 5.4s | 5.2s | 5.1s |
| npm ci | 34.0s | 22.8s | 20.6s |
| npm run typecheck | 23.7s | 16.5s | 13.9s |

## Recommendation

> Use 2 vCPU: 48 s for $0.0012 per run, within 10% of the 4 vCPU time (46 s) at 53% of its cost. GitHub ubuntu-latest median is 39 s ($0.010/run).

## Findings

| Severity | Code | Step | Message | Suggestion |
| --- | --- | --- | --- | --- |
| low | NO_TIMEOUT | job | Job `typecheck` has no job-level timeout. | Add `timeout-minutes: 15` under this job to bound its runtime. |
| low | NO_CONCURRENCY | job | This pull-request workflow has no concurrency group to cancel superseded runs. | Add `concurrency: {group: ci-${{ github.ref }}, cancel-in-progress: true}` at the workflow top level. |
| high | HIGH_FAILURE_RATE | job | The historical failure rate for `typecheck` is 35%. | Review the flaky step and test logs, then fix or isolate the failures before scaling this job. |

## How measured

_Measured on Solari microVMs; workflow steps run natively. Actions shimmed: actions/setup-python, astral-sh/setup-uv, actions/setup-node, pnpm/action-setup, oven-sh/setup-bun. Actions skipped/no-op: actions/checkout (runner clones the repository), actions/cache, actions/upload-artifact, actions/download-artifact, codecov/*, unsupported actions._
