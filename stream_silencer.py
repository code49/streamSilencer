#!/usr/bin/env python3
"""
streamSilencer: Focus-based audio stream manager for Hyprland and PipeWire.

Monitors Hyprland window focus events and dynamically manages PipeWire audio streams.
Ensures that only the focused window plays audio, while other managed windows are muted.
Uses idempotent `wpctl set-mute` calls.
"""

import argparse
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set

# Set up clean logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("streamSilencer")


def get_hyprland_socket_path() -> str:
    """Resolve the active Hyprland socket2 path."""
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not signature:
        raise RuntimeError("HYPRLAND_INSTANCE_SIGNATURE environment variable not set.")
    socket_path = os.path.join(xdg_runtime, "hypr", signature, ".socket2.sock")
    if not os.path.exists(socket_path):
        raise FileNotFoundError(f"Hyprland socket not found at: {socket_path}")
    return socket_path


def get_process_ancestors(pid: int) -> Set[int]:
    """Return all parent PIDs of a given process ID by reading /proc."""
    ancestors: Set[int] = set()
    current = pid
    while current > 1:
        try:
            with open(f"/proc/{current}/status", "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        if ppid > 1:
                            ancestors.add(ppid)
                            current = ppid
                        else:
                            current = 1
                        break
                else:
                    break
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            break
    return ancestors


def get_hyprland_windows() -> List[Dict]:
    """Fetch current client windows from Hyprland via hyprctl."""
    try:
        output = subprocess.check_output(["hyprctl", "clients", "-j"], text=True)
        return json.loads(output)
    except Exception as exc:
        logger.error("Failed to query hyprctl clients: %s", exc)
        return []


def get_hyprland_active_window() -> Dict:
    """Fetch the currently active window from Hyprland via hyprctl."""
    try:
        output = subprocess.check_output(["hyprctl", "activewindow", "-j"], text=True)
        if not output.strip():
            return {}
        return json.loads(output)
    except Exception as exc:
        logger.error("Failed to query hyprctl activewindow: %s", exc)
        return {}


def get_pipewire_audio_streams() -> List[Dict]:
    """Fetch all active audio output streams from PipeWire via pw-dump."""
    try:
        output = subprocess.check_output(["pw-dump"], text=True)
        data = json.loads(output)
    except Exception as exc:
        logger.error("Failed to query pw-dump: %s", exc)
        return []

    streams = []
    for obj in data:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Stream/Output/Audio":
            continue

        raw_pid = props.get("application.process.id")
        try:
            pid = int(raw_pid) if raw_pid is not None else None
        except (ValueError, TypeError):
            pid = None

        streams.append({
            "id": obj["id"],
            "name": props.get("node.name", ""),
            "app_name": props.get("application.name", ""),
            "media_name": props.get("media.name", ""),
            "pid": pid,
        })
    return streams


def is_stream_muted(node_id: int) -> Optional[bool]:
    """Check if a PipeWire node is currently muted using wpctl."""
    try:
        out = subprocess.check_output(["wpctl", "get-volume", str(node_id)], text=True)
        return "[MUTED]" in out
    except subprocess.CalledProcessError:
        return None


def set_stream_mute(node_id: int, mute: bool, dry_run: bool = False) -> None:
    """Set the mute state of a PipeWire node using wpctl."""
    action = "mute" if mute else "unmute"
    val = "1" if mute else "0"
    if dry_run:
        logger.info("[DRY-RUN] wpctl set-mute %d %s", node_id, val)
        return

    current = is_stream_muted(node_id)
    if current == mute:
        logger.debug("Node %d is already %sd, skipping.", node_id, action)
        return

    try:
        subprocess.check_call(["wpctl", "set-mute", str(node_id), val])
        logger.info("Set node %d -> %s", node_id, action)
    except subprocess.CalledProcessError as exc:
        logger.error("Failed to %s node %d: %s", action, node_id, exc)


class StreamSilencer:
    """Coordinates Hyprland focus with PipeWire stream mute states."""

    def __init__(
        self,
        class_filter: str = "firefox",
        title_filter: str = "",
        mute_on_unfocus: bool = False,
        dry_run: bool = False,
    ):
        self.class_filter = class_filter.lower()
        self.title_filter = title_filter.lower()
        self.mute_on_unfocus = mute_on_unfocus
        self.dry_run = dry_run
        self.last_active_address: Optional[str] = None

    def window_matches(self, win: Dict) -> bool:
        """Check if a Hyprland window matches the target criteria."""
        if not win:
            return False
        win_class = win.get("class", "").lower()
        win_title = win.get("title", "").lower()
        win_initial_title = win.get("initialTitle", "").lower()

        if self.class_filter and self.class_filter not in win_class:
            return False
        if self.title_filter:
            if self.title_filter not in win_title and self.title_filter not in win_initial_title:
                return False
        return True

    def map_streams_to_windows(
        self,
        windows: List[Dict],
        streams: List[Dict],
    ) -> Dict[int, Optional[Dict]]:
        """Map each audio stream node ID to a matching window."""
        stream_to_win: Dict[int, Optional[Dict]] = {}
        for stream in streams:
            stream_pid = stream.get("pid")
            if not stream_pid:
                stream_to_win[stream["id"]] = None
                continue

            matched_win = None
            ancestors = get_process_ancestors(stream_pid)

            for win in windows:
                win_pid = win.get("pid")
                if not win_pid:
                    continue
                # Match direct PID or ancestry in process tree
                if stream_pid == win_pid or win_pid in ancestors:
                    matched_win = win
                    break

            stream_to_win[stream["id"]] = matched_win
        return stream_to_win

    def update_states(self) -> None:
        """Evaluate focus and update stream mute states."""
        active_win = get_hyprland_active_window()
        all_windows = get_hyprland_windows()
        managed_windows = [w for w in all_windows if self.window_matches(w)]

        if not managed_windows:
            logger.debug("No matching managed windows found.")
            return

        streams = get_pipewire_audio_streams()
        stream_map = self.map_streams_to_windows(managed_windows, streams)

        active_address = active_win.get("address")
        is_active_managed = self.window_matches(active_win)

        for stream in streams:
            node_id = stream["id"]
            associated_win = stream_map.get(node_id)

            if not associated_win:
                continue

            # If focused window matches this stream's window: unmute
            if is_active_managed and associated_win.get("address") == active_address:
                set_stream_mute(node_id, mute=False, dry_run=self.dry_run)
            else:
                # If active window is another managed window, mute this stream
                if is_active_managed:
                    set_stream_mute(node_id, mute=True, dry_run=self.dry_run)
                elif self.mute_on_unfocus:
                    # Active window is outside managed set, mute if configured
                    set_stream_mute(node_id, mute=True, dry_run=self.dry_run)

    def print_status(self) -> None:
        """Print diagnostics of windows and streams."""
        print("=== Managed Windows (Hyprland) ===")
        all_windows = get_hyprland_windows()
        managed = [w for w in all_windows if self.window_matches(w)]
        active = get_hyprland_active_window()
        active_addr = active.get("address")

        if not managed:
            print("  (No windows matching filter criteria)")
        for w in managed:
            is_cur = "[ACTIVE]" if w.get("address") == active_addr else "        "
            print(
                f"  {is_cur} {w.get('address')} | PID {w.get('pid')} | "
                f"Class: {w.get('class')} | Title: {w.get('title')[:45]}"
            )

        print("\n=== PipeWire Audio Streams ===")
        streams = get_pipewire_audio_streams()
        stream_map = self.map_streams_to_windows(managed, streams)
        if not streams:
            print("  (No active audio streams)")
        for s in streams:
            node_id = s["id"]
            muted = is_stream_muted(node_id)
            mute_str = "[MUTED]  " if muted else "[UNMUTED]"
            assoc = stream_map.get(node_id)
            assoc_str = (
                f"-> Window {assoc.get('address')} (PID {assoc.get('pid')})"
                if assoc
                else "-> (Unmatched / Other app)"
            )
            print(
                f"  Node {node_id:3d} {mute_str} | PID {s.get('pid')} | "
                f"App: {s.get('app_name')} | Name: {s.get('media_name')} {assoc_str}"
            )

    def run_daemon(self) -> None:
        """Listen to Hyprland socket2 events and update streams on focus changes."""
        socket_path = get_hyprland_socket_path()
        logger.info("Connecting to Hyprland socket2: %s", socket_path)

        # Initial synchronization
        self.update_states()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)

        def shutdown_handler(signum, frame):
            logger.info("Received signal %d, stopping daemon...", signum)
            sock.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

        logger.info("Daemon active. Waiting for Hyprland focus events...")
        buffer = ""
        while True:
            chunk = sock.recv(4096).decode("utf-8", errors="replace")
            if not chunk:
                logger.warning("Hyprland socket disconnected.")
                break
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                # Focus event triggers: activewindow, activewindowv2, closewindow
                if (
                    line.startswith("activewindowv2>>")
                    or line.startswith("activewindow>>")
                    or line.startswith("closewindow>>")
                ):
                    logger.debug("Received event: %s", line)
                    self.update_states()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="streamSilencer: Focus-based audio stream manager for Hyprland."
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously in the background listening to Hyprland focus events.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Evaluate current focus and update audio streams once, then exit.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print diagnostic table of detected windows and PipeWire streams.",
    )
    parser.add_argument(
        "--class-filter",
        default="firefox",
        help="Target window class name (default: 'firefox').",
    )
    parser.add_argument(
        "--title-filter",
        default="",
        help="Optional window title or profile substring filter.",
    )
    parser.add_argument(
        "--mute-on-unfocus-all",
        action="store_true",
        help="Mute all managed streams when focus moves to an unmanaged window.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log actions without modifying PipeWire mute states.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose debug logging.",
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    silencer = StreamSilencer(
        class_filter=args.class_filter,
        title_filter=args.title_filter,
        mute_on_unfocus=args.mute_on_unfocus_all,
        dry_run=args.dry_run,
    )

    if args.status:
        silencer.print_status()
    elif args.daemon:
        silencer.run_daemon()
    else:
        # Default action: run once
        silencer.update_states()


if __name__ == "__main__":
    main()
