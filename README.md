# streamSilencer

An automated, focus-based audio stream manager for Hyprland and PipeWire on Linux.

When running multiple media streams (such as simultaneous sports broadcasts or video feeds across tiled windows), `streamSilencer` tracks Hyprland window focus events, mutes inactive streams dynamically, and unmutes the active window.

---

## Features

- **Hyprland IPC Integration**: Connects directly to Hyprland's UNIX domain socket (`socket2.sock`) to react instantly to focus events (`activewindowv2`).
- **Dynamic Visual Border Highlighting**: Overrides Hyprland window borders in real time via Hyprland IPC:
  - Active / Unmuted: Soft mint green (`#caffbf`)
  - Inactive / Muted: Soft coral red (`#ffadad`)
  - Fast 200ms animation transitions with clean border restoration on exit.
- **PipeWire Audio Isolation**: Isolates managed browser instances via `PULSE_PROP="application.name=streamSilencer application.id=streamSilencer"` to prevent WirePlumber crosstalk or accidental muting of personal browser windows.
- **Interactive Session Controller**: CLI manager to launch, chain, list, focus, and close stream windows on the fly.
- **Cascade Lifecycle Cleanup**: Traps `SIGINT`, `SIGTERM`, and `SIGHUP` (terminal closure) to close all spawned Firefox instances, terminate the focus daemon, and restore Hyprland borders.
- **Automatic Credential Sync**: Clones logins, cookies, and local storage automatically from your personal Firefox profile (`dchan-personal`) into isolated runtime instances.
- **Process Tree Mapping**: Traverses `/proc/<pid>/status` parent chains to map PipeWire content streams to their parent Hyprland client windows.
- **NixOS Dev Support**: Native, reproducible development environment via `shell.nix`.

---

## Project Structure

- **`stream_launcher.py`**: The interactive session controller and CLI frontend for managing stream windows and lifecycle cleanup.
- **`stream_silencer.py`**: The focus daemon and PipeWire audio manager handling Hyprland socket events, process tree matching, and `wpctl` calls.
- **`shell.nix`**: The Nix development environment containing Python 3, WirePlumber, and PipeWire dependencies.
- **`.gitignore`**: Git exclusion rules for virtual environments and Python cache files.

---

## Usage

### 1. Enter Environment

```bash
nix-shell
```

### 2. Launch Interactive Stream Session Manager

Start the session controller without opening windows upfront:

```bash
python stream_launcher.py
```

Launch directly with preset shortcuts (e.g. 3x YouTube TV or 2x YouTube):

```bash
python stream_launcher.py 4
# or multi-window chaining:
python stream_launcher.py 1 1
# or preset name:
python stream_launcher.py 3yttv
```

Launch with specific streaming URLs:

```bash
python stream_launcher.py https://stream1.example https://stream2.example
```

Configure master profile logins, extensions, or themes:

```bash
python stream_launcher.py --edit
```

#### Launcher CLI Options

| Option | Description |
| :--- | :--- |
| `[urls...]` | Initial stream URLs or preset shortcuts (e.g. `1`, `4`, `yttv`). |
| `-n, --count <n>` | Initial number of stream windows to open (default: `0`). |
| `--profile <name>` | Master Firefox profile name (default: `streamSilencer`). |
| `--edit` | Open master profile in Firefox for configuration. |
| `-r, --reset` | Re-clone all runtime session instances fresh from master profile. |
| `--no-daemon` | Do not start the background focus daemon. |
| `--no-sync` | Skip automatic credential synchronization on launch. |

---

### 3. Interactive Commands & Presets

Inside the `streamSilencer> ` command prompt:

| Command | Description |
| :--- | :--- |
| `1` / `yt` | Open 1x YouTube window (`https://www.youtube.com`). Supports chaining: `1 1`, `1 2`. |
| `2` / `yttv` | Open 1x YouTube TV window (`https://tv.youtube.com`). |
| `3` / `prime` | Open 1x Prime Video window (`https://www.primevideo.com`). |
| `4` / `3yttv` | Open 3x YouTube TV windows (multiview). |
| `add [urls\|presets...]` | Launch window(s) with custom URLs or preset names (e.g. `add 1 1`, `add yt prime`). |
| `presets` | Display all predefined streaming shortcuts and aliases. |
| `list` / `ls` | Display all active stream windows, titles, PIDs, and audio mute states. |
| `close <ids...>` / `rm <ids...>` | Terminate one or more stream windows by ID (e.g. `close 1 2`). |
| `focus <id>` | Switch Hyprland focus directly to that stream window. |
| `reset` | Re-clone all active session instances fresh from master profile. |
| `sync` | Sync latest logins and cookies from your personal Firefox profile. |
| `edit` | Open master profile to adjust extensions, themes, or logins. |
| `clear` | Clear the terminal screen. |
| `quit` / `exit` (or `Ctrl+C`) | Cleanly close all stream windows, stop the daemon, and exit. |

---

### 4. Inspect Windows and Audio Streams

View currently detected Hyprland windows and PipeWire streams directly:

```bash
python stream_silencer.py --status
```

---

### 5. Run Focus Daemon Standalone

Run the focus daemon independently:

```bash
python stream_silencer.py --daemon
```

#### Daemon CLI Options

| Flag | Description |
| :--- | :--- |
| `--daemon` | Run continuously in background listening to Hyprland socket events. |
| `--status` | Print diagnostic table of detected windows and PipeWire streams. |
| `--once` | Evaluate current window focus, update stream mute states, and exit. |
| `--class-filter <str>` | Target window class filter (default: `firefox`). |
| `--title-filter <str>` | Substring filter for window title or profile. |
| `--mute-on-unfocus-all` | Mute all managed streams when focus leaves managed windows. |
| `--dry-run` | Log actions without executing `wpctl` commands. |
| `-v`, `--verbose` | Enable debug logging. |
