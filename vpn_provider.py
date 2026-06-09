import subprocess
import time
import os
import json
import random
from collections import deque

class VPNManager:
    """
    NordVPN controller for Windows (EXE version)
    Rotates VPN countries by reading from countries.txt.
    Picks a location that is NOT in the last 10 connections (different server each time).
    """

    # How many recent connections to avoid reusing (e.g. India(1), India(2) different; don't repeat last 10)
    LAST_N_AVOID = 10

    def __init__(self, wait_after_connect=3, countries_file="countries.txt"):
        self.wait = wait_after_connect
        # Keep exe path unquoted; we will quote it in the command string.
        self.exe_path = r"C:\Program Files\NordVPN\NordVPN.exe"
        self.countries_file = countries_file
        self.country_index = 0

        # Path to persist last N connections so we avoid them even after restart
        self._history_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "vpn_last_connections.json",
        )

        # Load countries from file
        self.countries = self.load_countries()

        # Last N countries we connected to (so we don't reuse same as last 10),
        # initialised from persisted history if available.
        self._last_connections = self._load_history()

    def load_countries(self):
        """Load country list from countries.txt (one per line; # = comment)."""
        if not os.path.exists(self.countries_file):
            raise FileNotFoundError(
                f"{self.countries_file} not found! Create it with one country per line."
            )

        countries = []
        with open(self.countries_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Strip inline comment
                if "#" in line:
                    line = line.split("#")[0].strip()
                if line:
                    countries.append(line)

        if not countries:
            raise ValueError("countries.txt is empty!")

        return countries

    # ---------- history persistence ----------
    def _load_history(self):
        """
        Load last N connections from JSON file, keep only valid countries,
        and return as deque(maxlen=LAST_N_AVOID).
        """
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path, "r", encoding="utf-8") as f:
                    data = json.load(f) or []
                # keep only entries still present in countries.txt
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

    def run_cmd(self, cmd):
        """Run system command"""
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, shell=True)
            return out.decode()
        except subprocess.CalledProcessError as e:
            return f"[ERROR] {e.output.decode()}"

    def connect(self, country=None):
        """Connect to specific country"""
        if country:
            # IMPORTANT: quote country because many have spaces (e.g. "United States")
            cmd = f"\"{self.exe_path}\" -c -g \"{country}\""
        else:
            cmd = f"\"{self.exe_path}\" -c"

        print(f"[VPN] Connecting to: {country or 'Best Server'}")
        result = self.run_cmd(cmd)
        time.sleep(self.wait)
        return result

    def disconnect(self):
        """Disconnect VPN"""
        print("[VPN] Disconnecting...")
        return self.run_cmd(f"\"{self.exe_path}\" -d")

    def change_country(self, country):
        """Disconnect and then connect to new country"""
        print(f"[VPN] Changing country -> {country}")
        disc = self.disconnect()
        if disc and disc.strip():
            print(disc.strip()[:300])
        time.sleep(2)
        res = self.connect(country)
        if res and res.strip().startswith("[ERROR]"):
            print(res.strip()[:300])
        return res

    def status(self):
        """Check VPN status"""
        return self.run_cmd(f"\"{self.exe_path}\" -s")

    # ----------------------------------------------------------------------
    # Rotate VPN: pick a location NOT in the last 10 connections (different server)
    # ----------------------------------------------------------------------
    def change_vpn(self):
        """
        Connect to a country that is NOT the same as any of the last 10 connections.
        E.g. India(1), India(2) = different; India(36), India(36) = same → we avoid repeating.
        """
        if not self.countries:
            return "[ERROR] No countries loaded."

        last_n_set = set(self._last_connections)

        # Build candidate pool excluding last N used
        candidates = [c for c in self.countries if c not in last_n_set]

        if not candidates:
            # All countries were in last 10 (e.g. file has ≤10 entries) → fall back to full list
            candidates = list(self.countries)
            print(f"\n[VPN ROTATE] All countries were used in last {self.LAST_N_AVOID}; picking from full list.")

        # Choose random country from candidates (jumble order each time)
        chosen = random.choice(candidates)
        print(f"\n[VPN ROTATE] Switching to: {chosen.upper()} (avoiding last {self.LAST_N_AVOID} where possible)")

        self._last_connections.append(chosen)
        self._save_history()
        return self.change_country(chosen)


def change_vpn_once(countries_file=None):
    """
    Change VPN to a new country once (manual one-shot). Use this inside safe_get or
    other code when you need a single rotation (e.g. after IP block).
    countries_file: path to countries.txt, or None to use same folder as this script.
    """
    if countries_file is None:
        countries_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "countries.txt")
    vpn = VPNManager(wait_after_connect=5, countries_file=countries_file)
    return vpn.change_vpn()

# from vpn_manager import VPNManager

# vpn = VPNManager()
# print("testing ...")
# time.sleep(5)
# Each call rotates to the NEXT country in countries.txt
# vpn.change_vpn()
# print("testing ...")
# time.sleep(5)
# vpn.change_vpn()
# print("testing ...")
# time.sleep(5)
# vpn.change_vpn()
# print("Its Completed ...")