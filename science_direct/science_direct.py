"""
ScienceDirect scraper – four clean phases:

  Phase 1  collect_journals()     Browse all journals (JS pagination), visit
                                  each archive page to extract ISSNs, save to
                                  local JSONL.

  Phase 2  post_journals_to_db()  Push the local JSONL to the database API.

  Phase 3  main(part)             Per-journal pipeline driven from DB/cache:
               3a  build_volume_issue_map()   scrape /issues archive page
               3b  extract_issue_articles()   scrape each issue TOC page
               3c  extract_article_data()     scrape each article page
             Article HTMLs are archived to E:\\science_direct_html\\<jid>\\
             for future re-parsing without re-scraping.
             Telegram alert fires when expected selectors go missing.

Usage (command-line):
  python science_direct.py collect        # Phase 1 only
  python science_direct.py upload         # Phase 2 only
  python science_direct.py mine 1         # Phase 3, part 1
  python science_direct.py all  1         # Phases 1 + 2 + 3

Imported by science_direct2.py for multi-process mining:
  from science_direct import main
  main(part_number)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import ssl
import sys
import threading
import time
from datetime import datetime
from urllib.parse import unquote, urljoin

import requests
from bs4 import BeautifulSoup

ssl._create_default_https_context = ssl._create_unverified_context

# ── Optional Pillow (used to downscale + JPEG-compress embedded images) ─────
try:
    from PIL import Image as _PIL_Image  # type: ignore
    _HAVE_PIL = True
except Exception:
    _PIL_Image = None  # type: ignore
    _HAVE_PIL = False

# Module-level handle to the active Selenium driver, used so the image-block
# helpers can call browser-fetch without plumbing `driver` through ten levels
# of BS4 walking. Set in process_journal before extract_article_data runs.
_CURRENT_BROWSER = None

# Switches / knobs for image embedding.
EMBED_IMAGES        = True       # set False to keep src/alt only (no bytes)
IMAGE_FETCH_TIMEOUT = 25         # seconds per image
IMAGE_MAX_DIM       = 1400       # downscale longest side to this many px
IMAGE_JPEG_QUALITY  = 80         # Pillow JPEG quality (1-95)

# Keep the whole rendered page HTML in the batch (and therefore in db.sections.html)?
# True  (default) → safety net: the full rendered page is saved alongside the
#         structured sections, so if we ever need to re-parse for a new field
#         we don't have to re-mine the URL. Adds ~500KB-1.5MB per article.
# False → only the *structured* sections survive (ABSTRACT, RESULTS, …, OTHERS).
#         Article content is still complete — you just lose the surrounding SD
#         page wrapper (nav, sidebar, footer, JS). Saves ~60-85% storage / article.
STORE_RAW_HTML      = True


# ── Project root on sys.path ────────────────────────────────────────────────
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── Fix Windows console encoding (emojis → '?' instead of crash) ────────────
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# ════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════════════════

API_ROOT      = "http://139.84.134.18:8002"
DATABASE      = "science_direct"
BATCH_SIZE    = 10          # articles per offline batch file
HEADLESS      = False       # set False for visual debugging

BROWSE_URL    = "https://www.sciencedirect.com/browse/journals-and-books?contentType=JL"
BASE_URL      = "https://www.sciencedirect.com"

# Article HTMLs go here (E: drive) – one sub-folder per journal ID
HTML_SAVE_DIR = r"E:\science_direct_html"

# Pre-mined journals JSON (from previous runs / DB export). Phase 1 will reuse
# ISSN/PSSN data from here whenever an archive_page_url matches, instead of
# re-fetching the archive page.
EXISTING_JOURNALS_JSON = r"E:\New folder\science_direct.journals.json"

# Human-like delays (seconds) to reduce bot-detection risk
DELAY_ISSN_FETCH   = (10, 25)   # between ISSN fetches on archive pages
DELAY_ARTICLE_LOAD = (5, 12)    # between article page fetches

# SD challenge / block page markers.
# _SD_CAPTCHA_MARKERS    → a SOLVABLE challenge — must indicate that an actual
#                          checkbox / Turnstile widget is on the page. Used by
#                          _is_sd_hardblock to disambiguate "real captcha" from
#                          "hard-block that happens to share page chrome".
# _SD_HARDBLOCK_MARKERS  → Imperva / Akamai server-side block ("problem
#                          providing content", reference number). NOT solvable
#                          — no checkbox. Right response: close + reopen with
#                          fresh UA, NOT click.
# _SD_WRAPPER_MARKERS    → generic SD challenge / error page chrome that
#                          appears on BOTH the captcha page AND the hard-block
#                          page. Belongs to _SD_CHALLENGES (so _is_sd_challenge
#                          still trips on the wrapped error page), but MUST
#                          NOT count as "captcha widget present" — otherwise
#                          the hard-block page gets misclassified as a captcha
#                          and the click goes to nothing.
_SD_CAPTCHA_MARKERS = [
    "are you a robot?",
    "please confirm you are a human",
    "challenges.cloudflare.com",
    "cf-chl-widget",
    "cf_challenge_response",
    "cf-turnstile-response",
]
_SD_HARDBLOCK_MARKERS = [
    "problem providing the content you requested",
    "problem providing content",
    "reference number:",
]
_SD_WRAPPER_MARKERS = [
    "blue-background fixed-width-container",
]
_SD_CHALLENGES = _SD_CAPTCHA_MARKERS + _SD_HARDBLOCK_MARKERS + _SD_WRAPPER_MARKERS

# Expected CSS selectors per page type.
# If NONE of the listed selectors match → pattern-change alert + skip.
_EXPECTED_PATTERNS: dict[str, list[str]] = {
    "browse": [
        "a.anchor.js-publication-title",
    ],
    "archive": [
        "li.accordion-panel",
        "div.js-issue-list",             # alternate layout
    ],
    "issue": [
        "div.issue-items-container",
        "ol.article-list",               # some journals use a list
    ],
    "article": [
        "div.author-info",               # open-access full-text
        "div.author-group",              # subscription / abstract-only
        "div.authors-affiliations__list",  # newer SD layout
        "section.article-header-section",  # minimal header
    ],
}


# ════════════════════════════════════════════════════════════════════════════
#  SHARED UTILITIES (imported from project-level utils_vpn)
# ════════════════════════════════════════════════════════════════════════════

from utils_vpn.offline_utils_vpn import (
    create_driver,
    save_offline,
    save_skipped,
    save_last_state,
    load_last_state,
    save_backup_json,
    read_backup_json,
    fetch_and_cache_journals,
    load_journals_from_cache,
    safe_get as _base_safe_get,
)
from utils_vpn.name_cleaner import clean_authors
from captcha_solver.captcha_client import enqueue_captcha_system  # noqa: F401

# ── Telegram notifications ───────────────────────────────────────────────────
try:
    from telegram_notifications.notifier import notify_pattern_change, notify_crash, notify_info
except Exception:
    def notify_pattern_change(jid: str, url: str, details: str) -> None:  # type: ignore[misc]
        print(f"[PATTERN] jid={jid} | {details}")

    def notify_crash(pid: str, error: str) -> None:  # type: ignore[misc]
        print(f"[CRASH] {pid}: {error}")

    def notify_info(pid: str, msg: str) -> None:  # type: ignore[misc]
        print(f"[INFO] {pid}: {msg}")

try:
    from telegram_notifications.status_reporter import report_status
except Exception:
    def report_status(pid: str, status: str, msg: str = "") -> None:  # type: ignore[misc]
        print(f"[STATUS] {pid} {status}: {msg}")


# ════════════════════════════════════════════════════════════════════════════
#  LOW-LEVEL HELPERS
# ════════════════════════════════════════════════════════════════════════════

def _safe_quit(driver) -> None:
    if driver is None:
        return
    try:
        driver.quit()
    except Exception:
        pass


def _is_sd_challenge(page_source: str) -> bool:
    """Return True when the page is a ScienceDirect / Cloudflare block page."""
    if not page_source:
        return False
    src = page_source.lower()
    return any(p in src for p in _SD_CHALLENGES)


# When True, treat EVERY challenge (Turnstile captcha + Imperva hard-block)
# as a hardblock — i.e. always close the driver, spin up a new one with a
# fresh UA from user_agents.txt, and re-navigate (bypassing captcha_worker).
#
# DEFAULT IS OFF so the captcha_worker click path actually runs:
#   captcha appears → SD detects iframe rect → sends viewport coords to
#   captcha_worker → worker clicks at those coords (which are also screen
#   coords because Chrome is fullscreen).
#
# Set ``SD_CLOSE_ON_CAPTCHA=1`` to switch back to the "close + reopen"
# behavior (skips the click attempt entirely).
_SD_CLOSE_ON_CAPTCHA = os.environ.get("SD_CLOSE_ON_CAPTCHA", "0") == "1"


def _is_sd_hardblock(page_source: str) -> bool:
    """Return True when SD's response should trigger a driver close +
    reopen with a fresh UA (instead of attempting a captcha-worker click).

    Two modes:
      • ``SD_CLOSE_ON_CAPTCHA=1`` (default) — treat ANY challenge as a
        hardblock: every captcha + Imperva page → close + reopen. The
        click-to-solve flow is never invoked.
      • ``SD_CLOSE_ON_CAPTCHA=0`` — original behavior. Hardblock = page
        has Imperva markers AND no captcha widget. Captcha pages take
        the click-to-solve path via the captcha_worker queue.
    """
    if not page_source:
        return False
    src = page_source.lower()

    # Aggressive mode — close on ANY challenge marker.
    if _SD_CLOSE_ON_CAPTCHA:
        return any(p in src for p in _SD_CHALLENGES)

    # Original strict-hardblock mode.
    has_hardblock_text = any(p in src for p in _SD_HARDBLOCK_MARKERS)
    if not has_hardblock_text:
        return False
    has_captcha_widget = any(p in src for p in _SD_CAPTCHA_MARKERS)
    return not has_captcha_widget


_SD_COPYRIGHT_FOOTER_MARKER = (
    "all content on this site: copyright"  # ~Elsevier B.V. … licensors …
)


# Stable Cloudflare iframe selectors, same list as test_captcha_click.py.
# Tried broadest-to-narrowest so we catch novel layouts.
_SD_IFRAME_SELECTORS = [
    'iframe[title="Widget containing a Cloudflare security challenge"]',
    'iframe[title*="challenge"]',
    'iframe[title*="Cloudflare"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[src*="turnstile"]',
    'iframe[src*="cloudflare.com"]',
    'iframe[id^="cf-chl-widget"]',
    'iframe[allow*="cross-origin-isolated"]',
    'iframe[src*="hcaptcha.com"]',
    'iframe[title*="hCaptcha"]',
    'iframe[src*="recaptcha"]',
    'iframe[title*="reCAPTCHA"]',
]


def _find_captcha_iframe_direct(driver):
    """Direct CSS-selector lookup for the captcha iframe in the regular
    light DOM. Works when the page doesn't wrap the iframe in a closed
    shadow root (older Cloudflare layouts, hCaptcha, reCAPTCHA, etc.).
    Returns ``{left, top, width, height}`` or None."""
    if driver is None:
        return None
    try:
        from selenium.webdriver.common.by import By
        for sel in _SD_IFRAME_SELECTORS:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            if els:
                return driver.execute_script(
                    "const r = arguments[0].getBoundingClientRect();"
                    "return {left: r.left, top: r.top, "
                    "        width: r.width, height: r.height};",
                    els[0],
                )
    except Exception:
        return None
    return None


def _find_captcha_iframe_rect_cdp(driver):
    """Find the Cloudflare Turnstile iframe's viewport rect via CDP,
    piercing closed shadow DOM. Returns ``{left, top, width, height}``
    in CSS pixels, or None if no captcha iframe found.

    Modern SD / Sagepub / etc. Cloudflare challenge pages wrap the
    Turnstile iframe inside a CLOSED shadow root that Selenium's
    `find_elements` / `document.querySelector` cannot enter. CDP's
    `DOM.getDocument` with `pierce: true` walks across that boundary
    so we can locate the iframe and get its true on-page position."""
    if driver is None:
        return None
    try:
        doc = driver.execute_cdp_cmd(
            "DOM.getDocument", {"depth": -1, "pierce": True}
        )
    except Exception:
        return None

    iframe_node_id = [None]

    def _walk(node):
        if iframe_node_id[0] is not None:
            return
        if node.get("nodeName") == "IFRAME":
            attrs = node.get("attributes") or []
            attr_dict = dict(zip(attrs[::2], attrs[1::2]))
            src = attr_dict.get("src", "")
            title = (attr_dict.get("title") or "").lower()
            if ("challenges.cloudflare.com" in src
                    or "turnstile" in src
                    or "cloudflare" in title
                    or "challenge" in title):
                iframe_node_id[0] = node["nodeId"]
                return
        for child in (node.get("children") or []):
            _walk(child)
        for shadow in (node.get("shadowRoots") or []):
            _walk(shadow)
        cd = node.get("contentDocument")
        if cd:
            _walk(cd)

    _walk(doc["root"])
    if iframe_node_id[0] is None:
        return None
    try:
        box = driver.execute_cdp_cmd(
            "DOM.getBoxModel", {"nodeId": iframe_node_id[0]}
        )
    except Exception:
        return None
    border = (box.get("model") or {}).get("border") or []
    if len(border) < 8:
        return None
    x1, y1, x2, y2 = border[0], border[1], border[2], border[5]
    w, h = float(x2 - x1), float(y2 - y1)
    if w <= 0 or h <= 0:
        return None
    return {"left": float(x1), "top": float(y1), "width": w, "height": h}


def _find_captcha_shadow_host(driver):
    """Fallback locator: find the closed-shadow host via the hidden
    cf-turnstile-response input that lives in the LIGHT DOM next to it.
    Returns ``{left, top, width, height}`` of the host or None."""
    if driver is None:
        return None
    try:
        from selenium.webdriver.common.by import By
        inputs = driver.find_elements(
            By.CSS_SELECTOR,
            "input[name='cf-turnstile-response'], input[id^='cf-chl-widget']",
        )
        if not inputs:
            return None
        return driver.execute_script(
            "const p = arguments[0].parentElement; "
            "if (!p) return null;"
            "const r = p.getBoundingClientRect();"
            "return {left: r.left, top: r.top, width: r.width, height: r.height};",
            inputs[0],
        )
    except Exception:
        return None


# Turnstile checkbox sits ~30 px from the iframe's left edge, vertically
# centered. Same constant the test script uses.
_SD_TURNSTILE_CHECKBOX_OFFSET_X = 30


# Visual debug — draw a marker on the page at the detected click point so
# you can SEE where the captcha will be clicked. Auto-clears after 5s.
# Set the env var ``SD_VIEWPORT_DEBUG=0`` to disable.
_SD_VIEWPORT_DEBUG = os.environ.get("SD_VIEWPORT_DEBUG", "1") != "0"


# When True, SD dispatches the captcha checkbox click DIRECTLY into Chrome
# via CDP at the detected viewport coords — completely bypassing the
# captcha_worker / pyautogui / screen-coord math path. This is the only
# click strategy that's GUARANTEED to land at the same place as the
# bullseye marker, regardless of DPI scaling, window position, or
# pygetwindow's coordinate space. Cloudflare's challenge widget receives
# the click event with `isTrusted: true` (CDP dispatches as trusted),
# though some strict sites may still score it as automated.
# Disable with ``set SD_CDP_CLICK=0`` to fall back to the captcha_worker
# pipeline.
_SD_CDP_CLICK = os.environ.get("SD_CDP_CLICK", "1") != "0"


def _sd_cdp_viewport_click(driver, vp_x: float, vp_y: float) -> bool:
    """Click directly at viewport (vp_x, vp_y) via Chrome DevTools Protocol.

    Sends mouseMoved → mousePressed → mouseReleased into Chrome's input
    pipeline at the exact viewport pixel. No OS-level mouse, no screen
    math, no DPI awareness needed. The click lands at the same viewport
    coord that the bullseye marker is drawn at — they CANNOT mismatch.

    Returns True if the CDP calls succeeded.
    """
    if driver is None:
        return False
    try:
        # Three preliminary moves so Chrome sees a small mouse-trail
        # arriving at the spot (some bot scoring flags single-event
        # teleport clicks). Cheap — each is ~30 ms in CDP.
        for dx, dy in [(-25, -8), (-10, -3), (0, 0)]:
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": "mouseMoved",
                "x": float(vp_x + dx), "y": float(vp_y + dy),
                "button": "none", "buttons": 0,
                "pointerType": "mouse",
            })
            time.sleep(0.04)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed",
            "x": float(vp_x), "y": float(vp_y),
            "button": "left", "buttons": 1,
            "clickCount": 1,
            "pointerType": "mouse",
        })
        time.sleep(0.08)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased",
            "x": float(vp_x), "y": float(vp_y),
            "button": "left", "buttons": 0,
            "clickCount": 1,
            "pointerType": "mouse",
        })
        print(f"[SD] CDP click sent at viewport ({int(vp_x)},{int(vp_y)}) "
              "— event dispatched directly into Chrome input pipeline")
        return True
    except Exception as e:
        print(f"[SD] CDP click failed: {e}")
        return False


def _draw_viewport_marker(driver, vp_x: float, vp_y: float, rect: dict) -> None:
    """Inject a red dashed outline around the captcha iframe and a red
    bullseye + label at the click point, on top of the live page.

    The marker is pure DOM injection (one div + bullseye + label) and
    auto-removes itself after 5 seconds — long enough for you to look,
    short enough not to interfere with the click that follows. All
    overlays use ``pointer-events: none`` so they can't intercept clicks.

    Disable with: ``set SD_VIEWPORT_DEBUG=0``  (Windows)
                  ``export SD_VIEWPORT_DEBUG=0`` (Linux/Mac)
    """
    if not _SD_VIEWPORT_DEBUG or driver is None:
        return
    try:
        driver.execute_script(
            r"""
            (function(rect, vp_x, vp_y){
              // Wipe any previous markers so re-runs don't pile up.
              ['__sd_vp_box__','__sd_vp_dot__','__sd_vp_label__'].forEach(id=>{
                const old = document.getElementById(id); if (old) old.remove();
              });

              // Red dashed outline around the detected iframe rect.
              const box = document.createElement('div');
              box.id = '__sd_vp_box__';
              box.style.cssText = `position: fixed;
                left:${rect.left}px; top:${rect.top}px;
                width:${rect.width}px; height:${rect.height}px;
                border: 2px dashed #dc2626;
                background: rgba(220,38,38,0.08);
                z-index: 2147483646; pointer-events: none;
                box-sizing: border-box;`;
              document.body.appendChild(box);

              // Bullseye + crosshair at the click point.
              const dot = document.createElement('div');
              dot.id = '__sd_vp_dot__';
              dot.style.cssText = `position: fixed;
                left:${vp_x - 14}px; top:${vp_y - 14}px;
                width:28px; height:28px; border-radius:50%;
                border: 3px solid #dc2626;
                background: rgba(220,38,38,0.40);
                z-index: 2147483647; pointer-events: none;
                box-shadow: 0 0 14px #dc2626, inset 0 0 6px #dc2626;`;
              const mk = (css)=>{const d=document.createElement('div');
                                   d.style.cssText=css; return d;};
              dot.appendChild(mk('position:absolute; left:-18px; top:50%;'
                + ' width:64px; height:2px; background:#dc2626;'
                + ' transform:translateY(-50%);'));
              dot.appendChild(mk('position:absolute; top:-18px; left:50%;'
                + ' height:64px; width:2px; background:#dc2626;'
                + ' transform:translateX(-50%);'));
              document.body.appendChild(dot);

              // Floating label showing coords + iframe rect.
              const label = document.createElement('div');
              label.id = '__sd_vp_label__';
              label.innerHTML =
                  '<div style="font-weight:700">SD viewport click</div>'
                + `<div>vp = (${Math.round(vp_x)}, ${Math.round(vp_y)})</div>`
                + `<div>iframe rect: left=${Math.round(rect.left)}`
                +   ` top=${Math.round(rect.top)}`
                +   ` ${Math.round(rect.width)}×${Math.round(rect.height)}</div>`;
              label.style.cssText = `position: fixed;
                left:${vp_x + 24}px; top:${vp_y - 36}px;
                background: #dc2626; color: white;
                font: 11px/1.35 system-ui, sans-serif;
                padding: 6px 10px; border-radius: 4px;
                z-index: 2147483647; pointer-events: none;
                box-shadow: 0 2px 8px rgba(0,0,0,0.35);
                white-space: nowrap;`;
              document.body.appendChild(label);

              // Auto-clear after 5 seconds so the click below isn't blocked
              // (overlays are pointer-events: none anyway, but cleaner).
              setTimeout(()=>{
                ['__sd_vp_box__','__sd_vp_dot__','__sd_vp_label__']
                .forEach(id=>{ const el = document.getElementById(id);
                               if (el) el.remove(); });
              }, 5000);
            })(arguments[0], arguments[1], arguments[2]);
            """,
            rect, vp_x, vp_y,
        )
    except Exception as e:
        print(f"[SD] viewport marker failed: {e}")


def _find_captcha_rect(driver):
    """Return ``({left, top, width, height}, kind)`` for the captcha
    widget — or ``(None, None)`` if not found.

    Same three-tier strategy as test_captcha_click.py's
    ``_find_captcha_element``:

      1. **Direct iframe selector** (regular light DOM) — fastest;
         catches older Cloudflare, hCaptcha, reCAPTCHA pages.
      2. **CDP-pierced iframe** — handles closed shadow DOM that
         document.querySelector can't enter (newer Cloudflare).
      3. **Shadow host fallback** — the hidden cf-turnstile-response
         input's parent is the shadow host; its rect ≈ iframe rect
         when the iframe is the host's only visible child.
    """
    if driver is None:
        return None, None
    rect = _find_captcha_iframe_direct(driver)
    if rect is not None and rect.get("width", 0) > 0:
        return rect, "iframe-direct"
    rect = _find_captcha_iframe_rect_cdp(driver)
    if rect is not None and rect.get("width", 0) > 0:
        return rect, "iframe-cdp"
    rect = _find_captcha_shadow_host(driver)
    if rect is not None and rect.get("width", 0) > 0:
        return rect, "shadow-host"
    return None, None


def _compute_dynamic_captcha_click(driver):
    """Return ``(vp_x, vp_y)`` — VIEWPORT click coords on the Cloudflare
    Turnstile checkbox (top-left of page-content area as origin).

    The chrome offsets (``side_border`` / ``chrome_top``) are sent in
    SEPARATE queue-file fields by ``offline_utils_vpn``; the worker adds
    them when computing the screen position. We DO NOT include them in
    the returned coord — doing so used to double-count.

    Diagnostic block prints:
      • viewport coord we're sending
      • side_border / chrome_top we're sending alongside it
      • window.screenX / window.screenY (Chrome's view of window pos)
      • predicted final screen pixel = screenX + sideBorder + vp_x, ...
    so you can compare to where the worker actually clicks.
    """
    if driver is None:
        return None
    rect, kind = _find_captcha_rect(driver)
    if rect is None:
        return None
    vp_x = int(rect["left"] + _SD_TURNSTILE_CHECKBOX_OFFSET_X)
    vp_y = int(rect["top"]  + rect["height"] / 2.0)

    # Pull window-pos + chrome info from the browser so the diagnostic
    # log shows the same physical pixel from THREE reference frames.
    try:
        info = driver.execute_script(
            "return {ow: window.outerWidth, oh: window.outerHeight,"
            " iw: window.innerWidth, ih: window.innerHeight,"
            " sx: window.screenX, sy: window.screenY,"
            " dpr: window.devicePixelRatio || 1};"
        )
    except Exception:
        info = {}
    chrome_top  = max(0, info.get("oh", 0) - info.get("ih", 0)) if info else 0
    side_border = max(0, (info.get("ow", 0) - info.get("iw", 0)) // 2) if info else 0
    sx = info.get("sx", 0) if info else 0
    sy = info.get("sy", 0) if info else 0
    dpr = info.get("dpr", 1) if info else 1
    pred_screen_x = int(sx + side_border + vp_x)
    pred_screen_y = int(sy + chrome_top  + vp_y)

    print(f"[SD] iframe rect ({kind}): viewport=({vp_x},{vp_y}) "
          f"size={int(rect['width'])}x{int(rect['height'])}")
    print(f"[SD]   chrome: sideBorder={side_border} chromeTop={chrome_top} "
          f"dpr={dpr}")
    print(f"[SD]   window.screenX/Y = ({sx},{sy})")
    print(f"[SD]   predicted SCREEN click = ({pred_screen_x},{pred_screen_y})"
          " (compare to worker log's 'Clicking at:' line — they should match)")

    # Visual debug — draw THREE markers on the page so you can see where
    # the click will land. Auto-clears after 5s. Disable with
    # `set SD_VIEWPORT_DEBUG=0`.
    _draw_viewport_marker(driver, vp_x, vp_y, rect)
    return vp_x, vp_y


def _sd_captcha_click_x(html: str, driver=None) -> int:
    """Window-relative X click coord for the Cloudflare Turnstile checkbox.

    Strategy:
      1. **Dynamic (preferred)** — locate the captcha iframe via CDP
         (pierces closed shadow DOM) or via the hidden cf-turnstile-response
         input's parent. Compute window-relative coords from the actual
         iframe rect.
      2. **Static fallback** — original layout-based constants when the
         iframe can't be located (no driver, missing iframe, etc.).

    captcha_worker.py is UNCHANGED — it still treats the returned int as
    a window-relative offset and adds ``win.left`` to get screen pixels.
    Only `_x` prints (called immediately before `_y`); `_y` stays silent.
    """
    dyn = _compute_dynamic_captcha_click(driver)
    if dyn is not None:
        vp_x, vp_y = dyn

        # ── PRIMARY click path: CDP directly into Chrome ──────────────
        # Fires NOW (synchronously inside this callable, before the
        # captcha_worker is enqueued). The click lands at viewport
        # (vp_x, vp_y) — the SAME viewport coord the bullseye marker
        # is drawn at. No screen-pixel math involved → cannot miss.
        if _SD_CDP_CLICK and driver is not None:
            _sd_cdp_viewport_click(driver, vp_x, vp_y)

        # We still return coords so safe_get continues to also enqueue
        # the captcha_worker job — belt-and-suspenders. If CDP succeeded
        # at clearing the challenge, the worker's click will simply land
        # on a non-existent widget (harmless).
        return vp_x

    # Fallback — original static layout-based behavior
    if _SD_COPYRIGHT_FOOTER_MARKER in (html or "").lower():
        print("[SD] captcha 1  (static fallback, Elsevier-footer) → click (360, 360)")
        return 360
    print("[SD] captcha 2  (static fallback, default Cloudflare) → click (400, 420)")
    return 400


def _sd_captcha_click_y(html: str, driver=None) -> int:
    dyn = _compute_dynamic_captcha_click(driver)
    if dyn is not None:
        return dyn[1]
    if _SD_COPYRIGHT_FOOTER_MARKER in (html or "").lower():
        return 360
    return 420


def safe_get(skipped_file: str, driver, url: str, journal_id: str,
             retries: int = 3, wait: int = 180):
    """Thin wrapper around the shared safe_get with SD-specific challenge detection."""
    # NOTE: create_driver/connect_driver treats `head=True` as headless=True
    # (i.e. invisible). When this module says HEADLESS=False we want a visible
    # browser, so we pass head=HEADLESS (not `not HEADLESS`).
    #
    # Click coords are CALLABLES — the base library evaluates them with the
    # current challenge page source so we pick (360, 360) on the Elsevier-
    # footer layout and (400, 420) on the default Cloudflare Turnstile.
    return _base_safe_get(
        skipped_file, driver, url, journal_id,
        retries=retries,
        wait_time=wait,
        head=HEADLESS,
        challenge_predicate=_is_sd_challenge,
        hardblock_predicate=_is_sd_hardblock,
        captcha_click_x=_sd_captcha_click_x,
        captcha_click_y=_sd_captcha_click_y,
    )


def _url_sha1(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()


def _journal_jsonl_path() -> str:
    d = os.path.join(_PROJECT_ROOT, "science_direct")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{DATABASE}_journals.jsonl")


def _journal_chunks_dir() -> str:
    d = os.path.join(_PROJECT_ROOT, "science_direct", "journals_chunks")
    os.makedirs(d, exist_ok=True)
    return d


def _load_existing_journals_lookup() -> dict[str, dict]:
    """
    Load EXISTING_JOURNALS_JSON (a previous mining export) and return a lookup
    keyed by both archive_page_url and journal_url → record. Records with no
    ISSN are kept so the caller can still see "we know this one, but it has
    no ISSN → fall back to web".
    """
    lookup: dict[str, dict] = {}
    if not os.path.exists(EXISTING_JOURNALS_JSON):
        print(f"[EXISTING] File not found: {EXISTING_JOURNALS_JSON}")
        return lookup
    try:
        with open(EXISTING_JOURNALS_JSON, "r", encoding="utf-8", errors="ignore") as fh:
            data = json.load(fh)
    except Exception as e:
        print(f"[EXISTING] Failed to parse {EXISTING_JOURNALS_JSON}: {e}")
        return lookup
    if not isinstance(data, list):
        print(f"[EXISTING] Unexpected format (not a list) in {EXISTING_JOURNALS_JSON}")
        return lookup
    for r in data:
        if not isinstance(r, dict):
            continue
        arc = (r.get("archive_page_url") or "").strip()
        ju  = (r.get("journal_url") or "").strip()
        if arc:
            lookup[arc] = r
        if ju:
            lookup.setdefault(ju, r)
    print(f"[EXISTING] Loaded {len(data)} journals from {EXISTING_JOURNALS_JSON}")
    return lookup


# ════════════════════════════════════════════════════════════════════════════
#  HTML ARCHIVING  (E: drive)
# ════════════════════════════════════════════════════════════════════════════

def save_article_html(html: str, url: str, journal_id: str) -> None:
    """
    Save a copy of the article HTML to E:\\science_direct_html\\<jid>\\<sha1>.html.
    Silent if the E: drive is unavailable or the file already exists.
    """
    try:
        dest_dir = os.path.join(HTML_SAVE_DIR, str(journal_id))
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, _url_sha1(url) + ".html")
        if not os.path.exists(dest):
            with open(dest, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(html)
    except Exception as e:
        print(f"[HTML] Save failed for {url[:80]}: {e}")


# ════════════════════════════════════════════════════════════════════════════
#  PATTERN DETECTION
# ════════════════════════════════════════════════════════════════════════════

def check_patterns(soup: BeautifulSoup, page_type: str, url: str, jid: str) -> bool:
    """
    Returns True  – at least one expected selector matched (page looks normal).
    Returns False – NONE matched; fires a Telegram alert; caller should stop/skip.
    """
    selectors = _EXPECTED_PATTERNS.get(page_type, [])
    if not selectors:
        return True
    for sel in selectors:
        if soup.select(sel):
            return True
    details = (
        f"Page type '{page_type}': none of these selectors found:\n"
        + "\n".join(f"  • {s}" for s in selectors)
        + f"\n\nURL: {url}"
    )
    notify_pattern_change(jid, url, details)
    return False


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 1 – COLLECT JOURNALS
# ════════════════════════════════════════════════════════════════════════════

def _load_saved_archive_urls() -> set[str]:
    """Return archive_page_url values already written to the local JSONL."""
    saved: set[str] = set()
    path = _journal_jsonl_path()
    if not os.path.exists(path):
        return saved
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                rec = json.loads(line.strip())
                u = rec.get("archive_page_url") or rec.get("archive_url", "")
                if u:
                    saved.add(u)
            except Exception:
                pass
    return saved


def _append_journal_batch(records: list[dict], chunks_dir: str) -> None:
    """Append to single JSONL + write a timestamped chunk for crash safety."""
    if not records:
        return
    with open(_journal_jsonl_path(), "a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    chunk_path = os.path.join(chunks_dir, f"journals_{ts}.jsonl")
    with open(chunk_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[SAVE] {len(records)} journals saved")


def collect_journals(driver=None) -> list[dict]:
    """
    Phase 1: paginate the browse page to collect journal stubs, then visit
    each archive (/issues) page to extract Online/Print ISSNs.

    Saves results to science_direct/science_direct_journals.jsonl.
    Returns a list of new records collected in this run.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException, WebDriverException

    SKIPPED = os.path.join(_PROJECT_ROOT, "science_direct",
                           f"{DATABASE}_journal_collect_skipped.txt")
    chunks_dir  = _journal_chunks_dir()
    saved_urls  = _load_saved_archive_urls()

    own_driver = driver is None
    if own_driver:
        driver = create_driver(HEADLESS)

    # ── Step A: paginate browse page, collect journal stubs ─────────────────

    def _click_next() -> bool:
        try:
            btn = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "button[aria-label='Next page']")
                )
            )
            if (btn.get_attribute("disabled")
                    or btn.get_attribute("aria-disabled") == "true"):
                return False
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(10)
            return True
        except TimeoutException:
            return False

    stubs: list[dict] = []
    seen: set[str]    = set()

    try:
        driver = safe_get(SKIPPED, driver, BROWSE_URL, "sd_browse")
        WebDriverWait(driver, 50).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(random.uniform(5, 10))
    except Exception as e:
        print(f"[COLLECT] Browse page load failed: {e}")
        if own_driver:
            _safe_quit(driver)
        return []

    page = 1
    while True:
        soup = BeautifulSoup(driver.page_source, "html.parser")
        tags = soup.find_all("a", class_="anchor js-publication-title anchor-primary")

        if not tags:
            # Alert only on the first page – later pages ending is normal (last page)
            if page == 1:
                check_patterns(soup, "browse", BROWSE_URL, "sd_browse")
            print(f"[COLLECT] No journals on page {page} – end of browse.")
            break

        for tag in tags:
            name = tag.get_text(strip=True)
            href = tag.get("href", "").lstrip("/")
            if not href:
                continue
            j_url   = f"{BASE_URL}/{href}"
            arc_url = f"{j_url}/issues"
            if j_url not in seen:
                seen.add(j_url)
                stubs.append({
                    "journal_name":     name,
                    "journal_url":      j_url,
                    "archive_page_url": arc_url,
                })

        print(f"[COLLECT] Browse page {page}: {len(tags)} journals (total stubs: {len(stubs)})")
        if not _click_next():
            break
        page += 1

    print(f"[COLLECT] {len(stubs)} journal stubs collected over {page} browse pages")

    # ── Step B: visit each archive page to extract ISSNs ────────────────────

    existing_lookup = _load_existing_journals_lookup()

    new_records: list[dict] = []
    batch:       list[dict] = []
    url_count = 0
    from_file_count = 0
    from_web_count  = 0

    for idx, stub in enumerate(stubs, start=1):
        arc_url  = stub["archive_page_url"]
        j_name   = stub["journal_name"]
        j_url    = stub["journal_url"]

        if arc_url in saved_urls:
            print(f"[SKIP] Already saved: {j_name}")
            continue

        # ── Reuse existing JSON when it already has an ISSN for this journal
        existing = existing_lookup.get(arc_url) or existing_lookup.get(j_url)
        existing_issn = (existing.get("issn") or "").strip() if existing else ""
        if existing_issn:
            existing_pssn = (existing.get("pssn") or "").strip()
            rec = {
                "publisher":        "science_direct",
                "journal_name":     j_name,
                "journal_url":      j_url,
                "archive_page_url": arc_url,
                "issn":             existing_issn,
                "pssn":             existing_pssn,
                "jid":              existing.get("jid") or existing_issn.replace("-", ""),
            }
            new_records.append(rec)
            batch.append(rec)
            saved_urls.add(arc_url)
            from_file_count += 1
            print(f"[{idx}/{len(stubs)}] {j_name} | ISSN={existing_issn} | "
                  f"PISSN={existing_pssn or 'N/A'} (from file)")
            if len(batch) >= 10:
                _append_journal_batch(batch, chunks_dir)
                batch.clear()
            continue

        # Rotate to a fresh driver every 10 web requests to vary user-agent
        if url_count > 0 and url_count % 10 == 0:
            _safe_quit(driver)
            driver = create_driver(HEADLESS)
            print(f"[DRIVER] Rotated after {url_count} requests")

        time.sleep(random.uniform(*DELAY_ISSN_FETCH))

        try:
            driver = safe_get(SKIPPED, driver, arc_url, "sd_browse")
            url_count += 1
        except WebDriverException:
            continue
        if not driver:
            print(f"[SKIP] Could not load: {arc_url}")
            continue

        soup  = BeautifulSoup(driver.page_source, "html.parser")
        issn  = ""
        pssn  = ""
        info  = soup.find("p", class_="u-margin-xs-bottom text-s u-display-block js-issn")
        text  = info.get_text(" ", strip=True) if info else ""

        m = re.search(r"Online ISSN:\s*([\dXx\-]+)", text, re.I)
        if m:
            issn = m.group(1)
        m = re.search(r"Print ISSN:\s*([\dXx\-]+)", text, re.I)
        if m:
            pssn = m.group(1)
        if not issn:
            m = re.search(r"ISSN:\s*([\dXx\-]+)", text, re.I)
            if m:
                issn = m.group(1)

        # Deterministic jid: use clean ISSN or MD5 of URL
        jid = (issn.replace("-", "")
               if issn
               else hashlib.md5(arc_url.encode()).hexdigest()[:12])

        rec = {
            "publisher":        "science_direct",
            "journal_name":     j_name,
            "journal_url":      j_url,
            "archive_page_url": arc_url,
            "issn":             issn,
            "pssn":             pssn,
            "jid":              jid,
        }
        new_records.append(rec)
        batch.append(rec)
        saved_urls.add(arc_url)
        from_web_count += 1
        print(f"[{idx}/{len(stubs)}] {j_name} | ISSN={issn or 'N/A'} | PISSN={pssn or 'N/A'} (from web)")

        if len(batch) >= 10:
            _append_journal_batch(batch, chunks_dir)
            batch.clear()

    if batch:
        _append_journal_batch(batch, chunks_dir)

    if own_driver:
        _safe_quit(driver)

    print(f"[COLLECT] Done – {len(new_records)} new journals collected "
          f"(from file: {from_file_count}, from web: {from_web_count})")
    return new_records


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 2 – POST JOURNALS TO DATABASE
# ════════════════════════════════════════════════════════════════════════════

def load_journals_from_file() -> list[dict]:
    """Read & deduplicate journals from the local JSONL."""
    path = _journal_jsonl_path()
    if not os.path.exists(path):
        return []
    by_url: dict[str, dict] = {}
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                key = rec.get("journal_url") or rec.get("jid", f"__{len(by_url)}")
                by_url[key] = rec
            except Exception:
                pass
    print(f"[FILE] {len(by_url)} journals loaded (deduplicated by journal_url)")
    return list(by_url.values())


def split_journals(num_parts: int = 12) -> None:
    """
    Split the local JSONL (built by collect_journals) into N part files:
      science_direct/science_direct_part_1.txt ... _part_N.txt

    Each file is the {"page", "page_size", "records"} JSON format that
    load_journals_from_cache expects, so main(part) / build_maps(part)
    will pick them up without any further config.

    Fallback: if there is no local JSONL yet (Phase 1 was never run on
    this machine), download the journals straight from the API into a
    cache file and split from there. Lets fresh checkouts run `split`
    without first having to do `collect` + `upload`.
    """
    sd_dir = os.path.join(_PROJECT_ROOT, "science_direct")
    os.makedirs(sd_dir, exist_ok=True)

    journals = load_journals_from_file()
    if not journals:
        cache_file = os.path.join(sd_dir, f"{DATABASE}_journals.json")
        print(f"[SPLIT] No local JSONL — fetching from API → {cache_file}")
        try:
            journals = fetch_and_cache_journals(cache_file, DATABASE)
        except Exception as e:
            print(f"[SPLIT] API fetch failed: {e}")
            journals = []
        if not journals:
            print("[SPLIT] No journals available from API either — run `collect` first.")
            return
        print(f"[SPLIT] Downloaded {len(journals)} journals from API")

    n = len(journals)
    chunk = (n + num_parts - 1) // num_parts
    for i in range(1, num_parts + 1):
        start = (i - 1) * chunk
        end   = min(i * chunk, n)
        sl    = journals[start:end]
        path  = os.path.join(sd_dir, f"{DATABASE}_part_{i}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"page": i, "page_size": len(sl), "records": sl},
                      fh, ensure_ascii=False, indent=2)
        print(f"[SPLIT] Wrote {path} ({len(sl)} journals)")
    print(f"[SPLIT] Done — {n} journals split into {num_parts} parts")


def post_journals_to_db(batch_size: int = 50) -> None:
    """Phase 2: push local JSONL to the database API."""
    journals = load_journals_from_file()
    if not journals:
        print("[UPLOAD] No journals to upload – run collect_journals() first.")
        return

    url   = f"{API_ROOT}/{DATABASE}/add/journals"
    sent  = 0
    failed = 0
    total  = len(journals)

    for i in range(0, total, batch_size):
        # Strip local-only 'jid' so the server generates its own ID
        chunk = [{k: v for k, v in r.items() if k != "jid"}
                 for r in journals[i: i + batch_size]]
        try:
            resp = requests.post(url, json=chunk, timeout=60)
            if resp.status_code == 200:
                sent += len(chunk)
                print(f"[UPLOAD] Batch {i // batch_size + 1}: {len(chunk)} sent ({sent}/{total})")
            else:
                failed += len(chunk)
                print(f"[UPLOAD] Batch failed {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            failed += len(chunk)
            print(f"[UPLOAD] Request error: {e}")

    print(f"[UPLOAD] Done – sent={sent}  failed={failed}  total={total}")


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3a – VOLUME / ISSUE MAP
# ════════════════════════════════════════════════════════════════════════════

def build_volume_issue_map(driver, journal: dict, skipped_file: str) -> tuple[dict, object]:
    """
    Returns (volume_issue_map, driver).

    Map format:
      { "vol": { "volume": "vol", "year": "2024", "issues": { "iss": "url" } } }

    Loads from backup (C:\\science_direct\\<jid>\\volume_issue_map.json) if
    available; otherwise scrapes the /issues archive page.
    """
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.common.exceptions import TimeoutException

    jid     = journal["jid"]
    arc_url = journal.get("archive_page_url", "")

    # Use cached map if it exists (skip re-scraping on restart)
    cached = read_backup_json(jid, DATABASE)
    if cached and isinstance(cached, dict):
        print(f"[MAP] Loaded from cache: {jid} ({len(cached)} volumes)")
        return cached, driver

    if not arc_url:
        print(f"[MAP] No archive URL for {jid}")
        return {}, driver

    if not arc_url.endswith("/issues"):
        arc_url = arc_url.rstrip("/") + "/issues"

    driver = safe_get(skipped_file, driver, arc_url, jid)
    if not driver:
        return {}, driver

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        time.sleep(2)
    except Exception:
        pass

    soup = BeautifulSoup(driver.page_source, "html.parser")
    if not check_patterns(soup, "archive", arc_url, jid):
        # Pattern changed – save empty map and alert was already sent
        save_backup_json(DATABASE, jid, {}, "volume_issue_map.json")
        return {}, driver

    vol_map: dict = {}

    # ── Selectors for /issues page ────────────────────────────────────────
    # Year/volume panels live inside this exact ol:
    #   <ol class="accordion-container u-font-sans js-accordion-container">
    #     <li class="accordion-panel">
    #       <button aria-expanded="false" ...>2026 — Volumes 130-134</button>
    #       ...
    #     </li>
    #   </ol>
    # The sibling sidebar accordion (Articles & Issues / About / Publish)
    # uses li.accordion-panel inside a different ol (no js-accordion-container),
    # so this selector excludes it.
    #
    # The archive is paginated: a Next/Prev pager sits in
    #   div.js-issues-archive .pagination-controls
    # We walk every page until Next is missing/disabled.
    ARCHIVE_OL_SELECTOR = (
        "ol.accordion-container.u-font-sans.js-accordion-container"
    )
    ARCHIVE_OL_FALLBACK = "div.js-issues-archive ol.accordion-container"

    def _archive_ol():
        els = driver.find_elements(By.CSS_SELECTOR, ARCHIVE_OL_SELECTOR)
        if not els:
            els = driver.find_elements(By.CSS_SELECTOR, ARCHIVE_OL_FALLBACK)
        return els

    def _record_issue_links(panel_html: str, fallback_year: str = "") -> int:
        """Parse one panel's innerHTML for a.js-issue-item-link entries."""
        added = 0
        psoup = BeautifulSoup(panel_html, "html.parser")
        for a in psoup.select("a.js-issue-item-link"):
            href = a.get("href", "")
            # SD uses two URL shapes for issues:
            #   numbered issues:   /journal/<slug>/vol/N/issue/M
            #   continuous-pub:    /journal/<slug>/vol/N/suppl/C   (one entry = one whole volume)
            m = re.search(r"/vol/(\d+)/(?:issue/(\d+)|suppl/([A-Za-z0-9]+))", href)
            if not m:
                continue
            vol = m.group(1)
            iss = m.group(2) or (f"suppl-{m.group(3)}" if m.group(3) else "")
            if not iss:
                continue
            issue_url = urljoin(BASE_URL, href)

            # Year may live either on the panel button title ("2026 - Volumes ...")
            # or in a per-issue js-issue-status pill ("(March 2024)")
            year = fallback_year
            holder = a.find_parent("div", class_=lambda c: c and "issue-item" in c) \
                or a.find_parent("li") \
                or psoup
            st = holder.find("div", class_=lambda c: c and "js-issue-status" in c)
            if st:
                ym = re.search(r"\((\d{4})\)|(\d{4})", st.get_text(" ", strip=True))
                if ym:
                    year = ym.group(1) or ym.group(2) or year

            if vol not in vol_map:
                vol_map[vol] = {"volume": vol, "year": year, "issues": {}}
            if iss not in vol_map[vol]["issues"]:
                added += 1
            vol_map[vol]["issues"][iss] = issue_url
            if year:
                vol_map[vol]["year"] = year
            print(f"  Vol {vol} | Iss {iss} | {year} | {issue_url}")
        return added

    def _process_current_page() -> int:
        """Expand every year/volume panel on the current pagination page."""
        ols = _archive_ol()
        if not ols:
            print(f"[MAP] js-accordion-container missing on current page")
            return 0
        panels = ols[0].find_elements(By.CSS_SELECTOR, "li.accordion-panel")
        total = len(panels)
        print(f"[MAP] {total} year/volume panels on this page")

        page_added = 0
        for idx in range(total):
            ols = _archive_ol()
            if not ols:
                print(f"[MAP] ol disappeared at idx={idx}")
                break
            panels = ols[0].find_elements(By.CSS_SELECTOR, "li.accordion-panel")
            if idx >= len(panels):
                print(f"[MAP] Panel count shrank ({len(panels)} < {idx+1})")
                break

            btns = panels[idx].find_elements(By.TAG_NAME, "button")
            if not btns:
                continue
            btn = btns[0]

            title_txt = (btn.text or "").strip().replace("\n", " | ")[:80]
            # Pre-extract a year hint from the title (e.g. "2026 — Volumes 130-134")
            ym_title = re.search(r"\b(19|20)\d{2}\b", title_txt)
            year_hint = ym_title.group(0) if ym_title else ""
            expanded = (btn.get_attribute("aria-expanded") or "").lower() == "true"

            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", btn
                )
                time.sleep(0.3)
                if not expanded:
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(1.5)
            except Exception as e:
                print(f"[MAP] Click failed idx={idx} ({title_txt}): {e}")
                continue

            # Wait for an issue link inside THIS panel
            try:
                WebDriverWait(driver, 10).until(
                    lambda _: bool(
                        panels[idx].find_elements(
                            By.CSS_SELECTOR, "a.js-issue-item-link"
                        )
                    )
                )
            except TimeoutException:
                print(f"[MAP] Issue links didn't load for idx={idx} ({title_txt})")

            ols = _archive_ol()
            panels2 = (
                ols[0].find_elements(By.CSS_SELECTOR, "li.accordion-panel")
                if ols else []
            )
            try:
                li_html = (
                    panels2[idx].get_attribute("innerHTML")
                    if idx < len(panels2) else ""
                )
            except Exception:
                li_html = ""

            page_added += _record_issue_links(li_html, fallback_year=year_hint)
        return page_added

    def _next_pager_button():
        """Return the active Next-page button (None if missing or disabled)."""
        candidates = driver.find_elements(
            By.CSS_SELECTOR,
            "div.js-issues-archive button[aria-label='Next page']",
        )
        if not candidates:
            candidates = driver.find_elements(
                By.CSS_SELECTOR, "button[aria-label='Next page']"
            )
        for b in candidates:
            if (b.get_attribute("disabled") or "").lower() in ("", "false"):
                if (b.get_attribute("aria-disabled") or "").lower() != "true":
                    return b
        return None

    # ── Walk every pagination page ────────────────────────────────────────
    seen_first_titles: set[str] = set()
    page_num = 1
    while True:
        print(f"[MAP] === Archive page {page_num} ===")
        _process_current_page()

        nxt = _next_pager_button()
        if not nxt:
            print(f"[MAP] No more pages (Next disabled / missing) at page {page_num}")
            break

        # Snapshot the first panel's title to detect when the page actually changes
        ols = _archive_ol()
        first_title = ""
        if ols:
            first_panel = ols[0].find_elements(By.CSS_SELECTOR, "li.accordion-panel")
            if first_panel:
                btns = first_panel[0].find_elements(By.TAG_NAME, "button")
                if btns:
                    first_title = (btns[0].text or "").strip()
        if first_title in seen_first_titles:
            print(f"[MAP] First-panel title repeated ({first_title!r}) – stopping")
            break
        seen_first_titles.add(first_title)

        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", nxt)
            time.sleep(0.4)
            driver.execute_script("arguments[0].click();", nxt)
        except Exception as e:
            print(f"[MAP] Next-page click failed: {e}")
            break

        # Wait until the first-panel title changes (= new page loaded)
        try:
            WebDriverWait(driver, 15).until(
                lambda _: (
                    (_archive_ol() or [None])[0] is not None
                    and (_archive_ol()[0].find_elements(By.CSS_SELECTOR, "li.accordion-panel") or [None])[0] is not None
                    and (_archive_ol()[0]
                         .find_elements(By.CSS_SELECTOR, "li.accordion-panel")[0]
                         .find_elements(By.TAG_NAME, "button")[0].text or "").strip() != first_title
                )
            )
        except TimeoutException:
            print(f"[MAP] Pagination didn't refresh after click (page {page_num})")
            break
        time.sleep(1.5)
        page_num += 1

    save_backup_json(DATABASE, jid, vol_map, "volume_issue_map.json")
    print(f"[MAP] Saved: {jid} – {len(vol_map)} volumes, "
          f"{sum(len(v['issues']) for v in vol_map.values())} issues "
          f"across {page_num} archive page(s)")
    return vol_map, driver


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3b – EXTRACT ARTICLE LINKS FROM ONE ISSUE PAGE
# ════════════════════════════════════════════════════════════════════════════

def extract_issue_articles(
    driver,
    issue_url: str,
    jid: str,
    skipped_file: str,
    vol: str,
    iss: str,
    year: str,
) -> tuple[list[dict], object]:
    """
    Scrape one issue TOC page.
    Returns ([article_stubs], driver).

    Each stub contains: jid, article_title, article_type, article_url,
                        pdf, published_date, published_year, volume, issue.
    """
    print(f"[3b] Fetching issue TOC: Vol {vol} Iss {iss} | {issue_url}")
    driver = safe_get(skipped_file, driver, issue_url, jid)
    if not driver:
        print(f"[3b] safe_get returned no driver for {issue_url}")
        return [], driver

    time.sleep(3)
    soup = BeautifulSoup(driver.page_source, "html.parser")

    # Pattern check is advisory only — log the mismatch but still try to parse.
    # Bailing here was hiding silent skips when SD tweaks the wrapper class.
    if not check_patterns(soup, "issue", issue_url, jid):
        print(f"[3b] Issue pattern check failed (parsing anyway): {issue_url}")

    articles: list[dict] = []
    seen_urls: set[str] = set()

    def _add(item, a_title, article_type: str) -> None:
        """Build one article record from the wrapping element + the title anchor."""
        href = a_title.get("href", "")
        if not href:
            return
        art_url = urljoin(BASE_URL, href.split("?")[0])
        if art_url in seen_urls or art_url == issue_url:
            return
        # Prefer the inner span carrying the visible title; fall back to anchor text
        title_span = a_title.select_one("span.js-article-title") or a_title
        title = title_span.get_text(" ", strip=True)

        # PDF link — only count anchors actually flagged as the PDF download.
        # Absolutize: SD sometimes ships relative paths like
        # "/science/article/pii/…/pdfft" so the saved URL is openable as-is.
        pdf_url = ""
        pdf_a = item.select_one("a.pdf-download, li.PdfLink a") if item else None
        if pdf_a:
            href = pdf_a.get("href", "")
            if href:
                pdf_url = urljoin(BASE_URL, href)

        # Published date e.g. "15 March 2024" (only present on some layouts)
        pub_date = ""
        date_li = item.find("li", class_="ePubDate") if item else None
        if date_li:
            spans = date_li.find_all("span")
            if len(spans) >= 2:
                pub_date = spans[1].get_text(strip=True)

        seen_urls.add(art_url)
        articles.append({
            "jid":             jid,
            "article_title":   title,
            "article_type":    article_type,
            "article_url":     art_url,
            "pdf":             pdf_url,
            "published_date":  pub_date,
            "published_year":  year,
            "volume":          vol,
            "issue":           iss,
        })
        print(f"  [{len(articles):>3}] {article_type or '-':<20} | {title[:70]} | {art_url}")

    # Layout A (current SD): <li class="js-article-list-item article-item">
    #   wrapping <a class="article-content-title" href="/science/article/pii/…">
    list_items = soup.select("li.js-article-list-item, li.article-item")
    for item in list_items:
        a = item.select_one("a.article-content-title")
        if not a:
            continue
        type_span = item.select_one("span.js-article-subtype")
        article_type = type_span.get_text(strip=True) if type_span else ""
        _add(item, a, article_type)

    # Layout B (older SD): div.issue-items-container > div.issue-item > a.issue-item__title
    if not articles:
        for section in soup.find_all("div", class_="issue-items-container"):
            h3 = section.find("h3", class_="toc__heading")
            article_type = h3.get_text(strip=True) if h3 else ""
            for item in section.find_all("div", class_="issue-item"):
                a = item.find("a", class_="issue-item__title")
                if a:
                    _add(item, a, article_type)

    # Fallback: still nothing → use the title-anchor class globally (NOT any
    # /science/article/ href, which would scoop PDF download links too).
    if not articles:
        for a in soup.select("a.article-content-title, a.issue-item__title"):
            _add(a.parent, a, "")
        if articles:
            print(f"[3b] Fallback (title-anchor only) found {len(articles)} articles")

    print(f"[ISSUE] Vol {vol} Iss {iss}: {len(articles)} articles | {issue_url}")
    return articles, driver


# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3c – EXTRACT AUTHOR DATA FROM ONE ARTICLE PAGE
# ════════════════════════════════════════════════════════════════════════════

def _extract_preloaded_state(html: str) -> dict | None:
    """Pull window.__PRELOADED_STATE__ out of an SD article page as a dict.
    Returns None if the marker isn't present or the JSON can't be parsed."""
    m = re.search(r"window\.__PRELOADED_STATE__\s*=\s*", html)
    if not m:
        return None
    start = m.end()
    # Brace-balanced extraction (string-aware) — the value runs until the
    # matching closing brace, possibly tens of KB of JSON.
    depth = 0
    in_str = False
    escape = False
    i = start
    n = len(html)
    while i < n:
        c = html[i]
        if escape:
            escape = False
        elif in_str:
            if c == "\\":
                escape = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    i += 1
                    break
        i += 1
    try:
        return json.loads(html[start:i])
    except Exception:
        return None


def _decode_sd_email(encoded: str) -> str:
    """SD emails are base64-encoded URL-encoded JSON. Decode → URL-decode → JSON."""
    if not encoded:
        return ""
    try:
        url_encoded = base64.b64decode(encoded).decode("utf-8", errors="replace")
        decoded = unquote(url_encoded)
        data = json.loads(decoded)
        return (data.get("_") or "").strip()
    except Exception:
        return ""


def _parse_preloaded_authors(html: str) -> list[dict]:
    """Pattern C – pull authors from window.__PRELOADED_STATE__ JSON.

    Most reliable on modern SD pages: visible author markup is now a side-panel
    dialog (only names visible in DOM), but the full author/affiliation graph
    plus encoded emails live in the inlined preloaded-state script.
    """
    state = _extract_preloaded_state(html)
    if not state:
        return []

    # PRELOADED_STATE on Letter / Reply / Commentary articles contains
    # MULTIPLE <author-group> blocks — the article's own group plus the
    # original-letter / responded-to author group(s). A naive recursive
    # walk picks up all of them. We instead find the FIRST author-group
    # outside reference / discussion / commentary subtrees (= the article's
    # own) and collect authors only from that. Affiliations still come
    # from a global walk because they're often siblings of author-group
    # under <head>, not children.
    #
    # Subtrees to skip when locating the primary author-group:
    #   * bib-reference / reference / ref-list  – cited papers
    #   * discussion / letter / commentary       – referenced-article authors
    #   * reply / response / comment / editorial – e.g. "Reply to [Smith et al.]"
    #   * related-article / correction / erratum – attached secondary content
    _SKIP_SUBTREES = {
        "bib-reference", "reference", "ref", "citation",
        "ref-list", "references", "bibliography",
        "discussion", "letter", "commentary", "reply", "response",
        "comment", "editorial", "related-article", "related-content",
        "correction", "erratum", "errata",
    }

    def _find_primary_author_group(node, in_skip: bool = False):
        if isinstance(node, dict):
            n = node.get("#name")
            if not in_skip and n == "author-group":
                return node
            child_in_skip = in_skip or (n in _SKIP_SUBTREES)
            for v in node.values():
                r = _find_primary_author_group(v, child_in_skip)
                if r is not None:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = _find_primary_author_group(v, in_skip)
                if r is not None:
                    return r
        return None

    primary_group = _find_primary_author_group(state)

    found_authors: list[dict] = []
    found_affils: list[dict] = []

    def _walk_authors(node, in_skip: bool = False) -> None:
        if isinstance(node, dict):
            n = node.get("#name")
            if not in_skip and n == "author":
                found_authors.append(node)
            child_in_skip = in_skip or (n in _SKIP_SUBTREES)
            for v in node.values():
                _walk_authors(v, child_in_skip)
        elif isinstance(node, list):
            for v in node:
                _walk_authors(v, in_skip)

    if primary_group is not None:
        # Just walk inside the primary group — no further skip needed,
        # author-group can't contain reference subtrees in SD's schema.
        _walk_authors(primary_group)
    else:
        # Fallback: some pages don't wrap authors in author-group.
        _walk_authors(state)

    # Affiliation id prefix varies across SD pages: full-text uses "aff0010",
    # preview / abstract-only uses "af0005". Accept both — anything matching
    # /^aff?\d/ is an affiliation, /^naf/ is a non-author note we skip.
    def _is_aff_id(aid: str) -> bool:
        return bool(re.match(r"^aff?\d", str(aid)))

    def _walk_affils(node, in_skip: bool = False) -> None:
        if isinstance(node, dict):
            n = node.get("#name")
            if not in_skip and n == "affiliation":
                aid = (node.get("$", {}) or {}).get("id", "")
                if _is_aff_id(aid):
                    found_affils.append(node)
            child_in_skip = in_skip or (n in _SKIP_SUBTREES)
            for v in node.values():
                _walk_affils(v, child_in_skip)
        elif isinstance(node, list):
            for v in node:
                _walk_affils(v, in_skip)

    _walk_affils(state)
    if not found_authors:
        return []

    def text_of(node: dict, name: str) -> str:
        for child in (node.get("$$") or []):
            if isinstance(child, dict) and child.get("#name") == name:
                return (child.get("_") or "").strip()
        return ""

    # affiliation id → text
    aff_map: dict[str, str] = {}
    for af in found_affils:
        aid = (af.get("$", {}) or {}).get("id", "")
        if not aid:
            continue
        aff_map[aid] = text_of(af, "textfn") or text_of(af, "label")

    authors: list[dict] = []
    seen_ids: set[str] = set()    # PRELOADED_STATE often duplicates the author list
    seen_names: set[str] = set()  # …and authors without an id (preview pages)
    for au in found_authors:
        attrs = au.get("$", {}) or {}
        au_id = attrs.get("author-id") or attrs.get("id") or ""
        if au_id and au_id in seen_ids:
            continue
        if au_id:
            seen_ids.add(au_id)

        given   = text_of(au, "given-name")
        surname = text_of(au, "surname")
        name    = " ".join(p for p in (given, surname) if p).strip()
        if not name:
            continue

        # Name-based dedupe catches duplicates that don't share author-id
        # (e.g. when SD emits the same author twice in head + author-group).
        name_key = name.lower()
        if name_key in seen_names:
            continue
        seen_names.add(name_key)

        # Walk children once for refs + email. SD ships two id-prefix
        # conventions: full-text articles use "aff…" / "cor…", preview /
        # abstract-only pages use "af…" / "cr…". Accept both.
        refs: list[str] = []
        is_corresp = False
        email = ""
        for child in (au.get("$$") or []):
            if not isinstance(child, dict):
                continue
            nm = child.get("#name")
            if nm == "cross-ref":
                rid = str((child.get("$", {}) or {}).get("refid", ""))
                if _is_aff_id(rid):
                    refs.append(rid)
                elif re.match(r"^(cor|cr)\d", rid):
                    is_corresp = True
            elif nm == "encoded-e-address" and not email:
                email = _decode_sd_email(child.get("__encoded", ""))

        affil = " | ".join(aff_map[r] for r in refs if r in aff_map)
        country = affil.split(",")[-1].strip().strip(".") if affil else ""

        # Degrees are intentionally dropped from author_name — clean_authors
        # mangles parenthesised suffixes like " (MD, MBA, FASA)" into
        # "Fasa)" by stripping recognised tokens but leaving the trailing
        # paren. The name field stays clean; degrees aren't load-bearing
        # downstream.
        authors.append({
            "author_name": name,
            "orcid":       attrs.get("orcid") or "",
            "email":       email,
            "affiliation": affil,
            "country":     country,
            "author_type": "Corresponding Author" if is_corresp else "Co-author",
        })
    return authors


def _parse_open_access_authors(soup: BeautifulSoup) -> list[dict]:
    """
    Pattern A – Open Access / full-text pages.
    Each author has a dedicated div.author-info block with name, type,
    email, ORCID, and affiliation paragraph.
    """
    authors = []
    for div in soup.select("div.author-info"):
        name_tag = div.select_one("p.author-name")
        if not name_tag:
            continue
        name = name_tag.get_text(strip=True)

        atype_tag = div.select_one("p.author-type")
        atype = atype_tag.get_text(strip=True) if atype_tag else "Co-author"

        email_a = div.select_one("a[href^='mailto:']")
        email   = email_a["href"].replace("mailto:", "").strip() if email_a else ""

        orcid_a = div.select_one("a[href*='orcid.org']")
        orcid   = orcid_a["href"].strip() if orcid_a else ""

        # First non-name, non-role paragraph = affiliation
        affil = ""
        for p in div.find_all("p"):
            txt = p.get_text(strip=True)
            if txt and txt != name and "Correspondence" not in txt and "Corresponding" not in txt:
                affil = txt
                break

        country = affil.split(",")[-1].strip().strip(".") if affil else ""

        authors.append({
            "author_name": name,
            "orcid":       orcid,
            "email":       email,
            "affiliation": affil,
            "country":     country,
            "author_type": atype,
        })
    return authors


def _build_sd_affil_map(soup: BeautifulSoup) -> dict[str, str]:
    """Return ``{sup_key → affiliation text}`` for one SD article page.

    SD ships affiliations in three different DOM shapes depending on layout:

      * Preview / abstract-only pages (Pattern 2):
          <dl class="affiliation">
            <dt><sup>a</sup></dt>
            <dd>College of Business, …, USA</dd>
          </dl>

      * Full-text subscription pages:
          <dl class="author-affiliation">
            <sup>a</sup>
            <span>…</span><span>…</span>   <!-- text split across spans -->
          </dl>

      * Open-access pages:
          <div class="affiliation"><sup>a</sup>…</div>

    Reading order: prefer the explicit `<dd>` text, then fall back to
    concatenated `<span>` children, then to the dl/div's own text minus
    the sup label.
    """
    affil_map: dict[str, str] = {}
    for el in soup.select(
        "dl.affiliation, dl.author-affiliation, div.affiliation"
    ):
        sup = el.find("sup")
        key = sup.get_text(strip=True) if sup else ""
        if not key:
            continue

        text = ""
        dd = el.find("dd")
        if dd:
            text = _clean_text(dd.get_text(" ", strip=True))
        if not text:
            parts = [s.get_text(strip=True) for s in el.find_all("span")
                     if s.get_text(strip=True)]
            text = " ".join(parts).strip()
        if not text:
            # Last resort: take the el's own text and strip the leading sup.
            full = _clean_text(el.get_text(" ", strip=True))
            if full.startswith(key):
                full = full[len(key):].strip()
            text = full

        if text:
            affil_map[key] = text
    return affil_map


def _parse_react_xocs_authors(soup: BeautifulSoup) -> list[dict]:
    """Pattern D – modern SD React-rendered author block (replaces the legacy
    a.author / button.author-link markup on newer subscription pages).

    Shape::

        <div class="author-group" id="author-group">
          <button data-xocs-content-type="author" data-xocs-content-id="auN">
            <span class="given-name">First</span>
            <span class="text surname">Last</span>
            <span>Degrees</span>                       <!-- optional, no class -->
            <span class="author-ref" id="baffN"><sup>a</sup></span>
            <svg title="Correspondence author icon"/>  <!-- optional -->
            <svg title="Author email or social media contact details icon"/>
          </button>,
          <a class="anchor anchor-secondary anchor-underline" href="/author/...">
            <span class="given-name">…</span> <span class="text surname">…</span>
            <span class="author-ref"><sup>c</sup></span>
            <span class="author-ref"><sup>d</sup></span>
          </a>
          <button class="react-xocs-icon-only-link" …>   <!-- marker for the
            <svg title="Correspondence author icon"/>          previous author;
            <svg title="Author email or social media contact details icon"/>
          </button>                                          NOT a separate author -->
        </div>

    Affiliation labels (a, b, c) live in `span.author-ref > sup`; the actual
    affiliation text comes from a separate affiliation block elsewhere on the
    page, indexed by the same letter via `_build_sd_affil_map`.
    """
    affil_map = _build_sd_affil_map(soup)

    authors: list[dict] = []
    seen_names: set[str] = set()

    for group in soup.select("div.author-group"):
        entries = group.select(
            "button[data-xocs-content-type='author'], "
            "a.anchor.anchor-secondary[href*='/author/']"
        )
        for entry in entries:
            # Icon-only buttons (`react-xocs-icon-only-link`) are visual
            # markers for the previous author's corresponding / email status
            # — they carry no name, just SVGs. Skip them as a separate author.
            classes = entry.get("class") or []
            if "react-xocs-icon-only-link" in classes:
                continue

            given_el = entry.find("span", class_="given-name")
            if not given_el:
                continue  # not a real author entry
            given = _clean_text(given_el.get_text(" ", strip=True))

            surname_el = entry.find(
                "span",
                class_=lambda c: c and "surname" in (
                    c if isinstance(c, str) else " ".join(c)
                ),
            )
            surname = (_clean_text(surname_el.get_text(" ", strip=True))
                       if surname_el else "")

            name = " ".join(p for p in (given, surname) if p).strip()
            if not name:
                continue

            name_key = name.lower()
            if name_key in seen_names:
                continue
            seen_names.add(name_key)

            sup_keys: list[str] = []
            for ref_sup in entry.select("span.author-ref sup"):
                k = ref_sup.get_text(strip=True)
                if k:
                    sup_keys.append(k)
            affil = " | ".join(affil_map[k] for k in sup_keys if k in affil_map)
            country = affil.split(",")[-1].strip().strip(".") if affil else ""

            # Corresponding-author detection: the SVG can sit either INSIDE
            # this entry (layout 2) or in the immediately-following icon-only
            # button (layout 1, where the indicator is a separate sibling).
            def _has_corresp_icon(node) -> bool:
                for svg in node.find_all("svg"):
                    if "Correspondence" in (svg.get("title") or ""):
                        return True
                return False

            is_corresp = _has_corresp_icon(entry)
            if not is_corresp:
                nxt = entry.find_next_sibling("button")
                if nxt and "react-xocs-icon-only-link" in (nxt.get("class") or []):
                    is_corresp = _has_corresp_icon(nxt)

            authors.append({
                "author_name": name,
                "orcid":       "",
                "email":       "",   # this layout doesn't expose the email
                "affiliation": affil,
                "country":     country,
                "author_type": "Corresponding Author" if is_corresp else "Co-author",
            })

    return authors


def _parse_subscription_authors(soup: BeautifulSoup) -> list[dict]:
    """
    Pattern B – Subscription / abstract-only pages.
    Authors are listed in div.author-group; affiliations in dl.author-affiliation.
    """
    authors = []
    seen: set[str] = set()

    affil_map = _build_sd_affil_map(soup)

    for a in soup.select("div.author-group a.author, button.author-link"):
        name_span = a.find("span", class_="content-author-text") or a.find("span")
        if not name_span:
            continue
        name = name_span.get_text(strip=True)
        if not name or name in seen:
            continue
        seen.add(name)

        sup = a.find("sup")
        sup_keys = re.split(r"[,\s]+", sup.get_text(strip=True)) if sup else []
        affil    = " | ".join(affil_map[k] for k in sup_keys if k in affil_map)
        country  = affil.split(",")[-1].strip() if affil else ""

        email_a  = a.find("a", href=lambda h: h and "mailto:" in h)
        email    = email_a["href"].replace("mailto:", "").strip() if email_a else ""

        orcid_a  = a.find("a", href=lambda h: h and "orcid.org" in h)
        orcid    = orcid_a["href"].strip() if orcid_a else ""

        atype = "Corresponding Author" if sup and "*" in sup.get_text() else "Co-author"

        authors.append({
            "author_name": name,
            "orcid":       orcid,
            "email":       email,
            "affiliation": affil,
            "country":     country,
            "author_type": atype,
        })
    return authors


# ─── Section extraction (normalized canonical names) ────────────────────────
# SD article body layout (modern):
#   <div class="Body" | "body"> contains many <section id="secN"> blocks,
#   each starting with <h2 class="u-h4"> or <h3 class="rx-font-18"> + content.
#   IDs follow patterns: secN, secN.M, dtboxN secM, coiN, ackN, appA, d1eN.
#   Abstract / Highlights / Keywords live separately under <div id="abstracts">
#   as nested <div class="abstract author"|"abstract author-highlights"|...>.
# Older layout (p3 sample):
#   No body container; sections nest as <section id="s0010"><section id="s0015">
#   …heading + content…</section></section>. Outer wraps inner.
# Noise sections to skip: id startswith "ot-" (OneTrust cookie consent).

_SECTION_CANONICAL_MAP: list[tuple[tuple[str, ...], str]] = [
    # Order matters — first match wins. More specific entries come first so
    # phrases like "ethical approval" (could mean ETHICS_STATEMENT or
    # PATIENT_CONSENT) and "code availability" (could mean CODE_AVAILABILITY
    # or DATA_AVAILABILITY) land in the more specific bucket.
    (("graphical abstract", "visual abstract", "pictorial abstract"), "GRAPHICAL_ABSTRACT"),
    (("abstract",), "ABSTRACT"),
    (("keywords", "index terms", "key words", "mesh terms", "author keywords"), "KEYWORDS"),
    (("introduction", "background and introduction", "overview"), "INTRODUCTION"),
    (("limitations", "study limitations", "limitations of the study",
      "strengths and limitations"), "LIMITATIONS"),
    (("conclusion", "conclusions", "summary", "concluding remarks",
      "final remarks", "closing remarks", "outlook"), "CONCLUSION"),
    (("challenges and future directions", "future work", "future directions",
      "perspectives", "challenges and opportunities", "future research",
      "open problems", "remaining challenges"), "FUTURE_DIRECTIONS"),
    (("background", "background/objective", "background and objective",
      "objective", "objectives",
      "literature review", "related work", "prior work",
      "state of the art", "theoretical background"), "BACKGROUND"),
    (("methods", "materials and methods", "materials & methods", "methodology",
      "experimental section", "experimental", "study design", "survey methodology",
      "research design", "patients and methods", "subjects and methods",
      "approach", "procedure"), "MATERIALS_METHODS"),
    (("results", "findings", "experimental results", "observations", "outcomes",
      "results and data"), "RESULTS"),
    (("discussion", "interpretation", "implications", "analysis", "commentary",
      "evaluation"), "DISCUSSION"),
    (("case report", "case description", "case presentation", "patient information",
      "case summary", "case history", "presenting concerns"), "CASE_PRESENTATION"),
    (("clinical findings", "examination", "diagnostic assessment",
      "physical examination", "laboratory findings", "investigations"),
     "CLINICAL_FINDINGS"),
    (("acknowledgements", "acknowledgments", "acknowledgement", "acknowledgment"),
     "ACKNOWLEDGMENTS"),
    # ETHICS_STATEMENT before PATIENT_CONSENT so "ethical approval" lands here.
    (("ethics statement", "irb approval", "ethical considerations",
      "ethical approval", "ethics approval", "institutional review"),
     "ETHICS_STATEMENT"),
    (("consent", "patient consent", "informed consent",
      "consent for publication", "statement of patient consent",
      "patient consent statement"), "PATIENT_CONSENT"),
    (("funding", "funding statement", "funding information", "financial support",
      "grant information", "funding sources", "support"), "FUNDING"),
    (("conflict of interest", "conflicts of interest", "competing interests",
      "declaration of interest", "declaration of competing interest",
      "coi statement", "disclosure", "disclosures",
      "competing declarations"), "CONFLICT_OF_INTEREST"),
    # CODE_AVAILABILITY before DATA_AVAILABILITY so "code availability" lands here.
    (("code availability", "software availability", "availability of code",
      "code and data availability", "open source"), "CODE_AVAILABILITY"),
    (("data availability", "data statement", "data sharing",
      "data availability statement", "open data"), "DATA_AVAILABILITY"),
    (("supporting information", "supplementary data", "appendix",
      "supplementary material", "supplementary materials",
      "additional information", "online resources",
      "electronic supplementary material"), "SUPPLEMENTARY"),
    (("authorship contribution statement",
      "credit authorship contribution statement",
      "author contributions", "authors' contributions",
      "credit author statement", "contributors", "author roles",
      "contributions"), "AUTHOR_CONTRIBUTION"),
    (("list of abbreviations", "abbreviations", "nomenclature", "glossary",
      "symbols", "list of symbols", "notation"), "ABBREVIATIONS"),
    (("references", "bibliography", "works cited", "literature cited",
      "citations"), "REFERENCES"),
]


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


# ─── Image embedding helpers (matched to rsc's batch shape) ─────────────────
# Pulls an image's bytes, optionally downscales with Pillow, returns the
# {data, mime, bytes, [width, height]} dict that gets merged into an img block.

_BROWSER_FETCH_JS = """
var cb = arguments[arguments.length - 1];
fetch(arguments[0], {credentials: 'include', cache: 'force-cache'})
  .then(function(r) {
    if (!r.ok) { cb({err: 'HTTP ' + r.status}); return; }
    var mime = r.headers.get('content-type') || 'image/jpeg';
    return r.arrayBuffer().then(function(buf) {
      var bytes = new Uint8Array(buf);
      var bin = '';
      for (var i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      cb({data: btoa(bin), mime: mime.split(';')[0].trim(), bytes: bytes.length});
    });
  })
  .catch(function(e) { cb({err: String(e)}); });
"""


def _fetch_image_via_browser(driver, url: str) -> dict:
    """Use Selenium's session (right cookies/Referer/TLS fingerprint) to
    grab the image as base64. Returns {data, mime, bytes} or {} on failure."""
    if driver is None:
        return {}
    try:
        driver.set_script_timeout(IMAGE_FETCH_TIMEOUT)
        result = driver.execute_async_script(_BROWSER_FETCH_JS, url)
    except Exception:
        return {}
    if not isinstance(result, dict) or result.get("err") or not result.get("data"):
        return {}
    return result


def _compress_base64_jpeg(b64: str) -> dict:
    """Re-encode any base64 image as a downscaled JPEG. Saves 3-10× storage
    vs the original PNG/GIF. Returns {} when Pillow isn't installed or it
    can't decode the input."""
    if not _HAVE_PIL:
        return {}
    try:
        from io import BytesIO
        raw = base64.b64decode(b64)
        img = _PIL_Image.open(BytesIO(raw))
        if img.mode in ("RGBA", "LA", "P"):
            bg = _PIL_Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > IMAGE_MAX_DIM:
            scale = IMAGE_MAX_DIM / float(max(img.size))
            img = img.resize(
                (int(img.size[0] * scale), int(img.size[1] * scale)),
                _PIL_Image.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG",
                 quality=IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
        data = buf.getvalue()
        return {
            "data":   base64.b64encode(data).decode("ascii"),
            "mime":   "image/jpeg",
            "bytes":  len(data),
            "width":  img.size[0],
            "height": img.size[1],
        }
    except Exception:
        return {}


def _download_and_compress_image(url: str) -> dict:
    """Two-path image grab: prefer the browser (no CDN blocks, no Pillow
    needed); fall back to direct HTTPS + Pillow when no driver is around
    (e.g. parsing a saved HTML file offline). Returns {} on full failure
    so the caller keeps the block URL-only."""
    # Path 1: browser fetch
    if _CURRENT_BROWSER is not None:
        result = _fetch_image_via_browser(_CURRENT_BROWSER, url)
        if result.get("data"):
            if _HAVE_PIL:
                compressed = _compress_base64_jpeg(result["data"])
                if compressed:
                    return compressed
            return result

    # Path 2: direct HTTPS + Pillow (offline test path)
    if not _HAVE_PIL:
        return {}
    try:
        from io import BytesIO
        r = requests.get(url, timeout=IMAGE_FETCH_TIMEOUT, verify=False)
        if r.status_code != 200:
            return {}
        img = _PIL_Image.open(BytesIO(r.content))
        if img.mode in ("RGBA", "LA", "P"):
            bg = _PIL_Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > IMAGE_MAX_DIM:
            scale = IMAGE_MAX_DIM / float(max(img.size))
            img = img.resize(
                (int(img.size[0] * scale), int(img.size[1] * scale)),
                _PIL_Image.LANCZOS,
            )
        buf = BytesIO()
        img.save(buf, format="JPEG",
                 quality=IMAGE_JPEG_QUALITY, optimize=True, progressive=True)
        data = buf.getvalue()
        return {
            "data":   base64.b64encode(data).decode("ascii"),
            "mime":   "image/jpeg",
            "bytes":  len(data),
            "width":  img.size[0],
            "height": img.size[1],
        }
    except Exception:
        return {}


# ─── Block builders (paragraph / heading / image / table / list / fig) ──────

_SKIP_DATA_ATTRS = {"data-original", "data-src"}
# Wrappers that just hold other blocks — we recurse into them rather than
# emitting them as a block on their own. NOTE: `div` is NOT here — SD uses
# `<div id="pXXXX">` as the paragraph element (mixed text + topic-link
# anchors). _element_to_blocks treats divs case-by-case.
_TRANSPARENT_TAGS = {"section", "article", "aside", "span"}
# Tag names that are themselves block-level (used by the div-classifier).
_BLOCK_LEVEL_CHILDREN = {
    "p", "div", "figure", "table", "section", "article", "aside",
    "ul", "ol", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
}


def _extract_meta(elem) -> dict:
    """Capture class / id / title / role / data-* on an element so cross-refs
    survive into the saved JSON. Returns {} when nothing of interest."""
    if elem is None or not hasattr(elem, "attrs"):
        return {}
    meta: dict = {}
    cls = elem.get("class")
    if cls:
        meta["class"] = [c for c in cls if c]
    for key in ("id", "title", "role"):
        v = elem.get(key)
        if v:
            meta[key] = v
    for attr, val in (elem.attrs or {}).items():
        if not attr.startswith("data-") or attr in _SKIP_DATA_ATTRS:
            continue
        meta[attr] = " ".join(val) if isinstance(val, list) else val
    return meta

def _with_meta(block: dict, elem) -> dict:
    m = _extract_meta(elem)
    if m:
        block["meta"] = m
    return block

def _img_block_from_node(img_tag) -> dict | None:
    """Build a single img block. Returns None if no usable src."""
    if img_tag is None or not getattr(img_tag, "get", None):
        return None
    src = (img_tag.get("src") or "").strip()
    if not src:
        src = (img_tag.get("data-original") or img_tag.get("data-src") or "").strip()
    if not src:
        return None
    abs_src = urljoin(BASE_URL, src)
    block: dict = {
        "type":  "img",
        "src":   abs_src,
        "alt":   (img_tag.get("alt") or "").strip(),
    }
    title = (img_tag.get("title") or "").strip()
    if title:
        block["title"] = title
    if EMBED_IMAGES and (_CURRENT_BROWSER is not None or _HAVE_PIL):
        block.update(_download_and_compress_image(abs_src))
    return _with_meta(block, img_tag)

def _table_to_block(tbl) -> dict:
    """Build a tbl block: rows (list[list[str]]), raw html, optional caption."""
    rows: list[list[str]] = []
    for tr in tbl.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    block: dict = {"type": "tbl", "rows": rows, "html": str(tbl)}
    cap = tbl.find("caption")
    if cap:
        cap_text = _clean_text(cap.get_text(" ", strip=True))
        if cap_text:
            block["caption"] = cap_text
    return _with_meta(block, tbl)

def _figure_to_blocks(fig) -> list[dict]:
    """Build fig blocks. SD wraps figure caption in <span class="captions">
    rather than <figcaption>, so we accept both."""
    out: list[dict] = []
    figure_meta = _extract_meta(fig)
    cap_node = (fig.find("figcaption")
                or fig.find("span", class_="captions")
                or fig.find(["p"], class_=lambda c: c and "caption" in (c if isinstance(c, str) else " ".join(c))))
    caption = _clean_text(cap_node.get_text(" ", strip=True)) if cap_node else ""
    for img_tag in fig.find_all("img"):
        img_block = _img_block_from_node(img_tag)
        if not img_block:
            continue
        if caption:
            fig_block: dict = {"type": "fig", "image": img_block, "caption": caption}
            if figure_meta:
                fig_block["meta"] = figure_meta
            out.append(fig_block)
            caption = ""  # attach to first image only
        else:
            out.append(img_block)
    for tbl in fig.find_all("table"):
        out.append(_table_to_block(tbl))
    if not out and caption:
        out.append({"type": "p", "text": caption})
    return out

def _element_to_blocks(elem) -> list[dict]:
    """Convert one BS4 element into one or more typed blocks (recursively
    for transparent containers)."""
    if elem is None or not hasattr(elem, "name") or elem.name is None:
        return []
    name = elem.name.lower()

    if name == "p":
        text = _clean_text(elem.get_text(" ", strip=True))
        return [_with_meta({"type": "p", "text": text}, elem)] if text else []

    if name in ("h2", "h3", "h4", "h5"):
        text = _clean_text(elem.get_text(" ", strip=True))
        if not text:
            return []
        return [_with_meta({"type": "h", "level": int(name[1]), "text": text}, elem)]

    if name in ("ul", "ol"):
        items = []
        for li in elem.find_all("li", recursive=False):
            t = _clean_text(li.get_text(" ", strip=True))
            if t:
                items.append(t)
        if not items:
            return []
        return [_with_meta(
            {"type": "list", "ordered": name == "ol", "items": items}, elem)]

    if name == "table":
        return [_table_to_block(elem)]

    if name == "figure":
        return _figure_to_blocks(elem)

    if name == "img":
        block = _img_block_from_node(elem)
        return [block] if block else []

    if name == "blockquote":
        text = _clean_text(elem.get_text(" ", strip=True))
        return [_with_meta({"type": "quote", "text": text}, elem)] if text else []

    if name == "div":
        # SD uses divs both as paragraph elements (id="pXXXX" / class
        # "u-margin-s-bottom" holding spans + topic-link anchors) and as
        # transparent wrappers around real block children. Distinguish:
        # if any direct child is block-level (p / figure / table / nested
        # div with its own structure), recurse; otherwise treat the div as
        # a single paragraph and capture all its text in one shot.
        has_block_children = any(
            getattr(c, "name", None) in _BLOCK_LEVEL_CHILDREN
            for c in elem.children
        )
        if has_block_children:
            out: list[dict] = []
            for child in elem.children:
                out.extend(_element_to_blocks(child))
            return out
        text = _clean_text(elem.get_text(" ", strip=True))
        return [_with_meta({"type": "p", "text": text}, elem)] if text else []

    if name in _TRANSPARENT_TAGS:
        out = []
        for child in elem.children:
            out.extend(_element_to_blocks(child))
        return out

    # Fallback: anything else with text → paragraph block.
    text = _clean_text(elem.get_text(" ", strip=True))
    return [{"type": "p", "text": text}] if text else []

def _blocks_to_text(blocks: list) -> str:
    """Flat plain-text representation of every block in order — kept on the
    side of the structured `blocks` so callers can still do full-text search
    without re-walking."""
    parts: list[str] = []
    for b in blocks or []:
        t = b.get("type")
        if t in ("p", "quote", "h"):
            txt = b.get("text") or ""
            if txt:
                parts.append(txt)
        elif t == "list":
            for item in (b.get("items") or []):
                if item:
                    parts.append(f"- {item}")
        elif t == "tbl":
            for row in (b.get("rows") or []):
                parts.append(" | ".join(str(c) for c in row if c is not None))
        elif t == "img":
            cap = b.get("caption") or b.get("alt") or ""
            if cap:
                parts.append(f"[Image: {cap}]")
        elif t == "fig":
            cap = b.get("caption") or (b.get("image") or {}).get("alt") or ""
            if cap:
                parts.append(f"[Figure: {cap}]")
    return _clean_text("\n\n".join(parts))

def _build_section_content(blocks: list) -> dict:
    """Collapse pure-text sections to {text:…} to save bytes; emit
    {blocks, text} only when there's structure (image/table/heading/etc.)."""
    text = _blocks_to_text(blocks)
    has_structure = any(b.get("type") != "p" for b in (blocks or []))
    if has_structure:
        return {"blocks": blocks, "text": text}
    return {"text": text}

def _normalize_heading(raw: str) -> str:
    """Map a raw heading like '2.1. Materials and Methods' → 'MATERIALS_METHODS'."""
    if not raw:
        return "OTHERS"
    t = re.sub(r"^\s*\d+(?:\.\d+)*\.?\s*", "", raw).strip().lower()
    t = re.sub(r"[.:;]+$", "", t).strip()
    if not t:
        return "OTHERS"
    for variants, canonical in _SECTION_CANONICAL_MAP:
        if t in variants:
            return canonical
    return "OTHERS"

def _section_text_minus_heading(section, heading_text: str) -> str:
    """Return section text with the leading heading stripped."""
    full = _clean_text(section.get_text(" ", strip=True))
    if heading_text and full.startswith(heading_text):
        full = full[len(heading_text):].strip()
    return full

def _section_blocks_from_children(container, skip_first_heading: bool = True) -> list[dict]:
    """Walk the direct children of a section/div container and emit typed
    blocks. The first <h2>/<h3> is treated as the section heading (skipped
    here, since it's used as the canonical key) when skip_first_heading."""
    blocks: list[dict] = []
    first_heading_seen = not skip_first_heading
    for child in container.children:
        if not hasattr(child, "name") or child.name is None:
            continue
        if not first_heading_seen and child.name in ("h2", "h3"):
            first_heading_seen = True
            continue
        # Inside the heading: capture any h3/h4 subheadings + content
        blocks.extend(_element_to_blocks(child))
    return blocks

def extract_article_sections(html: str) -> dict:
    """Parse one SD article HTML into a dict of normalized sections.

    Output shape (matches rsc's batch shape so the same downstream uploader
    can consume both):
        {
          "TITLE":              "plain string",
          "ABSTRACT":           {"text": "..."},
          "GRAPHICAL_ABSTRACT": {"src", "alt", "bytes", "data", "mime"},
          "INTRODUCTION":       {"blocks": [...], "text": "..."},      # has structure
          "RESULTS":            {"blocks": [{"type":"p"|"h"|"fig"|"tbl"|"list"|"img"|"quote", ...}], "text": "..."},
          "ACKNOWLEDGMENTS":    {"text": "..."},                       # pure-text → flat
          ...
          "OTHERS":             [{"heading": "...", "text": "..."} or {"heading":..., "blocks":..., "text":...}]
        }
    """
    if not html:
        return {}
    soup = BeautifulSoup(html, "html.parser")

    out: dict = {}
    others: list = []

    def _merge(canonical: str, raw_heading: str, content: dict) -> None:
        """Add a {blocks, text} or {text} payload under `canonical`. If the
        canonical key already exists, append blocks in document order."""
        if not content or not (content.get("blocks") or content.get("text")):
            return
        if canonical == "OTHERS":
            others.append({"heading": raw_heading, **content})
            return
        existing = out.get(canonical)
        if not existing:
            out[canonical] = content
            return
        # Merge: prefer the structured form when either side has blocks.
        merged_blocks = (existing.get("blocks") or
                         ([{"type": "p", "text": existing.get("text", "")}]
                          if existing.get("text") else [])) + \
                        (content.get("blocks") or
                         ([{"type": "p", "text": content.get("text", "")}]
                          if content.get("text") else []))
        if any(b.get("type") != "p" for b in merged_blocks):
            existing["blocks"] = merged_blocks
            existing["text"]   = _blocks_to_text(merged_blocks)
        else:
            existing["text"] = ((existing.get("text") or "") +
                                ("\n\n" if existing.get("text") else "") +
                                (content.get("text") or "")).strip()

    # ── Title ──────────────────────────────────────────────────────────────
    title_el = (soup.select_one("span.title-text")
                or soup.select_one("h1.content-title")
                or soup.select_one("h1"))
    if title_el:
        title_text = _clean_text(title_el.get_text(" ", strip=True))
        if title_text:
            out["TITLE"] = title_text

    # ── Graphical abstract (always — runs before outline walk) ───────────
    # Flat {src, alt, [bytes, data, mime]} dict matching the rsc shape.
    ga_img = (soup.select_one("img[alt^='Graphical abstract']")
              or soup.select_one("img[src*='ga1']"))
    if ga_img and "GRAPHICAL_ABSTRACT" not in out:
        img_block = _img_block_from_node(ga_img)
        if img_block:
            out["GRAPHICAL_ABSTRACT"] = {k: v for k, v in img_block.items()
                                         if k != "type"}

    # ── Outline-driven extraction ──────────────────────────────────────────
    # SD ships two sidebar TOCs we can drive from:
    #
    #   Pattern 1 — full article outline (open-access pages):
    #     <li class="toc-list-entry-outline-padding">
    #       <a class="anchor u-truncate-anchor-text anchor-primary"
    #          href="#abs0010" title="Abstract">Abstract</a>
    #
    #   Pattern 2 — preview outline (subscription / abstract-only pages):
    #     <div class="outline-area"> … <ul class="content-outline-list">
    #       <li class="content-outline-list-item-depth-1">
    #         <a class="anchor anchor-secondary preview-toc-anchor"
    #            href="#abstracts">Abstract</a>
    #
    # Both use the same shape: an <a href="#section-id"> whose visible text
    # (or `title` attribute) is the section heading. We share one helper.
    # When Pattern 1 is present it's authoritative — full body is in the
    # DOM. When only Pattern 2 is present, the body is paywalled but we
    # still extract what the preview anchors point at (abstract, intro,
    # references) and let the merge logic dedupe with the structure walk.

    def _process_outline_anchor(a) -> None:
        """Extract one section from an outline anchor.
        The anchor's href points at the section container's id; its title
        (or text) becomes the heading. Trailing "(N)" reference counts SD
        inlines into the anchor text are stripped so "References (76)"
        still normalizes to REFERENCES."""
        href = (a.get("href") or "").strip()
        target_id = href.lstrip("#")
        if not target_id:
            return
        title = (a.get("title") or _clean_text(a.get_text(" ", strip=True))).strip()
        title = re.sub(r"\s*\(\s*[\d,.\s]+\s*\)\s*$", "", title).strip()
        if not title:
            return
        target = soup.find(id=target_id)
        if not target:
            return
        tag_name = getattr(target, "name", "")
        if tag_name in ("table", "figure"):
            return

        target_cls = target.get("class") or []
        if "keywords-section" in target_cls:
            items = [_clean_text(d.get_text(" ", strip=True))
                     for d in target.select("div.keyword")]
            items = [it for it in items if it]
            blocks = ([{"type": "list", "ordered": False, "items": items}]
                      if items else [])
        else:
            blocks = _section_blocks_from_children(target, skip_first_heading=True)
            if not blocks:
                full = _clean_text(target.get_text(" ", strip=True))
                if full.startswith(title):
                    full = full[len(title):].strip()
                if full:
                    blocks = [{"type": "p", "text": full}]

        if not blocks:
            return
        canonical = _normalize_heading(title)
        _merge(canonical, title, _build_section_content(blocks))

    # Pattern 1: full article outline.
    outline_lis = soup.select("li.toc-list-entry-outline-padding")
    if outline_lis:
        # Top-level entries only. Nested LIs (abstract sub-sections like
        # Background/Objective, Case Report, Discussion, Conclusion) are
        # intentionally skipped — their content is already captured inside
        # the parent Abstract by _section_blocks_from_children, and
        # re-processing them would duplicate that text into the full-body
        # CASE_PRESENTATION / DISCUSSION / CONCLUSION buckets.
        for li in outline_lis:
            if li.find_parent("li", class_="toc-list-entry-outline-padding"):
                continue
            a = li.find(
                "a",
                class_=lambda c: c and "anchor-primary" in (
                    c if isinstance(c, str) else " ".join(c)
                ),
            ) or li.find("a")
            if a:
                _process_outline_anchor(a)

        if others:
            out["OTHERS"] = others
        return out

    # Pattern 2: preview outline (subscription / abstract-only articles).
    preview_anchors = soup.select("a.preview-toc-anchor[href^='#']")
    if preview_anchors:
        for a in preview_anchors:
            _process_outline_anchor(a)

        if others:
            out["OTHERS"] = others
        return out

    # ── Fallback: no outline on the page (older layout / Cloudflare strip) ──
    # ── Abstract block: Highlights / Abstract / Keywords / etc. ─────────────
    # Each lives as <div class="abstract …"> with its own <h2 class="section-title">.
    # Modern SD structured abstracts wrap labelled sub-sections in
    # <div id="abssec…"><h3>Case Report</h3><div id="abspara…">…</div></div>;
    # we emit those as their own canonical sections and also concatenate
    # them as the overall ABSTRACT for backwards compatibility.
    abstract_root = soup.select_one("div#abstracts") or soup
    for ab in abstract_root.select("div.abstract"):
        h = ab.find(["h2", "h3"])
        raw = _clean_text(h.get_text(" ", strip=True)) if h else ""
        if not raw:
            continue

        sub_blocks = ab.select("div[id^='abssec']")
        if sub_blocks and raw.lower() in ("abstract", "summary"):
            abstract_parts: list[str] = []
            for sb in sub_blocks:
                sh = sb.find(["h2", "h3", "h4"])
                if not sh:
                    continue
                sub_raw = _clean_text(sh.get_text(" ", strip=True))
                if not sub_raw:
                    continue
                child_blocks = _section_blocks_from_children(sb)
                content = _build_section_content(child_blocks)
                if not content.get("text"):
                    continue
                abstract_parts.append(f"{sub_raw}: {content['text']}")
                sub_canonical = _normalize_heading(sub_raw)
                if sub_canonical != "OTHERS":
                    _merge(sub_canonical, sub_raw, content)
            if abstract_parts:
                _merge("ABSTRACT", raw, {"text": "  ".join(abstract_parts)})
            continue

        # Unstructured abstract (or Highlights / Keywords / etc.) — walk
        # children into blocks so any embedded figures survive.
        blocks = _section_blocks_from_children(ab)
        content = _build_section_content(blocks)
        canonical = _normalize_heading(raw)
        _merge(canonical, raw, content)

    # ── Keywords / Abbreviations panels ────────────────────────────────────
    # SD ships these as <div class="keywords-section" id="kwrdsXXXX"> with
    # an h2 heading + a flat list of <div class="keyword"> children. They
    # live OUTSIDE div#abstracts, so the abstract loop above misses them.
    for kw_panel in soup.select("div.keywords-section"):
        h = kw_panel.find(["h2", "h3"])
        if not h:
            continue
        raw_heading = _clean_text(h.get_text(" ", strip=True))
        if not raw_heading:
            continue
        canonical = _normalize_heading(raw_heading)
        # Collect each <div class="keyword"> as a list item; fall back to
        # the panel's text if the markup ever drops the keyword wrappers.
        items = [_clean_text(d.get_text(" ", strip=True))
                 for d in kw_panel.select("div.keyword")]
        items = [it for it in items if it]
        if items:
            blocks = [{"type": "list", "ordered": False, "items": items}]
        else:
            text = _section_text_minus_heading(kw_panel, raw_heading)
            blocks = [{"type": "p", "text": text}] if text else []
        if blocks:
            _merge(canonical, raw_heading, _build_section_content(blocks))

    # ── Body sections ──────────────────────────────────────────────────────
    # Process every <section id="…"> that isn't nested inside another such
    # section. The outer wraps the heading+content (older layout nests once).
    for sec in soup.find_all("section", id=True):
        sid = (sec.get("id") or "").lower()
        if sid.startswith(("ot-", "preview-section")):
            continue
        if sec.find_parent("section", id=True):
            continue
        h = sec.find(["h2", "h3"])
        if not h:
            continue
        raw_heading = _clean_text(h.get_text(" ", strip=True))
        if not raw_heading:
            continue
        canonical = _normalize_heading(raw_heading)
        # Avoid double-counting Abstract/Keywords — already captured above
        if canonical in ("ABSTRACT", "KEYWORDS") and canonical in out:
            continue
        blocks = _section_blocks_from_children(sec)
        if not blocks:
            # Last resort: dump bare section text minus heading, so we
            # never silently drop content the block walker didn't recognize.
            full = _clean_text(sec.get_text(" ", strip=True))
            if full.startswith(raw_heading):
                full = full[len(raw_heading):].strip()
            if full:
                blocks = [{"type": "p", "text": full}]
            else:
                continue
        _merge(canonical, raw_heading, _build_section_content(blocks))

    if others:
        out["OTHERS"] = others

    return out

def extract_article_data(html: str, url: str, jid: str) -> tuple[dict, list[dict]]:
    """
    Parse one article page.

    Returns:
      article_extra  – dict with doi, abstract, funding, acknowledgement,
                       access_type, success, queue
      authors        – list of author dicts
    """
    soup = BeautifulSoup(html, "html.parser")

    # ── Access type ──────────────────────────────────────────────────────────
    if soup.find("div", id="buybox") or soup.find(
        "button", class_=lambda c: c and "buy-access" in c
    ):
        access_type = "subscription"
    elif soup.find(
        "div", class_=lambda c: c and "open-access" in (c if isinstance(c, str) else " ".join(c))
    ):
        access_type = "open_access"
    else:
        access_type = "unknown"

    # ── DOI ──────────────────────────────────────────────────────────────────
    # Modern SD preview pages:
    #   <a class="anchor doi anchor-primary" href="https://doi.org/10.1108/..." ...>
    # Older full-text pages:
    #   <a class="epub-doi" ...>doi:10.1016/...</a>
    # Last-resort: regex over the whole rendered page text.
    doi = ""
    doi_a = (soup.select_one("a.anchor.doi[href*='doi.org']")
             or soup.select_one("a.anchor.doi"))
    if doi_a:
        href = (doi_a.get("href") or "").strip()
        m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", href, re.I)
        if m:
            doi = f"https://doi.org/{m.group(0)}"
    if not doi:
        doi_tag = soup.find("a", class_="epub-doi")
        if doi_tag:
            m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
                          doi_tag.get_text(" ", strip=True), re.I)
            if m:
                doi = f"https://doi.org/{m.group(0)}"
    if not doi:
        m = re.search(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+",
                      soup.get_text(" ", strip=True), re.I)
        if m:
            doi = f"https://doi.org/{m.group(0)}"

    # ── Abstract – try multiple selector patterns ─────────────────────────
    abstract = ""
    for sel in [
        "div.article-section__content.en.main",     # open-access full text
        "div.abstract.author",                       # subscription abstract
        "#abstracts div.abstract p",                 # alternate structure
        "div[class*='abstract'] p",                  # fallback
    ]:
        el = soup.select_one(sel)
        if el:
            abstract = el.get_text(" ", strip=True)
            break

    # ── Funding ──────────────────────────────────────────────────────────────
    funding = ""
    fund_div = soup.find("div", class_="header-note-content")
    if fund_div:
        funding = fund_div.get_text(" ", strip=True).replace("Funding:", "").strip()

    # ── Acknowledgements ─────────────────────────────────────────────────────
    ack = ""
    ack_h2 = soup.find("h2", string=lambda x: x and "Acknowledgement" in x)
    if ack_h2:
        parent = ack_h2.find_parent("div", class_="article-section__content")
        if parent:
            ack = parent.get_text(" ", strip=True).replace("Acknowledgements", "").strip()

    # ── Authors ─────────────────────────────────────────────────────────────
    # Modern SD hides author info behind a side-panel dialog; the visible
    # DOM only shows names. Full data lives in window.__PRELOADED_STATE__,
    # so try that first. Fall back to the legacy DOM scrapers for older
    # layouts where the preloaded blob isn't present.
    authors = _parse_preloaded_authors(html)
    if not authors:
        authors = _parse_open_access_authors(soup)
    if not authors:
        authors = _parse_react_xocs_authors(soup)
    if not authors:
        authors = _parse_subscription_authors(soup)
    # Clean & deduplicate names (strips titles/degrees/emojis/HTML entities)
    authors = clean_authors(authors)

    # Conform author keys to the publisher-wide offline-batch shape used by
    # the rest of the pipeline (rsc et al.): orcid → orcid_id, add phone.
    normalized_authors: list[dict] = []
    for a in authors:
        normalized_authors.append({
            "author_name":  a.get("author_name", ""),
            "email":        a.get("email", ""),
            "affiliation":  a.get("affiliation", ""),
            "country":      a.get("country", ""),
            "author_type":  a.get("author_type", "Co-author"),
            "orcid_id":     a.get("orcid_id") or a.get("orcid", ""),
            "phone":        a.get("phone", ""),
        })
    authors = normalized_authors

    # ── Normalized sections (TITLE, ABSTRACT, INTRODUCTION, …, OTHERS) ───
    # Figures live inside section blocks (sections.<NAME>.blocks → fig blocks).
    # We don't emit a separate top-level images array — that would duplicate
    # what's already inside the section that owns the figure.
    sections = extract_article_sections(html)

    # ── Published date ───────────────────────────────────────────────────
    # SD ships two dates: citation_publication_date (print/issue date,
    # YYYY/MM/DD) and citation_online_date (e-pub date, YYYY/MM/DD).
    # Prefer the cover-date when present (matches the print issue), fall
    # back to the online date, then to the PRELOADED_STATE article dict.
    published_date = ""
    for meta_name in ("citation_publication_date", "citation_cover_date",
                      "citation_online_date", "dc.date"):
        meta = soup.find("meta", attrs={"name": meta_name})
        if meta and (meta.get("content") or "").strip():
            published_date = meta["content"].strip().replace("/", "-")
            break
    if not published_date:
        state = _extract_preloaded_state(html)
        if state:
            art = state.get("article") or {}
            published_date = (art.get("cover-date-start")
                              or art.get("cover-date-text")
                              or "").strip()

    # Prefer the cleaner section text: targeted SD selectors are brittle
    # (e.g. div.abstract.author can match the Highlights wrapper on some
    # pages), so trust the section extractor when it found one. Sections
    # are now {text:…} or {blocks:…, text:…} dicts — read their .text field.
    def _section_plain_text(section_value) -> str:
        if not section_value:
            return ""
        if isinstance(section_value, str):
            return section_value
        return section_value.get("text", "") if isinstance(section_value, dict) else ""

    if sections.get("ABSTRACT"):
        abstract = _section_plain_text(sections["ABSTRACT"]) or abstract
    if sections.get("ACKNOWLEDGMENTS"):
        ack = _section_plain_text(sections["ACKNOWLEDGMENTS"]) or ack
    if sections.get("FUNDING"):
        funding = _section_plain_text(sections["FUNDING"]) or funding

    extra = {
        "doi":                 doi,
        "abstract":            abstract,
        "funding":             funding,
        "funding_information": [],
        "acknowledgement":     ack,
        "access_type":         access_type,
        "sections":            sections,
        "success":             True,
        "queue":               True,
    }
    # STORE_RAW_HTML=False omits the bulky full-page HTML.
    # Structured sections (above) still carry every piece of article content —
    # you only lose the surrounding chrome.
    if STORE_RAW_HTML:
        extra["html"] = html
    if published_date:
        # Overrides the stub's published_date (often empty on the newer SD
        # issue layout — the date isn't surfaced in the TOC list).
        extra["published_date"] = published_date
    return extra, authors

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3 – PROCESS ONE JOURNAL (orchestrate 3a + 3b + 3c)
# ════════════════════════════════════════════════════════════════════════════

def process_journal(
    driver,
    journal: dict,
    state_file: str,
    skipped_file: str,
    resume_vol:  str | None = None,
    resume_iss:  str | None = None,
    resume_url:  str | None = None,
) -> object:
    """
    Full pipeline for one journal.
    Returns driver (possibly a new instance if the old one died).
    """
    jid = journal["jid"]

    # ── 3a: volume/issue map ─────────────────────────────────────────────────
    vol_map, driver = build_volume_issue_map(driver, journal, skipped_file)
    if not vol_map:
        print(f"[JOURNAL] No vol/issue map for {jid} – skipping")
        return driver

    total_issues = sum(len(v.get("issues", {})) for v in vol_map.values()
                       if isinstance(v, dict))
    print(f"[JOURNAL] {jid}: starting 3b/3c — {len(vol_map)} volumes, "
          f"{total_issues} issues, resume_vol={resume_vol} resume_iss={resume_iss}")

    batch: list[dict] = []
    update_vol = update_iss = update_url = None

    # resume_found starts True when there is no resume point
    found_resume = (resume_vol is None and resume_iss is None)

    # Iterate newest volumes first
    for vol_key in sorted(vol_map, key=lambda x: int(x) if x.isdigit() else 0, reverse=True):
        vol_data = vol_map[vol_key]
        vol      = vol_data["volume"]
        year     = vol_data.get("year", "")
        issues   = vol_data.get("issues", {})

        for iss_key in sorted(issues, key=lambda x: int(x) if x.isdigit() else 0, reverse=True):
            issue_url = issues[iss_key]

            # Resume: skip until we reach the saved volume+issue
            if not found_resume:
                if str(vol) == str(resume_vol) and str(iss_key) == str(resume_iss):
                    found_resume = True
                else:
                    continue

            # ── 3b: article links ──────────────────────────────────────────
            stubs, driver = extract_issue_articles(
                driver, issue_url, jid, skipped_file, vol, iss_key, year
            )
            if not stubs:
                update_vol, update_iss, update_url = vol, iss_key, ""
                continue

            for stub in stubs:
                art_url = stub["article_url"]
                print(f"\n[ARTICLE] {stub['article_title']} | {art_url}")
                # Article-level resume
                if resume_url:
                    if art_url != resume_url:
                        continue
                    resume_url = None

                time.sleep(random.uniform(*DELAY_ARTICLE_LOAD))
                time.sleep(30)            
                # ── 3c: article data ───────────────────────────────────────
                driver = safe_get(skipped_file, driver, art_url, jid)
                if not driver:
                    continue

                html = driver.page_source
                soup = BeautifulSoup(html, "html.parser")

                # Check article page pattern; alert but continue (don't skip)
                check_patterns(soup, "article", art_url, jid)

                # Archive HTML to E: drive
                save_article_html(html, art_url, jid)

                # Expose the active driver so image-block helpers can
                # browser-fetch figure bytes from els-cdn.com (uses the same
                # session, no Cloudflare blocks).
                global _CURRENT_BROWSER
                _CURRENT_BROWSER = driver
                try:
                    extra, authors = extract_article_data(html, art_url, jid)
                finally:
                    _CURRENT_BROWSER = None

                # ── Print article-level extracted data ─────────────────────
                print(f"  DOI:         {extra.get('doi') or '-'}")
                print(f"  Access:      {extra.get('access_type') or '-'}")
                print(f"  Abstract:    {(extra.get('abstract') or '-')[:140]}")
                print(f"  Funding:     {(extra.get('funding') or '-')[:140]}")
                print(f"  Acknowl.:    {(extra.get('acknowledgement') or '-')[:140]}")
                sec_keys = [k for k in (extra.get('sections') or {}).keys()]
                print(f"  Sections:    {sec_keys}")
                # Quick figure count from section blocks (no top-level images dup)
                fig_count = sum(
                    1
                    for sec_v in (extra.get('sections') or {}).values()
                    if isinstance(sec_v, dict)
                    for b in (sec_v.get('blocks') or [])
                    if isinstance(b, dict) and b.get('type') == 'fig'
                )
                print(f"  Figures inside sections: {fig_count}")
                print(f"  Authors ({len(authors)}):")
                for i, au in enumerate(authors, 1):
                    print(f"    [{i}] name={au.get('author_name') or '-'!r}")
                    print(f"        type={au.get('author_type') or '-'!r} | "
                          f"email={au.get('email') or '-'!r} | "
                          f"orcid={au.get('orcid') or '-'!r}")
                    print(f"        affiliation={au.get('affiliation') or '-'!r}")
                    print(f"        country={au.get('country') or '-'!r}")

                batch.append({
                    "article_link": {**stub, **extra},
                    "article_data": authors,
                })

                update_vol, update_iss, update_url = vol, iss_key, art_url

                if len(batch) >= BATCH_SIZE:
                    save_offline(DATABASE, batch)
                    save_last_state(state_file, jid, update_url, update_vol, update_iss)
                    batch.clear()

        # Flush any remaining articles after finishing this volume
        if batch:
            save_offline(DATABASE, batch)
            if update_url:
                save_last_state(state_file, jid, update_url, update_vol, update_iss)
            batch.clear()

    print(f"[JOURNAL] Done: {jid}")
    return driver

# ════════════════════════════════════════════════════════════════════════════
#  HEARTBEAT THREAD
# ════════════════════════════════════════════════════════════════════════════

def _heartbeat(process_id: str) -> None:
    while True:
        report_status(process_id, "running", "alive")
        time.sleep(300)

# ════════════════════════════════════════════════════════════════════════════
#  MAIN – Phase 3 entry point (also called by science_direct2.py)
# ════════════════════════════════════════════════════════════════════════════

_ALLOWED_JOURNAL_FIELDS = (
    "jid", "publisher", "journal_name", "journal_url",
    "archive_page_url", "issn", "pssn", "last_state", "till_date",
)

def main(part: int | str = 1) -> None:
    """
    Phase 3 entry point.
    Loads journals from the database / cache, then runs the full per-journal
    pipeline.  Resume from last saved state on restart.
    """
    process_id = f"{DATABASE}_part_{part}"

    threading.Thread(target=_heartbeat, args=(process_id,), daemon=True).start()
    report_status(process_id, "started", f"part={part}")

    sd_dir = os.path.join(_PROJECT_ROOT, "science_direct")
    os.makedirs(sd_dir, exist_ok=True)

    cache_file   = os.path.join(sd_dir, f"{DATABASE}_part_{part}.txt")
    skipped_file = os.path.join(sd_dir, f"{DATABASE}_skipped{part}.txt")
    state_file   = os.path.join(sd_dir, f"{DATABASE}_last{part}.json")
    completed_f  = os.path.join(sd_dir, f"completed_{DATABASE}.txt")

    driver = create_driver(HEADLESS)
    try:
        # ── Load journals ────────────────────────────────────────────────────
        if os.path.exists(cache_file):
            journals = load_journals_from_cache(cache_file)
        else:
            journals = fetch_and_cache_journals(cache_file, DATABASE)

        if not journals:
            print(f"[MAIN] No journals for part {part} – run collect + upload first.")
            return

        # Sanitize (strip _id / extra DB fields) and deduplicate by journal_url
        clean: dict[str, dict] = {}
        for r in journals:
            if not isinstance(r, dict):
                continue
            rec = {k: r.get(k, "") for k in _ALLOWED_JOURNAL_FIELDS}
            key = rec.get("journal_url") or rec.get("jid", "")
            if key:
                clean[key] = rec
        journals = list(clean.values())
        print(f"[MAIN] {len(journals)} journals for part {part}")

        # ── Resume state ─────────────────────────────────────────────────────
        last = load_last_state(state_file) or {}
        resume_jid = last.get("journal_id")
        resume_vol = last.get("last_volume")
        resume_iss = last.get("last_issue")
        resume_url = last.get("last_url")

        # Completed journals (skip entirely)
        completed: set[str] = set()
        if os.path.exists(completed_f):
            with open(completed_f) as fh:
                completed = {line.strip() for line in fh if line.strip()}

        # ── Process each journal ─────────────────────────────────────────────
        for journal in journals:
            jid = journal.get("jid", "")

            if jid in completed:
                print(f"[SKIP] Completed: {jid}")
                continue
            if resume_jid and jid != resume_jid:
                continue   # fast-forward to the resume point

            print(f"\n[JOURNAL] Starting: {jid} – {journal.get('journal_name', '')}")

            driver = process_journal(
                driver, journal, state_file, skipped_file,
                resume_vol=resume_vol,
                resume_iss=resume_iss,
                resume_url=resume_url,
            )
            if driver is None:
                driver = create_driver(HEADLESS)

            # Clear resume pointers after the first journal is processed
            resume_jid = resume_vol = resume_iss = resume_url = None

        report_status(process_id, "completed", f"part={part} done")
        notify_info(process_id, f"Part {part} completed successfully.")

    except Exception as e:
        report_status(process_id, "crashed", str(e))
        notify_crash(process_id, str(e))
        raise
    finally:
        _safe_quit(driver)

# ════════════════════════════════════════════════════════════════════════════
#  PHASE 3a-ONLY – BUILD VOLUME/ISSUE MAPS FOR EVERY JOURNAL
# ════════════════════════════════════════════════════════════════════════════

def build_maps(part: int | str = 1, force: bool = False) -> None:
    """
    Run only Phase 3a: scrape the /issues archive for every journal in this
    part and save volume_issue_map.json under
        C:\\science_direct\\<jid>\\volume_issue_map.json

    No issue TOCs, no article pages, no DB writes – just the maps.

    Set force=True to ignore the per-journal cache and rebuild every map.
    """
    process_id = f"{DATABASE}_maps_part_{part}"
    threading.Thread(target=_heartbeat, args=(process_id,), daemon=True).start()
    report_status(process_id, "started", f"part={part} force={force}")

    sd_dir = os.path.join(_PROJECT_ROOT, "science_direct")
    os.makedirs(sd_dir, exist_ok=True)

    cache_file   = os.path.join(sd_dir, f"{DATABASE}_part_{part}.txt")
    skipped_file = os.path.join(sd_dir, f"{DATABASE}_maps_skipped{part}.txt")

    driver = create_driver(HEADLESS)
    try:
        if os.path.exists(cache_file):
            journals = load_journals_from_cache(cache_file)
        else:
            journals = fetch_and_cache_journals(cache_file, DATABASE)

        if not journals:
            print(f"[MAPS] No journals for part {part} – run collect + upload first.")
            return

        clean: dict[str, dict] = {}
        for r in journals:
            if not isinstance(r, dict):
                continue
            rec = {k: r.get(k, "") for k in _ALLOWED_JOURNAL_FIELDS}
            key = rec.get("journal_url") or rec.get("jid", "")
            if key:
                clean[key] = rec
        journals = list(clean.values())
        total = len(journals)
        print(f"[MAPS] {total} journals for part {part} (force={force})")

        built = cached_skip = empty = failed = 0
        for i, journal in enumerate(journals, start=1):
            jid = journal.get("jid", "")
            name = journal.get("journal_name", "")
            if not jid:
                continue

            if not force:
                existing = read_backup_json(jid, DATABASE)
                if existing and isinstance(existing, dict) and existing:
                    cached_skip += 1
                    print(f"[MAPS] [{i}/{total}] cached, skipping: {jid} {name}")
                    continue

            print(f"\n[MAPS] [{i}/{total}] Building map for {jid} – {name}")
            try:
                vol_map, driver = build_volume_issue_map(driver, journal, skipped_file)
                if driver is None:
                    driver = create_driver(HEADLESS)
                if vol_map:
                    issues = sum(len(v.get("issues", {})) for v in vol_map.values())
                    print(f"[MAPS] [{i}/{total}] {jid}: {len(vol_map)} volumes, {issues} issues")
                    built += 1
                else:
                    print(f"[MAPS] [{i}/{total}] {jid}: empty map")
                    empty += 1
            except Exception as e:
                failed += 1
                print(f"[MAPS] [{i}/{total}] {jid} FAILED: {e}")

        summary = (
            f"part={part} total={total} built={built} cached_skip={cached_skip} "
            f"empty={empty} failed={failed}"
        )
        report_status(process_id, "completed", summary)
        notify_info(process_id, f"Maps {summary}")
        print(f"\n[MAPS] DONE – {summary}")

    except Exception as e:
        report_status(process_id, "crashed", str(e))
        notify_crash(process_id, str(e))
        raise
    finally:
        _safe_quit(driver)

# ════════════════════════════════════════════════════════════════════════════
#  COMMAND-LINE INTERFACE
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="ScienceDirect scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  collect            Phase 1 – scrape all journal ISSNs, save to local JSONL
  upload             Phase 2 – push local JSONL to database API
  split <N>          Split local JSONL into N part files (default N=12)
  maps  <part>       Phase 3a only – build/save volume_issue_map.json per journal
  mine  <part>       Phase 3 – mine article data  (default part=1)
  all   <part>       Phases 1 + 2 + 3
Flags:
  --run <part>       Shortcut for `mine <part>` (e.g. --run 1)
  --force            (maps only) ignore cached maps and rebuild them
        """,
    )
    parser.add_argument(
        "command", nargs="?", default="mine",
        choices=["collect", "upload", "split", "maps", "mine", "all"],
    )
    parser.add_argument("part", nargs="?", default="1",
                        help="Part number (maps/mine/all) or N for split (default 12)")
    parser.add_argument("--run", metavar="PART", default=None,
                        help="Shortcut for `mine <part>` — overrides positional command")
    parser.add_argument("--force", action="store_true",
                        help="Rebuild volume_issue_maps even if cached (maps only)")
    args = parser.parse_args()

    if args.run is not None:
        main(args.run)
    elif args.command == "collect":
        collect_journals()
    elif args.command == "upload":
        post_journals_to_db()
    elif args.command == "split":
        n = int(args.part) if args.part and str(args.part).isdigit() else 12
        split_journals(n)
    elif args.command == "maps":
        build_maps(args.part, force=args.force)
    elif args.command == "mine":
        main(args.part)
    elif args.command == "all":
        collect_journals()
        post_journals_to_db()
        main(args.part)

#  python science_direct.py --run 6  
