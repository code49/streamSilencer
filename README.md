# streamSilencer

An automated, focus-based audio stream manager for Hyprland and PipeWire on Linux.

When running multiple media streams (e.g., simultaneous sports broadcasts or video feeds across tiled windows), `streamSilencer` listens to Hyprland window focus events and dynamically mutes inactive streams while unmuting the focused window.

---

## Features

- **Hyprland IPC Integration**: Connects directly to Hyprland's UNIX domain socket (`socket2.sock`) to react instantly to focus events (`activewindowv2`).
- **Idempotent Audio Control**: Controls PipeWire nodes directly via WirePlumber (`wpctl set-mute`), avoiding state desynchronization.
- **Process Tree Resolution**: Traverses `/proc/<pid>/status` parent chains to map PipeWire content streams to their parent Hyprland client windows.
- **Selective Filtering**: Filter target windows by Wayland window class (e.g. `firefox`), title substring, or specific profile names.
- **Diagnostics Mode**: Built-in `--status` flag displays a real-time table of detected windows, active focus, and matching PipeWire audio streams.
- **NixOS Dev Support**: Native, reproducible development environment via `shell.nix`.

---

## Project Structure

- **`stream_silencer.py`**: The core daemon script handling Hyprland socket2 events, process tree matching, and `wpctl` calls.
- **`shell.nix`**: The Nix development environment containing Python 3, WirePlumber, and PipeWire dependencies.
- **`.gitignore`**: Git exclusion rules for virtual environments and Python cache files.

---

## Usage

### 1. Enter Environment

```bash
nix-shell
```

### 2. Launch Interactive Stream Session Manager

Launch 2 stream windows (default) and enter the interactive controller:

```bash
python stream_launcher.py
```

Launch 3 stream windows with specified URLs:

```bash
python stream_launcher.py -n 3 https://stream1.example https://stream2.example
```

Configure the master profile (themes, extensions, logins):

```bash
python stream_launcher.py --edit
```

### 3. Interactive Commands

Inside the `streamSilencer> ` command prompt:

| Command | Description |
| :--- | :--- |
| `add [url]` (or paste URL) | Launch a new isolated stream window into the pool. |
| `list` / `ls` | Display all active stream windows, titles, and audio mute states. |
| `close <id>` / `rm <id>` | Terminate a specific stream window by ID. |
| `focus <id>` | Switch Hyprland focus directly to that stream window. |
| `reset` | Re-clone all active session instances fresh from master profile. |
| `edit` | Open master profile to adjust extensions, themes, or logins. |
| `clear` | Clear the terminal screen. |
| `quit` / `exit` (or `Ctrl+C`) | Cleanly close all stream windows, stop the daemon, and exit. |

### 4. Inspect Windows and Audio Streams

View currently detected Hyprland windows and PipeWire streams directly:

```bash
python stream_silencer.py --status
```

### 5. Run the Focus Daemon Standalone

Launch the daemon independently:

```bash
python stream_silencer.py --daemon
```

### Available CLI Flags

| Flag | Description |
| :--- | :--- |
| `--daemon` | Run continuously in the background listening to Hyprland socket events. |
| `--status` | Print diagnostic table of detected windows and PipeWire streams. |
| `--once` | Evaluate current window focus, update stream mute states, and exit. |
| `--class-filter <str>` | Target window class filter (default: `firefox`). |
| `--title-filter <str>` | Substring filter for window title or profile. |
| `--mute-on-unfocus-all` | Mute all managed streams when focus leaves managed windows. |
| `--dry-run` | Log actions without executing `wpctl` commands. |
| `-v`, `--verbose` | Enable debug logging. |
