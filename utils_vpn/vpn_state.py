"""
Shared VPN/driver state. All VPN change and rotator logic lives here + offline_utils_vpn.
- ip_blocked.flag: when set, one process rotates VPN and deletes it; others wait until gone (multi-driver safe).
- vpn_rotating.flag: when set by vpn_rotator, miners wait before driver.get until gone (pause during rotate).
"""
import os
import time

# VPN change code inside utils_vpn: one place for big data / multi-driver
def change_vpn_on_block(countries_file=None):
    """Change VPN once (e.g. on IP block). Used by safe_get in offline_utils_vpn. Retries up to 3 times."""
    import sys
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _root not in sys.path:
        sys.path.insert(0, _root)
    try:
        from vpn_provider import change_vpn_once
    except ImportError as e:
        print(f"[VPN] Cannot import vpn_provider: {e}. Ensure vpn_provider.py and countries.txt are in {_root}")
        raise RuntimeError(f"VPN change failed: {e}") from e
    print("[VPN] Calling change_vpn_once...")
    try:
        change_vpn_once(countries_file=countries_file)
        print("[VPN] change_vpn_once completed.")
    except Exception as e:
        print(f"[VPN] change_vpn_once failed: {e}")
        raise RuntimeError(f"VPN change failed: {e}") from e

# Project root = parent of utils_vpn (or same dir as vpn_rotator when run from root)
def _state_dir():
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vpn_state")
    os.makedirs(d, exist_ok=True)
    return d

def get_state_dir():
    return _state_dir()

def _flag_path(name):
    return os.path.join(_state_dir(), name)

IP_BLOCKED_FLAG = "ip_blocked.flag"
VPN_ROTATING_FLAG = "vpn_rotating.flag"

def wait_if_vpn_rotating(poll_sec=2):
    """Block until vpn_rotating.flag is gone (rotator sets it during VPN change)."""
    path = _flag_path(VPN_ROTATING_FLAG)
    while os.path.exists(path):
        time.sleep(poll_sec)

def on_ip_block(rotate_fn=None):
    """
    When IP block is detected: one process rotates VPN, others wait until rotation is done.
    rotate_fn: callable that changes VPN (e.g. change_vpn_once). If None, only wait for others to clear.
    Blocks until safe to retry (we rotated or someone else did).
    """
    path = _flag_path(IP_BLOCKED_FLAG)
    try:
        with open(path, "x") as f:
            pass
    except FileExistsError:
        # Another process is rotating; wait until they clear the flag
        while os.path.exists(path):
            time.sleep(2)
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
                        print("[VPN] All 3 rotate attempts failed; clearing flag so other processes can retry.")
        else:
            # No VPN in this process; wait then clear so others can proceed
            time.sleep(120)
    finally:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

def set_vpn_rotating():
    path = _flag_path(VPN_ROTATING_FLAG)
    with open(path, "w") as f:
        pass

def clear_vpn_rotating():
    path = _flag_path(VPN_ROTATING_FLAG)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass
