#!/usr/bin/env python3
"""
stream_launcher.py: Multi-window stream session launcher for streamSilencer.

Launches X number of stream-compatible Firefox windows configured with the
streamSilencer profile and Wayland app_id/class. Automatically applies
pseudo-fullscreen in Hyprland and coordinates with streamSilencer for focus muting.
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from typing import List


def find_firefox_profile_dir(profile_name: str) -> str:
    """Find the path to a given Firefox profile directory."""
    mozilla_dir = os.path.expanduser("~/.mozilla/firefox")
    profiles_ini = os.path.join(mozilla_dir, "profiles.ini")
    if not os.path.exists(profiles_ini):
        return ""

    with open(profiles_ini, "r", encoding="utf-8") as f:
        content = f.read()

    # Simple section parse
    sections = content.split("[Profile")
    for sec in sections[1:]:
        lines = sec.splitlines()
        p_name = ""
        p_path = ""
        is_rel = True
        for line in lines:
            line = line.strip()
            if line.startswith("Name="):
                p_name = line.split("=", 1)[1]
            elif line.startswith("Path="):
                p_path = line.split("=", 1)[1]
            elif line.startswith("IsRelative="):
                is_rel = line.split("=", 1)[1] == "1"

        if p_name == profile_name:
            if is_rel:
                return os.path.join(mozilla_dir, p_path)
            return p_path
    return ""


def launch_windows(
    count: int,
    urls: List[str],
    profile: str = "streamSilencer",
    isolated: bool = False,
) -> None:
    """Launch count windows with the streamSilencer profile."""
    # Ensure default URL if not provided
    target_urls = []
    for i in range(count):
        if i < len(urls):
            target_urls.append(urls[i])
        else:
            target_urls.append("about:blank")

    base_profile_dir = find_firefox_profile_dir(profile)
    tmp_base = f"/tmp/{profile}_sessions"

    print(f"Launching {count} stream window(s)...")

    for i in range(count):
        url = target_urls[i]
        cmd = ["firefox", "--name", profile]

        if isolated:
            # Create a dedicated profile copy to guarantee separate PIDs for PipeWire
            inst_dir = os.path.join(tmp_base, f"inst_{i + 1}")
            if os.path.exists(inst_dir):
                shutil.rmtree(inst_dir)
            if base_profile_dir and os.path.exists(base_profile_dir):
                # Copy core auth and preferences without bulky caches
                os.makedirs(inst_dir, exist_ok=True)
                for item in ["prefs.js", "logins.json", "key4.db", "cert9.db"]:
                    src = os.path.join(base_profile_dir, item)
                    if os.path.exists(src):
                        shutil.copy2(src, os.path.join(inst_dir, item))
            else:
                os.makedirs(inst_dir, exist_ok=True)

            cmd.extend(["--profile", inst_dir, "--new-instance", url])
        else:
            # Standard single-instance multi-window mode
            cmd.extend(["-P", profile, "-new-window", url])

        print(f"  Window {i + 1}: {' '.join(cmd)}")
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        # Brief pause to allow Wayland compositor to map window cleanly
        time.sleep(0.5)

    print("All stream windows launched.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch X stream-compatible Firefox windows with streamSilencer."
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=2,
        help="Number of windows to launch (default: 2).",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Optional stream URLs to open in each window.",
    )
    parser.add_argument(
        "--profile",
        default="streamSilencer",
        help="Firefox profile name to launch (default: 'streamSilencer').",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Run each window in an isolated profile instance with a unique PID.",
    )
    parser.add_argument(
        "--start-daemon",
        action="store_true",
        help="Also launch the streamSilencer background focus daemon.",
    )

    args = parser.parse_args()

    # Determine window count
    count = max(args.count, len(args.urls))
    launch_windows(
        count=count,
        urls=args.urls,
        profile=args.profile,
        isolated=args.isolated,
    )

    if args.start_daemon:
        daemon_script = os.path.join(os.path.dirname(__file__), "stream_silencer.py")
        print("Starting streamSilencer daemon...")
        subprocess.Popen(
            [sys.executable, daemon_script, "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


if __name__ == "__main__":
    main()
