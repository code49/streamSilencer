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
import select
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


def set_border_animation_speed(speed: float = 2.0) -> None:
    """Configure Hyprland border animation speed for snappy color transitions."""
    code = f"hl.animation({{ leaf = 'border', enabled = true, speed = {speed}, bezier = 'borderChangeBezier' }})"
    try:
        subprocess.run(["hyprctl", "eval", code], capture_output=True, text=True)
    except Exception as exc:
        logger.debug("Failed to set border animation speed: %s", exc)


def set_window_border(
    address: str,
    muted: bool,
    color_muted: str = "rgb(ffadad)",
    color_active: str = "rgb(caffbf)",
    dry_run: bool = False,
) -> None:
    """Highlight window border red when muted or green when active."""
    if not address:
        return
    val = color_muted if muted else color_active
    if dry_run:
        logger.info("[DRY-RUN] Set border for %s -> %s", address, val)
        return

    code = f"""
    for _, w in ipairs(hl.get_windows()) do
        if w.address == '{address}' then
            hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'inactive_border_color', value = '{val}' }}))
            hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'active_border_color', value = '{val}' }}))
            break
        end
    end
    """
    try:
        res = subprocess.run(["hyprctl", "eval", code], capture_output=True, text=True)
        if res.returncode != 0:
            logger.debug("hyprctl eval error: %s", res.stderr.strip())
    except Exception as exc:
        logger.debug("Failed to set window border for %s: %s", address, exc)


class StreamSilencer:
    """Coordinates Hyprland focus with PipeWire stream mute states."""

    def __init__(
        self,
        class_filter: str = "streamSilencer",
        title_filter: str = "",
        mute_on_unfocus: bool = False,
        highlight_muted: bool = True,
        border_color: str = "rgb(ffadad)",
        focus_border_color: str = "rgb(caffbf)",
        border_anim_speed: float = 2.0,
        dry_run: bool = False,
    ):
        self.class_filter = class_filter.lower()
        self.title_filter = title_filter.lower()
        self.mute_on_unfocus = mute_on_unfocus
        self.highlight_muted = highlight_muted
        self.border_color = border_color
        self.focus_border_color = focus_border_color
        self.border_anim_speed = border_anim_speed
        self.dry_run = dry_run
        self.last_active_address: Optional[str] = None
        self.window_mute_states: Dict[str, bool] = {}

    def update_window_border(self, address: str, muted: bool) -> None:
        """Update window border highlight with state caching."""
        if not self.highlight_muted or not address:
            return
        if self.window_mute_states.get(address) == muted:
            return
        self.window_mute_states[address] = muted
        set_window_border(
            address,
            muted=muted,
            color_muted=self.border_color,
            color_active=self.focus_border_color,
            dry_run=self.dry_run,
        )

    def clear_all_borders(self) -> None:
        """Reset all managed window borders back to default."""
        for addr in list(self.window_mute_states.keys()):
            code = f"""
            for _, w in ipairs(hl.get_windows()) do
                if w.address == '{addr}' then
                    hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'inactive_border_color', value = '-1' }}))
                    hl.dispatch(hl.dsp.window.set_prop({{ window = w, prop = 'active_border_color', value = '-1' }}))
                    break
                end
            end
            """
            try:
                subprocess.run(["hyprctl", "eval", code], capture_output=True, text=True)
            except Exception:
                pass
        self.window_mute_states.clear()

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

        # Update PipeWire audio streams
        for stream in streams:
            node_id = stream["id"]
            associated_win = stream_map.get(node_id)

            if not associated_win:
                # Protect unmanaged Firefox streams (e.g. personal profiles) against WirePlumber crosstalk
                if stream.get("app_name") == "Firefox" and is_stream_muted(node_id):
                    logger.info("Restoring unmanaged Firefox stream (node %d) unmuted state", node_id)
                    set_stream_mute(node_id, mute=False, dry_run=self.dry_run)
                continue

            win_addr = associated_win.get("address")

            # If focused window matches this stream's window: unmute
            if is_active_managed and win_addr == active_address:
                set_stream_mute(node_id, mute=False, dry_run=self.dry_run)
            else:
                # If active window is another managed window, or mute_on_unfocus is set
                if is_active_managed or self.mute_on_unfocus:
                    set_stream_mute(node_id, mute=True, dry_run=self.dry_run)

        # Update window border highlights for all managed windows
        if is_active_managed:
            self.last_active_address = active_address
            for win in managed_windows:
                win_addr = win.get("address")
                if not win_addr:
                    continue
                is_active = (win_addr == active_address)
                self.update_window_border(win_addr, muted=(not is_active))
        elif self.mute_on_unfocus:
            for win in managed_windows:
                win_addr = win.get("address")
                if win_addr:
                    self.update_window_border(win_addr, muted=True)
        else:
            # Focus moved outside managed windows; preserve last active window's unmuted state
            for win in managed_windows:
                win_addr = win.get("address")
                if not win_addr:
                    continue
                is_last_active = (win_addr == self.last_active_address)
                self.update_window_border(win_addr, muted=(not is_last_active))

        # Clean up stale addresses from mute state cache
        active_addrs = {w.get("address") for w in managed_windows}
        for addr in list(self.window_mute_states.keys()):
            if addr not in active_addrs:
                del self.window_mute_states[addr]

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

        # Configure snappy border animation speed if requested
        if self.border_anim_speed > 0:
            set_border_animation_speed(self.border_anim_speed)

        # Initial synchronization
        self.update_states()

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(socket_path)

        def shutdown_handler(signum, frame):
            logger.info("Received signal %d, stopping daemon...", signum)
            self.clear_all_borders()
            sock.close()
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown_handler)
        signal.signal(signal.SIGTERM, shutdown_handler)

        logger.info("Daemon active. Waiting for Hyprland focus events...")
        buffer = ""
        while True:
            # Poll with a 1-second timeout to catch newly started audio streams
            readable, _, _ = select.select([sock], [], [], 1.0)
            if not readable:
                self.update_states()
                continue

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
        "--launch",
        type=int,
        metavar="COUNT",
        help="Launch COUNT stream-compatible Firefox windows.",
    )
    parser.add_argument(
        "--shared-profile",
        action="store_true",
        help="Use a single shared Firefox instance instead of isolated instances.",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="Optional stream URLs to open when launching windows.",
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
        default="streamSilencer",
        help="Target window class name (default: 'streamSilencer').",
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
        "--no-border-highlight",
        action="store_true",
        help="Disable red border highlight on muted windows.",
    )
    parser.add_argument(
        "--border-color",
        default="rgb(ffadad)",
        help="Border color for muted windows (default: 'rgb(ffadad)').",
    )
    parser.add_argument(
        "--focus-border-color",
        default="rgb(caffbf)",
        help="Border color for focused/unmuted stream windows (default: 'rgb(caffbf)').",
    )
    parser.add_argument(
        "--border-anim-speed",
        type=float,
        default=2.0,
        help="Hyprland border animation speed (lower is faster, default: 2.0).",
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
        highlight_muted=(not args.no_border_highlight),
        border_color=args.border_color,
        focus_border_color=args.focus_border_color,
        border_anim_speed=args.border_anim_speed,
        dry_run=args.dry_run,
    )

    if args.launch:
        from stream_launcher import launch_windows
        launch_windows(
            count=args.launch,
            urls=args.urls,
            isolated=(not args.shared_profile),
        )
        if not args.daemon:
            return

    if args.status:
        silencer.print_status()
    elif args.daemon:
        silencer.run_daemon()
    else:
        # Default action: run once
        silencer.update_states()


if __name__ == "__main__":
    main()
