"""Stateless Browser Tool for LLM Function Calling.

Design principles (informed by Browser-Use, Stagehand, Browserbase research):
- Chromium over Firefox: faster launch, better site compatibility, better stealth
- Resource blocking: skip images/fonts/media for ~3x faster loads
- Structured extraction: returns title, text, links, AND interactive elements
  so the LLM can reason about what actions are available (Stagehand-style)
- Stealth fingerprinting: realistic headers + viewport to reduce bot detection
- Actionable errors: every failure includes a HINT field to guide the agent
- Single retry on transient network errors before giving up
"""

import json
import random
import re
from typing import Any, Dict, List, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

_BLOCK_TYPES = {"image", "media", "font"}

_VALID_WAIT_UNTIL = {"commit", "domcontentloaded", "load", "networkidle"}

# Markers in exception messages that indicate a transient network failure worth
# retrying. Kept as a module constant so the retry wrapper and _run() agree.
_TRANSIENT_MARKERS = (
    "net::ERR_CONNECTION",
    "net::ERR_NAME",
    "socket hang up",
    "ECONNRESET",
)

_JS_STEALTH = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
"""

_JS_EXTRACT = """
() => {
    const interactive = [];
    const seen = new Set();
    const add = (el, role) => {
        const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 80);
        const sel = el.id ? '#' + el.id : el.name ? `[name="${el.name}"]` : null;
        if (text && !seen.has(text)) { seen.add(text); interactive.push({role, text, selector: sel}); }
    };
    document.querySelectorAll('a[href]').forEach(el => add(el, 'link'));
    document.querySelectorAll('button, [role="button"]').forEach(el => add(el, 'button'));
    document.querySelectorAll('input:not([type="hidden"]), textarea').forEach(el => add(el, 'input'));
    document.querySelectorAll('select').forEach(el => add(el, 'select'));

    const body = document.body || document.documentElement;
    const clone = body.cloneNode(true);
    clone.querySelectorAll('script,style,noscript,svg').forEach(n => n.remove());
    const text = clone.innerText.replace(/[ \\t]{2,}/g, ' ').replace(/\\n{3,}/g, '\\n\\n').trim();

    return {
        title: document.title,
        url: location.href,
        text,
        interactive: interactive.slice(0, 60),
        links: Array.from(document.querySelectorAll('a[href]'))
                    .map(a => ({text: a.innerText.trim().slice(0, 60), href: a.href}))
                    .filter(l => l.href.startsWith('http'))
                    .slice(0, 40),
    };
}
"""

# ---------------------------------------------------------------------------
# Error helpers
# ---------------------------------------------------------------------------

# Ordered list of (pattern, hint) pairs.
#
# Ordering matters: more specific patterns appear first to prevent short
# strings from matching unrelated messages.  HTTP status patterns are prefixed
# with "HTTP " (matching the format used in _err calls below) to avoid false
# positives — e.g. "4030 records processed" would otherwise match "403".
# "selector not found" is specific enough not to match action ValueError
# messages such as "'selector' is required for click".
_HINT_PATTERNS: List[tuple[str, str]] = [
    ("net::ERR_NAME_NOT_RESOLVED",   "The domain could not be resolved. Check the URL for typos or try a different URL."),
    ("net::ERR_CONNECTION_REFUSED",  "The server refused the connection. The site may be down or blocking automated access."),
    ("net::ERR_CONNECTION_TIMED_OUT","Connection timed out. Try again, increase navigation_timeout, or the site may be blocking bots."),
    ("net::ERR_TOO_MANY_REDIRECTS",  "The page is caught in a redirect loop. Try visiting a more specific URL."),
    ("HTTP 403",                     "Access forbidden (403). The site is blocking automated access. Try a different entry URL."),
    ("HTTP 404",                     "Page not found (404). The URL may be outdated or incorrect."),
    ("HTTP 429",                     "Rate limited (429). Wait before retrying or reduce request frequency."),
    ("HTTP 500",                     "Server error (500). The site is having issues; try again later."),
    ("TimeoutError",                 "Page load timed out. Try increasing navigation_timeout or use wait_until='networkidle'."),
    ("selector not found",           "Use extract_elements=true to discover available selectors, or omit selector for full-page text."),
]


def _hint(msg: str) -> str:
    for pattern, hint in _HINT_PATTERNS:
        if pattern in msg:
            return hint
    return "Try a different URL, check network connectivity, or simplify the request."


def _err(message: str, hint: Optional[str] = None, **extra) -> str:
    """Return a JSON error envelope.

    Callers may supply a custom hint; otherwise one is derived from the message
    text via _hint(). Extra keyword arguments are merged into the response dict.
    """
    return json.dumps({"error": message, "hint": hint or _hint(message), **extra})


# ---------------------------------------------------------------------------
# Retry sentinel
#
# _run() raises this for errors that are worth retrying (dropped connections,
# DNS hiccups). Every other error is returned as a JSON string directly so the
# caller always gets a well-formed response.  The outer retry wrapper catches
# _TransientNetworkError and calls _run() a second time.
# ---------------------------------------------------------------------------

class _TransientNetworkError(Exception):
    """Raised by _run() to signal that a single retry is warranted."""


# ---------------------------------------------------------------------------
# Core function
# ---------------------------------------------------------------------------

def browser(
    url: str,
    actions: Optional[List[Dict[str, Any]]] = None,
    selector: Optional[str] = None,
    extract_elements: bool = False,
    navigation_timeout: int = 30000,
    action_timeout: int = 5000,
    max_content_length: int = 8000,
    wait_until: str = "domcontentloaded",
) -> str:
    """Stateless browser tool. Launches a fresh Chromium instance per call.

    Parameters
    ----------
    url:
        The page to visit. Must include scheme (https://...). A missing scheme
        is auto-corrected to https://.
    actions:
        Optional list of interactions performed *before* content extraction.
        Supported types:

        - ``{"type": "click",      "selector": "CSS"}``
        - ``{"type": "type",       "selector": "CSS", "text": "value"}``
          Empty string is valid (clears the field). Key must be present.
        - ``{"type": "select",     "selector": "CSS", "value": "option"}``
        - ``{"type": "wait",       "selector": "CSS"}``
        - ``{"type": "wait",       "timeout": 2000}``
        - ``{"type": "scroll",     "direction": "down"|"up", "amount": 500}``
          ``amount`` is always treated as positive; ``direction`` controls sign.
        - ``{"type": "navigate",   "url": "https://..."}``
          HTTP errors and timeouts on navigate are treated as action errors.
        - ``{"type": "screenshot", "path": "file.png"}``

        Action errors are non-fatal: logged in ``action_errors``, ``status``
        set to ``"partial"``, and execution continues with the next action.
    selector:
        CSS selector to scope text extraction to one element. When set, ``title``
        is omitted from the response. Omit for full-page extraction.
    extract_elements:
        When True, include ``interactive`` (buttons, inputs, links) and ``links``
        in the response — useful for discovering what actions are available.
    navigation_timeout:
        Milliseconds to wait for page navigation (default 30 000).
    action_timeout:
        Milliseconds to wait for each action's selector/interaction (default 5 000).
    max_content_length:
        Maximum characters of page text returned (default 8 000).
    wait_until:
        Playwright navigation event — one of ``"commit"``, ``"domcontentloaded"``
        (default, fastest), ``"load"``, or ``"networkidle"`` (slowest).

    Returns
    -------
    str
        Always valid JSON. On success::

            {
                "status": "success" | "partial",
                "url": "https://...",
                "title": "...",          # omitted when selector= is set
                "content": "...",
                "actions": [...],        # omitted when no actions were given
                "action_errors": [...],  # omitted when all actions succeeded
                "interactive": [...],    # included when extract_elements=True
                "links": [...]           # included when extract_elements=True
            }

        On failure::

            {"error": "...", "hint": "..."}
    """

    # =========================================================================
    # INPUT VALIDATION
    # All checks happen here, before any browser process is launched, so that
    # invalid calls fail cheaply and with clear messages.
    # =========================================================================

    if not isinstance(url, str) or not url.strip():
        return _err(
            "url is required and must be a non-empty string.",
            hint="Provide a full URL, e.g. https://example.com",
        )

    url = url.strip()
    if not re.match(r"https?://", url):
        url = "https://" + url

    if actions is not None:
        if not isinstance(actions, list):
            return _err(
                f"actions must be a list, got {type(actions).__name__}.",
                hint='Provide a list of action dicts, e.g. [{"type": "click", "selector": "h1"}]',
            )
        for i, act in enumerate(actions):
            if not isinstance(act, dict):
                return _err(
                    f"actions[{i}] must be a dict, got {type(act).__name__}.",
                    hint='Each action must be a dict with a "type" key.',
                )
            # No mutation needed: act.get("type", "") in the action loop
            # already handles a missing "type" key without modifying caller data.

    if wait_until not in _VALID_WAIT_UNTIL:
        return _err(
            f"wait_until must be one of {sorted(_VALID_WAIT_UNTIL)}, got {wait_until!r}.",
        )

    # bool is a subclass of int in Python, so isinstance(True, int) is True.
    # Exclude it explicitly — a boolean timeout is never intentional.
    if isinstance(navigation_timeout, bool) or not isinstance(navigation_timeout, int) or navigation_timeout <= 0:
        return _err("navigation_timeout must be a positive integer (milliseconds).")

    if isinstance(action_timeout, bool) or not isinstance(action_timeout, int) or action_timeout <= 0:
        return _err("action_timeout must be a positive integer (milliseconds).")

    if isinstance(max_content_length, bool) or not isinstance(max_content_length, int) or max_content_length <= 0:
        return _err("max_content_length must be a positive integer.")

    # =========================================================================
    # BROWSER SESSION
    # =========================================================================

    def _run() -> str:
        """Launch a single browser session and return a JSON result string.

        Raises _TransientNetworkError for errors that merit a retry.
        Returns a JSON error string for all other failures.
        """
        with sync_playwright() as p:
            browser_inst = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                # new_context() is inside try so browser_inst.close() runs if it raises.
                ctx = browser_inst.new_context(
                    user_agent=random.choice(_USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                    timezone_id="America/New_York",
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    },
                )
                try:
                    ctx.route(
                        "**/*",
                        lambda route: route.abort()
                        if route.request.resource_type in _BLOCK_TYPES
                        else route.continue_(),
                    )

                    page = ctx.new_page()
                    page.add_init_script(_JS_STEALTH)

                    # ── Initial navigation ─────────────────────────────────
                    try:
                        resp = page.goto(url, timeout=navigation_timeout, wait_until=wait_until)
                    except PWTimeout:
                        return _err(f"TimeoutError navigating to {url}")
                    except Exception as e:
                        msg = str(e)
                        if any(m in msg for m in _TRANSIENT_MARKERS):
                            raise _TransientNetworkError(msg)
                        return _err(f"Navigation failed: {e}")

                    status = resp.status if resp else 0
                    if status in (403, 404, 429, 500):
                        return _err(f"HTTP {status} from {url}")

                    # ── Actions ────────────────────────────────────────────
                    log: List[str] = []
                    action_errors: List[str] = []

                    for i, act in enumerate(actions or []):
                        act_type = act.get("type", "")
                        try:
                            if act_type == "click":
                                sel = act.get("selector", "")
                                if not sel:
                                    raise ValueError("'selector' is required for click")
                                page.click(sel, timeout=action_timeout)
                                log.append(f"clicked '{sel}'")

                            elif act_type == "type":
                                sel = act.get("selector", "")
                                if not sel:
                                    raise ValueError("'selector' is required for type")
                                # Use sentinel None — missing key is an error, but
                                # empty string is valid (clears the input field).
                                text = act.get("text", None)
                                if text is None:
                                    raise ValueError("'text' is required for type (use \"\" to clear a field)")
                                page.fill(sel, str(text), timeout=action_timeout)
                                log.append(f"typed into '{sel}'")

                            elif act_type == "select":
                                sel = act.get("selector", "")
                                if not sel:
                                    raise ValueError("'selector' is required for select")
                                val = act.get("value", "")
                                page.select_option(sel, value=val, timeout=action_timeout)
                                log.append(f"selected '{val}' in '{sel}'")

                            elif act_type == "wait":
                                if "selector" in act:
                                    page.wait_for_selector(act["selector"], timeout=action_timeout)
                                    log.append(f"waited for '{act['selector']}'")
                                elif "timeout" in act:
                                    page.wait_for_timeout(int(act["timeout"]))
                                    log.append(f"waited {act['timeout']}ms")
                                else:
                                    raise ValueError("'selector' or 'timeout' is required for wait")

                            elif act_type == "scroll":
                                direction = act.get("direction", "down")
                                # abs() ensures a negative amount doesn't silently
                                # invert the intended scroll direction.
                                amount = abs(int(act.get("amount", 500)))
                                dy = amount if direction == "down" else -amount
                                page.evaluate(f"window.scrollBy(0, {dy})")
                                log.append(f"scrolled {direction} {amount}px")

                            elif act_type == "navigate":
                                dest = act.get("url", "")
                                if not dest:
                                    raise ValueError("'url' is required for navigate")
                                try:
                                    nav_resp = page.goto(dest, timeout=navigation_timeout, wait_until=wait_until)
                                except PWTimeout:
                                    raise ValueError(f"timed out navigating to '{dest}'")
                                nav_status = nav_resp.status if nav_resp else 0
                                if nav_status in (403, 404, 429, 500):
                                    raise ValueError(f"HTTP {nav_status} from '{dest}'")
                                log.append(f"navigated to '{dest}'")

                            elif act_type == "screenshot":
                                path = act.get("path", "screenshot.png")
                                page.screenshot(path=path)
                                log.append(f"screenshot saved to '{path}'")

                            else:
                                log.append(
                                    f"unknown action type '{act_type}' (skipped) – "
                                    "supported: click, type, select, wait, scroll, navigate, screenshot"
                                )

                        except PWTimeout:
                            msg = (
                                f"TIMEOUT on action {i} ({act_type}) – "
                                "element not found or not interactable; "
                                "use extract_elements=true to inspect the page"
                            )
                            log.append(msg)
                            action_errors.append(msg)
                        except Exception as e:
                            msg = f"ERROR on action {i} ({act_type}): {e}"
                            log.append(msg)
                            action_errors.append(msg)

                    # ── Content extraction ─────────────────────────────────
                    # Read page.url after all actions so it reflects any
                    # redirects or client-side route changes that occurred.
                    final_url = page.url if page.url not in ("about:blank", "") else url

                    if selector:
                        try:
                            page.wait_for_selector(selector, timeout=action_timeout)
                            content = "\n".join(page.locator(selector).all_inner_texts())
                        except Exception:
                            return _err(
                                f"selector not found: '{selector}'",
                                page_url=final_url,
                            )
                        result: Dict[str, Any] = {
                            "status": "partial" if action_errors else "success",
                            "url": final_url,
                            "content": content[:max_content_length],
                        }
                    else:
                        page_data: Dict[str, Any] = page.evaluate(_JS_EXTRACT)
                        result = {
                            "status": "partial" if action_errors else "success",
                            "url": final_url,
                            "title": page_data.get("title", ""),
                            "content": page_data.get("text", "")[:max_content_length],
                        }
                        if extract_elements:
                            result["interactive"] = page_data.get("interactive", [])
                            result["links"] = page_data.get("links", [])

                    if log:
                        result["actions"] = log
                    if action_errors:
                        result["action_errors"] = action_errors

                    return json.dumps(result)

                finally:
                    ctx.close()
            finally:
                browser_inst.close()

    # ── Retry wrapper ──────────────────────────────────────────────────────
    # _run() raises _TransientNetworkError for errors worth retrying and
    # returns JSON for everything else.  This keeps the retry path explicit
    # and prevents silent swallowing of non-transient failures.
    try:
        return _run()
    except _TransientNetworkError:
        try:
            return _run()
        except _TransientNetworkError as e:
            return _err(f"Failed after retry: {e}")
        except Exception as e:
            return _err(f"Unexpected error on retry: {e}")
    except Exception as e:
        return _err(f"Unexpected error: {e}")


# ---------------------------------------------------------------------------
# Tool definition (OpenAI / Anthropic function-calling schema)
# ---------------------------------------------------------------------------

func = browser
name = "browser"
description = """\
Stateless browser tool. Visits a URL and returns the page's text content.
Each call is a fully independent session — no cookies or state are shared.

Parameters:
- url (required): Full URL including scheme, e.g. https://example.com.
  A missing scheme is auto-corrected to https://.
- actions (optional): Ordered list of interactions performed before extraction.
  Types:
    click      – {"type":"click","selector":"CSS"}
    type       – {"type":"type","selector":"CSS","text":"value"}
                 Empty string is valid (clears the field). Key must be present.
    select     – {"type":"select","selector":"CSS","value":"option"}
    wait       – {"type":"wait","selector":"CSS"} | {"type":"wait","timeout":2000}
    scroll     – {"type":"scroll","direction":"down","amount":500}
                 direction: "down" (default) or "up". amount must be positive.
    navigate   – {"type":"navigate","url":"https://..."}
                 HTTP errors and navigation timeouts are treated as action errors.
    screenshot – {"type":"screenshot","path":"file.png"}
  Action errors are non-fatal: logged in action_errors, status set to "partial".
- selector (optional): CSS selector to scope extracted text to one element.
  When set, title is omitted. Use extract_elements=true first to find selectors.
- extract_elements (optional, bool): Include interactive elements and links.
- wait_until (optional): commit | domcontentloaded (default) | load | networkidle
- navigation_timeout (optional): ms to wait for page navigation (default 30000).
- action_timeout (optional): ms to wait per action (default 5000).
- max_content_length (optional): max chars of text returned (default 8000).

Returns JSON:
  Success: {status, url, title?, content, actions?, action_errors?,
            interactive?, links?}
  Failure: {error, hint}\
"""

__all__ = ["browser", "func", "name", "description"]