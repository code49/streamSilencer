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


def sync_theme_and_extensions(sessions_base: str, count: int) -> None:
    """Propagate active theme and installed extensions from inst_1 to all other instances."""
    inst_1 = os.path.join(sessions_base, "inst_1")
    if not os.path.exists(inst_1):
        return

    active_theme = None
    inst_1_prefs = os.path.join(inst_1, "prefs.js")
    if os.path.exists(inst_1_prefs):
        with open(inst_1_prefs, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if "extensions.activeThemeID" in line:
                    active_theme = line.strip()
                    break

    for i in range(1, count):
        target_inst = os.path.join(sessions_base, f"inst_{i + 1}")
        if not os.path.exists(target_inst):
            continue

        if active_theme:
            user_js = os.path.join(target_inst, "user.js")
            lines = []
            if os.path.exists(user_js):
                try:
                    os.chmod(user_js, 0o644)
                    with open(user_js, "r", encoding="utf-8", errors="ignore") as f:
                        lines = [l for l in f if "extensions.activeThemeID" not in l]
                except Exception:
                    pass
            lines.append(f"{active_theme}\n")
            try:
                with open(user_js, "w", encoding="utf-8") as f:
                    f.writelines(lines)
            except Exception:
                pass

        src_ext = os.path.join(inst_1, "extensions")
        dst_ext = os.path.join(target_inst, "extensions")
        if os.path.exists(src_ext) and not os.path.exists(dst_ext):
            try:
                shutil.copytree(src_ext, dst_ext, dirs_exist_ok=True)
            except Exception:
                pass


def launch_windows(
    count: int,
    urls: List[str],
    profile: str = "streamSilencer",
    isolated: bool = False,
    reset_sessions: bool = False,
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
    if not base_profile_dir:
        base_profile_dir = os.path.expanduser(f"~/.mozilla/firefox/{profile}")
        os.makedirs(base_profile_dir, exist_ok=True)
        user_js = os.path.join(base_profile_dir, "user.js")
        if not os.path.exists(user_js):
            with open(user_js, "w", encoding="utf-8") as f:
                f.write('user_pref("services.sync.engine.addons", false);\n')

    sessions_base = os.path.expanduser(f"~/.mozilla/firefox/{profile}_sessions")
    os.makedirs(sessions_base, exist_ok=True)

    print(f"Launching {count} stream window(s)...")

    if isolated:
        sync_theme_and_extensions(sessions_base, count)

    for i in range(count):
        url = target_urls[i]
        cmd = ["firefox", "--name", profile]

        if isolated:
            # Dedicated persistent session instance to guarantee separate PIDs for PipeWire
            inst_dir = os.path.join(sessions_base, f"inst_{i + 1}")
            if reset_sessions and os.path.exists(inst_dir):
                shutil.rmtree(inst_dir)
            is_new = not os.path.exists(inst_dir)
            os.makedirs(inst_dir, exist_ok=True)
            if is_new and base_profile_dir and os.path.exists(base_profile_dir):
                # Initial seed of core auth, cookies, and preferences
                for item in [
                    "prefs.js", "user.js", "cookies.sqlite", "cookies.sqlite-wal",
                    "logins.json", "logins.db", "key4.db", "cert9.db", "storage.sqlite"
                ]:
                    src = os.path.join(base_profile_dir, item)
                    if os.path.exists(src):
                        shutil.copy2(src, os.path.join(inst_dir, item))
                # Copy storage directory for local web sessions
                src_storage = os.path.join(base_profile_dir, "storage")
                dst_storage = os.path.join(inst_dir, "storage")
                if os.path.exists(src_storage) and not os.path.exists(dst_storage):
                    try:
                        shutil.copytree(src_storage, dst_storage)
                    except Exception:
                        pass

                # Copy Widevine and media plugins for DRM (YouTube TV, sports streams)
                for gmp in ["gmp", "gmp-gmpopenh264", "gmp-widevinecdm"]:
                    src_gmp = os.path.join(base_profile_dir, gmp)
                    if not os.path.exists(src_gmp):
                        fallback_gmp = os.path.expanduser(f"~/.mozilla/firefox/dchan-personal/{gmp}")
                        if os.path.exists(fallback_gmp):
                            src_gmp = fallback_gmp
                    dst_gmp = os.path.join(inst_dir, gmp)
                    if os.path.exists(src_gmp) and not os.path.exists(dst_gmp):
                        try:
                            shutil.copytree(src_gmp, dst_gmp)
                        except Exception:
                            pass

                # Ensure EME / Widevine preferences are set in instance user.js
                inst_user_js = os.path.join(inst_dir, "user.js")
                if os.path.exists(inst_user_js):
                    try:
                        os.chmod(inst_user_js, 0o644)
                        with open(inst_user_js, "a", encoding="utf-8") as f:
                            f.write('\nuser_pref("media.eme.enabled", true);\nuser_pref("media.gmp-widevinecdm.enabled", true);\nuser_pref("media.gmp-widevinecdm.visible", true);\n')
                    except Exception:
                        pass

            cmd.extend(["--profile", inst_dir, "--new-instance", url])
        else:
            # Use direct directory path to bypass profiles.ini
            if i == 0:
                cmd.extend(["--profile", base_profile_dir, url])
            else:
                cmd.extend(["--profile", base_profile_dir, "-new-window", url])

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
        "--shared-profile",
        action="store_true",
        help="Use a single shared Firefox instance instead of isolated instances.",
    )
    parser.add_argument(
        "--reset-sessions",
        action="store_true",
        help="Clear existing session profile data and re-seed from base profile.",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Do not start the streamSilencer background focus daemon.",
    )

    args = parser.parse_args()

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

    # Determine window count
    count = max(args.count, len(args.urls))
    launch_windows(
        count=count,
        urls=args.urls,
        profile=args.profile,
        isolated=(not args.shared_profile),
        reset_sessions=args.reset_sessions,
    )


if __name__ == "__main__":
    main()
