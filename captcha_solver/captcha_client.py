# captcha_client.py
import os
import json
import time
import uuid
from datetime import datetime

BASE = "C:/common_code/captcha_queue"
QUEUE_DIR = os.path.join(BASE, "queue")
os.makedirs(QUEUE_DIR, exist_ok=True)

def enqueue_captcha_system(
    journal_id,
    window_title,        # ✅ REQUIRED
    click_x=300,
    click_y=400,
    side_border=0,       # legacy — viewport-relative path
    chrome_top=0,        # legacy — viewport-relative path
    screen_x=None,       # NEW — preferred. Bullseye screen X (CSS px).
    screen_y=None,       # NEW — preferred. Bullseye screen Y (CSS px).
    dpr=1.0,             # NEW — window.devicePixelRatio
    timeout=900          # optional safety timeout
):
    """Queue a captcha-click job.

    Two paths supported (worker prefers the SCREEN path when present):

      SCREEN path (preferred — matches what test_static_click.py uses):
          ``screen_x`` / ``screen_y`` are CSS-pixel SCREEN coords of the
          bullseye, computed by the browser via getBoundingClientRect +
          window.screenX/Y + chrome offsets. ``dpr`` is
          window.devicePixelRatio. The worker multiplies by ``dpr`` only
          when the OS process is DPI-aware (modern pyautogui makes the
          process DPI-aware on import, so on a 125% display CSS coords
          need ×1.25 to hit the right physical pixel).

      VIEWPORT path (legacy fallback — kept for old producers):
          ``click_x`` / ``click_y`` are viewport coords; the worker
          computes screen = win.left + side_border + click_x etc.
    """
    ticket = f"{int(time.time()*1000)}_{uuid.uuid4().hex}.json"
    path = os.path.join(QUEUE_DIR, ticket)

    os.makedirs(QUEUE_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "journal_id": journal_id,
            "window_title": window_title,
            "click_x": click_x,
            "click_y": click_y,
            "side_border": side_border,
            "chrome_top": chrome_top,
            "screen_x": screen_x,
            "screen_y": screen_y,
            "dpr": dpr,
            "time": time.time()
        }, f, indent=0)
        f.flush()
        os.fsync(f.fileno())

    print(f"[{datetime.now():%H:%M:%S}] CAPTCHA queued -> {journal_id}")
    print(f"[{datetime.now():%H:%M:%S}] Window title -> {window_title}")

    # ⛔ BLOCK this browser until worker releases it
    start = time.time()
    while os.path.exists(path):
        if time.time() - start > timeout:
            print(f"[{datetime.now():%H:%M:%S}] CAPTCHA timeout -> {journal_id}")
            break
        time.sleep(1)

    print(f"[{datetime.now():%H:%M:%S}] CAPTCHA solved -> {journal_id}")
