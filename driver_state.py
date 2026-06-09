"""
Driver state folder: track launched chromedriver PIDs so we can:
- After launch: save driver details (PID, created_at, owner_pid for multiprocessing).
- On close: mark as closed (remove file).
- Orphans: only kill drivers whose OWNER PROCESS has exited (safe for multiprocessing).
"""
import os
import json
import time
from datetime import datetime

# Folder next to this script: mining_cursor/driver_state/
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DRIVER_STATE_DIR = os.path.join(_SCRIPT_DIR, "driver_state")

def _safe_int(x):
    try:
        return int(x)
    except Exception:
        return None


def _guess_latest_driver_pid(owner_pid: int | None = None):
    """
    Best-effort fallback for undetected_chromedriver where driver.service.process.pid may be missing.
    Guess newest driver PID that is a direct/indirect child of owner_pid (defaults to current process).
    """
    try:
        import psutil
    except ImportError:
        return None

    owner_pid = _safe_int(owner_pid) or os.getpid()
    try:
        owner = psutil.Process(owner_pid)
    except Exception:
        return None

    driver_names = {"chromedriver.exe", "chromedriver", "msedgedriver.exe", "msedgedriver"}
    candidates = []
    try:
        for p in owner.children(recursive=True):
            try:
                name = (p.name() or "").lower()
                if name not in driver_names:
                    continue
                # Prefer the one started most recently
                candidates.append((p.create_time(), p.pid))
            except Exception:
                continue
    except Exception:
        return None

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _ensure_dir():
    os.makedirs(DRIVER_STATE_DIR, exist_ok=True)


def _pid_file(pid):
    return os.path.join(DRIVER_STATE_DIR, f"{pid}.json")


def register_driver(driver):
    """
    After launching a driver, save its details in the state folder.
    owner_pid = this process; used so we never kill another process's driver.
    """
    if driver is None:
        return
    pid = None
    try:
        pid = driver.service.process.pid
    except Exception:
        pid = None
    if pid is None:
        # Fallback for undetected_chromedriver
        pid = _guess_latest_driver_pid(owner_pid=os.getpid())
    if pid is None:
        return
    _ensure_dir()
    path = _pid_file(pid)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "pid": pid,
                "owner_pid": os.getpid(),
                "created_at": datetime.utcnow().isoformat() + "Z",
                "status": "running",
            }, f, indent=2)
    except Exception:
        pass


def mark_driver_closed(driver=None, pid=None):
    """
    On close: remove the state file so this driver is no longer tracked.
    Pass either driver (to read pid) or pid. If both, pid takes precedence.
    """
    if pid is None and driver is not None:
        try:
            pid = driver.service.process.pid
        except Exception:
            return
    if pid is None:
        return
    path = _pid_file(pid)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# Run orphan cleanup only once per process to avoid killing live drivers by mistake
_orphan_cleanup_done = False


def kill_orphan_drivers(force=False):
    """
    Only kill drivers whose OWNER PROCESS has exited (true orphans).
    Never kill a driver that belongs to another running process (multiprocessing-safe).
    By default runs only ONCE per process (at first safe_get) to avoid races and over-aggressive cleanup.
    Pass force=True to run again (e.g. after you know all your drivers are closed).
    """
    global _orphan_cleanup_done
    if not force and _orphan_cleanup_done:
        return
    _orphan_cleanup_done = True

    try:
        import psutil
    except ImportError:
        return
    _ensure_dir()
    killed = []
    for name in os.listdir(DRIVER_STATE_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(DRIVER_STATE_DIR, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            owner_pid = data.get("owner_pid")
            if pid is None:
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue

            # Legacy file (no owner_pid): do NOT kill - PID may be reused by another process.
            # Only remove the file if that chromedriver process is already dead (cleanup).
            if owner_pid is None:
                try:
                    if not psutil.pid_exists(pid):
                        os.remove(path)
                except Exception:
                    pass
                continue

            # Owner process still running -> do not touch this driver
            try:
                if psutil.pid_exists(owner_pid):
                    continue
            except Exception:
                pass

            # Owner is dead: safe to kill this chromedriver (true orphan)
        except Exception:
            try:
                os.remove(path)
            except Exception:
                pass
            continue

        # Force-kill this PID and all children (chromedriver + chrome.exe)
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            try:
                parent.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            killed.append(pid)
        except (psutil.NoSuchProcess, ValueError):
            pass
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    if killed:
        print(f"[driver_state] Killed orphan driver(s): PIDs {killed}")


def _selenium_process_names():
    """Process basenames (lowercase) spawned by Selenium / undetected_chromedriver under our Python process."""
    return frozenset(
        {
            "chromedriver.exe",
            "chromedriver",
            "msedgedriver.exe",
            "msedgedriver",
            "chrome.exe",
            "chrome",
            "msedge.exe",
            "msedge",
        }
    )


def kill_all_selenium_browsers_under_owner(owner_pid=None):
    """
    Kill EVERY chromedriver + Chrome (or Edge) process in the subtree of owner_pid (default: current process).
    Use when replacing a hung driver or mopping up zombies — avoids leaving multiple Chrome instances when
    force_close only killed one guessed PID.

    On Windows uses taskkill /F /T on each chromedriver first (reliable tree kill), then psutil on survivors.
    """
    owner_pid = _safe_int(owner_pid) or os.getpid()
    try:
        import psutil
    except ImportError:
        return

    try:
        root = psutil.Process(owner_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return

    names = _selenium_process_names()

    def collect_children():
        try:
            return list(root.children(recursive=True))
        except Exception:
            return []

    children = collect_children()
    chromedriver_pids = []
    for p in children:
        try:
            n = (p.name() or "").lower()
            if n in ("chromedriver.exe", "chromedriver", "msedgedriver.exe", "msedgedriver"):
                chromedriver_pids.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if os.name == "nt" and chromedriver_pids:
        import subprocess

        creationflags = 0
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            creationflags = subprocess.CREATE_NO_WINDOW
        for pid in chromedriver_pids:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=45,
                    creationflags=creationflags,
                )
            except Exception:
                pass
        time.sleep(0.5)

    children = collect_children()
    for p in children:
        try:
            n = (p.name() or "").lower()
            if n in names:
                try:
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # Remove driver_state entries for this owner (single-driver miners like Wiley)
    _ensure_dir()
    try:
        for name in os.listdir(DRIVER_STATE_DIR):
            if not name.endswith(".json"):
                continue
            path = os.path.join(DRIVER_STATE_DIR, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("owner_pid") == owner_pid:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass
