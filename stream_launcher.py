#!/usr/bin/env python3
"""
stream_launcher.py: Multi-window stream session launcher for streamSilencer.

Launches X stream-compatible Firefox windows duplicated directly from the master
streamSilencer profile. Automatically applies pseudo-fullscreen in Hyprland and
coordinates with streamSilencer for focus-based muting.
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
        return os.path.join(mozilla_dir, profile_name)

    with open(profiles_ini, "r", encoding="utf-8") as f:
        content = f.read()

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
    return os.path.join(mozilla_dir, profile_name)


def duplicate_base_profile(base_dir: str, target_dir: str) -> None:
    """Duplicate base profile directly into an isolated runtime instance."""
    ignore = shutil.ignore_patterns("*.marker", "*.lock", "lock", ".parentlock", "cache", "cache2", "startupCache")
    if os.path.exists(target_dir):
        try:
            shutil.rmtree(target_dir)
        except Exception:
            pass
    shutil.copytree(base_dir, target_dir, ignore=ignore)
    for lk in [".parentlock", "lock"]:
        p = os.path.join(target_dir, lk)
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


def launch_windows(
    count: int,
    urls: List[str],
    profile: str = "streamSilencer",
    reset: bool = False,
) -> None:
    """Launch count windows, each duplicated directly from the master profile."""
    target_urls = []
    for i in range(count):
        if i < len(urls):
            target_urls.append(urls[i])
        else:
            target_urls.append("about:blank")

    base_profile_dir = find_firefox_profile_dir(profile)
    if not os.path.exists(base_profile_dir):
        os.makedirs(base_profile_dir, exist_ok=True)

    sessions_base = os.path.expanduser(f"~/.mozilla/firefox/{profile}_sessions")
    os.makedirs(sessions_base, exist_ok=True)

    print(f"Launching {count} stream window(s) from '{profile}'...")

    for i in range(count):
        url = target_urls[i]
        inst_dir = os.path.join(sessions_base, f"inst_{i + 1}")

        # Duplicate from master profile if new or explicitly requested via --reset
        if reset or not os.path.exists(inst_dir):
            duplicate_base_profile(base_profile_dir, inst_dir)

        cmd = [
            "firefox",
            "--name", profile,
            "--profile", inst_dir,
            "--new-instance",
            url,
        ]

        print(f"  Window {i + 1}: {' '.join(cmd)}")
        env = os.environ.copy()
        env["PULSE_PROP"] = f"application.name={profile} application.id={profile} media.software={profile}"
        subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.5)

    print("All stream windows launched.")


def edit_master_profile(profile: str = "streamSilencer") -> None:
    """Launch the master profile directly to configure themes, logins, or extensions."""
    base_dir = find_firefox_profile_dir(profile)
    print(f"Opening master '{profile}' profile for configuration...")
    print(f"Profile directory: {base_dir}")
    print("Make your changes, then close the browser. Future stream windows will duplicate these settings.")
    cmd = ["firefox", "--name", profile, "--profile", base_dir, "--new-instance"]
    env = os.environ.copy()
    env["PULSE_PROP"] = f"application.name={profile} application.id={profile} media.software={profile}"
    subprocess.call(cmd, env=env)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Launch X stream-compatible Firefox windows duplicated from streamSilencer."
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
        help="Firefox profile name to duplicate (default: 'streamSilencer').",
    )
    parser.add_argument(
        "--edit",
        action="store_true",
        help="Open the master profile directly to configure themes, extensions, or logins.",
    )
    parser.add_argument(
        "-r",
        "--reset",
        action="store_true",
        help="Re-clone all runtime session instances fresh from the master profile.",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Do not start the streamSilencer background focus daemon.",
    )

    args = parser.parse_args()

    if args.edit:
        edit_master_profile(args.profile)
        return

    # Automatically manage focus daemon unless opted out
    if not args.no_daemon:
        subprocess.run(["pkill", "-f", "stream_silencer.py --daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        daemon_script = os.path.join(os.path.dirname(__file__), "stream_silencer.py")
        print("Starting streamSilencer focus daemon...")
        subprocess.Popen(
            [sys.executable, daemon_script, "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    count = max(args.count, len(args.urls))
    launch_windows(
        count=count,
        urls=args.urls,
        profile=args.profile,
        reset=args.reset,
    )


if __name__ == "__main__":
    main()
