#!/usr/bin/env python3
"""
stream_launcher.py: Interactive multi-window session manager for streamSilencer.

Manages the lifecycle of streamSilencer Firefox windows and the background focus
daemon. Runs as an interactive command-line manager in the foreground. Cleanly
terminates all stream windows, the daemon, and window border overrides on exit
(including Ctrl+C or terminal window closure).
"""

import argparse
import cmd
import os
import readline
import shutil
import signal
import subprocess
import sys
import time
from typing import Dict, List, Optional


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


# Predefined streaming site presets
PRESETS = {
    "1": {
        "name": "YouTube",
        "description": "1x YouTube window",
        "urls": ["https://www.youtube.com"],
        "aliases": ["yt", "youtube"],
    },
    "2": {
        "name": "YouTube TV",
        "description": "1x YouTube TV window",
        "urls": ["https://tv.youtube.com"],
        "aliases": ["yttv", "youtubetv"],
    },
    "3": {
        "name": "Prime Video",
        "description": "1x Prime Video window",
        "urls": ["https://www.primevideo.com"],
        "aliases": ["prime", "primevideo"],
    },
    "4": {
        "name": "3x YouTube TV",
        "description": "3x YouTube TV windows (multiview)",
        "urls": ["https://tv.youtube.com", "https://tv.youtube.com", "https://tv.youtube.com"],
        "aliases": ["3yttv", "triple"],
    },
}


def resolve_preset_urls(arg: str) -> Optional[List[str]]:
    """Resolve a shortcut or preset name into a list of URLs."""
    key = arg.strip().lower()
    if key in PRESETS:
        return PRESETS[key]["urls"]
    for p in PRESETS.values():
        if key in p["aliases"]:
            return p["urls"]
    return None


def clear_hyprland_borders(class_name: str = "streamSilencer") -> None:
    """Reset Hyprland border overrides on all streamSilencer windows."""
    code = f"""
    for _, w in ipairs(hl.get_windows()) do
        if w.class == '{class_name}' then
            hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'inactive_border_color', value = '-1' }}))
            hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'active_border_color', value = '-1' }}))
        end
    end
    """
    try:
        subprocess.run(["hyprctl", "eval", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class StreamSessionManager:
    """Coordinates lifecycle of streamSilencer Firefox instances and focus daemon."""

    def __init__(self, profile: str = "streamSilencer", start_daemon: bool = True):
        self.profile = profile
        self.base_profile_dir = find_firefox_profile_dir(profile)
        self.sessions_base = os.path.expanduser(f"~/.mozilla/firefox/{profile}_sessions")
        os.makedirs(self.sessions_base, exist_ok=True)

        self.windows: Dict[int, Dict] = {}
        self.daemon_proc: Optional[subprocess.Popen] = None
        self.shutting_down = False
        self.start_daemon_enabled = start_daemon

        # Ensure master profile directory exists
        if not os.path.exists(self.base_profile_dir):
            os.makedirs(self.base_profile_dir, exist_ok=True)

    def start_focus_daemon(self) -> None:
        """Launch the background stream_silencer focus daemon."""
        if self.daemon_proc is not None and self.daemon_proc.poll() is None:
            return

        subprocess.run(["pkill", "-f", "stream_silencer.py --daemon"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        daemon_script = os.path.join(os.path.dirname(__file__), "stream_silencer.py")
        print("Starting streamSilencer focus daemon...")
        self.daemon_proc = subprocess.Popen(
            [sys.executable, daemon_script, "--daemon"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop_focus_daemon(self) -> None:
        """Stop the background focus daemon."""
        if self.daemon_proc is not None:
            if self.daemon_proc.poll() is None:
                self.daemon_proc.terminate()
                try:
                    self.daemon_proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    self.daemon_proc.kill()
            self.daemon_proc = None

    def add_window(self, url: str = "about:blank", reset: bool = False) -> int:
        """Launch a new stream window into the pool."""
        # Find next available instance ID
        inst_id = 1
        while inst_id in self.windows and self.windows[inst_id]["proc"].poll() is None:
            inst_id += 1

        inst_dir = os.path.join(self.sessions_base, f"inst_{inst_id}")
        # Always clone fresh from master profile to ensure identical logins and cookies
        print(f"Duplicating fresh profile for window #{inst_id}...")
        duplicate_base_profile(self.base_profile_dir, inst_dir)

        # Clear stale locks
        for lk in [".parentlock", "lock"]:
            p = os.path.join(inst_dir, lk)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        cmd = [
            "firefox",
            "--name", self.profile,
            "--profile", inst_dir,
            "--new-instance",
            url,
        ]

        env = os.environ.copy()
        env["PULSE_PROP"] = f"application.name={self.profile} application.id={self.profile} media.software={self.profile}"

        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self.windows[inst_id] = {
            "proc": proc,
            "url": url,
            "dir": inst_dir,
            "inst_id": inst_id,
            "launched": time.time(),
        }

        print(f"Launched window #{inst_id} (PID {proc.pid}) -> {url}")
        return inst_id

    def close_window(self, inst_id: int) -> bool:
        """Close an individual stream window by its ID."""
        info = self.windows.get(inst_id)
        if not info:
            print(f"Window #{inst_id} not found.")
            return False

        proc = info["proc"]
        if proc.poll() is None:
            print(f"Closing window #{inst_id} (PID {proc.pid})...")
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Clean locks
        inst_dir = info["dir"]
        for lk in [".parentlock", "lock"]:
            p = os.path.join(inst_dir, lk)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass

        del self.windows[inst_id]
        print(f"Window #{inst_id} closed.")
        return True

    def prune_dead_windows(self) -> None:
        """Remove windows that exited independently."""
        dead = []
        for inst_id, info in self.windows.items():
            if info["proc"].poll() is not None:
                dead.append(inst_id)
        for inst_id in dead:
            del self.windows[inst_id]

    def list_windows(self) -> None:
        """Display an overview of active stream windows and mute states."""
        self.prune_dead_windows()
        if not self.windows:
            print("No active stream windows in pool.")
            return

        from stream_silencer import (
            get_hyprland_active_window,
            get_hyprland_windows,
            get_pipewire_audio_streams,
            get_process_ancestors,
            is_stream_muted,
        )

        active_win = get_hyprland_active_window()
        active_addr = active_win.get("address", "")
        hl_windows = get_hyprland_windows()
        streams = get_pipewire_audio_streams()

        print("\n=== Active streamSilencer Windows ===")
        print(f"{'ID':<4} {'PID':<8} {'Audio State':<14} {'Hyprland Title':<38} {'URL'}")
        print("-" * 80)

        for inst_id in sorted(self.windows.keys()):
            info = self.windows[inst_id]
            pid = info["proc"].pid

            # Find matching Hyprland window
            matched_hl = None
            for w in hl_windows:
                if w.get("pid") == pid or pid in get_process_ancestors(w.get("pid", 0)):
                    matched_hl = w
                    break

            # Find matching audio stream
            matched_stream = None
            for s in streams:
                s_pid = s.get("pid")
                if s_pid == pid or pid in get_process_ancestors(s_pid or 0):
                    matched_stream = s
                    break

            # Determine audio status
            if matched_stream:
                node_id = matched_stream["id"]
                muted = is_stream_muted(node_id)
                audio_status = "[MUTED] (Red)" if muted else "[PLAYING] (Green)"
            else:
                audio_status = "[IDLE / NO AUDIO]"

            # Format title
            title = matched_hl.get("title", "(Loading...)") if matched_hl else "(Unmapped)"
            if len(title) > 36:
                title = title[:33] + "..."

            is_focused = " *" if (matched_hl and matched_hl.get("address") == active_addr) else "  "
            url_str = info["url"]
            if len(url_str) > 30:
                url_str = url_str[:27] + "..."

            print(f"{inst_id:<2}{is_focused:<2} {pid:<8} {audio_status:<14} {title:<38} {url_str}")
        print("-" * 80)
        print("(* indicates focused window)\n")

    def focus_window(self, inst_id: int) -> bool:
        """Switch Hyprland focus directly to the specified stream window."""
        info = self.windows.get(inst_id)
        if not info:
            print(f"Window #{inst_id} not found.")
            return False

        pid = info["proc"].pid
        code = f"""
        for _, w in ipairs(hl.get_windows()) do
            if w.pid == {pid} or w.class == '{self.profile}' then
                hl.dispatch(hl.dsp.focus({{ window = w }}))
                break
            end
        end
        """
        try:
            subprocess.run(["hyprctl", "eval", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"Focused window #{inst_id} (PID {pid}).")
            return True
        except Exception as exc:
            print(f"Failed to focus window #{inst_id}: {exc}")
            return False

    def reset_all_sessions(self) -> None:
        """Re-clone all session directories fresh from the master profile."""
        print("Re-cloning all session instances from master profile...")
        urls = {inst_id: info["url"] for inst_id, info in list(self.windows.items())}
        for inst_id in list(self.windows.keys()):
            self.close_window(inst_id)

        for inst_id, url in urls.items():
            self.add_window(url=url, reset=True)
        print("All session profiles reset and re-launched.")

    def sync_credentials(self, source_profile: str = "dchan-personal", silent: bool = False) -> None:
        """Sync latest login cookies, passwords, and storage from personal profile."""
        src_dir = find_firefox_profile_dir(source_profile)
        if not os.path.exists(src_dir):
            if not silent:
                print(f"Source profile '{source_profile}' not found.")
            return

        if not silent:
            print(f"Syncing authentication and cookies from '{source_profile}'...")
        for f in ["cookies.sqlite", "cookies.sqlite-wal", "logins.json", "logins.db", "key4.db", "cert9.db"]:
            sp = os.path.join(src_dir, f)
            dp = os.path.join(self.base_profile_dir, f)
            if os.path.exists(sp):
                try:
                    shutil.copy2(sp, dp)
                except Exception:
                    pass

        storage_src = os.path.join(src_dir, "storage")
        storage_dst = os.path.join(self.base_profile_dir, "storage")
        if os.path.exists(storage_src):
            try:
                if os.path.exists(storage_dst):
                    shutil.rmtree(storage_dst)
                shutil.copytree(storage_src, storage_dst, ignore=shutil.ignore_patterns("*.lock", ".parentlock"))
            except Exception:
                pass

        if not silent:
            print("Authentication credentials synced to master profile.")

    def shutdown(self) -> None:
        """Cleanly terminate all windows, stop daemon, and restore Hyprland borders."""
        if self.shutting_down:
            return
        self.shutting_down = True
        print("\nShutting down streamSilencer session manager...")

        # Close all active stream windows
        for inst_id, info in list(self.windows.items()):
            proc = info["proc"]
            if proc.poll() is None:
                print(f"  Terminating window #{inst_id} (PID {proc.pid})...")
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()

        self.windows.clear()

        # Stop focus daemon
        print("  Stopping focus daemon...")
        self.stop_focus_daemon()

        # Reset Hyprland border colors
        print("  Resetting Hyprland window borders...")
        clear_hyprland_borders(self.profile)

        # Clear remaining lock files
        for item in os.listdir(self.sessions_base):
            p = os.path.join(self.sessions_base, item)
            if os.path.isdir(p):
                for lk in [".parentlock", "lock"]:
                    lf = os.path.join(p, lk)
                    if os.path.exists(lf):
                        try:
                            os.remove(lf)
                        except Exception:
                            pass

        print("Cleanup complete. Goodbye!")


class StreamCLI(cmd.Cmd):
    """Interactive command-line interface for managing streamSilencer windows."""

    intro = (
        "\n======================================================\n"
        " streamSilencer Session Controller                    \n"
        " Type 'help' or '?' for commands. Press Ctrl+C to quit\n"
        "======================================================\n\n"
        "Quick Presets / Shortcuts:\n"
        "  [1] or 'yt'     : 1x YouTube window\n"
        "  [2] or 'yttv'   : 1x YouTube TV window\n"
        "  [3] or 'prime'  : 1x Prime Video window\n"
        "  [4] or '3yttv'  : 3x YouTube TV windows (multiview)\n"
    )
    prompt = "streamSilencer> "

    def __init__(self, manager: StreamSessionManager):
        super().__init__()
        self.manager = manager

    def default(self, line: str) -> None:
        """Handle multi-argument shortcuts (e.g. '1 1' or 'yt prime') and direct URLs."""
        tokens = line.strip().split()
        if not tokens:
            return

        # Check if all tokens can be interpreted as presets, shortcuts, or URLs
        target_urls = []
        all_resolved = True
        for t in tokens:
            p_urls = resolve_preset_urls(t)
            if p_urls is not None:
                target_urls.extend(p_urls)
            elif t.startswith("http://") or t.startswith("https://") or t.startswith("www."):
                url = t if (t.startswith("http://") or t.startswith("https://")) else "https://" + t
                target_urls.append(url)
            else:
                all_resolved = False
                break

        if all_resolved and target_urls:
            print(f"Launching {len(target_urls)} stream window(s)...")
            for u in target_urls:
                self.manager.add_window(url=u)
                time.sleep(0.3)
            return

        print(f"Unknown command: '{line}'. Type 'help' or '?' for available commands.")

    def do_preset(self, arg: str) -> None:
        """preset <1-4|name ...>: Launch predefined streaming windows (e.g. 'preset 4' or 'preset 1 1')."""
        tokens = arg.strip().split()
        if not tokens:
            self.do_presets("")
            return

        target_urls = []
        for t in tokens:
            p_urls = resolve_preset_urls(t)
            if p_urls is None:
                print(f"Unknown preset: '{t}'. Type 'presets' to list available shortcuts.")
                return
            target_urls.extend(p_urls)

        print(f"Launching preset(s) ({len(target_urls)} window(s))...")
        for u in target_urls:
            self.manager.add_window(url=u)
            time.sleep(0.3)

    def do_presets(self, arg: str) -> None:
        """presets: Display list of predefined streaming site shortcuts."""
        print("\nAvailable Presets:")
        print("==================")
        for k, p in sorted(PRESETS.items()):
            aliases_str = ", ".join(f"'{a}'" for a in p["aliases"])
            print(f"  [{k}] {p['name']:<16} : {p['description']:<36} (Aliases: {aliases_str})")
        print("\nTip: Type '1', '2', '3', or '4' directly (or chain them like '1 1' or '1 2') to launch.\n")

    def do_add(self, arg: str) -> None:
        """add [URL|preset ...]: Launch window(s) with URL or preset name (e.g. 'add 1 1' or 'add yttv prime')."""
        tokens = arg.strip().split()
        if not tokens:
            self.manager.add_window(url="about:blank")
            return

        target_urls = []
        for t in tokens:
            preset_urls = resolve_preset_urls(t)
            if preset_urls is not None:
                target_urls.extend(preset_urls)
            elif t.startswith("http://") or t.startswith("https://") or t == "about:blank":
                target_urls.append(t)
            elif "." in t or t.startswith("www."):
                target_urls.append("https://" + t if not t.startswith("http") else t)
            else:
                target_urls.append("https://" + t)

        print(f"Adding {len(target_urls)} stream window(s)...")
        for u in target_urls:
            self.manager.add_window(url=u)
            time.sleep(0.3)

    def do_list(self, arg: str) -> None:
        """list: Display all active stream windows, titles, and audio mute states."""
        self.manager.list_windows()

    def do_ls(self, arg: str) -> None:
        """ls: Alias for 'list'."""
        self.do_list(arg)

    def do_status(self, arg: str) -> None:
        """status: Alias for 'list'."""
        self.do_list(arg)

    def do_close(self, arg: str) -> None:
        """close <ID ...>: Terminate one or more active stream windows by ID (e.g. 'close 1' or 'close 1 2')."""
        tokens = arg.strip().split()
        if not tokens:
            print("Usage: close <ID> [ID ...]")
            return

        for t in tokens:
            try:
                inst_id = int(t)
                self.manager.close_window(inst_id)
            except ValueError:
                print(f"Invalid window ID: '{t}'. Must be an integer.")

    def do_rm(self, arg: str) -> None:
        """rm <ID ...>: Alias for 'close'."""
        self.do_close(arg)

    def do_focus(self, arg: str) -> None:
        """focus <ID>: Bring a specific stream window into Hyprland focus (e.g. 'focus 1')."""
        arg = arg.strip()
        if not arg:
            print("Usage: focus <ID>")
            return
        try:
            inst_id = int(arg)
            self.manager.focus_window(inst_id)
        except ValueError:
            print(f"Invalid window ID: '{arg}'. Must be an integer.")

    def do_reset(self, arg: str) -> None:
        """reset: Re-clone all active stream windows from the master profile."""
        self.manager.reset_all_sessions()

    def do_sync(self, arg: str) -> None:
        """sync: Synchronize latest cookies and logins from your personal Firefox profile."""
        self.manager.sync_credentials()

    def do_edit(self, arg: str) -> None:
        """edit: Open the master profile in Firefox to configure themes, logins, or extensions."""
        base_dir = self.manager.base_profile_dir
        print(f"Opening master '{self.manager.profile}' profile for configuration...")
        print("Make changes, then close the browser window. New windows will inherit these settings.")
        cmd = ["firefox", "--name", self.manager.profile, "--profile", base_dir, "--new-instance"]
        env = os.environ.copy()
        env["PULSE_PROP"] = f"application.name={self.manager.profile} application.id={self.manager.profile}"
        subprocess.call(cmd, env=env)

    def do_clear(self, arg: str) -> None:
        """clear: Clear the terminal screen."""
        os.system("clear")

    def do_help(self, arg: str) -> None:
        """help [command]: Display command reference in vertical list format."""
        if arg.strip():
            super().do_help(arg.strip())
            return

        print("\nDocumented commands:")
        print("====================")
        print("  '1' / 'yt'            : Open 1x YouTube window (supports chaining: '1 1', '1 2')")
        print("  '2' / 'yttv'          : Open 1x YouTube TV window")
        print("  '3' / 'prime'         : Open 1x Prime Video window")
        print("  '4' / '3yttv'         : Open 3x YouTube TV windows (multiview)")
        print("  'add [url|preset ...]' : Launch window(s) with URLs or presets (e.g. 'add yt prime')")
        print("  'presets'             : Display details of all streaming site presets")
        print("  'list' / 'ls'         : Display active stream windows, titles, PIDs, and audio mute states")
        print("  'close <id ...>'      : Terminate stream window(s) by ID (e.g. 'close 1 2', alias: 'rm')")
        print("  'focus <id>'          : Switch Hyprland focus directly to that stream window")
        print("  'reset'               : Re-clone all active session instances fresh from the master profile")
        print("  'sync'                : Sync latest cookies and logins from your personal Firefox profile")
        print("  'edit'                : Open master profile directly to configure themes, logins, or extensions")
        print("  'clear'               : Clear the terminal screen")
        print("  'help' / '?'          : Show this command reference")
        print("  'quit' / 'exit'       : Terminate all windows, stop focus daemon, and exit (alias: 'q')\n")

    def do_quit(self, arg: str) -> bool:
        """quit: Close all stream windows, stop the focus daemon, and exit."""
        return True

    def do_exit(self, arg: str) -> bool:
        """exit: Alias for 'quit'."""
        return True

    def do_q(self, arg: str) -> bool:
        """q: Alias for 'quit'."""
        return True

    def emptyline(self) -> bool:
        """Do nothing on empty line."""
        return False


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
        description="Interactive session manager and launcher for streamSilencer."
    )
    parser.add_argument(
        "-n",
        "--count",
        type=int,
        default=0,
        help="Initial number of stream windows to open (default: 0).",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Optional initial stream URLs or preset shortcuts (e.g. '1', '4', 'yttv').",
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
        help="Re-clone runtime session instances fresh from the master profile.",
    )
    parser.add_argument(
        "--no-daemon",
        action="store_true",
        help="Do not start the streamSilencer background focus daemon.",
    )
    parser.add_argument(
        "--no-sync",
        action="store_true",
        help="Do not automatically sync cookies and logins from personal profile on launch.",
    )

    args = parser.parse_args()

    if args.edit:
        edit_master_profile(args.profile)
        return

    manager = StreamSessionManager(
        profile=args.profile,
        start_daemon=(not args.no_daemon),
    )

    # Automatically sync latest authentication and cookies from personal profile
    if not args.no_sync:
        manager.sync_credentials(silent=False)

    # Setup signal handlers for graceful teardown on SIGINT, SIGTERM, SIGHUP
    def handle_signal(signum, frame):
        manager.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGHUP, handle_signal)

    # Start focus daemon
    if not args.no_daemon:
        manager.start_focus_daemon()

    # Expand any preset arguments from command line
    target_urls: List[str] = []
    for u in args.urls:
        p_urls = resolve_preset_urls(u)
        if p_urls is not None:
            target_urls.extend(p_urls)
        else:
            target_urls.append(u)

    # Pad with about:blank only if -n was explicitly specified higher than URL count
    if args.count > len(target_urls):
        while len(target_urls) < args.count:
            target_urls.append("about:blank")

    # Launch initial windows if requested
    for url in target_urls:
        manager.add_window(url=url, reset=args.reset)
        time.sleep(0.3)

    # Enter interactive CLI loop
    cli = StreamCLI(manager)
    try:
        cli.cmdloop()
    except KeyboardInterrupt:
        pass
    finally:
        manager.shutdown()


if __name__ == "__main__":
    main()
