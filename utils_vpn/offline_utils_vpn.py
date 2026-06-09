"""
offline_utils_vpn: STANDALONE. Copy this single file to another system; everything (VPN state + VPN change + safe_get) is inside.

On the other system, put in ONE folder:
  1. This file (offline_utils_vpn.py)
  2. countries.txt (one country per line; used for NordVPN rotation)
  3. vpn_data_driver.py (for create_driver; if missing, create_driver will raise)

No vpn_state.py or vpn_provider.py needed. State dir = same folder as this file/vpn_state/.
- use_vpn_rotator=True (default): run vpn_rotator.py (import set_vpn_rotating, clear_vpn_rotating, change_vpn_on_block from this file).
- use_vpn_rotator=False: only change VPN when IP block (no rotator).
"""
import requests
import json
import os
import sys
import time
import subprocess
import random
from collections import deque


class _HardBlockRetry(Exception):
    """Internal signal raised when a callsite detects an SD-style hard-block
    (Imperva "problem providing the content you requested" page with NO
    captcha widget). The outer except clause in safe_get catches this
    specifically — closes the blocked driver, sleeps, spins up a FRESH
    driver (new random UA from user_agents.txt), and `continue`s the outer
    loop without consuming an attempt. Distinct from TimeoutException so
    there's no risk of being misclassified by string-matching `msg`."""
    pass


# Consecutive captcha-click failures ─────────────────────────────────────────
# When 3 captcha clicks in a row leave the page still blocked (Cloudflare did
# NOT accept the click), pause for 120 s before the next attempt. Gives the
# site / IP fingerprint a chance to cool down so we're not hammering a hostile
# captcha back-to-back. Counter is module-level (per-process); resets on any
# success path (no captcha needed OR captcha cleared).
_CAPTCHA_CONSECUTIVE_FAILURES = 0
_CAPTCHA_FAILURE_THRESHOLD = 3
_CAPTCHA_FAILURE_COOLDOWN_SEC = 120

# Folder containing this file (for standalone: state dir and countries.txt live here)
_OFFLINE_UTILS_VPN_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------- VPN state (all in this file) ----------
def _vpn_state_dir():
    d = os.path.join(_OFFLINE_UTILS_VPN_DIR, "vpn_state")
    os.makedirs(d, exist_ok=True)
    return d

def _vpn_flag_path(name):
    return os.path.join(_vpn_state_dir(), name)

IP_BLOCKED_FLAG = "ip_blocked.flag"
VPN_ROTATING_FLAG = "vpn_rotating.flag"

def wait_if_vpn_rotating(poll_sec=2):
    path = _vpn_flag_path(VPN_ROTATING_FLAG)
    while os.path.exists(path):
        time.sleep(poll_sec)

def set_vpn_rotating():
    with open(_vpn_flag_path(VPN_ROTATING_FLAG), "w") as f:
        pass

def clear_vpn_rotating():
    try:
        p = _vpn_flag_path(VPN_ROTATING_FLAG)
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        pass

# ---------- VPN change (NordVPN, inlined so no vpn_provider needed) ----------
class _VPNManager:
    LAST_N_AVOID = 10
    def __init__(self, wait_after_connect=3, countries_file=None):
        self.wait = wait_after_connect
        self.exe_path = r"C:\Program Files\NordVPN\NordVPN.exe"
        self.countries_file = countries_file or os.path.join(_OFFLINE_UTILS_VPN_DIR, "countries.txt")
        self.country_index = 0
        # Path to persist last N connections so we avoid them even after restart
        self._history_path = os.path.join(_OFFLINE_UTILS_VPN_DIR, "vpn_last_connections.json")
        self.countries = self._load_countries()
        # Last N countries we connected to, initialised from persisted history
        self._last_connections = self._load_history()
    def _load_countries(self):
        if not os.path.exists(self.countries_file):
            raise FileNotFoundError(f"countries.txt not found: {self.countries_file}")
        countries = []
        with open(self.countries_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip().split("#")[0].strip() if "#" in line else line.strip()
                if line:
                    countries.append(line)
        if not countries:
            raise ValueError("countries.txt is empty")
        # Shuffle at load time so we never cycle through alphabetic/geographic clusters
        random.shuffle(countries)
        return countries

    def _load_history(self):
        """
        Load last N connections from JSON file, keep only valid countries,
        and return as deque(maxlen=LAST_N_AVOID).
        """
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path, "r", encoding="utf-8") as f:
                    data = json.load(f) or []
                filtered = [c for c in data if c in self.countries]
                return deque(filtered[-self.LAST_N_AVOID :], maxlen=self.LAST_N_AVOID)
        except Exception:
            pass
        return deque(maxlen=self.LAST_N_AVOID)

    def _save_history(self):
        """Persist last N connections to JSON so next run also avoids them."""
        try:
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(list(self._last_connections), f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    def _run_cmd(self, cmd):
        try:
            return subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True).decode()
        except subprocess.CalledProcessError as e:
            return f"[ERROR] {e.output.decode()}"
    def _connect(self, country=None):
        cmd = f'"{self.exe_path}" -c -g "{country}"' if country else f'"{self.exe_path}" -c'
        print(f"[VPN] Connecting to: {country or 'Best Server'}")
        result = self._run_cmd(cmd)
        time.sleep(self.wait)
        return result
    def _disconnect(self):
        print("[VPN] Disconnecting...")
        return self._run_cmd(f'"{self.exe_path}" -d')
    def _change_country(self, country):
        print(f"[VPN] Changing country -> {country}")
        self._disconnect()
        time.sleep(2)
        res = self._connect(country)
        if res and res.strip().startswith("[ERROR]"):
            print(res.strip()[:300])
        return res
    def change_vpn(self):
        if not self.countries:
            return "[ERROR] No countries loaded."
        last_n_set = set(self._last_connections)
        candidates = [c for c in self.countries if c not in last_n_set]
        if not candidates:
            candidates = list(self.countries)
            print(f"[VPN] All countries were used in last {self.LAST_N_AVOID}; picking from full list.")
        # Shuffle candidates so we never stay in the same geographic cluster
        random.shuffle(candidates)
        chosen = candidates[0]
        print(f"[VPN ROTATE] Switching to: {chosen.upper()} (avoiding last {self.LAST_N_AVOID})")
        self._last_connections.append(chosen)
        self._save_history()
        result = self._change_country(chosen)
        # Verify NordVPN actually connected — retry up to 3 times if disconnected
        for verify_attempt in range(3):
            time.sleep(3)
            status = self._run_cmd(f'"{self.exe_path}" -s').lower()
            if "connected" in status:
                print(f"[VPN] Connected confirmed: {chosen}")
                break
            print(f"[VPN] Not connected yet (attempt {verify_attempt+1}/3), retrying connect...")
            self._connect(chosen)
        else:
            print(f"[VPN] WARNING: Could not confirm connection to {chosen} after 3 attempts.")
        return result

def change_vpn_on_block(countries_file=None):
    countries_file = countries_file or os.path.join(_OFFLINE_UTILS_VPN_DIR, "countries.txt")
    print("[VPN] Calling change_vpn_once...")
    try:
        vpn = _VPNManager(wait_after_connect=5, countries_file=countries_file)
        vpn.change_vpn()
        print("[VPN] change_vpn_once completed.")
    except Exception as e:
        print(f"[VPN] change_vpn_once failed: {e}")
        raise RuntimeError(f"VPN change failed: {e}") from e

def on_ip_block(rotate_fn=None):
    path = _vpn_flag_path(IP_BLOCKED_FLAG)
    STALE_AFTER_SEC = 300   # flag older than 5 min = left by a crashed process

    took_ownership = False
    try:
        with open(path, "x") as f:
            pass
        took_ownership = True
    except FileExistsError:
        # Another process may be rotating — check if flag is stale
        try:
            age = time.time() - os.path.getmtime(path)
        except Exception:
            age = 0

        if age > STALE_AFTER_SEC:
            print(f"[VPN] Stale ip_blocked flag ({age:.0f}s old) — removing and taking over VPN change.")
            try:
                os.remove(path)
            except Exception:
                pass
            # Re-create as owner
            try:
                with open(path, "x") as f:
                    pass
                took_ownership = True
            except FileExistsError:
                pass  # race — another process beat us, fall through to wait

        if not took_ownership:
            # Wait for the owning process to finish — but with a hard timeout
            waited = 0
            while os.path.exists(path) and waited < STALE_AFTER_SEC:
                time.sleep(2)
                waited += 2
            # If still there, force-remove so we don't hang forever
            if os.path.exists(path):
                print("[VPN] Flag still present after timeout — force-removing stale flag.")
                try:
                    os.remove(path)
                except Exception:
                    pass
            return

    try:
        if rotate_fn:
            for attempt in range(1, 4):
                try:
                    rotate_fn()
                    break
                except Exception as e:
                    print(f"[VPN] on_ip_block rotate attempt {attempt}/3 failed: {e}")
                    if attempt < 3:
                        time.sleep(5)
                    else:
                        print("[VPN] All 3 rotate attempts failed; clearing flag.")
        else:
            time.sleep(120)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

# ---------- End VPN state / VPN change ----------

try:
    from . import vpn_data_driver as connect_driver
except Exception:
    try:
        import vpn_data_driver as connect_driver
    except Exception:
        connect_driver = None
from bs4 import BeautifulSoup
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait,Select
from selenium.webdriver.support import expected_conditions as ec
from selenium.common.exceptions import TimeoutException
import re
from selenium.common.exceptions import TimeoutException
from urllib.parse import urljoin
import urllib.parse
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import uuid
from datetime import datetime
API_ROOT = "http://139.84.134.18:8002"

def create_driver(head=False):
    if connect_driver is None:
        raise RuntimeError("vpn_data_driver not found; put it in same folder as this file or on PYTHONPATH")
    try:
        from driver_state import register_driver
    except Exception:
        register_driver = lambda d: None
    driver = connect_driver.connect_driver(head)
    if driver is not None:
        # Prevent indefinite hangs on driver.get()
        try:
            # Keep these fairly low so a stuck navigation doesn't stall the whole run.
            driver.set_page_load_timeout(30)
            driver.set_script_timeout(30)
        except Exception:
            pass
        # Maximize — belt-and-suspenders backup to --start-maximized.
        # Keeps the title bar visible so the captcha worker can find the
        # window via document.title = "CAPTCHA_BROWSER_<jid>".
        try:
            driver.maximize_window()
        except Exception:
            pass
        register_driver(driver)
    return driver

def save_offline(database, data):
    """
    Safe offline writer:
    - Unique filename (no collision)
    - Atomic write (temp → rename)
    - Always valid JSON array
    """
    folder = f"C:/{database}_offline_uploads/offline_uploads"
    os.makedirs(folder, exist_ok=True)

    # 🔹 Unique filename parts
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    unique_id = uuid.uuid4().hex[:8]

    final_path = os.path.join(folder, f"batch_{timestamp}_{unique_id}.json")
    temp_path = final_path + ".tmp"

    try:
        # 1️⃣ Write to TEMP file
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())  # ensure disk write

        # 2️⃣ Atomic rename (Windows-safe)
        os.replace(temp_path, final_path)

        print(f"💾 Saved offline safely → {final_path}")
        return True

    except Exception as e:
        # Cleanup temp if anything failed
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except:
            pass

        print(f"❌ Failed to save offline: {e}")
        return False

def save_skipped(SKIPPED_FILE,journal_id, url, reason="Timeout"):
    with open(SKIPPED_FILE, "a") as f:
        f.write(json.dumps({"jid": journal_id, "url": url, "reason": reason}) + "\n")

def save_last_state(STATE_FILE,journal_id, url, volume=None,issue=None):
    state = {}
    final_path = os.path.abspath(STATE_FILE)
    if os.path.exists(final_path):
        with open(final_path, "r", encoding="utf-8") as f:
            try:
                state = json.load(f)
            except json.JSONDecodeError:
                state = {}

    # Update mandatory fields
    state["journal_id"] = journal_id
    state["last_url"] = url

    # Conditionally update volume and issue
    if volume is not None and volume != "":
        state["last_volume"] = volume
    elif "last_volume" not in state:
        state["last_volume"] = None  # or omit if you prefer

    if issue is not None and issue != "":
        state["last_issue"] = issue
    elif "last_issue" not in state:
        state["last_issue"] = None  # or omit if you prefer

    # Atomic write: same-dir .tmp then os.replace. Avoid NamedTemporaryFile on
    # Windows (random names + single replace -> WinError 5 when AV/indexer
    # briefly locks the destination).
    dir_name = os.path.dirname(final_path) or "."
    os.makedirs(dir_name, exist_ok=True)
    temp_path = final_path + ".tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
        f.flush()
        os.fsync(f.fileno())

    for attempt in range(15):
        try:
            os.replace(temp_path, final_path)
            return
        except (PermissionError, OSError):
            time.sleep(0.15 * (attempt + 1))

    # Last resort: non-atomic overwrite (some Windows setups block replace)
    try:
        with open(final_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except OSError:
            pass

def save_backup_json(database, journal_id, data, filename, base_dir=None):
    """
    Save JSON backup. If base_dir is set, path = base_dir/journal_id/filename (same folder as script).
    Else path = C:\\database\\journal_id\\filename.
    """
    try:
        if base_dir:
            target_dir = os.path.join(base_dir, str(journal_id))
        else:
            target_dir = os.path.join("C:\\", str(database), str(journal_id))
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, filename)
            # NOTE: window title is NO LONGER stamped here. Renaming the
            # tab to CAPTCHA_BROWSER_<jid> on every page load polluted
            # the title bar even when no captcha appeared. The title is
            # now set only inside the captcha-handling block below —
            # i.e., right before we enqueue the click — so the worker
            # can find the window EXACTLY when it needs to and not a
            # moment earlier.
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # Keep output ASCII-only (Windows consoles often use cp1252).
        print(f"Backup saved -> {target_path}")
    except Exception as e:
        print(f"Backup skipped: {e}")

def read_backup_json(journal_id,database,filename="volume_issue_map.json", base_dir=None):
    """
    Read JSON backup. If base_dir is set, path = base_dir/journal_id/filename. Else C:\\database\\journal_id\\filename.
    """

    try:
        if base_dir:
            target_path = os.path.join(base_dir, str(journal_id), filename)
        else:
            target_path = os.path.join("C:\\", str(database), str(journal_id), filename)

        if not os.path.exists(target_path):
            print(f"[ERR] File not found: {target_path}")
            return None

        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        print(f"[OK] Loaded backup JSON -> {target_path}")
        return data

    except Exception as e:
        print(f"[ERR] Error reading JSON: {e}")
        return None
   
def load_last_state(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        # Corrupted last_state can happen if written during a crash.
        # Treat as "no state" and resume from scratch.
        return None
    
def is_driver_alive(driver):
    try:
        _ = driver.session_id
        return True
    except Exception:
        return False


def _driver_responds(driver, timeout_sec=10):
    """True if driver executes JS quickly; False if hung (window open but not responding)."""
    if driver is None:
        return False
    import threading

    ok = [False]

    def ping():
        try:
            driver.execute_script("return 1")
            ok[0] = True
        except Exception:
            pass

    t = threading.Thread(target=ping, daemon=True)
    t.start()
    t.join(timeout_sec)
    return ok[0]


def force_close_driver(driver, timeout_sec=10):
    """
    Ensure Chrome + chromedriver are fully terminated; no zombie processes.
    - Tries driver.quit() in a thread with timeout (quit() can hang).
    - Then kills EVERY chromedriver/chrome under this Python process (Windows: taskkill /T on each driver).
    """
    try:
        from driver_state import kill_all_selenium_browsers_under_owner
    except Exception:
        kill_all_selenium_browsers_under_owner = None

    if driver is None:
        if kill_all_selenium_browsers_under_owner:
            kill_all_selenium_browsers_under_owner()
        return

    import threading

    def do_quit():
        try:
            driver.quit()
        except Exception:
            pass

    t = threading.Thread(target=do_quit, daemon=True)
    t.start()
    t.join(timeout=timeout_sec)

    if kill_all_selenium_browsers_under_owner:
        kill_all_selenium_browsers_under_owner()


try:
    # Current repo layout
    from captcha_solver.captcha_client import enqueue_captcha_system
except Exception:
    # Backward-compatible import
    from captcha_solver.captcha_client import enqueue_captcha_system

def safe_get(
    SKIPPED_FILE,
    driver,
    url,
    journal_id,
    retries=3,
    wait_time=180,
    head=False,
    *,
    # use_vpn_rotator=True: wait for vpn_rotator every N min. use_vpn_rotator=False: change VPN only on IP block (no rotator).
    # Override via env: set VPN_CHANGE_ON_BLOCK_ONLY=1 or USE_VPN_ROTATOR=0 to use "change only on IP block".
    use_vpn_rotator=None,
    # Customization hooks (backward compatible for existing callers)
    challenge_predicate=None,     # function(str)->bool, for site-specific blocks (e.g. ScienceDirect)
    # function(str)->bool — when True, the page is an UNCLICKABLE hard-block
    # (e.g. Imperva "problem providing content" page). Instead of enqueuing a
    # captcha solve, the base loop force-closes the driver, spins up a new one
    # (which gets a fresh random UA from user_agents.txt) and re-navigates to
    # the URL — typically the new session is served a normal Turnstile, which
    # the regular captcha flow can solve.
    hardblock_predicate=None,
    captcha_click_x=470,
    captcha_click_y=520,
    # If True: do not kill/recreate driver just because JS ping is slow (still recreate if session dead).
    relax_driver_health: bool = False,
):
    """
    One driver per process. On IP block: one process changes VPN, others wait (multi-driver safe).
    use_vpn_rotator=True: pause during vpn_rotator.py (run it in another terminal).
    use_vpn_rotator=False: change VPN only when IP block detected (do not run vpn_rotator).
    """
    if use_vpn_rotator is None:
        use_vpn_rotator = not (os.environ.get("VPN_CHANGE_ON_BLOCK_ONLY") or
                               str(os.environ.get("USE_VPN_ROTATOR", "")).lower() in ("0", "false", "no"))
    from driver_state import kill_orphan_drivers
    kill_orphan_drivers()  # clean zombies before first attempt
    attempt = 1
    ip_block_attempts = 0
    MAX_IP_BLOCK_RETRIES = 3      # stop retrying after 3 consecutive IP blocks
    hardblock_attempts = 0
    MAX_HARDBLOCK_RETRIES = 5     # close+UA-rotate up to 5 times before giving up

    while attempt <= retries:
        try:
            if use_vpn_rotator:
                wait_if_vpn_rotating()
            # 🛡️ One driver per process: ensure driver is usable (session alive + JS responds)
            if not driver or not is_driver_alive(driver):
                force_close_driver(driver)
                driver = create_driver(head)
            elif not relax_driver_health and not _driver_responds(driver):
                print("[driver] Hung (no JS response) -> closing all browser processes under miner")
                force_close_driver(driver)
                driver = create_driver(head)

            # ⏱️ Allow slow internet + redirects
            driver.set_page_load_timeout(wait_time + 60)
            driver.get(url)


            # ⏳ Wait for basic page structure
            WebDriverWait(driver, wait_time).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )

            src = driver.page_source.lower()

            # 🚫 IP BLOCK → VPN change code inside utils_vpn; one process rotates, all others wait (multi-driver safe)
            is_ip_block = (
                "ip has been blocked" in src
                or "ip blocked" in src
                or "error 1015" in src
                or "you are being rate limited" in src
                or "banned you temporarily from accessing this website" in src
            )
            if is_ip_block:
                ip_block_attempts += 1
                print(f"[VPN] IP blocked (attempt {ip_block_attempts}/{MAX_IP_BLOCK_RETRIES}) -> changing VPN...")
                if ip_block_attempts > MAX_IP_BLOCK_RETRIES:
                    print("[VPN] Max IP block retries reached — saving as skipped.")
                    force_close_driver(driver)
                    save_skipped(SKIPPED_FILE, journal_id, url, "IP blocked - max retries")
                    return None
                force_close_driver(driver)
                driver = None
                on_ip_block(change_vpn_on_block)
                # Give VPN + network time to fully settle
                print("[VPN] Waiting 20s for VPN/network to settle...")
                time.sleep(20)
                # Restart driver (retry a few times in case Chrome/network not ready)
                for _ in range(3):
                    driver = create_driver(head)
                    if driver and is_driver_alive(driver):
                        print("[VPN] Driver restarted after VPN change.")
                        break
                    force_close_driver(driver)
                    time.sleep(5)
                if not driver or not is_driver_alive(driver):
                    print("[VPN] WARN: Could not restart driver after VPN change; will retry at top of loop.")
                continue
            
            captcha_handled = False
            # start = time.time()         
            # ☁️ Cloudflare / JS challenge (SOFT wait)
            start = time.time()
            while time.time() - start < wait_time:
                src = driver.page_source.lower()
                is_generic = (
                    "just a moment" in src
                    or "verify you are human" in src
                    or "checking your browser" in src
                )
                is_custom = False
                if challenge_predicate:
                    try:
                        is_custom = bool(challenge_predicate(src))
                    except Exception:
                        is_custom = False

                if is_generic or is_custom:
                    # 🛑 Hard-block (Imperva / Akamai "problem providing
                    # content" page) — there's no captcha to solve on this
                    # page. The only recovery is to close the driver, reopen
                    # with a fresh user agent, and re-navigate. The new
                    # session is usually served a normal Turnstile, which the
                    # regular captcha flow then solves on the next attempt.
                    is_hardblock = False
                    if hardblock_predicate:
                        try:
                            is_hardblock = bool(hardblock_predicate(src))
                        except Exception:
                            is_hardblock = False
                    if is_hardblock:
                        hardblock_attempts += 1
                        print(f"[SD] new driver  "
                              f"(hard-block attempt {hardblock_attempts}/{MAX_HARDBLOCK_RETRIES}) "
                              f"→ closing & reopening with fresh UA  |  {url[:80]}")
                        if hardblock_attempts > MAX_HARDBLOCK_RETRIES:
                            print(f"[SD] new driver  giving up after "
                                  f"{MAX_HARDBLOCK_RETRIES} hard-block recoveries — skipping {url[:80]}")
                            force_close_driver(driver)
                            save_skipped(SKIPPED_FILE, journal_id, url,
                                         "Hardblock - max retries")
                            return None
                        # Raise our sentinel so the outer except can recognise
                        # this case unambiguously — close THIS driver, sleep,
                        # create a fresh one (new random UA), and `continue`
                        # the outer loop, re-navigating to `url` from the top.
                        raise _HardBlockRetry()

                    if not captcha_handled:
                        captcha_handled = True

                        # ⏳ Give Cloudflare a moment to settle iframe
                        time.sleep(5)

                        # 1) Assign UNIQUE window title so worker can find this browser
                        window_title = f"CAPTCHA_BROWSER_{journal_id}"
                        try:
                            driver.execute_script(
                                f"document.title = '{window_title}'"
                            )
                            time.sleep(2)  # let browser update title bar before worker looks
                        except Exception as e:
                            print(f"Failed to set window title: {e}")
                            window_title = None

                        # 1.4) WAIT FOR PAGE LOAD before we touch anything.
                        # On a slow connection the challenge page is still
                        # downloading scripts/resources when we get here —
                        # if we measure now, the captcha iframe may not be
                        # mounted yet and the click lands on empty space.
                        # We poll document.readyState; if it doesn't reach
                        # "complete" within READYSTATE_TIMEOUT_SEC, we
                        # refresh and try once more.
                        READYSTATE_TIMEOUT_SEC = 25
                        READYSTATE_POLL_SEC    = 0.5

                        def _ready_now():
                            try:
                                return driver.execute_script(
                                    "return document.readyState;"
                                ) == "complete"
                            except Exception:
                                return False

                        def _wait_ready(label):
                            """Poll readyState. Returns True if complete
                            within READYSTATE_TIMEOUT_SEC."""
                            _t0 = time.time()
                            while time.time() - _t0 < READYSTATE_TIMEOUT_SEC:
                                if _ready_now():
                                    _waited = time.time() - _t0
                                    print(f"[captcha] page READY ({label}) "
                                          f"after {_waited:.1f}s")
                                    return True
                                time.sleep(READYSTATE_POLL_SEC)
                            return False

                        if not _wait_ready("initial"):
                            # Page still loading after timeout — bad
                            # connection or stalled fetch. Refresh once
                            # and try the wait again. If THAT also
                            # times out we just press on with whatever
                            # state the page is in.
                            print(f"[captcha] page NOT ready after "
                                  f"{READYSTATE_TIMEOUT_SEC}s — "
                                  f"refreshing once and waiting again")
                            try:
                                driver.refresh()
                            except Exception as _re:
                                print(f"[captcha] refresh raised: {_re}")
                            if not _wait_ready("after-refresh"):
                                print(f"[captcha] STILL not ready after "
                                      f"refresh — proceeding anyway")

                        # 1.45) BRING THE BROWSER TO FRONT before we
                        # measure / click. If Chrome is hidden behind
                        # another window (VS Code, terminal, etc.) the
                        # window.screenX/Y math is still numerically
                        # correct, but pyautogui's OS-level click lands
                        # on whatever app is actually in front. By
                        # bringing the captcha Chrome to the foreground
                        # NOW — before _measure_loc() — we make sure:
                        #   • the bullseye sits on a window you can see
                        #   • the worker's second bring-to-front (just
                        #     before the click) usually becomes a no-op
                        # The whole block is wrapped in try / except so
                        # missing pygetwindow / win32gui can't crash
                        # safe_get; we just log and fall through.
                        try:
                            import pygetwindow as _gw
                            import win32gui as _w32g
                            import win32con as _w32c
                            import ctypes as _ctypes

                            # Find Chrome by the unique CAPTCHA_BROWSER_<jid> title.
                            _target = window_title or f"CAPTCHA_BROWSER_{journal_id}"
                            _wins = [w for w in _gw.getAllWindows()
                                     if w.title and _target in w.title]
                            if not _wins:
                                # Title may not have propagated to the
                                # OS title bar yet — wait briefly.
                                time.sleep(1.0)
                                _wins = [w for w in _gw.getAllWindows()
                                         if w.title and _target in w.title]

                            if _wins:
                                _w = _wins[0]
                                _hwnd = (getattr(_w, "_hWnd", None)
                                         or getattr(_w, "hWnd", None))
                                if _hwnd:
                                    # Restore ONLY if minimized — never on a
                                    # maximized window (SW_RESTORE would
                                    # un-maximize it and move our window).
                                    if _w32g.IsIconic(_hwnd):
                                        _w32g.ShowWindow(_hwnd, _w32c.SW_RESTORE)
                                        time.sleep(0.3)

                                    # Alt-key trick → grants foreground rights
                                    # to THIS thread so SetForegroundWindow
                                    # is allowed to actually work.
                                    _ALT = 0x12
                                    _KEYUP = 0x0002
                                    _ctypes.windll.user32.keybd_event(_ALT, 0, 0, 0)
                                    time.sleep(0.05)
                                    _ctypes.windll.user32.keybd_event(_ALT, 0, _KEYUP, 0)
                                    time.sleep(0.05)

                                    # Push window above all others.
                                    _SWP = _w32c.SWP_NOMOVE | _w32c.SWP_NOSIZE
                                    _w32g.SetWindowPos(_hwnd, _w32c.HWND_TOPMOST,
                                                       0, 0, 0, 0, _SWP)
                                    _w32g.SetWindowPos(_hwnd, _w32c.HWND_NOTOPMOST,
                                                       0, 0, 0, 0, _SWP)

                                    # Now SetForegroundWindow sticks.
                                    _w32g.SetForegroundWindow(_hwnd)
                                    time.sleep(0.5)

                                    _active = _w32g.GetForegroundWindow()
                                    if _active == _hwnd:
                                        print(f"[captcha] brought Chrome to "
                                              f"FRONT before measuring "
                                              f"(title={_target!r})")
                                    else:
                                        print(f"[captcha] foreground request "
                                              f"didn't stick — Chrome may still "
                                              f"be behind another window; the "
                                              f"worker will retry before clicking")
                                else:
                                    print(f"[captcha] no hwnd on matched window")
                            else:
                                print(f"[captcha] no window matched "
                                      f"{_target!r} — skipping bring-to-front")
                        except Exception as _fe:
                            print(f"[captcha] bring-to-front skipped: {_fe}")

                        # 1.5) SCROLL-SETTLE before we measure the click
                        # location. Some pages auto-scroll the captcha out
                        # of view (sticky headers, lazy-load placeholders)
                        # or paint it in two stages. We:
                        #
                        #   a. scrollTo(0, 0)        → force page to TOP
                        #   b. sleep 7s              → captcha re-renders
                        #                              cleanly at its top
                        #                              position
                        #   c. scrollTo(bottom)      → kicks any
                        #                              IntersectionObserver
                        #                              / lazy-load
                        #   d. scrollTo(0, 0)        → back to top so the
                        #                              measurement matches
                        #                              what the user sees
                        #   e. small settle pause    → so any reflow caused
                        #                              by the scroll trip
                        #                              finishes before we
                        #                              call _measure_loc()
                        #
                        # Whole block is wrapped in one try so a scroll
                        # failure can't crash safe_get — we just fall
                        # through to measurement at whatever position
                        # the page ended up at. (Test independently via
                        # `python science_direct\test_scroll.py <url>`.)
                        try:
                            driver.execute_script("window.scrollTo(0, 0);")
                            print(f"[captcha] scrolled to TOP — waiting 7s "
                                  f"for the captcha to settle / reload")
                            time.sleep(7)

                            print(f"[captcha] scroll exercise: bottom")
                            driver.execute_script(
                                "window.scrollTo(0, document.body.scrollHeight);"
                            )
                            time.sleep(1.0)

                            print(f"[captcha] scroll exercise: back to TOP")
                            driver.execute_script("window.scrollTo(0, 0);")
                            time.sleep(1.5)
                        except Exception as _se:
                            print(f"[captcha] scroll-settle failed: {_se}")

                        # 2) Enqueue CAPTCHA (worker will find window by title).
                        # captcha_click_x / captcha_click_y may be a scalar
                        # (most callers) OR a callable(html) -> int when the
                        # site shows different challenge layouts that need
                        # different click points. We evaluate right here so
                        # the choice is based on the CURRENT challenge page.
                        try:
                            _pre_src = driver.page_source
                        except Exception:
                            _pre_src = ""

                        # Backwards-compatible callable evaluation: pass
                        # driver as second arg when the callable accepts
                        # it. Lets site-specific click-coord functions
                        # introspect the live DOM (e.g. find the captcha
                        # iframe inside a closed shadow DOM via CDP and
                        # compute exact window-relative coords).
                        import inspect as _inspect

                        # ── AUTO IFRAME FINDER (for "normal" captchas) ──
                        # When the caller didn't pass a captcha_click_x/y
                        # callable (i.e. just defaults like 470/520), we
                        # try to locate the captcha iframe ourselves via
                        # standard JS selectors. Returns viewport coords
                        # of the checkbox center, or None if no iframe
                        # matched (then we fall back to the scalar).
                        #
                        # Covers Cloudflare Turnstile / Just-a-moment,
                        # hCaptcha, reCAPTCHA. Closed-shadow-DOM cases
                        # (like ScienceDirect's challenge) are NOT
                        # covered here — those sites pass a callable
                        # that uses CDP to pierce the shadow root.
                        _AUTO_IFRAME_JS = r"""
                        (function(){
                          var SELS = [
                            'iframe[title="Widget containing a Cloudflare security challenge"]',
                            'iframe[title*="challenge"]',
                            'iframe[title*="Cloudflare"]',
                            'iframe[src*="challenges.cloudflare.com"]',
                            'iframe[src*="turnstile"]',
                            'iframe[id^="cf-chl-widget"]',
                            'iframe[src*="hcaptcha.com"]',
                            'iframe[title*="hCaptcha"]',
                            'iframe[src*="recaptcha"]',
                            'iframe[title*="reCAPTCHA"]'
                          ];
                          for (var i=0; i<SELS.length; i++){
                            var el = document.querySelector(SELS[i]);
                            if (el){
                              var r = el.getBoundingClientRect();
                              if (r.width > 0 && r.height > 0){
                                return {found:true, sel:SELS[i],
                                  vp_x: r.left + 30,
                                  vp_y: r.top + r.height/2,
                                  w: r.width, h: r.height};
                              }
                            }
                          }
                          // Shadow-host fallback (Cloudflare's new
                          // wrapper with closed-shadow iframe; the
                          // hidden input remains in the light DOM).
                          var hid = document.querySelector(
                            'input[name="cf-turnstile-response"], '
                            + 'input[id^="cf-chl-widget"]'
                          );
                          if (hid){
                            var host = hid.parentElement;
                            if (host){
                              var rh = host.getBoundingClientRect();
                              if (rh.width > 0 && rh.height > 0){
                                return {found:true, sel:'shadow-host',
                                  vp_x: rh.left + 30,
                                  vp_y: rh.top + rh.height/2,
                                  w: rh.width, h: rh.height};
                              }
                            }
                          }
                          return {found:false};
                        })();
                        """

                        def _auto_find_captcha_xy_cdp():
                            """CDP-based iframe finder — pierces CLOSED
                            shadow DOMs that document.querySelector
                            cannot see. Returns (vp_x, vp_y) or None.

                            Uses DOM.getDocument(pierce=true) which
                            walks shadow roots, then DOM.getBoxModel to
                            get the iframe's viewport rect. This is the
                            only way to locate Cloudflare's new wrapped
                            challenge iframe (Sagepub uses this layout).
                            """
                            try:
                                doc = driver.execute_cdp_cmd(
                                    "DOM.getDocument",
                                    {"depth": -1, "pierce": True},
                                )
                            except Exception as e:
                                print(f"[auto-iframe-cdp] getDocument failed: {e}")
                                return None

                            iframe_node_id = [None]

                            def _walk(node):
                                if iframe_node_id[0] is not None:
                                    return
                                if node.get("nodeName") == "IFRAME":
                                    attrs = node.get("attributes") or []
                                    a = dict(zip(attrs[::2], attrs[1::2]))
                                    src = (a.get("src") or "").lower()
                                    ttl = (a.get("title") or "").lower()
                                    if ("challenges.cloudflare.com" in src
                                        or "turnstile" in src
                                        or "cloudflare" in ttl
                                        or "challenge" in ttl
                                        or "hcaptcha" in src
                                        or "recaptcha" in src):
                                        iframe_node_id[0] = node["nodeId"]
                                        return
                                for ch in (node.get("children") or []):
                                    _walk(ch)
                                # shadowRoots is where closed shadow
                                # content lives when pierce=True.
                                for sh in (node.get("shadowRoots") or []):
                                    _walk(sh)
                                cd = node.get("contentDocument")
                                if cd:
                                    _walk(cd)

                            try:
                                _walk(doc["root"])
                            except Exception as e:
                                print(f"[auto-iframe-cdp] walk failed: {e}")
                                return None
                            if iframe_node_id[0] is None:
                                return None

                            try:
                                box = driver.execute_cdp_cmd(
                                    "DOM.getBoxModel",
                                    {"nodeId": iframe_node_id[0]},
                                )
                            except Exception as e:
                                print(f"[auto-iframe-cdp] getBoxModel failed: {e}")
                                return None
                            border = (box.get("model") or {}).get("border") or []
                            if len(border) < 8:
                                return None
                            x1, y1 = border[0], border[1]
                            x2, y2 = border[2], border[5]
                            width  = x2 - x1
                            height = y2 - y1
                            if width <= 0 or height <= 0:
                                return None
                            vp_x = x1 + 30        # Cloudflare checkbox offset
                            vp_y = y1 + height / 2.0
                            print(f"[auto-iframe-cdp] found via CDP "
                                  f"(closed shadow DOM)  "
                                  f"size={width:.0f}x{height:.0f} "
                                  f"vp=({vp_x:.0f},{vp_y:.0f})")
                            return float(vp_x), float(vp_y)

                        def _auto_find_captcha_xy():
                            """Try to locate the captcha iframe.
                            Priority:
                              1. Regular JS selectors (fast, works for
                                 iframes in the light DOM).
                              2. CDP DOM.getDocument(pierce=true) — for
                                 closed shadow DOMs (Sagepub, etc.).
                            Returns (vp_x, vp_y) or None.
                            """
                            try:
                                r = driver.execute_script(
                                    "return " + _AUTO_IFRAME_JS
                                )
                            except Exception as _e:
                                print(f"[auto-iframe] JS raised: {_e}")
                                r = None
                            if r and r.get("found"):
                                print(f"[auto-iframe] found via {r.get('sel')!r} "
                                      f"size={r.get('w'):.0f}x{r.get('h'):.0f} "
                                      f"vp=({r.get('vp_x'):.0f},{r.get('vp_y'):.0f})")
                                return float(r["vp_x"]), float(r["vp_y"])

                            # JS selectors missed — try CDP shadow-DOM walk.
                            return _auto_find_captcha_xy_cdp()

                        def _measure_loc():
                            """Single fast read of (vp_x, vp_y, screen_x,
                            screen_y, side_border, chrome_top, dpr).
                            None if anything fails."""
                            try:
                                cur_src = driver.page_source
                            except Exception:
                                cur_src = _pre_src

                            def _eval(fn):
                                if not callable(fn):
                                    return fn
                                try:
                                    n = len(_inspect.signature(fn).parameters)
                                except Exception:
                                    n = 1
                                try:
                                    return fn(cur_src, driver) if n >= 2 else fn(cur_src)
                                except Exception as e:
                                    print(f"[click-fn] {fn.__name__} raised: {e}")
                                    return None

                            # Precedence (so the click is ALWAYS at the
                            # actual iframe position when possible):
                            #   1. JS auto-finder           ← regular DOM
                            #      (Cloudflare/Turnstile/hCaptcha/reCAPTCHA
                            #      iframes that document.querySelector
                            #      can see directly, including shadow
                            #      host of the new Cloudflare wrapper)
                            #   2. Caller-supplied callable ← edge cases
                            #      (closed shadow DOM that only CDP
                            #      DOM.getDocument({pierce:true}) can
                            #      pierce — e.g. ScienceDirect)
                            #   3. Scalar defaults          ← last resort
                            cx = cy = None
                            auto = _auto_find_captcha_xy()
                            if auto is not None:
                                cx, cy = auto
                            elif callable(captcha_click_x) or callable(captcha_click_y):
                                # JS couldn't find it — let the caller's
                                # callable try (it may use CDP to pierce
                                # closed shadow DOMs).
                                print(f"[auto-iframe] not found via JS; "
                                      f"trying caller-supplied callable")
                                cx = _eval(captcha_click_x)
                                cy = _eval(captcha_click_y)
                            else:
                                # No callable and JS didn't find it →
                                # use whatever scalar the caller provided.
                                cx = captcha_click_x
                                cy = captcha_click_y
                            if not (isinstance(cx, (int, float))
                                    and isinstance(cy, (int, float))):
                                return None
                            try:
                                w = driver.execute_script(
                                    "var ow=window.outerWidth, oh=window.outerHeight,"
                                    "    iw=window.innerWidth,  ih=window.innerHeight;"
                                    "var sb=Math.max(0,(ow-iw)/2);"
                                    "var ct=Math.max(0, oh-ih-sb);"
                                    "return {sx:window.screenX, sy:window.screenY,"
                                    "        sb:sb, ct:ct,"
                                    "        dpr:window.devicePixelRatio||1};"
                                )
                            except Exception as e:
                                print(f"[captcha] window-info fetch failed: {e}")
                                return None
                            sb  = float(w["sb"]);  ct  = float(w["ct"])
                            sx  = float(w["sx"]) + sb + float(cx)
                            sy  = float(w["sy"]) + ct + float(cy)
                            dpr = float(w.get("dpr") or 1.0)
                            return float(cx), float(cy), sx, sy, sb, ct, dpr

                        def _still_blocked():
                            """Quick recheck of page_source for the
                            challenge markers (same set used elsewhere)."""
                            try:
                                src_now = driver.page_source.lower()
                            except Exception:
                                return True
                            sb_now = (
                                "just a moment" in src_now
                                or "verify you are human" in src_now
                                or "checking your browser" in src_now
                            )
                            if challenge_predicate:
                                try:
                                    sb_now = sb_now or bool(challenge_predicate(src_now))
                                except Exception:
                                    pass
                            return sb_now

                        # ── FIRST measurement (the "first click" location). ─
                        # Producers return VIEWPORT click coords. We ask
                        # the BROWSER for the absolute screen pixel:
                        #   screen_x = window.screenX + sideBorder + vp_x
                        #   screen_y = window.screenY + chromeTop  + vp_y
                        # plus devicePixelRatio. The worker clicks at
                        # screen × dpr when DPI-aware (test_static_click.py
                        # --mode pyautogui proved this lands on the bull-
                        # eye). side_border / chrome_top still go on the
                        # ticket for the legacy worker fallback.
                        first_loc = _measure_loc()
                        if first_loc is None:
                            print(f"[captcha] first measurement failed — "
                                  f"using fallback (300, 400)")
                            first_loc = (300.0, 400.0, 0.0, 0.0, 0.0, 0.0, 1.0)
                        active_loc = first_loc

                        # ── RE-MEASURE-IF-FAILED LOOP ───────────────────
                        # MAX_LOC_ATTEMPTS = 2 means: do the first click
                        # with the first measurement, and if it fails do
                        # ONE more click with a re-measured location. If
                        # the second measurement matches the first within
                        # LOC_CHANGE_TOLERANCE_PX we reuse the first
                        # reading. Otherwise the captcha moved and we
                        # click the new spot.
                        #
                        # POST_CLICK_WAIT_SEC bounds how long we wait
                        # for the page to clear AFTER each click. On a
                        # slow connection the click can succeed but
                        # Cloudflare's page-replace fetch is still in
                        # flight — a single 3 s sleep gives a false
                        # negative. We POLL every POST_CLICK_POLL_SEC
                        # so a fast clear returns immediately; a slow
                        # one is given the full window before being
                        # declared failed.
                        MAX_LOC_ATTEMPTS        = 2
                        LOC_CHANGE_TOLERANCE_PX = 8
                        POST_CLICK_WAIT_SEC     = 20
                        POST_CLICK_POLL_SEC     = 1.0
                        # After clearance is detected, give the new page
                        # time to finish loading its resources before we
                        # hand the driver back to the caller. Otherwise
                        # the NEXT safe_get() call sees a busy browser
                        # and _driver_responds() (10s JS-ping timeout)
                        # fails → the driver gets killed and recreated,
                        # throwing away a captcha we just solved.
                        POST_CLEAR_SETTLE_SEC   = 6

                        still_blocked = True
                        for _loc_attempt in range(1, MAX_LOC_ATTEMPTS + 1):
                            (_click_x, _click_y,
                             _screen_x, _screen_y,
                             _sb_f, _ct_f, _dpr) = active_loc
                            _side_border = int(max(0, _sb_f))
                            _chrome_top  = int(max(0, _ct_f))
                            print(f"[captcha] click attempt "
                                  f"{_loc_attempt}/{MAX_LOC_ATTEMPTS}  "
                                  f"viewport=({_click_x},{_click_y})  "
                                  f"screen=({_screen_x},{_screen_y})  "
                                  f"dpr={_dpr}")

                            enqueue_captcha_system(
                                journal_id=journal_id,
                                window_title=window_title,
                                click_x=int(_click_x),
                                click_y=int(_click_y),
                                side_border=int(_side_border),
                                chrome_top=int(_chrome_top),
                                screen_x=_screen_x,
                                screen_y=_screen_y,
                                dpr=_dpr,
                            )

                            # Poll for clearance — on slow connections
                            # the click succeeded but Cloudflare's page
                            # transition is still loading. Polling lets
                            # a fast clear return immediately AND lets a
                            # slow one finish before we declare failure.
                            _wait_start = time.time()
                            still_blocked = True
                            while time.time() - _wait_start < POST_CLICK_WAIT_SEC:
                                time.sleep(POST_CLICK_POLL_SEC)
                                if not _still_blocked():
                                    still_blocked = False
                                    break
                            if not still_blocked:
                                _waited = time.time() - _wait_start
                                print(f"[captcha] cleared on click attempt "
                                      f"{_loc_attempt} after {_waited:.1f}s")
                                # Let the new real page finish loading
                                # before we return the driver, so the
                                # next safe_get's JS-ping doesn't time
                                # out and kill the browser.
                                print(f"[captcha] settling "
                                      f"{POST_CLEAR_SETTLE_SEC}s for new "
                                      f"page to finish loading")
                                time.sleep(POST_CLEAR_SETTLE_SEC)
                                break

                            # Last attempt — fall through to the existing
                            # refresh + failure path below.
                            if _loc_attempt >= MAX_LOC_ATTEMPTS:
                                print(f"[captcha] still blocked after "
                                      f"{MAX_LOC_ATTEMPTS} click attempts — "
                                      f"falling through to refresh path")
                                break

                            # ── RE-MEASURE for the next click. ──────────
                            # If the new reading agrees with the FIRST
                            # reading (within tolerance) → reuse first.
                            # If it moved → use the NEW reading.
                            new_loc = _measure_loc()
                            if new_loc is None:
                                print(f"[captcha] re-measure failed — "
                                      f"reusing first reading")
                                active_loc = first_loc
                                continue
                            dx = abs(new_loc[2] - first_loc[2])
                            dy = abs(new_loc[3] - first_loc[3])
                            if dx > LOC_CHANGE_TOLERANCE_PX or dy > LOC_CHANGE_TOLERANCE_PX:
                                print(f"[captcha] location MOVED "
                                      f"(diff=({dx:.1f},{dy:.1f})px) → "
                                      f"using NEW reading for retry "
                                      f"vp=({new_loc[0]:.0f},{new_loc[1]:.0f})")
                                active_loc = new_loc
                            else:
                                print(f"[captcha] location UNCHANGED "
                                      f"(diff=({dx:.1f},{dy:.1f})px) → "
                                      f"reusing FIRST reading for retry")
                                active_loc = first_loc

                        # ── REFRESH-AND-RECHECK before declaring failure ──
                        # Sometimes Cloudflare accepts the click (clearance
                        # token gets set in cookies) but the page itself
                        # stalls on the "Just a moment..." transition and
                        # never loads the real content. A single refresh
                        # almost always lands us on the clean target page
                        # because the cf_clearance cookie is already in
                        # the jar from the successful click. Much cheaper
                        # than closing+restarting the driver.
                        if still_blocked:
                            print(f"[CAPTCHA] page still shows challenge text "
                                  f"after click — refreshing once before "
                                  f"declaring failure ({url[:80]})")
                            try:
                                driver.refresh()
                                # Same poll pattern as the post-click
                                # check: slow connections can need 10+s
                                # for the refreshed page to render.
                                _ref_start = time.time()
                                still_blocked = True
                                while time.time() - _ref_start < POST_CLICK_WAIT_SEC:
                                    time.sleep(POST_CLICK_POLL_SEC)
                                    if not _still_blocked():
                                        still_blocked = False
                                        break
                                if not still_blocked:
                                    _waited = time.time() - _ref_start
                                    print(f"[CAPTCHA] refresh cleared the "
                                          f"transition after {_waited:.1f}s — "
                                          f"page is now usable")
                                    # Same settle wait as the post-click
                                    # path: let the real page finish
                                    # loading before returning the driver.
                                    print(f"[CAPTCHA] settling "
                                          f"{POST_CLEAR_SETTLE_SEC}s for "
                                          f"new page to finish loading")
                                    time.sleep(POST_CLEAR_SETTLE_SEC)
                            except Exception as _re:
                                print(f"[CAPTCHA] refresh failed: {_re}")

                        if still_blocked:
                            # Track consecutive failures. After N in a row,
                            # sleep for a cooldown before continuing —
                            # gives the IP / fingerprint a chance to settle
                            # so we're not hammering a hostile captcha
                            # back-to-back.
                            global _CAPTCHA_CONSECUTIVE_FAILURES
                            _CAPTCHA_CONSECUTIVE_FAILURES += 1
                            print(f"[CAPTCHA] Click failed - page still blocked "
                                  f"({_CAPTCHA_CONSECUTIVE_FAILURES}/"
                                  f"{_CAPTCHA_FAILURE_THRESHOLD} consecutive), "
                                  f"saving to skipped -> {url[:80]}...")
                            save_skipped(SKIPPED_FILE, journal_id, url, "Captcha click failed")
                            if _CAPTCHA_CONSECUTIVE_FAILURES >= _CAPTCHA_FAILURE_THRESHOLD:
                                print(f"[CAPTCHA] {_CAPTCHA_FAILURE_THRESHOLD} "
                                      f"consecutive click failures — "
                                      f"cooling down {_CAPTCHA_FAILURE_COOLDOWN_SEC}s "
                                      f"before next attempt")
                                time.sleep(_CAPTCHA_FAILURE_COOLDOWN_SEC)
                                _CAPTCHA_CONSECUTIVE_FAILURES = 0
                            return None

                        # Captcha solved — reset the failure streak.
                        _CAPTCHA_CONSECUTIVE_FAILURES = 0
                        return driver

                    # Already handled → just wait
                    time.sleep(2)
                    continue


                else:
                    # No captcha on the page — reset the failure streak.
                    _CAPTCHA_CONSECUTIVE_FAILURES = 0
                    return driver  # ✅ Page usable

            # If Cloudflare never cleared → treat as failure
            raise TimeoutException("Cloudflare not cleared")

        except _HardBlockRetry:
            # Dedicated path for SD-style hard-block recovery. We want a
            # FRESH driver with a fresh random UA — no captcha worker, no
            # VPN rotation, just a new browser session on the same IP.
            # `continue` skips `attempt += 1` so the URL gets up to
            # MAX_HARDBLOCK_RETRIES tries (the counter is enforced where
            # the exception was raised).
            #
            # NUKE PATTERN — replicate what Ctrl+C does:
            #   1) driver.quit()                        — graceful close
            #   2) taskkill /F /T every chrome.exe +    — mop up zombies
            #      chromedriver.exe under THIS process    that quit() leaves
            #   3) longer sleep                         — let Windows reap
            #   4) create_driver()                      — brand-new process,
            #                                             new temp profile,
            #                                             new UA
            # Without step 2, undetected-chromedriver often reuses lingering
            # chrome processes / temp profile dirs, so the "new" session
            # still carries the Cloudflare-flagged fingerprint. Step 2 makes
            # automatic recovery behave like manual Ctrl+C → re-run.
            # ── Choose recovery mode via env var ────────────────────────
            # Two recovery flavours:
            #
            #   CAPTCHA_RECOVERY_MODE=click   (default)
            #     Same Python process. Close the driver, sweep zombie
            #     Chrome processes, sleep 8 s, open a fresh maximized
            #     Chrome with a new UA, retry the same URL via the click
            #     path. Faster — no Python re-init.
            #
            #   CAPTCHA_RECOVERY_MODE=restart
            #     Quit this Python entirely and re-launch the script with
            #     the same args. Replicates manual Ctrl+C + retype.
            #     Slower (~120 s cool-down + Python re-init) but the
            #     cleanest possible reset — new interpreter, new socket
            #     pools, no inherited state at all. Resume from
            #     last_state.json picks up the same URL automatically.
            #
            # Legacy alias: CAPTCHA_RESTART_SCRIPT=1 also triggers restart.
            _mode = (os.environ.get("CAPTCHA_RECOVERY_MODE", "click")
                     .strip().lower())
            if os.environ.get("CAPTCHA_RESTART_SCRIPT", "") == "1":
                _mode = "restart"

            force_close_driver(driver)
            try:
                from driver_state import kill_all_selenium_browsers_under_owner
                kill_all_selenium_browsers_under_owner()
                print("[SD] kill_all_selenium_browsers_under_owner — "
                      "swept zombie Chrome/chromedriver processes")
            except Exception as _e:
                print(f"[SD] zombie sweep skipped: {_e}")

            if _mode == "restart":
                # ── Mode: restart — Ctrl+C-and-re-run automated. ────────
                cool_down_s = 120
                print(f"[SD] recovery mode=RESTART — cooling down "
                      f"{cool_down_s}s then re-launching the script")
                time.sleep(cool_down_s)

                import sys as _sys
                import subprocess as _sp
                print(f"[SD] restarting script  (argv={_sys.argv}) — "
                      "resume state will pick up exactly where we stopped")
                try:
                    if _sys.platform == "win32":
                        # subprocess.Popen with list-args correctly quotes
                        # Windows paths containing spaces (Raj Kumar etc.) —
                        # os.execv on Windows DOES NOT, which produced
                        # `C:\Users\Raj: can't open file ...` errors.
                        _sp.Popen([_sys.executable] + _sys.argv,
                                  cwd=os.getcwd())
                        os._exit(0)
                    else:
                        os.execv(_sys.executable,
                                 [_sys.executable] + _sys.argv)
                except Exception as _e:
                    print(f"[SD] restart failed ({_e}); falling back to "
                          "click-mode recreate")
                    # Fall through to click mode below.

            # ── Mode: click (default) — fast in-process recreate. ──────
            print("[SD] recovery mode=CLICK — close driver + reopen with "
                  "fresh UA (same Python process)")
            time.sleep(8)
            driver = create_driver(head)
            continue

        except Exception as e:
            msg = str(e).lower()
            print(f"⚠️ Error: {e}")

            # 🚨 DRIVER STUCK (timeout) or DEAD → refresh driver, one driver per process
            is_timeout = "timeout" in msg or "timed out" in msg
            is_dead = (
                is_timeout or
                "connection refused" in msg or
                "max retries exceeded" in msg or
                "failed to establish a new connection" in msg
            )

            if is_dead:
                force_close_driver(driver)
                time.sleep(5)
                driver = create_driver(head)
                if is_timeout:
                    continue  # retry without consuming attempt
            else:
                # 🟡 Non-fatal issue (slow, temporary)
                time.sleep(5)

        attempt += 1

    # 🔁 Final fallback: close driver completely and signal failure
    force_close_driver(driver)
    save_skipped(SKIPPED_FILE, journal_id, url, "Final failure")
    return None

def fetch_and_cache_journals(CACHE_FILE,DATABASE):
    page = 1
    all_records = []
    while True:
        resp = requests.get(f"{API_ROOT}/{DATABASE}/journals?page={page}")
        if resp.status_code != 200:
            print(f"Failed to fetch page {page}: {resp.status_code}")
            break
        data = resp.json()
        records = data.get("records", [])
        if not records:
            break
        all_records.extend(records)
        page += 1
    # Prepare wrapper object for saving
    full_data = {
        "page": 1,
        "page_size": len(all_records),
        "records": all_records
    }
    with open(CACHE_FILE, "w") as f:
        json.dump(full_data, f, indent=2)
    return all_records

def load_journals_from_cache(CACHE_FILE):
    if not os.path.exists(CACHE_FILE):
        print(f"Cache file {CACHE_FILE} not found")
        return []
    with open(CACHE_FILE, "r") as f:
        data = json.load(f)
    return data.get("records", [])

def post_journals(journals,DATABASE):
    response = requests.post(f"{API_ROOT}/{DATABASE}/add/journals", json=journals)
    if response.status_code == 200:
        print(f"✅ Uploaded {len(journals)} journals successfully.")
    else:
        print(f"❌ Failed to upload journals. Status: {response.status_code}, Error: {response.text}")

def post_article_links(article_links,DATABASE):
    response = requests.post(f"{API_ROOT}/{DATABASE}/add/article_links", json=article_links)
    if response.status_code == 200:
        print(f"✅ Uploaded {len(article_links)} article links successfully.")
        print(response.text)
    else:
        print(f"❌ Failed to upload article links. Status: {response.status_code}, Error: {response.text}")

def post_article_data(article_data,DATABASE):
    response = requests.post(f"{API_ROOT}/{DATABASE}/add/article_data_by_url", json=article_data)
    if response.status_code == 200:
        print(f"✅ Uploaded {len(article_data)} article data successfully.")
    else:
        print(f"❌ Failed to upload article data. Status: {response.status_code}, Error: {response.text}")

def post_article_links_with_data(database, combined_data):
    url = f"{API_ROOT}/{database}/add/article_links_with_data"
    try:
        response = requests.post(url, json=combined_data,timeout=(10, 120))
        if response.status_code == 200:
            print(f"✅ Uploaded {len(combined_data)} articles successfully.")
        else:
            print(f"❌ Failed to upload. Status: {response.status_code}, Error: {response.text}")
    except Exception as e:
        print(f"⚠️ Error during upload: {e}")

def clear_last_state(STATE_FILE):
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


# ── Telegram / status reporting (built-in so miners don't import status_reporter) ──
def _load_status_config():
    try:
        from config import SERVER_URL, SYSTEM_ID
        return SERVER_URL, SYSTEM_ID
    except Exception:
        return None, None


def report_status(process_id, status, message=""):
    server_url, system_id = _load_status_config()
    if not server_url or not system_id:
        return
    try:
        requests.post(
            server_url,
            json={
                "system_id": system_id,
                "process_id": process_id,
                "status": status,
                "message": message,
            },
            timeout=5,
        )
    except Exception:
        pass


def start_heartbeat(process_id, interval_sec=300, message="Process alive"):
    import threading

    def _loop():
        while True:
            report_status(process_id, "running", message)
            time.sleep(interval_sec)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    return t


# ── Telegram alerts (crash + task lifecycle) ──────────────────────────────
# Mirrors the helpers in utils/offline_utils.py so a VPN-rotating miner
# can hook the same Telegram bot for crash / start / end alerts. Bot creds
# come from telegram_notifications/config.py; if not set, every helper is
# a silent no-op.

def _telegram_send(text: str) -> None:
    """Best-effort POST to Telegram via telegram_notifications.notifier."""
    try:
        from telegram_notifications.notifier import _send as _tg_send  # type: ignore
        _tg_send(text)
    except Exception:
        pass


def notify_task_started(process_id: str, extra: str = "") -> None:
    msg = f"▶️ <b>{process_id}</b> – started"
    if extra:
        msg += f"\n{extra}"
    _telegram_send(msg)


def notify_task_ended(process_id: str, extra: str = "") -> None:
    global _CRASH_TASK_ENDED
    _CRASH_TASK_ENDED = True  # suppress the atexit "exited unexpectedly" alert
    msg = f"✅ <b>{process_id}</b> – ended"
    if extra:
        msg += f"\n{extra}"
    _telegram_send(msg)


def notify_crash(process_id: str, error: str) -> None:
    msg = (
        f"💥 <b>{process_id}</b> – CRASHED\n"
        f"<pre>{(error or '')[:1500]}</pre>"
    )
    _telegram_send(msg)


_CRASH_HANDLER_INSTALLED = False
_CRASH_TASK_ENDED = False


def install_crash_handler(process_id: str) -> None:
    """Wire up Telegram alerts for unexpected termination.

    Hooks ``sys.excepthook`` (uncaught exceptions), ``signal.SIGINT/SIGTERM/SIGBREAK``
    (Ctrl+C, kill, Ctrl+Break) and ``atexit`` (catches ``sys.exit`` paths
    that bypass the excepthook). Idempotent.
    """
    global _CRASH_HANDLER_INSTALLED
    if _CRASH_HANDLER_INSTALLED:
        return
    _CRASH_HANDLER_INSTALLED = True

    import atexit
    import signal
    import sys as _sys
    import traceback

    prev_hook = _sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        global _CRASH_TASK_ENDED
        try:
            tb_str = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
            notify_crash(process_id, tb_str)
            _CRASH_TASK_ENDED = True
        except Exception:
            pass
        prev_hook(exc_type, exc_value, exc_tb)

    _sys.excepthook = _excepthook

    def _on_signal(signum, frame):
        global _CRASH_TASK_ENDED
        try:
            name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
        except Exception:
            name = str(signum)
        notify_crash(process_id, f"Process received signal {name}")
        _CRASH_TASK_ENDED = True
        _sys.exit(128 + int(signum))

    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        try:
            sig = getattr(signal, sig_name)
            signal.signal(sig, _on_signal)
        except Exception:
            pass

    def _on_exit():
        if not _CRASH_TASK_ENDED:
            notify_crash(process_id, "Process exited without notify_task_ended (silent abort)")

    atexit.register(_on_exit)


# ─────────────────────────────────────────────────────────────────────────────
# Country extraction from affiliation strings
# Uses my_countries.txt (alias→canonical mapping) + pycountry as fallback.
# my_countries.txt must be in the same directory as this file.
#
# Usage:
#   from utils_vpn.offline_utils_vpn import extract_country_from_affiliation
#   country = extract_country_from_affiliation("Ege University, Izmir, Turkey")
#   # → "Turkey"
# ─────────────────────────────────────────────────────────────────────────────
import unicodedata as _unicodedata

_COUNTRIES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "my_countries.txt")

def _load_country_mapping():
    mapping = {}
    try:
        with open(_COUNTRIES_FILE, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#"):
                    continue
                if "|" in _line:
                    _alias, _canonical = _line.split("|", 1)
                    mapping[_alias.strip().lower()] = _canonical.strip()
    except FileNotFoundError:
        pass
    return mapping

_COUNTRY_MAPPING       = _load_country_mapping()
_SORTED_COUNTRY_KEYS   = sorted(_COUNTRY_MAPPING.keys(), key=len, reverse=True)

try:
    import pycountry as _pycountry

    def _nc(t):
        t = _unicodedata.normalize("NFKD", t)
        t = "".join(c for c in t if not _unicodedata.combining(c))
        return t.lower().strip()

    _PYCOUNTRY_NAMES      = {_nc(c.name): c.name for c in _pycountry.countries}
    _SORTED_PYCOUNTRY_KEYS = sorted(_PYCOUNTRY_NAMES.keys(), key=len, reverse=True)
except ImportError:
    _PYCOUNTRY_NAMES       = {}
    _SORTED_PYCOUNTRY_KEYS = []

_INST_KEYWORDS = {
    "university", "universite", "universidad", "universidade", "universitat",
    "institute", "instituto", "institution", "institut",
    "college", "escola", "school",
    "department", "departamento", "dept",
    "faculty", "facultad", "faculte",
    "hospital", "clinic", "medical centre", "medical center",
    "center", "centre", "centro",
    "laboratory", "laboratories", "lab",
    "academy", "academie", "akademie",
    "polytechnic", "polytech",
    "foundation", "fondation",
    "corporation", "company", "gmbh", "inc", "ltd", "llc",
    "ministry", "government",
}

def _is_inst_part(part):
    words = set(part.lower().split())
    if words & _INST_KEYWORDS:
        return True
    return any(kw in part.lower() for kw in _INST_KEYWORDS if " " in kw)

def _fw_match(key, text):
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text))

def _exact_match(part):
    part_s = part.strip()
    for k in _SORTED_COUNTRY_KEYS:
        if re.fullmatch(rf"{re.escape(k)}[\s\d\-]*", part_s):
            return _COUNTRY_MAPPING[k]
    for k in _SORTED_PYCOUNTRY_KEYS:
        if re.fullmatch(rf"{re.escape(k)}[\s\d\-]*", part_s):
            return _PYCOUNTRY_NAMES[k]
    return None

def _loose_match(part):
    for k in _SORTED_COUNTRY_KEYS:
        if _fw_match(k, part):
            return _COUNTRY_MAPPING[k]
    for k in _SORTED_PYCOUNTRY_KEYS:
        if _fw_match(k, part):
            return _PYCOUNTRY_NAMES[k]
    return None

def _norm_aff(text):
    text = _unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not _unicodedata.combining(c))
    return text.lower().strip()

def extract_country_from_affiliation(affiliation):
    """Extract country from an affiliation string.

    Strategy (right-to-left, stops at first match):
      1. Exact match on the last comma/semicolon-separated segment
         (skip if it looks like an institution)
      2. Exact match on the last space-separated token of the last segment
      3. Right-to-left scan: exact match, skipping institution segments
      4. Right-to-left scan: loose match (country name embedded inside text)

    Returns the canonical country name string, or "" if not found.
    Works with multiple affiliations joined by "; " — call once per
    affiliation entry and join results, or pass the full joined string.
    """
    if not affiliation or not isinstance(affiliation, str) or not affiliation.strip():
        return ""

    text  = _norm_aff(str(affiliation))
    # Strip trailing phone/fax block before splitting (avoids "Turkey; phone/fax..." junk)
    text  = re.sub(r'[;,\s]+(?:phone|tel|fax|mobile)[^\w]*[\d\s\+\(\)\-\.\/]{4,}.*$', '', text, flags=re.I)
    parts = [p.strip() for p in re.split(r'[;,]', text) if p.strip()]

    if not parts:
        return ""

    last = parts[-1]

    # Step 1: exact on last segment
    if not _is_inst_part(last):
        r = _exact_match(last)
        if r:
            return r

    # Step 2: last space-separated token
    last_tok = last.split()[-1] if last.split() else ""
    if last_tok:
        r = _exact_match(last_tok)
        if r:
            return r

    # Step 3: right-to-left exact, skip institution segments
    for seg in reversed(parts):
        if _is_inst_part(seg):
            continue
        r = _exact_match(seg)
        if r:
            return r

    # Step 4: right-to-left loose (country name anywhere inside segment)
    for seg in reversed(parts):
        r = _loose_match(seg)
        if r:
            return r

    return ""
