# Solari platform findings

I pulled these together while building three tools on Solari: solari-ci (right-sizing GitHub Actions jobs in Solari microVMs), solari-lab (a CLI that measures the Solari account itself), and solari-playtest (a browser agent that plays web games in Solari cloud Chrome). Dates: 2026-09-02 to 2026-09-04. I am writing these as tickets I would paste into a Discord bug or feedback channel, one per observation, so a Solari developer can act on each without reading three codebases.

## Tickets

### 1. exec has a hard wall clock cap around 28 s
type: bug
what I observed: a single `POST /sandboxes/:id/exec` call takes up to roughly 28 seconds wall clock before it gets killed or times out, even when I ask for a longer timeout.
how to reproduce: run any shell command that takes longer than about 28 s through exec and watch it get cut off.
how I worked around it: I clamp my own client timeout to 24 s, launch the real command with `nohup` in the background, and poll a log file and an exit-code file until the command finishes.
what I would want: either document this cap explicitly, or give me a streaming or async exec endpoint so I do not need a log/exit-file dance for anything longer than half a minute.

### 2. exec request body has a size cap around 16 KB (16384 bytes)
type: bug
what I observed: sending a large base64-encoded payload as part of an exec command fails once the request body gets big enough. I never found the exact documented number, so I am reporting the figure I was told to expect, roughly 16384 bytes, and my own code treats two combined base64 preloads as unsafe to send in one call even though either one alone is fine.
how to reproduce: base64-encode a file of a few tens of KB and try to write it in one exec call with `printf | base64 -d`.
how I worked around it: I split uploads into separate exec calls, one per file, instead of batching them.
what I would want: document the exact request body size limit and return a clear 4xx error naming the limit instead of a generic failure.

### 3. vCPUs beyond the first hot-plug 1 to 15 s after boot
type: quirk
what I observed: right after `POST /sandboxes` returns, `nproc` reports 1 even if I asked for more vCPUs. The rest come online somewhere between 1 and 15 seconds later.
how to reproduce: create a sandbox with cpu > 1 and immediately exec `nproc`.
how I worked around it: I poll `nproc` in a loop until it reports the requested count, bounded at 40 s before I give up.
what I would want: either return the sandbox only once every vCPU is online, or document the hot-plug delay so callers know to wait.

### 4. polling GET /sandboxes/:id seems to refresh the idle timeout
type: bug
what I observed: a sandbox that someone keeps polling with GET never seems to hit its idle timeout, even while doing nothing else. Conversely, sandboxes orphaned by a crashed client stayed alive until I deleted them by id.
how to reproduce: create a sandbox, poll GET on it repeatedly without running any exec, and watch it stay alive past its stated idle timeout.
how I worked around it: I make sure to always delete sandboxes explicitly in cleanup rather than relying on the idle timer, and I record cleanup failures instead of trusting expiry.
what I would want: the idle timer should key off exec activity, not reads, so a watched sandbox does not live forever.

### 5. Starter plan allows 2 concurrent sandboxes
type: docs gap
what I observed: a third `POST /sandboxes` call fails once 2 are already running on the Starter plan. This blocks a 4-size sweep from ever running fully in parallel.
how to reproduce: create 2 sandboxes on a Starter key, then try to create a third at the same time.
how I worked around it: I run sizes sequentially or in small batches instead of all at once.
what I would want: document the concurrency limit per plan in both the error response and the plan page.

### 6. HOME is unset in a fresh sandbox
type: bug
what I observed: a brand new sandbox has no `HOME` environment variable set, and this breaks tools like npm, pip, and git that assume it exists.
how to reproduce: create a sandbox and exec `echo $HOME` before setting anything.
how I worked around it: I export `HOME=/root` explicitly before running any step or shim.
what I would want: set a sane default `HOME` on sandbox creation so tools do not silently misbehave.

### 7. Default memory is 2048 MB or 1024 MB per vCPU, whichever is larger
type: docs gap
what I observed: when I do not specify memory, sandboxes come up with `max(2048, 1024 * cpu)` MB. This matches what my README says, and I confirmed it against my own default calculation.
how to reproduce: create a sandbox with cpu=4 and no memMb, then check reported memory.
how I worked around it: I compute the same default client-side so my cost estimates match what I actually get.
what I would want: state this formula in the sandbox creation docs so nobody has to reverse engineer it.

### 8. A base sandbox has roughly 2.2 GB of disk free
type: quirk
what I observed: before cloning anything, a fresh sandbox has about 2.2 GB of usable disk. Larger monorepos or a Playwright browser download do not fit comfortably.
how to reproduce: create a sandbox and check free disk space before cloning a repo.
how I worked around it: I keep clones shallow and skip steps that would need much more disk than that.
what I would want: document the disk allowance per template, and let me request more disk at creation time if I need it.

### 9. No Docker or container runtime inside a sandbox
type: feature request
what I observed: there is no way to run Docker or any container runtime inside a Solari sandbox, so jobs that declare `services:` or `container:` cannot run natively.
how to reproduce: try to run `docker` inside any sandbox, or run a GitHub Actions job that uses `services:`.
how I worked around it: I detect these jobs ahead of time and refuse to run them with a clear exit code instead of trying and failing partway through.
what I would want: either support a container runtime, or document clearly upfront that this is out of scope so tools do not need to guess.

### 10. Preview URL auth token gets dropped by relative URL resolution
type: bug
what I observed: `GET /sandboxes/:id/ports/:port` returns a preview URL that carries a required auth token in its query string. If a client resolves a relative path (like `/`) against that URL as a base, the WHATWG URL constructor drops the base's own query string, so the token is silently lost and the navigation loses auth.
how to reproduce: use the preview URL as a Playwright `baseURL` and call `page.goto("/")`.
how I worked around it: I rebuild the URL myself at navigation time, preserving the token, instead of letting the browser tool resolve it.
what I would want: accept the token as a header or cookie instead of (or in addition to) a query parameter, or move to a subdomain-based preview URL that does not need one.

### 11. Cloud browser sessions drop after about 10 minutes
type: quirk
what I observed: a CDP session over `wss://api.getsolari.com/cdp/<id>` can drop after roughly 10 minutes, which breaks a long-running Playwright suite mid-run.
how to reproduce: hold a raw CDP connection open and keep using it past the 10 minute mark.
how I worked around it: I scope sessions narrowly, one per test step, and release them right after; for longer agent runs I detect the drop and reconnect, redoing the step that was interrupted.
what I would want: document the session lifetime, or offer a longer TTL option per session for workloads that need it.

### 12. No endpoint to list browser sessions
type: feature request
what I observed: there is no `GET /sessions` or equivalent, so a session leaked by a crashed client cannot be found and cleaned up through the API.
how to reproduce: create a session, kill the client process before it releases, then try to find that session again through the API.
how I worked around it: I keep a local ledger of every session I create so I can at least track and clean up my own leaks client-side.
what I would want: a `GET /sessions` endpoint so leaked sessions are discoverable and killable without a client-side ledger.

### 13. WebGL is software rendered
type: docs gap
what I observed: WebGL inside a Solari sandbox runs on Mesa llvmpipe, a software renderer, so frame rates are noticeably lower than on a real GPU.
how to reproduce: run any WebGL canvas inside a sandbox and check the renderer string.
how I worked around it: I report the lower frame rate as expected rather than as a bug in the game being tested.
what I would want: document that GPU acceleration is not available so people testing graphics-heavy apps know upfront what they are measuring.

## What worked well

Sandbox boot is fast, about 1 second, and a shallow git clone right after is sub-second too, which makes short-lived sandboxes practical for CI-style workloads. Requested `cpu` and `memMb` are honored up to 16 vCPU and 16 GB, which is more than enough headroom for the sizes I tested. Once I accounted for the quirks above, the platform was reliable and predictable to build on.

## Ready to paste

1. exec calls get killed around 28s wall clock, no documented cap or async option.
2. exec request body has a size cap near 16KB, no clear error when you hit it.
3. nproc reports 1 right after boot, extra vCPUs hot-plug 1-15s later.
4. Polling GET /sandboxes/:id seems to refresh the idle timer, sandboxes never expire while watched.
5. Starter plan caps concurrent sandboxes at 2, not documented in the error.
6. HOME is unset in a fresh sandbox, breaks npm/pip/git until you export it.
7. Default memory is max(2048MB, 1024MB per vCPU), not stated anywhere obvious.
8. Base sandbox only has about 2.2GB free disk, too tight for some monorepos.
9. No Docker or container runtime, services/container jobs cannot run at all.
10. Preview URL auth token in the query string gets dropped by relative URL resolution.
11. Cloud browser CDP sessions drop after about 10 minutes, no documented TTL.
12. No GET /sessions endpoint, leaked browser sessions cannot be found or killed.
13. WebGL is software rendered (Mesa llvmpipe), no GPU acceleration, not documented.
14. Good: about 1s sandbox boot and sub-second shallow clone.
15. Good: cpu/memMb honored up to 16 vCPU and 16GB.
