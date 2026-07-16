# AGENTS.md — Portkey

Portkey is a small Windows desktop app: pick a saved VPS from a list and it opens an SSH
session for you (via Windows Terminal), or browse/transfer files to it over SFTP. Harry
Potter theming (dark background, gold accents), built to feel like real shipped software,
not a dev tool.

## Stack & layout

- Python 3.12, stdlib **Tkinter only** — no `ttk` anywhere, deliberately. Native ttk
  theming clashes with the fully custom dark/gold palette used throughout.
- `paramiko` for SSH/SFTP, `PyYAML` for config.
- **Single file**: `portkey.pyw` (~1970 lines) is the entire app. There is no package
  structure to navigate — `Grep`/`Read` directly, it's faster than spawning an explore
  agent for a file this size.
- Packaged with PyInstaller (`Portkey.spec`, `--onefile`, windowed/no console).
  `pyinstaller-hooks-contrib` handles paramiko's hidden imports automatically — no
  manual `hiddenimports` needed.
- `requirements.txt`: `pyyaml`, `paramiko>=3.4`.

## Running

```
python portkey.pyw          # dev mode
```

No build step needed for dev iteration — it's a plain script.

## Building the exe

```
python -m PyInstaller Portkey.spec --noconfirm
cp dist/Portkey.exe Portkey.exe
cp dist/Portkey.exe release/Portkey.exe
rm -f Portkey.zip && powershell -Command "Compress-Archive -Path 'release\*' -DestinationPath 'Portkey.zip'"
```

**Always verify the exe from a clean folder** after rebuilding — copy just `Portkey.exe`
into an empty directory and launch it there. Dev-mode working is not sufficient proof;
bundled-asset paths (`icon.ico`, `logo_header.png`, `logo_button_dark.png`, resolved via
`sys._MEIPASS` when frozen) have broken silently before.

## Architecture

`PortkeyApp(tk.Tk)` is the whole app. Three views — picker, manage servers, transfer
files — are separate `Frame`s stacked in one `container` and switched with `tkraise()`
(`show_picker()` / `show_manage()` / `show_transfer()`), not separate windows. This was a
deliberate choice after an earlier version used a `Toplevel` for settings and had a "stuck
behind the main window" bug.

- **Picker** (`_build_picker`): scrollable custom row list (`Canvas` + `Frame`, not a
  `Listbox` — a `Listbox` can't embed a per-row button). Each row has a reachability dot,
  the server name, and a "⇅" button that jumps straight into Transfer Files with that
  server pre-selected (`picker_open_transfer`).
- **Manage Servers** (`_build_manage`): CRUD for the server list, plus the
  status-check-interval control. Doubles as the app's only "settings" screen.
- **Transfer Files** (`_build_transfer` / `_build_transfer_pane`): dual-pane local/remote
  SFTP browser. Local pane populates immediately on open (no SSH connection needed to
  browse your own PC); remote pane only populates after `transfer_on_connect`.

### Config

`%APPDATA%\Portkey\config.yaml` — **not** next to the exe. This is intentional: the exe
needs to be runnable from anywhere (Desktop included) without dropping a stray file beside
it. `load_config()` / `save_config()` are the only I/O; `DEFAULT_SETTINGS` holds the
schema for the `settings` block (currently `close_on_connect`, `status_check_interval`).

> `release/README.txt` currently says config.yaml lives "next to Portkey.exe" — that's
> stale, left over from before the `%APPDATA%` move. Worth fixing if you're touching that
> file for anything else.

### Threading

Any blocking call (SSH connect, SFTP list/upload/download, the reachability check) goes
through `self._submit(work_fn, on_success, on_error=None)`: spawns a daemon thread, pushes
the result onto `self._task_queue`, drained every 50ms by `_poll_task_queue` on the main
thread via `self.after(50, ...)`. Never call paramiko or `socket` directly from a Tk
callback. `self._sftp_lock` serializes all `self.sftp_client.*` calls — paramiko's
`SFTPClient` isn't safe for concurrent use from multiple threads on one connection.

`_default_task_error` pops a `messagebox` for uncaught exceptions in a submitted task —
so `work_fn`s must return a normal value for *expected* failure states (e.g. "server is
offline") rather than raising, or a routine offline server pops an error dialog.

### Custom popups

No `messagebox`/`OptionMenu`/native dialogs anywhere in the UI — everything is a
borderless `Toplevel` (`overrideredirect(True)`, `attributes("-topmost", True)`) styled to
match the app (`_show_input_dialog`, `_show_confirm_dialog`, the server dropdown). If you
add another one: call `popup.grab_set()` then `popup.focus_force()` **then**
`self.wait_window(popup)`, in that order. Skipping `focus_force()` before `wait_window()`
is a real, non-obvious bug that broke keyboard input on a *second* dialog shown right
after a first one — `focus_set()` alone isn't enough.

### Windows-specific bits

- **DPI awareness**: `SetProcessDpiAwareness(1)` (System-aware) is called at module load,
  before any Tk window exists. Deliberately *not* level 2 (Per-Monitor v2) — Tk has no
  logic to respond to `WM_DPICHANGED`, so under Per-Monitor v2 Windows stops scaling on
  Tk's behalf and window geometry comes out wrong. Level 1 still fixes the original
  problem (DPI-unaware bitmap-stretching causes click coordinates to land a few pixels off
  from what's visually rendered — enough to miss a ~15px-tall listbox row entirely).
- **Known Folder API** (`get_known_folder`): resolves real Desktop/Downloads/Documents
  paths via `SHGetKnownFolderPath`, not `Path.home() / "Desktop"` — OneDrive's Known
  Folder Move silently redirects those and broke a naive string-guess once.
- Hidden/system-attribute files (legacy junctions like `Application Data`, `Cookies`,
  `Local Settings`) are filtered out of local directory listings in `_local_refresh` — use
  `item.lstat().st_file_attributes`, not `item.stat()` (which follows the junction and
  reports the *target's* attributes, not the junction's own).
- SSH sessions launch via `wt.exe` (Windows Terminal) wrapping `ssh.exe` — both are
  assumed present (see `release/README.txt` for the user-facing requirement note).

## Testing

There is currently **no test suite checked into the repo** — every test written so far
this project lived in a Claude session's scratch directory and wasn't persisted. If you
want tests to survive across sessions, put them in a `tests/` folder in this repo instead.

The pattern that's worked well and is worth continuing regardless of where the files live:

- **Offline, state-driven tests**: load `portkey.pyw` via
  `importlib.util.spec_from_file_location` + `exec_module`, instantiate `PortkeyApp()`,
  drive methods directly, assert on resulting state (`app.server_status`,
  `app.local_rows`, widget `.cget(...)` values, etc). Stub `socket.create_connection` /
  `paramiko` calls rather than touching the network for anything that isn't an explicit
  live-network check. Always patch `messagebox.*` in these tests — a mid-test
  `AttributeError` on an unstubbed method has previously popped a real blocking dialog.
- **Screenshots**: `C:\Users\<user>\.claude\skills\desktop-app-polish\scripts\capture_window.ps1`
  captures a window by exact title via `PrintWindow`, no focus stealing. It declares
  itself DPI-aware (`SetProcessDPIAware()`) before querying — **do not remove that call**;
  without it, `GetWindowRect`/`PrintWindow` against this (DPI-aware) app returns a
  silently shrunk, distorted size even though the real window is fine. This cost real
  debugging time once already.
- **Live network checks** (real SSH/SFTP against configured servers): fine for read-only
  checks like `_check_server_reachable` (a bare TCP connect, no auth). For anything that
  touches real files or credentials, get explicit confirmation first and wrap cleanup in
  `try/finally`.
- **Frozen exe**: rebuild → copy to a clean empty folder → launch → screenshot. Every
  feature in this app has been verified this way at least once before being called done;
  dev-mode-only verification has missed real bugs (bundled asset paths, packaging-only
  behavior).

## Conventions worth preserving

- Color palette lives in module-level constants (`BG_DARK`, `BG_PANEL`, `GOLD`,
  `STATUS_ONLINE`, ...) — reuse these, don't inline new hex values.
- `styled_label` / `styled_entry` / `styled_button` / `install_entry_placeholder` are the
  shared widget-styling helpers — use them for any new UI rather than raw `tk.Label`/
  `tk.Button` with hand-rolled colors.
- Vertical spacing on the Transfer Files screen follows a deliberate rhythm (major section
  gaps vs. within-pane gaps use two consistent values) — check the existing `pady` values
  nearby before picking a new one for an addition.
