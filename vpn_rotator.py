"""
Rotate VPN country every N minutes (e.g. 3 or 5).

=== HOW TO RUN ===
  cd c:\\Users\\Raj Kumar\\Desktop\\mining_cursor
  python vpn_rotator.py              # every 30 min (default)
  python vpn_rotator.py 5            # every 5 min. Or use safe_get(..., use_vpn_rotator=False) for change only on IP block.

=== HOW TO STOP ===
  In the terminal where vpn_rotator is running: press Ctrl+C
  VPN stays connected to the last country; only the rotator stops.

=== HOW TO PAUSE / RESUME ===
  Pause:  Create file  vpn_rotator.pause  in this folder (script will stop changing country until file is removed)
  Resume: Delete the file  vpn_rotator.pause
  (Or just Stop with Ctrl+C and run the script again later to resume rotation.)
"""
import time
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
# If utils_vpn / offline_utils_vpn live in C:/common_code (e.g. vpn_rotator beside wiley, utils in common_code)
_common_code = r"C:\common_code"
if os.path.isdir(_common_code) and _common_code not in sys.path:
    sys.path.insert(0, _common_code)

try:
    from utils_vpn.vpn_state import set_vpn_rotating, clear_vpn_rotating
except Exception:
    try:
        from utils_vpn.offline_utils_vpn import set_vpn_rotating, clear_vpn_rotating
    except Exception:
        from offline_utils_vpn import set_vpn_rotating, clear_vpn_rotating
try:
    from vpn_provider import VPNManager
    def _do_vpn_rotate():
        VPNManager(wait_after_connect=5, countries_file="countries.txt").change_vpn()
except Exception:
    try:
        from utils_vpn.offline_utils_vpn import change_vpn_on_block
    except Exception:
        from offline_utils_vpn import change_vpn_on_block
    _do_vpn_rotate = change_vpn_on_block

PAUSE_FILE = "vpn_rotator.pause"


def is_paused():
    return os.path.exists(PAUSE_FILE)


def wait_if_paused():
    """If pause file exists, wait until it is removed (check every 10 sec)."""
    while is_paused():
        print(f"[VPN ROTATOR] PAUSED (remove {PAUSE_FILE} to resume)")
        time.sleep(10)
    print(f"[VPN ROTATOR] Resumed.\n")


def main():
    # Interval in minutes (default 30 for auto-rotate)
    interval_min = 30
    if len(sys.argv) > 1:
        try:
            interval_min = int(sys.argv[1])
            if interval_min < 1:
                interval_min = 30
        except ValueError:
            interval_min = 30

    interval_sec = interval_min * 60
    print(f"[VPN ROTATOR] Starting: change country every {interval_min} minute(s)")
    print(f"[VPN ROTATOR] Drivers pause during rotate. Stop: Ctrl+C\n")

    try:
        while True:
            wait_if_paused()
            set_vpn_rotating()
            try:
                print("[VPN ROTATOR] Rotating VPN (drivers waiting)...")
                time.sleep(10)
                _do_vpn_rotate()
                time.sleep(5)
            finally:
                clear_vpn_rotating()
            print(f"[VPN ROTATOR] Next change in {interval_min} min ...\n")
            for _ in range(interval_sec):
                if is_paused():
                    break
                time.sleep(1)
    except KeyboardInterrupt:
        clear_vpn_rotating()
        print("\n[VPN ROTATOR] Stopped. VPN stays connected to last country.")


if __name__ == "__main__":
    main()
