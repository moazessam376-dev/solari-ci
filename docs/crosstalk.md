# solci report: `moazessam376-dev/crosstalk` / `test`

## Results

| CPU | Mem MB | Boot | CPU online | Total | Solari/run | Solari/month | Speedup vs 1 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 2,048 | 1.9 s | 0.0 s | 191.5 s | $0.0030 | $1.8188 | 1.00x |
| 2 | 2,048 | 1.9 s | 2.6 s | 197.5 s | $0.0050 | $3.0289 | 0.97x |
| 4 | 4,096 | 0.2 s | 3.1 s | 228.3 s | $0.0117 | $7.0006 | - |
| 8 | 8,192 | 0.3 s | 2.6 s | 277.7 s | $0.0284 | $17.0323 | - |

GitHub baseline: median 122.0 s, p90 160.0 s, 18 runs, $0.0000/run, 600.0 runs/month.

## Total time

| Size | Total | Cost/run |
| --- | --- | --- |
| 1 vCPU | 191.5 s | $0.0030 |
| 2 vCPU | 197.5 s | $0.0050 |
| 4 vCPU | 228.3 s | $0.0117 |
| 8 vCPU | 277.7 s | $0.0284 |
| GitHub ubuntu-latest | 122.0 s | $0.0000 |

## Per-step timing

| Step | 1 vCPU | 2 vCPU | 4 vCPU | 8 vCPU |
| --- | --- | --- | --- | --- |
| actions/checkout@v7 | 2.8s (checkout done by runner) | 2.8s (checkout done by runner) | 2.8s (checkout done by runner) | 2.8s (checkout done by runner) |
| actions/setup-node@v7 | 5.0s | 5.1s | 2.8s | 2.8s |
| npm ci | 11.7s | 10.2s | 9.4s | 9.5s |
| npm run typecheck | 16.2s | 11.7s | 9.5s | 7.3s |
| npm run build | 11.7s | 7.3s | 7.7s | 8.0s |
| npm test | 142.0s | 155.9s | failed (192.7s) | failed (244.4s) |

## Recommendation

> Use 1 vCPU: 191 s for $0.0030 per run, within 10% of the 1 vCPU time (191 s) at 100% of its cost. GitHub ubuntu-latest median is 122 s (free on public repos; private-rate reference $0.030/run).

## Findings

| Severity | Code | Step | Message | Suggestion |
| --- | --- | --- | --- | --- |
| low | NO_TIMEOUT | job | Job `test` has no job-level timeout. | Add `timeout-minutes: 15` under this job to bound its runtime. |
| info | MATRIX_NOTE | job | Solari only measured and analyzed the first matrix cell for this job. | Re-run with the other matrix values explicitly if you need results for every cell. |

## How measured

_Measured on Solari microVMs; workflow steps run natively. Actions shimmed: actions/setup-python, astral-sh/setup-uv, actions/setup-node, pnpm/action-setup, oven-sh/setup-bun. Actions skipped/no-op: actions/checkout (runner clones the repository), actions/cache, actions/upload-artifact, actions/download-artifact, codecov/*, unsupported actions._
