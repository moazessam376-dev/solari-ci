"""Cloud Chrome detection, preload payloads, and session helpers."""

from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import SolariClient

SUPPORTED_BROWSER_TOOLS = frozenset(
    {
        "playwright",
        "@playwright/test",
        "puppeteer",
        "cypress",
        "pytest-playwright",
        "browser-use",
    }
)

_PACKAGE_DEPENDENCY_FIELDS = (
    "dependencies",
    "devDependencies",
    "peerDependencies",
    "optionalDependencies",
)
_REQUIREMENTS_NAME = re.compile(r"^requirements(?:[-_.].*)?\.txt$", re.IGNORECASE)
_EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "dist", "build"}
_DEPENDENCY_SEPARATORS = re.compile(r"[<>=!~\[\];\s]")


JS_PRELOAD = r"""
(function () {
  const cdpUrl = process.env.SOLCI_CDP_URL;
  if (!cdpUrl) return;

  const baseUrlMap = (() => {
    try {
      return JSON.parse(process.env.SOLCI_BASE_URL_MAP || "{}") || {};
    } catch (_) {
      return {};
    }
  })();

  function mappedUrl(value) {
    if (typeof value !== "string") return null;
    const match = value.match(
      /^(https?):\/\/(localhost|127\.0\.0\.1):(\d+)(?=$|[\/?#])/i,
    );
    if (!match) return null;
    const source = `${match[1].toLowerCase()}://${match[2].toLowerCase()}:${match[3]}`;
    const target = baseUrlMap[source];
    if (typeof target !== "string") return null;
    return {
      target: String(target).replace(/\/$/, ""),
      suffix: value.slice(match[0].length),
    };
  }

  function rewriteUrl(value) {
    const mapping = mappedUrl(value);
    if (!mapping) return value;
    return mapping.target + mapping.suffix;
  }

  function mapAbsolute(absUrl) {
    if (typeof absUrl !== "string") return absUrl;
    let u;
    try {
      u = new URL(absUrl);
    } catch (_) {
      return absUrl;
    }
    const host = u.hostname.toLowerCase();
    if (host !== "localhost" && host !== "127.0.0.1") return absUrl;
    const key = `${u.protocol}//${host}:${u.port}`;
    const target = baseUrlMap[key];
    if (typeof target !== "string") return absUrl;
    let t;
    try {
      t = new URL(target);
    } catch (_) {
      return absUrl;
    }
    const params = new URLSearchParams(t.search);
    const orig = new URLSearchParams(u.search);
    for (const [k, v] of orig) params.append(k, v);
    const qs = params.toString();
    return t.origin + u.pathname + (qs ? "?" + qs : "") + u.hash;
  }

  function loadRepoModule(name) {
    return require(require.resolve(name, { paths: [process.cwd()] }));
  }

  function patchPage(page) {
    if (!page || page.__solciCloudBrowserPatched) return page;
    page.__solciCloudBrowserPatched = true;
    const goto = page.goto;
    if (typeof goto === "function") {
      page.goto = function (url, ...args) {
        // Playwright resolves a relative URL against the context's baseURL
        // using the WHATWG URL constructor, which DROPS the base URL's own
        // query string whenever the relative reference is path-absolute
        // (e.g. "/"). Our rewritten baseURL carries a required auth token in
        // its query string, so we must resolve relative URLs ourselves,
        // preserving that token, before handing an absolute URL to the real
        // goto implementation.
        let target = url;
        if (typeof url === "string" && !/^https?:\/\//i.test(url)) {
          const base =
            (this._browserContext &&
              this._browserContext._options &&
              this._browserContext._options.baseURL) ||
            undefined;
          if (base) {
            try {
              const baseParsed = new URL(base);
              const resolved = new URL(url, base);
              if (!resolved.search && baseParsed.search) {
                resolved.search = baseParsed.search;
              }
              target = resolved.toString();
            } catch (_) {
              target = url;
            }
          }
        }
        if (typeof target === "string" && /^https?:\/\//i.test(target)) {
          return goto.call(this, mapAbsolute(target), ...args);
        }
        return goto.call(this, target, ...args);
      };
    }
    return page;
  }

  function patchContextOptions(options) {
    if (!options || typeof options !== "object") return options;
    if (typeof options.baseURL === "string") {
      const rewritten = rewriteUrl(options.baseURL);
      if (rewritten !== options.baseURL) {
        return Object.assign({}, options, { baseURL: rewritten });
      }
    }
    return options;
  }

  // @playwright/test sets `use.baseURL` as `browserType._defaultContextOptions`
  // rather than passing it through the per-call `newContext(options)` argument,
  // so it must be rewritten in place on the browser type as well.
  function patchDefaultContextOptions(browser) {
    const browserType = browser && browser._browserType;
    const defaults = browserType && browserType._defaultContextOptions;
    if (defaults && typeof defaults === "object" && typeof defaults.baseURL === "string") {
      const rewritten = rewriteUrl(defaults.baseURL);
      if (rewritten !== defaults.baseURL) {
        defaults.baseURL = rewritten;
      }
    }
  }

  function patchContext(context) {
    if (!context || context.__solciCloudBrowserPatched) return context;
    context.__solciCloudBrowserPatched = true;
    const newPage = context.newPage;
    if (typeof newPage === "function") {
      context.newPage = async function (...args) {
        return patchPage(await newPage.apply(this, args));
      };
    }
    if (typeof context.pages === "function") {
      for (const page of context.pages()) patchPage(page);
    }
    return context;
  }

  function patchBrowser(browser) {
    if (!browser || browser.__solciCloudBrowserPatched) return browser;
    browser.__solciCloudBrowserPatched = true;
    if (typeof browser.contexts === "function") {
      for (const context of browser.contexts()) patchContext(context);
    }
    const newContext = browser.newContext;
    if (typeof newContext === "function") {
      browser.newContext = async function (options, ...args) {
        patchDefaultContextOptions(this);
        return patchContext(
          await newContext.call(this, patchContextOptions(options), ...args),
        );
      };
    }
    const newContextForReuse = browser._newContextForReuse;
    if (typeof newContextForReuse === "function") {
      browser._newContextForReuse = async function (options, ...args) {
        patchDefaultContextOptions(this);
        return patchContext(
          await newContextForReuse.call(this, patchContextOptions(options), ...args),
        );
      };
    }
    return browser;
  }

  let playwright;
  try {
    playwright = require(require.resolve('playwright-core', {paths: [process.cwd()]}));
  } catch (_) {
    try {
      playwright = require(require.resolve('playwright', {paths: [process.cwd()]}));
    } catch (_) {
      playwright = null;
    }
  }

  try {
    if (playwright && playwright.chromium) {
      const chromium = playwright.chromium;
      chromium.launch = async (opts) => patchBrowser(await chromium.connectOverCDP(cdpUrl));
      chromium.launchPersistentContext = async (dir, opts) => {
        const browser = await chromium.connectOverCDP(cdpUrl);
        const context =
          browser.contexts()[0] || (await browser.newContext(patchContextOptions(opts)));
        return patchContext(context);
      };
    }
  } catch (_) {
    // A non-Playwright Node step must remain unaffected by the preload.
  }

  try {
    const puppeteer = loadRepoModule("puppeteer");
    puppeteer.launch = () => puppeteer.connect({ browserWSEndpoint: cdpUrl });
  } catch (_) {
    // Puppeteer is optional and may not be installed in this step.
  }
})();
""".strip()


PYTHON_SITECUSTOMIZE = r"""
from __future__ import annotations

try:
    import json
except Exception:
    json = None

try:
    import os
except Exception:
    os = None

try:
    from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
except Exception:
    parse_qsl = urlencode = urljoin = urlsplit = urlunsplit = None


def _solci_url(value, base_url=None):
    if not isinstance(value, str):
        return value
    try:
        mapping = json.loads(os.environ.get("SOLCI_BASE_URL_MAP", "{}")) or {}
    except Exception:
        mapping = {}
    if not all((parse_qsl, urlencode, urljoin, urlsplit, urlunsplit)):
        return value
    try:
        absolute = urlsplit(urljoin(base_url, value) if base_url else value)
        if not absolute.scheme or not absolute.netloc:
            return value
        host = (absolute.hostname or "").lower()
        if host not in {"localhost", "127.0.0.1"}:
            return value
        port = absolute.port or ""
        source = f"{absolute.scheme.lower()}://{host}:{port}"
        target = mapping.get(source)
        if not isinstance(target, str):
            return value
        target_url = urlsplit(target)
        if not target_url.scheme or not target_url.netloc:
            return value
        params = parse_qsl(target_url.query, keep_blank_values=True)
        params.extend(parse_qsl(absolute.query, keep_blank_values=True))
        query = urlencode(params, doseq=True)
        return urlunsplit(
            (
                target_url.scheme,
                target_url.netloc,
                absolute.path,
                query,
                absolute.fragment,
            )
        )
    except Exception:
        return value


def _patch_page(page):
    try:
        if getattr(page, "_solci_cloud_browser_patched", False):
            return page
        page._solci_cloud_browser_patched = True
        original_goto = page.goto

        def goto(url, *args, **kwargs):
            base_url = None
            try:
                options = getattr(page.context, "_options", None)
                if isinstance(options, dict):
                    base_url = options.get("base_url") or options.get("baseURL")
            except Exception:
                pass
            return original_goto(_solci_url(url, base_url), *args, **kwargs)

        page.goto = goto
    except Exception:
        pass
    return page


def _patch_context(context):
    try:
        if getattr(context, "_solci_cloud_browser_patched", False):
            return context
        context._solci_cloud_browser_patched = True
        original_new_page = context.new_page

        def new_page(*args, **kwargs):
            return _patch_page(original_new_page(*args, **kwargs))

        context.new_page = new_page
        for page in context.pages:
            _patch_page(page)
    except Exception:
        pass
    return context


def _patch_browser(browser):
    try:
        if getattr(browser, "_solci_cloud_browser_patched", False):
            return browser
        browser._solci_cloud_browser_patched = True
        original_new_context = browser.new_context

        def new_context(*args, **kwargs):
            return _patch_context(original_new_context(*args, **kwargs))

        browser.new_context = new_context
        for context in browser.contexts:
            _patch_context(context)
    except Exception:
        pass
    return browser


try:
    import playwright.sync_api as _playwright_sync

    _sync_browser_type = _playwright_sync.BrowserType
    if not getattr(_sync_browser_type.launch, "_solci_cloud_browser", False):
        _sync_launch = _sync_browser_type.launch

        def _launch(self, *args, **kwargs):
            if getattr(self, "name", "") == "chromium" and os.environ.get("SOLCI_CDP_URL"):
                return _patch_browser(self.connect_over_cdp(os.environ["SOLCI_CDP_URL"]))
            return _sync_launch(self, *args, **kwargs)

        _launch._solci_cloud_browser = True
        _sync_browser_type.launch = _launch
except Exception:
    pass


try:
    import playwright.async_api as _playwright_async

    _async_browser_type = _playwright_async.BrowserType
    if not getattr(_async_browser_type.launch, "_solci_cloud_browser", False):
        _async_launch = _async_browser_type.launch

        async def _launch_async(self, *args, **kwargs):
            if getattr(self, "name", "") == "chromium" and os.environ.get("SOLCI_CDP_URL"):
                return _patch_browser(await self.connect_over_cdp(os.environ["SOLCI_CDP_URL"]))
            return await _async_launch(self, *args, **kwargs)

        _launch_async._solci_cloud_browser = True
        _async_browser_type.launch = _launch_async
except Exception:
    pass


try:
    from browser_use.browser import BrowserSession as _BrowserSession

    _session_init = _BrowserSession.__init__
    if not getattr(_session_init, "_solci_cloud_browser", False):
        def _browser_session_init(self, *args, **kwargs):
            _session_init(self, *args, **kwargs)
            cdp_url = os.environ.get("SOLCI_CDP_URL")
            profile = getattr(self, "browser_profile", None)
            if cdp_url and profile is not None and not getattr(profile, "cdp_url", None):
                profile.cdp_url = cdp_url
                if hasattr(profile, "is_local"):
                    profile.is_local = False

        _browser_session_init._solci_cloud_browser = True
        _BrowserSession.__init__ = _browser_session_init
except Exception:
    pass
""".strip()

@dataclass(frozen=True)
class BrowserSession:
    """The minimum cloud-browser state needed by a CI step."""

    session_id: str
    cdp_url: str


FileSource = (
    Path
    | str
    | Mapping[str | Path, str]
    | Iterable[Path | str | tuple[str | Path, str]]
)


def _tool_name(value: str) -> str | None:
    normalized = value.strip().lower().replace("_", "-")
    return normalized if normalized in SUPPORTED_BROWSER_TOOLS else None


def _dependency_name(value: str) -> str:
    candidate = value.split("#", 1)[0].strip()
    if candidate.startswith(("-r ", "--")):
        return ""
    return _DEPENDENCY_SEPARATORS.split(candidate, maxsplit=1)[0].strip()


def _read_candidate_files(source: FileSource) -> dict[str, str]:
    if isinstance(source, Mapping):
        return {str(path): str(contents) for path, contents in source.items()}

    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_dir():
            paths = [
                item
                for item in path.rglob("*")
                if item.is_file()
                and not any(part in _EXCLUDED_DIRS for part in item.relative_to(path).parts)
                and (
                    item.name == "package.json"
                    or item.name == "pyproject.toml"
                    or item.name == "requirements.txt"
                    or _REQUIREMENTS_NAME.fullmatch(item.name) is not None
                )
            ]
        else:
            paths = [path] if path.is_file() else []
    else:
        paths = []
        inline_contents: dict[str, str] = {}
        for item in source:
            if isinstance(item, tuple) and len(item) == 2:
                path, contents = item
                inline_contents[str(path)] = str(contents)
                continue
            path = Path(item)
            if path.is_file():
                paths.append(path)

    if isinstance(source, (str, Path, Mapping)):
        contents_by_name = {}
    else:
        contents_by_name = inline_contents
    for path in paths:
        try:
            contents_by_name[str(path)] = path.read_text(encoding="utf-8")
        except OSError:
            continue
    return contents_by_name


def _tools_from_package(text: str) -> set[str]:
    try:
        data = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    result: set[str] = set()
    for field in _PACKAGE_DEPENDENCY_FIELDS:
        dependencies = data.get(field)
        if isinstance(dependencies, dict):
            for name in dependencies:
                tool = _tool_name(str(name))
                if tool:
                    result.add(tool)
    return result


def _tools_from_requirements(text: str) -> set[str]:
    result: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = _dependency_name(line)
        tool = _tool_name(name)
        if tool:
            result.add(tool)
    return result


def _walk_pyproject(value: Any, result: set[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            tool = _tool_name(str(key))
            if tool:
                result.add(tool)
            _walk_pyproject(item, result)
    elif isinstance(value, list):
        for item in value:
            _walk_pyproject(item, result)
    elif isinstance(value, str):
        tool = _tool_name(_dependency_name(value))
        if tool:
            result.add(tool)


def _tools_from_pyproject(text: str) -> set[str]:
    try:
        data = tomllib.loads(text)
    except (TypeError, tomllib.TOMLDecodeError):
        return set()
    result: set[str] = set()
    _walk_pyproject(data, result)
    return result


def detect_browser_tools(repo_dir_or_files: FileSource) -> set[str]:
    """Detect supported browser dependencies in a repository or file source."""
    result: set[str] = set()
    for path, contents in _read_candidate_files(repo_dir_or_files).items():
        name = Path(path).name.lower()
        if name == "package.json":
            result.update(_tools_from_package(contents))
        elif name == "pyproject.toml":
            result.update(_tools_from_pyproject(contents))
        elif name == "requirements.txt" or _REQUIREMENTS_NAME.fullmatch(name):
            result.update(_tools_from_requirements(contents))
    return result


def browser_tools_in_text(text: str) -> set[str]:
    """Find browser-tool names in workflow text for static findings."""
    result: set[str] = set()
    lowered = text.lower()
    for tool in SUPPORTED_BROWSER_TOOLS:
        if tool.lower() in lowered:
            result.add(tool)
    return result


_WEBSERVER_START = re.compile(r"\bwebServer\s*:\s*\{", re.IGNORECASE)
_PORT_LITERAL = re.compile(r"\bport\s*:\s*(\d+)\b", re.IGNORECASE)
_URL_PORT = re.compile(
    r"\b(?:url|baseURL)\s*:\s*[\"'`]https?://(?:localhost|127\.0\.0\.1):(\d+)",
    re.IGNORECASE,
)


def _object_blocks(text: str, marker: re.Pattern[str]) -> Iterable[str]:
    for match in marker.finditer(text):
        start = match.end() - 1
        depth = 0
        for index in range(start, len(text)):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break


def detect_playwright_ports(config_text: str) -> set[int]:
    """Return localhost ports declared by a Playwright webServer config."""
    ports: set[int] = set()
    for block in _object_blocks(config_text, _WEBSERVER_START):
        for match in _PORT_LITERAL.finditer(block):
            ports.add(int(match.group(1)))
        for match in _URL_PORT.finditer(block):
            ports.add(int(match.group(1)))
    return ports


def _rewrite_base_url_map(base_url_map: Mapping[str, str] | None) -> str:
    return json.dumps(dict(base_url_map or {}), sort_keys=True)


def browser_env(cdp_url: str, base_url_map: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build environment variables that route supported clients to cloud Chrome."""
    node_require = "--require /tmp/solci/pw-preload.cjs"
    existing_node_options = os.environ.get("NODE_OPTIONS", "").strip()
    node_options = (
        f"{existing_node_options} {node_require}".strip()
        if node_require not in existing_node_options
        else existing_node_options
    )
    existing_pythonpath = os.environ.get("PYTHONPATH", "").strip()
    pythonpath = "/tmp/solci/py-preload"
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    return {
        "SOLCI_CDP_URL": cdp_url,
        "NODE_OPTIONS": node_options,
        "PYTHONPATH": pythonpath,
        "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD": "1",
        "PUPPETEER_SKIP_DOWNLOAD": "1",
        "SOLCI_BASE_URL_MAP": _rewrite_base_url_map(base_url_map),
    }


async def open_session(client: SolariClient) -> BrowserSession:
    """Create one cloud Chrome session and normalize the client response."""
    data = await client.create_session()
    if isinstance(data, BrowserSession):
        return data
    if not isinstance(data, Mapping):
        raise TypeError("browser session creation returned a non-object response")
    session_id = data.get("sessionId") or data.get("id")
    cdp_url = data.get("cdpUrl") or data.get("cdp_url") or data.get("cdpEndpoint")
    if not cdp_url and isinstance(data.get("wsEndpoint"), str):
        ws_endpoint = data["wsEndpoint"]
        marker = "/ws/"
        index = ws_endpoint.find(marker)
        cdp_url = (
            f"{ws_endpoint[:index]}/cdp/{ws_endpoint[index + len(marker):]}"
            if index >= 0
            else ws_endpoint
        )
    if not session_id or not cdp_url:
        raise RuntimeError("browser session creation returned no session id or CDP URL")
    return BrowserSession(str(session_id), str(cdp_url))


async def close_session(client: SolariClient, session_id: str) -> None:
    """Release a cloud Chrome session, allowing already-released sessions."""
    await client.delete_session(session_id)
