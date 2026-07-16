import ctypes
import os
import posixpath
import queue
import socket
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import paramiko
import yaml

# Without this, Windows treats the process as DPI-unaware and bitmap-stretches
# the whole window on any scaled display (125%, 150%, ...) -- text renders
# blurry and, worse, click coordinates get remapped through that stretch and
# can land a few pixels off from where they visually appear. On a tightly
# packed Listbox row (~15-20px tall) that's enough to miss the row entirely,
# which looks like double-click-to-navigate silently doing nothing. Must run
# before any Tk window is created. Deliberately System DPI Aware (1), not
# Per-Monitor v2 (2): Tk has no logic to respond to WM_DPICHANGED and rescale
# itself when the window moves to a monitor with a different DPI, so under
# Per-Monitor v2 Windows stops doing any scaling on Tk's behalf and geometry
# requests come out wrong (measured: a window explicitly sized 760x640 was
# physically drawn at 622x550 on a 125%-scaled display). System-aware still
# fixes the original blurry-bitmap-stretch click-offset problem for the
# common case (one monitor, or all monitors at the same scale) without that
# corruption.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# Bundled assets (icon, logos) live next to the script in dev mode, or get
# extracted into a temp dir at sys._MEIPASS when frozen by PyInstaller.
if getattr(sys, "frozen", False):
    RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    RESOURCE_DIR = Path(__file__).resolve().parent

# config.yaml is per-user data, so it lives in %APPDATA%\Portkey rather than
# next to the .exe -- that way the exe can sit anywhere (Desktop included)
# without dropping a stray file beside it.
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "Portkey"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = CONFIG_DIR / "config.yaml"

ICON_PATH = RESOURCE_DIR / "icon.ico"
LOGO_HEADER_PATH = RESOURCE_DIR / "logo_header.png"
# dark variant so the mark stays visible against the gold "Activate" button
LOGO_BUTTON_PATH = RESOURCE_DIR / "logo_button_dark.png"

CONFIG_HEADER = (
    "# Portkey VPS list\n"
    "# Each entry:\n"
    "#   name: label shown in the picker\n"
    "#   host: hostname or IP\n"
    "#   user: ssh username\n"
    "#   port: ssh port (optional, defaults to 22)\n"
    "#   key: path to private key file (optional, passed as -i)\n"
    "#\n"
    "# settings.close_on_connect: false keeps the window open after\n"
    "# activating a portkey, so you can launch several sessions in a row.\n"
)

DEFAULT_SETTINGS = {"close_on_connect": True, "status_check_interval": 30}

BG_DARK = "#0f0a1a"
BG_PANEL = "#1c1430"
BG_PANEL_LIGHT = "#251c3d"
GOLD = "#f0c44a"
GOLD_DIM = "#a9843a"
STATUS_ONLINE = "#5fbf77"
STATUS_OFFLINE = "#e5484d"
TEXT_LIGHT = "#e8e0f5"
TEXT_MUTED = "#8b7fa8"

SEARCH_PLACEHOLDER = "Search servers..."
FILE_SEARCH_PLACEHOLDER = "Search files..."


def load_config():
    data = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    settings = dict(DEFAULT_SETTINGS)
    settings.update(data.get("settings") or {})
    return {"vps": data.get("vps") or [], "settings": settings}


def save_config(vps, settings):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        f.write(CONFIG_HEADER)
        yaml.safe_dump(
            {"vps": vps, "settings": settings}, f, sort_keys=False, allow_unicode=True
        )


def build_entry(name, host, user, port, key):
    entry = {"name": name, "host": host}
    if user:
        entry["user"] = user
    if port:
        entry["port"] = port
    if key:
        entry["key"] = key
    return entry


def launch_ssh(entry):
    host = entry["host"]
    user = entry.get("user")
    port = entry.get("port", 22)
    key = entry.get("key")

    target = f"{user}@{host}" if user else host

    ssh_args = ["ssh", target, "-p", str(port)]
    if key:
        ssh_args += ["-i", key]

    subprocess.Popen(
        ["wt.exe", *ssh_args],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def format_size(num_bytes):
    if num_bytes is None:
        return ""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024


def format_mtime(epoch_seconds):
    if not epoch_seconds:
        return ""
    try:
        return datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_ubyte * 8),
    ]


FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"
FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"
FOLDERID_DOCUMENTS = "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"


def get_known_folder(guid_str):
    # Resolves real Windows Known Folder locations (SHGetKnownFolderPath)
    # rather than string-guessing Path.home() / "Desktop" -- OneDrive's Known
    # Folder Move can redirect Desktop/Downloads/Documents elsewhere, which
    # broke an earlier assumption like that in this same app.
    try:
        guid = _GUID()
        result = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(guid_str), ctypes.byref(guid))
        if result != 0:
            return None
        path_ptr = ctypes.c_wchar_p()
        hresult = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, 0, ctypes.byref(path_ptr)
        )
        if hresult != 0 or not path_ptr.value:
            return None
        path = path_ptr.value
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return path
    except Exception:
        return None


def styled_label(parent, text, **kwargs):
    opts = dict(bg=BG_DARK, fg=TEXT_LIGHT, font=("Segoe UI", 9))
    opts.update(kwargs)
    return tk.Label(parent, text=text, **opts)


def styled_entry(parent, textvariable):
    wrap = tk.Frame(parent, bg=BG_PANEL, highlightthickness=1, highlightbackground=GOLD_DIM, highlightcolor=GOLD)
    entry = tk.Entry(
        wrap,
        textvariable=textvariable,
        bg=BG_PANEL,
        fg=TEXT_LIGHT,
        insertbackground=GOLD,
        relief=tk.FLAT,
        font=("Segoe UI", 10),
        highlightthickness=0,
        border=0,
    )
    entry.pack(fill=tk.X, padx=8, pady=6)
    return wrap, entry


def install_entry_placeholder(entry, placeholder_text):
    non_typing_keys = {
        "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
        "Tab", "Down", "Up", "Return", "Escape",
    }

    def on_key(event):
        if entry.get() == placeholder_text and event.keysym not in non_typing_keys:
            entry.delete(0, tk.END)
            entry.config(fg=TEXT_LIGHT)

    def on_focus_out(_event):
        if not entry.get():
            entry.insert(0, placeholder_text)
            entry.config(fg=TEXT_MUTED)

    entry.insert(0, placeholder_text)
    entry.config(fg=TEXT_MUTED)
    entry.bind("<Key>", on_key)
    entry.bind("<FocusOut>", on_focus_out)


def styled_button(parent, text, command, primary=True, image=None):
    if primary:
        bg, fg, active_bg = GOLD, BG_DARK, GOLD_DIM
    else:
        bg, fg, active_bg = BG_PANEL_LIGHT, TEXT_LIGHT, BG_PANEL
    return tk.Button(
        parent,
        text=text,
        command=command,
        image=image,
        compound=tk.LEFT if image else tk.NONE,
        bg=bg,
        fg=fg,
        activebackground=active_bg,
        activeforeground=fg,
        relief=tk.FLAT,
        cursor="hand2",
        border=0,
        font=("Segoe UI", 10, "bold"),
        pady=6,
    )


class PortkeyApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Portkey")
        self.geometry("760x640")
        self.minsize(680, 560)
        self.configure(bg=BG_DARK)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if ICON_PATH.exists():
            try:
                self.iconbitmap(str(ICON_PATH))
            except tk.TclError:
                pass

        self.header_logo_img = (
            tk.PhotoImage(file=str(LOGO_HEADER_PATH)) if LOGO_HEADER_PATH.exists() else None
        )
        self.button_logo_img = (
            tk.PhotoImage(file=str(LOGO_BUTTON_PATH)) if LOGO_BUTTON_PATH.exists() else None
        )

        self.all_entries = []
        self.filtered_entries = []
        self.picker_row_widgets = []
        self.picker_selected_index = None
        self.manage_entries = []
        self.manage_selected_index = None
        self.settings = dict(DEFAULT_SETTINGS)
        self.close_on_connect_var = tk.BooleanVar(value=DEFAULT_SETTINGS["close_on_connect"])

        # ---- transfer view state ----
        self.ssh_client = None
        self.sftp_client = None
        self._transfer_entry = None
        self._reconnect_last_failed_at = 0
        self._did_reconnect = False
        self.local_dir = str(Path.home())
        self.remote_dir = "."
        self._remote_home_dir = None
        self.local_rows = []
        self.remote_rows = []
        self.local_filtered_rows = []
        self.remote_filtered_rows = []
        self._drag_start = None
        self._drag_start_widget = None
        self._drag_active = False
        self._drag_badge = None
        self._task_queue = queue.Queue()
        self.server_status = {}
        self._status_after_id = None
        # paramiko's SFTPClient isn't safe for concurrent calls from multiple
        # threads on the same connection -- serialize every remote operation
        # through this lock so overlapping requests (e.g. a listing still in
        # flight when another navigation/transfer starts) can't corrupt it.
        self._sftp_lock = threading.Lock()

        container = tk.Frame(self, bg=BG_DARK)
        container.pack(fill=tk.BOTH, expand=True)
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.picker_frame = tk.Frame(container, bg=BG_DARK)
        self.manage_frame = tk.Frame(container, bg=BG_DARK)
        self.transfer_frame = tk.Frame(container, bg=BG_DARK)
        for frame in (self.picker_frame, self.manage_frame, self.transfer_frame):
            frame.grid(row=0, column=0, sticky="nsew")

        self._build_picker(self.picker_frame)
        self._build_manage(self.manage_frame)
        self._build_transfer(self.transfer_frame)

        self.show_picker()
        self.reload_config()
        self._poll_task_queue()

    # ---------------------------------------------------------- picker view
    def _build_picker(self, parent):
        header = tk.Frame(parent, bg=BG_DARK)
        header.pack(fill=tk.X, padx=20, pady=(22, 6))
        header.columnconfigure(0, weight=1)

        title_box = tk.Frame(header, bg=BG_DARK)
        title_box.grid(row=0, column=0, sticky="w")

        tk.Label(
            title_box,
            image=self.header_logo_img,
            text=" PORTKEY",
            compound=tk.LEFT,
            font=("Georgia", 22, "bold"),
            fg=GOLD,
            bg=BG_DARK,
        ).pack(anchor="w")

        tk.Label(
            title_box,
            text="choose your destination",
            font=("Segoe UI", 9, "italic"),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        ).pack(anchor="w", pady=(2, 0))

        tk.Button(
            header,
            text="⚙",
            font=("Segoe UI", 14),
            bg=BG_DARK,
            fg=TEXT_MUTED,
            activebackground=BG_DARK,
            activeforeground=GOLD,
            relief=tk.FLAT,
            border=0,
            cursor="hand2",
            command=self.show_manage,
        ).grid(row=0, column=1, sticky="e")

        tk.Frame(parent, bg=GOLD_DIM, height=1).pack(fill=tk.X, padx=20, pady=(12, 14))

        search_wrap = tk.Frame(
            parent, bg=BG_PANEL, highlightthickness=1, highlightbackground=GOLD_DIM, highlightcolor=GOLD
        )
        search_wrap.pack(fill=tk.X, padx=20)

        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_wrap,
            textvariable=self.search_var,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            insertbackground=GOLD,
            relief=tk.FLAT,
            font=("Segoe UI", 11),
            highlightthickness=0,
            border=0,
        )
        self.search_entry.pack(fill=tk.X, padx=10, pady=8)
        self._install_placeholder()
        self.search_var.trace_add("write", lambda *_: self.refresh_list())

        list_frame = tk.Frame(parent, bg=BG_DARK)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=14)

        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # A plain Listbox can't embed a real button per row, so the server
        # list is a hand-built scrollable list of row Frames instead (the
        # standard Tk canvas+scrollbar+inner-frame recipe), each row holding
        # a name label and a small transfer-shortcut icon button.
        self.picker_canvas = tk.Canvas(
            list_frame,
            bg=BG_PANEL,
            bd=0,
            highlightthickness=0,
            yscrollcommand=scrollbar.set,
        )
        self.picker_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.picker_canvas.yview)

        self.picker_rows_frame = tk.Frame(self.picker_canvas, bg=BG_PANEL)
        self._picker_rows_window = self.picker_canvas.create_window(
            (0, 0), window=self.picker_rows_frame, anchor="nw"
        )
        self.picker_rows_frame.bind(
            "<Configure>",
            lambda e: self.picker_canvas.configure(scrollregion=self.picker_canvas.bbox("all")),
        )
        self.picker_canvas.bind(
            "<Configure>",
            lambda e: self.picker_canvas.itemconfig(self._picker_rows_window, width=e.width),
        )
        self.picker_canvas.bind("<Enter>", self._picker_bind_mousewheel)
        self.picker_canvas.bind("<Leave>", self._picker_unbind_mousewheel)

        tk.Checkbutton(
            parent,
            text="Close after connecting",
            variable=self.close_on_connect_var,
            command=self.on_toggle_close_on_connect,
            bg=BG_DARK,
            fg=TEXT_MUTED,
            selectcolor=BG_PANEL,
            activebackground=BG_DARK,
            activeforeground=GOLD,
            relief=tk.FLAT,
            highlightthickness=0,
            border=0,
            font=("Segoe UI", 9),
            cursor="hand2",
        ).pack(anchor="w", padx=20, pady=(0, 14))

        self.connect_btn = styled_button(
            parent, "  Activate Portkey", self.on_connect, image=self.button_logo_img
        )
        self.connect_btn.pack(fill=tk.X, padx=20, pady=(0, 8))

        tk.Label(
            parent,
            text="double-click a server, or select it and press Enter",
            font=("Segoe UI", 8),
            fg=TEXT_MUTED,
            bg=BG_DARK,
        ).pack(pady=(0, 14))

        self.search_entry.bind("<Down>", lambda e: self.picker_move_selection(1))
        self.search_entry.bind("<Up>", lambda e: self.picker_move_selection(-1))
        self.search_entry.bind("<Return>", self.on_connect)

    def _install_placeholder(self):
        non_typing_keys = {
            "Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R",
            "Tab", "Down", "Up", "Return", "Escape",
        }

        def on_key(event):
            if self.search_entry.get() == SEARCH_PLACEHOLDER and event.keysym not in non_typing_keys:
                self.search_entry.delete(0, tk.END)
                self.search_entry.config(fg=TEXT_LIGHT)

        def on_focus_out(_event):
            if not self.search_entry.get():
                self.search_entry.insert(0, SEARCH_PLACEHOLDER)
                self.search_entry.config(fg=TEXT_MUTED)

        self.search_entry.insert(0, SEARCH_PLACEHOLDER)
        self.search_entry.config(fg=TEXT_MUTED)
        self.search_entry.bind("<Key>", on_key)
        self.search_entry.bind("<FocusOut>", on_focus_out)

    def show_picker(self):
        self.picker_frame.tkraise()
        self.search_entry.focus_set()
        self._reschedule_status_checks()

    def reload_config(self):
        try:
            config = load_config()
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to read config.yaml:\n{exc}")
            config = {"vps": [], "settings": dict(DEFAULT_SETTINGS)}
        self.all_entries = config["vps"]
        self.settings = config["settings"]
        self.close_on_connect_var.set(self.settings.get("close_on_connect", True))
        self.refresh_list()
        self._reschedule_status_checks()

    def _check_server_reachable(self, entry):
        host = entry.get("host")
        port = entry.get("port") or 22
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2.5):
                return True, round((time.perf_counter() - start) * 1000)
        except OSError:
            return False, None

    def _refresh_server_statuses(self):
        for entry in self.all_entries:
            name = entry.get("name")
            self._submit(
                lambda e=entry: self._check_server_reachable(e),
                lambda result, n=name: self._on_status_checked(n, result),
            )

    def _on_status_checked(self, name, result):
        is_online, latency_ms = result
        self.server_status[name] = {"online": is_online, "latency_ms": latency_ms}
        for i, entry in enumerate(self.filtered_entries):
            if entry.get("name") == name:
                self._picker_set_dot_status(i, is_online, latency_ms)
                break

    def _reschedule_status_checks(self):
        if self._status_after_id is not None:
            self.after_cancel(self._status_after_id)
            self._status_after_id = None
        self._refresh_server_statuses()
        interval = self.settings.get("status_check_interval", 30)
        if interval and self.picker_frame.winfo_ismapped():
            self._status_after_id = self.after(int(interval) * 1000, self._reschedule_status_checks)

    def on_toggle_close_on_connect(self):
        self.settings["close_on_connect"] = self.close_on_connect_var.get()
        try:
            save_config(self.all_entries, self.settings)
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to save config.yaml:\n{exc}")

    def refresh_list(self):
        query = self.search_var.get().strip().lower()
        if query == SEARCH_PLACEHOLDER.lower():
            query = ""
        self.filtered_entries = [
            e for e in self.all_entries if query in e.get("name", "").lower()
        ]
        for child in self.picker_rows_frame.winfo_children():
            child.destroy()
        self.picker_row_widgets = []
        for index, entry in enumerate(self.filtered_entries):
            self._picker_build_row(index, entry)
        self.picker_selected_index = 0 if self.filtered_entries else None
        self._picker_refresh_selection_style()
        self.picker_canvas.yview_moveto(0)

    def _picker_build_row(self, index, entry):
        name = entry.get("name", "(unnamed)")
        row = tk.Frame(self.picker_rows_frame, bg=BG_PANEL)
        row.pack(fill=tk.X)

        label = styled_label(row, f"  {name}", bg=BG_PANEL, font=("Segoe UI", 12), anchor="w")
        label.pack(side=tk.LEFT, fill=tk.X, expand=True, pady=6)

        button = tk.Button(
            row,
            text="⇅",
            font=("Segoe UI", 12),
            bg=BG_PANEL,
            fg=TEXT_MUTED,
            activebackground=BG_PANEL,
            activeforeground=GOLD,
            relief=tk.FLAT,
            border=0,
            cursor="hand2",
            takefocus=0,
            command=lambda n=name: self.picker_open_transfer(n),
        )
        button.pack(side=tk.RIGHT, padx=(0, 8))

        # Reachability dot -- packed with side=RIGHT *after* the icon button,
        # which places it just to the button's left (a second side=RIGHT pack
        # sits to the left of the first), carving its own small slice out of
        # the label's fill=X/expand=True area exactly the way the button
        # already does. Its own bg is kept in sync with selection state by
        # _picker_refresh_selection_style; only the oval's fill color reflects
        # online/offline/unknown.
        dot = tk.Canvas(row, width=10, height=10, bg=BG_PANEL, highlightthickness=0, bd=0)
        status = self.server_status.get(name)
        dot_status = status["online"] if status else None
        latency_ms = status.get("latency_ms") if status else None
        dot_color = STATUS_ONLINE if dot_status is True else STATUS_OFFLINE if dot_status is False else TEXT_MUTED
        dot_oval = dot.create_oval(1, 1, 9, 9, fill=dot_color, outline="")
        dot.pack(side=tk.RIGHT, padx=(0, 8))

        # Latency -- packed side=RIGHT *after* the dot, so it lands just to
        # the dot's left (reads "name ... 42 ms ● ⇅" left to right). Blank
        # when offline/unknown, since latency is meaningless there.
        latency_text = f"{latency_ms} ms" if dot_status and latency_ms is not None else ""
        latency = styled_label(row, latency_text, bg=BG_PANEL, fg=TEXT_MUTED, font=("Segoe UI", 9))
        latency.pack(side=tk.RIGHT, padx=(0, 4))

        for widget in (row, label):
            widget.bind("<Button-1>", lambda e, i=index: self._picker_on_row_click(i))
            widget.bind("<Double-Button-1>", lambda e, i=index: self._picker_on_row_double_click(i))

        self.picker_row_widgets.append(
            {"frame": row, "label": label, "button": button, "dot": dot, "dot_oval": dot_oval, "latency": latency}
        )

    def _picker_on_row_click(self, index):
        self.picker_selected_index = index
        self._picker_refresh_selection_style()

    def _picker_on_row_double_click(self, index):
        self.picker_selected_index = index
        self._picker_refresh_selection_style()
        self.on_connect()

    def _picker_refresh_selection_style(self):
        for i, widgets in enumerate(self.picker_row_widgets):
            selected = i == self.picker_selected_index
            bg = GOLD if selected else BG_PANEL
            fg = BG_DARK if selected else TEXT_LIGHT
            widgets["frame"].config(bg=bg)
            widgets["label"].config(bg=bg, fg=fg)
            widgets["button"].config(
                bg=bg, activebackground=bg, fg=(BG_DARK if selected else TEXT_MUTED)
            )
            widgets["dot"].config(bg=bg)
            widgets["latency"].config(bg=bg, fg=(BG_DARK if selected else TEXT_MUTED))

    def _picker_set_dot_status(self, index, is_online, latency_ms=None):
        if index >= len(self.picker_row_widgets):
            return
        widgets = self.picker_row_widgets[index]
        color = STATUS_ONLINE if is_online else STATUS_OFFLINE
        widgets["dot"].itemconfig(widgets["dot_oval"], fill=color)
        widgets["latency"].config(text=f"{latency_ms} ms" if is_online and latency_ms is not None else "")

    def picker_move_selection(self, delta):
        if not self.filtered_entries:
            return "break"
        current = self.picker_selected_index if self.picker_selected_index is not None else -1
        new_index = min(max(current + delta, 0), len(self.filtered_entries) - 1)
        self.picker_selected_index = new_index
        self._picker_refresh_selection_style()
        self._picker_scroll_into_view(self.picker_row_widgets[new_index]["frame"])
        return "break"

    def _picker_scroll_into_view(self, row_frame):
        self.update_idletasks()
        bbox = self.picker_canvas.bbox("all")
        if not bbox:
            return
        content_height = bbox[3] - bbox[1]
        canvas_height = self.picker_canvas.winfo_height()
        if content_height <= canvas_height:
            return
        # row_frame's y-position is relative to picker_rows_frame, which sits
        # at (0, 0) inside the canvas -- so this is also its canvas-content y.
        row_top = row_frame.winfo_y()
        row_bottom = row_top + row_frame.winfo_height()
        top_frac, bottom_frac = self.picker_canvas.yview()
        visible_top = top_frac * content_height
        visible_bottom = bottom_frac * content_height
        if row_top < visible_top:
            self.picker_canvas.yview_moveto(row_top / content_height)
        elif row_bottom > visible_bottom:
            self.picker_canvas.yview_moveto((row_bottom - canvas_height) / content_height)

    def _picker_bind_mousewheel(self, _event=None):
        self.picker_canvas.bind_all("<MouseWheel>", self._picker_on_mousewheel)

    def _picker_unbind_mousewheel(self, _event=None):
        self.picker_canvas.unbind_all("<MouseWheel>")

    def _picker_on_mousewheel(self, event):
        self.picker_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def picker_open_transfer(self, server_name):
        self.show_transfer()
        self._select_transfer_server(server_name)
        self.transfer_on_connect()

    def on_connect(self, _event=None):
        if self.picker_selected_index is None or not self.filtered_entries:
            return
        entry = self.filtered_entries[self.picker_selected_index]
        try:
            launch_ssh(entry)
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to launch ssh:\n{exc}")
            return

        if self.close_on_connect_var.get():
            self.on_close()
        else:
            self._flash_connected()

    def _flash_connected(self):
        original_text = self.connect_btn.cget("text")
        self.connect_btn.config(text="✓  Portkey Activated")
        self.after(900, lambda: self.connect_btn.config(text=original_text))

    def on_close(self):
        if self._status_after_id is not None:
            self.after_cancel(self._status_after_id)
            self._status_after_id = None
        self._close_server_dropdown()
        self.transfer_disconnect()
        self.destroy()

    # ---------------------------------------------------------- manage view
    def _build_manage(self, parent):
        header = tk.Frame(parent, bg=BG_DARK)
        header.pack(fill=tk.X, padx=20, pady=(22, 6))
        header.columnconfigure(1, weight=1)

        tk.Button(
            header,
            text="← Back",
            font=("Segoe UI", 10, "bold"),
            bg=BG_DARK,
            fg=GOLD,
            activebackground=BG_DARK,
            activeforeground=GOLD,
            relief=tk.FLAT,
            border=0,
            cursor="hand2",
            command=self.show_picker,
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            header, text="MANAGE SERVERS", font=("Segoe UI", 14, "bold"), fg=GOLD, bg=BG_DARK
        ).grid(row=0, column=1)

        tk.Frame(parent, bg=GOLD_DIM, height=1).pack(fill=tk.X, padx=20, pady=(12, 14))

        body = tk.Frame(parent, bg=BG_DARK)
        body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 14))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        # ---- left: list of servers ----
        left = tk.Frame(body, bg=BG_DARK)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        list_wrap = tk.Frame(left, bg=BG_PANEL)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(0, 8))

        self.manage_listbox = tk.Listbox(
            list_wrap,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            selectbackground=GOLD,
            selectforeground=BG_DARK,
            relief=tk.FLAT,
            highlightthickness=0,
            border=0,
            activestyle="none",
            font=("Segoe UI", 12),
        )
        self.manage_listbox.pack(fill=tk.BOTH, expand=True)
        self.manage_listbox.bind("<<ListboxSelect>>", self.manage_on_select)

        styled_button(left, "+ New Server", self.manage_on_new, primary=False).pack(fill=tk.X, pady=(0, 4))
        styled_button(left, "Delete Selected", self.manage_on_delete, primary=False).pack(fill=tk.X)

        # ---- right: form ----
        right = tk.Frame(body, bg=BG_DARK)
        right.grid(row=0, column=1, sticky="nsew")

        self.manage_name_var = tk.StringVar()
        self.manage_host_var = tk.StringVar()
        self.manage_user_var = tk.StringVar()
        self.manage_port_var = tk.StringVar()
        self.manage_key_var = tk.StringVar()

        for label, var in [
            ("Name", self.manage_name_var),
            ("Host / IP", self.manage_host_var),
            ("User", self.manage_user_var),
            ("Port (default 22)", self.manage_port_var),
        ]:
            styled_label(right, label).pack(anchor="w", pady=(0, 2))
            wrap, _ = styled_entry(right, var)
            wrap.pack(fill=tk.X, pady=(0, 6))

        styled_label(right, "Private key (optional)").pack(anchor="w", pady=(0, 2))
        key_row = tk.Frame(right, bg=BG_DARK)
        key_row.pack(fill=tk.X)
        key_wrap, _ = styled_entry(key_row, self.manage_key_var)
        key_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)
        styled_button(key_row, "Browse", self.manage_on_browse_key, primary=False).pack(side=tk.LEFT, padx=(6, 0))

        styled_button(right, "Save Server", self.manage_on_save).pack(fill=tk.X, pady=(16, 0))

        status_row = tk.Frame(parent, bg=BG_DARK)
        status_row.pack(fill=tk.X, padx=20, pady=(0, 14))
        styled_label(status_row, "Status checks:").pack(side=tk.LEFT, padx=(0, 8))

        self.status_interval_buttons = {}
        for label, value in (("5s", 5), ("15s", 15), ("30s", 30), ("60s", 60), ("Off", 0)):
            btn = styled_button(
                status_row, label, lambda v=value: self.manage_on_set_status_interval(v), primary=False
            )
            btn.pack(side=tk.LEFT, padx=(0, 4))
            self.status_interval_buttons[value] = btn

    def manage_on_set_status_interval(self, value):
        self.settings["status_check_interval"] = value
        try:
            save_config(self.all_entries, self.settings)
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to save config.yaml:\n{exc}")
            return
        self._refresh_status_interval_buttons()
        self._reschedule_status_checks()

    def _refresh_status_interval_buttons(self):
        current = self.settings.get("status_check_interval", 30)
        for value, btn in self.status_interval_buttons.items():
            if value == current:
                btn.config(bg=GOLD, fg=BG_DARK, activebackground=GOLD_DIM, activeforeground=BG_DARK)
            else:
                btn.config(bg=BG_PANEL_LIGHT, fg=TEXT_LIGHT, activebackground=BG_PANEL, activeforeground=TEXT_LIGHT)

    def show_manage(self):
        self.manage_entries = [dict(e) for e in self.all_entries]
        self.manage_selected_index = None
        self.manage_on_new()
        self._manage_refresh_listbox()
        self._refresh_status_interval_buttons()
        self.manage_frame.tkraise()

    def _manage_refresh_listbox(self):
        self.manage_listbox.delete(0, tk.END)
        for entry in self.manage_entries:
            self.manage_listbox.insert(tk.END, entry.get("name", "(unnamed)"))

    def manage_on_select(self, _event=None):
        selection = self.manage_listbox.curselection()
        if not selection:
            return
        self.manage_selected_index = selection[0]
        entry = self.manage_entries[self.manage_selected_index]
        self.manage_name_var.set(entry.get("name", ""))
        self.manage_host_var.set(entry.get("host", ""))
        self.manage_user_var.set(entry.get("user", ""))
        self.manage_port_var.set(str(entry.get("port", "")) if entry.get("port") else "")
        self.manage_key_var.set(entry.get("key", ""))

    def manage_on_new(self):
        self.manage_selected_index = None
        self.manage_listbox.selection_clear(0, tk.END)
        for var in (
            self.manage_name_var,
            self.manage_host_var,
            self.manage_user_var,
            self.manage_port_var,
            self.manage_key_var,
        ):
            var.set("")

    def manage_on_browse_key(self):
        path = filedialog.askopenfilename(title="Select private key file")
        if path:
            self.manage_key_var.set(path)

    def manage_on_save(self):
        name = self.manage_name_var.get().strip()
        host = self.manage_host_var.get().strip()
        user = self.manage_user_var.get().strip()
        port_text = self.manage_port_var.get().strip()
        key = self.manage_key_var.get().strip()

        if not name or not host:
            messagebox.showerror("Portkey", "Name and Host are required.")
            return

        port = None
        if port_text:
            try:
                port = int(port_text)
            except ValueError:
                messagebox.showerror("Portkey", "Port must be a number.")
                return

        entry = build_entry(name, host, user, port, key)

        if self.manage_selected_index is None:
            self.manage_entries.append(entry)
        else:
            self.manage_entries[self.manage_selected_index] = entry

        if not self.manage_persist():
            return
        self._manage_refresh_listbox()
        self.manage_on_new()

    def manage_on_delete(self):
        selection = self.manage_listbox.curselection()
        if not selection:
            return
        index = selection[0]
        name = self.manage_entries[index].get("name", "this server")
        if not self._show_confirm_dialog("Delete Server", f"Delete '{name}'?"):
            return
        del self.manage_entries[index]
        if not self.manage_persist():
            return
        self._manage_refresh_listbox()
        self.manage_on_new()

    def manage_persist(self):
        try:
            save_config(self.manage_entries, self.settings)
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to save config.yaml:\n{exc}")
            return False
        self.reload_config()
        return True

    # -------------------------------------------------------- transfer view
    def _build_transfer(self, parent):
        header = tk.Frame(parent, bg=BG_DARK)
        header.pack(fill=tk.X, padx=20, pady=(22, 6))
        header.columnconfigure(1, weight=1)

        tk.Button(
            header,
            text="← Back",
            font=("Segoe UI", 10, "bold"),
            bg=BG_DARK,
            fg=GOLD,
            activebackground=BG_DARK,
            activeforeground=GOLD,
            relief=tk.FLAT,
            border=0,
            cursor="hand2",
            command=self.transfer_on_back,
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            header, text="TRANSFER FILES", font=("Segoe UI", 14, "bold"), fg=GOLD, bg=BG_DARK
        ).grid(row=0, column=1)

        tk.Frame(parent, bg=GOLD_DIM, height=1).pack(fill=tk.X, padx=20, pady=(12, 14))

        connect_bar = tk.Frame(parent, bg=BG_DARK)
        connect_bar.pack(fill=tk.X, padx=20, pady=(0, 8))

        styled_label(connect_bar, "Server:").pack(side=tk.LEFT, padx=(0, 6))

        self.transfer_server_var = tk.StringVar()
        self._server_dropdown_popup = None
        self._dropdown_just_closed = False
        self.transfer_dropdown_btn = tk.Button(
            connect_bar,
            text="Select a server  ▾",
            command=self._toggle_server_dropdown,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            activebackground=BG_PANEL_LIGHT,
            activeforeground=TEXT_LIGHT,
            relief=tk.FLAT,
            highlightthickness=1,
            highlightbackground=GOLD_DIM,
            highlightcolor=GOLD,
            border=0,
            font=("Segoe UI", 10),
            cursor="hand2",
            padx=10,
            pady=6,
            anchor="w",
            width=18,
        )
        self.transfer_dropdown_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.connect_server_btn = styled_button(connect_bar, "Connect", self.transfer_on_connect)
        self.connect_server_btn.pack(side=tk.LEFT)

        self.transfer_status_var = tk.StringVar(value="Not connected")
        styled_label(
            connect_bar, "", textvariable=self.transfer_status_var, fg=TEXT_MUTED
        ).pack(side=tk.LEFT, padx=(10, 0))

        panes = tk.Frame(parent, bg=BG_DARK)
        panes.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 8))
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)

        self.local_path_var = tk.StringVar(value="")
        self.remote_path_var = tk.StringVar(value="")
        self.local_pane, self.local_listbox, self.local_search_var = self._build_transfer_pane(
            panes, column=0, up_command=self.local_on_up, path_var=self.local_path_var,
            quick_access=self._build_local_quick_access(),
        )
        self.local_listbox.bind("<Double-Button-1>", self.local_on_navigate)
        self.local_search_var.trace_add("write", lambda *_: self._apply_local_search())

        self.remote_pane, self.remote_listbox, self.remote_search_var = self._build_transfer_pane(
            panes, column=1, up_command=self.remote_on_up, path_var=self.remote_path_var,
            extra_actions=[
                ("Home", self.remote_on_home),
                ("New Folder", self.remote_on_new_folder),
                ("Rename", self.remote_on_rename),
                ("Delete", self.remote_on_delete),
            ],
        )
        self.remote_listbox.bind("<Double-Button-1>", self.remote_on_navigate)
        self.remote_search_var.trace_add("write", lambda *_: self._apply_remote_search())

        for pane_listbox in (self.local_listbox, self.remote_listbox):
            pane_listbox.bind("<ButtonPress-1>", self._on_pane_drag_press, add="+")
            pane_listbox.bind("<B1-Motion>", self._on_pane_drag_motion, add="+")
            pane_listbox.bind("<ButtonRelease-1>", self._on_pane_drag_release, add="+")

        actions = tk.Frame(parent, bg=BG_DARK)
        actions.pack(fill=tk.X, padx=20, pady=(0, 8))
        # Use grid with the exact same columnconfigure/padx as `panes` above
        # (rather than approximating it with pack's expand/padx, whose parcel
        # math doesn't line up the same way) so the Upload/Download divide is
        # guaranteed pixel-identical to the pane boundary above it. `uniform`
        # is required, not just equal `weight` -- weight only splits *extra*
        # space evenly; each column's baseline size still comes from its own
        # content's minimum width first, and "← Download" is longer text
        # than "→ Upload", so without `uniform` the columns end up unequal.
        actions.columnconfigure(0, weight=1, uniform="transfer_actions")
        actions.columnconfigure(1, weight=1, uniform="transfer_actions")
        styled_button(actions, "Upload →", self.transfer_on_upload, primary=False).grid(
            row=0, column=0, sticky="ew", padx=(0, 8)
        )
        styled_button(actions, "← Download", self.transfer_on_download, primary=False).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        progress_frame = tk.Frame(parent, bg=BG_DARK)
        progress_frame.pack(fill=tk.X, padx=20, pady=(0, 8))
        self.progress_track = tk.Frame(progress_frame, bg=BG_PANEL, height=8)
        self.progress_track.pack(fill=tk.X)
        self.progress_fill = tk.Frame(self.progress_track, bg=GOLD)
        self.progress_label = styled_label(
            progress_frame, "", fg=TEXT_MUTED, font=("Segoe UI", 8)
        )
        self.progress_label.pack(anchor="e", pady=(2, 0))

    def _build_transfer_pane(self, parent, column, up_command, path_var, quick_access=None, extra_actions=None):
        pane = tk.Frame(parent, bg=BG_DARK)
        pane.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else (8, 0))
        # A very long path (deeply nested folders, long usernames, ...) has no
        # natural width limit on the label showing it, which otherwise forces
        # this whole pane to grow past its intended half of the window. pane's
        # own children are pack()-managed, so pack_propagate (not
        # grid_propagate, which only governs grid-managed children and would
        # be a no-op here) is what's needed to keep the pane's size tied to
        # the grid column (both columns have equal weight) instead of its
        # packed content -- without this, a pane with wider content (e.g.
        # more/longer quick-access buttons) visibly grows larger than the
        # other pane instead of the two staying equal width.
        pane.pack_propagate(False)

        pane_header = tk.Frame(pane, bg=BG_DARK)
        pane_header.pack(fill=tk.X)
        styled_label(
            pane_header, "", textvariable=path_var, fg=TEXT_MUTED, wraplength=260
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)

        # Button row: the "up a directory" button lives here alongside the
        # pane's other action buttons (quick_access for local, extra_actions
        # for remote) instead of sitting alone in its own row above them.
        buttons_row = tk.Frame(pane, bg=BG_DARK)
        buttons_row.pack(fill=tk.X, pady=(6, 0))
        styled_button(buttons_row, "↑", up_command, primary=False).pack(side=tk.LEFT, padx=(0, 4))

        # quick_access is local-pane-only: fixed shortcut buttons (Home,
        # Desktop, Downloads, Documents) that jump straight to that folder.
        if quick_access:
            for label, path in quick_access:
                styled_button(
                    buttons_row, label, lambda p=path: self._local_refresh(p), primary=False
                ).pack(side=tk.LEFT, padx=(0, 4))

        # extra_actions is remote-pane-only: Home / New Folder / Rename /
        # Delete -- kept above the list, matching quick_access's position.
        if extra_actions:
            for label, command in extra_actions:
                styled_button(buttons_row, label, command, primary=False).pack(
                    side=tk.LEFT, padx=(0, 4)
                )

        # A directory can easily hold more files than fit on screen -- a
        # search box filters the already-fetched listing (self.local_rows /
        # self.remote_rows) client-side, no re-fetch needed.
        search_row = tk.Frame(pane, bg=BG_PANEL, highlightthickness=1, highlightbackground=GOLD_DIM, highlightcolor=GOLD)
        search_row.pack(fill=tk.X, pady=(6, 0))
        search_var = tk.StringVar()
        search_entry = tk.Entry(
            search_row,
            textvariable=search_var,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            insertbackground=GOLD,
            relief=tk.FLAT,
            font=("Segoe UI", 9),
            highlightthickness=0,
            border=0,
        )
        search_entry.pack(fill=tk.X, padx=8, pady=4)
        install_entry_placeholder(search_entry, FILE_SEARCH_PLACEHOLDER)

        list_wrap = tk.Frame(pane, bg=BG_PANEL)
        list_wrap.pack(fill=tk.BOTH, expand=True, pady=(6, 0))
        listbox = tk.Listbox(
            list_wrap,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            selectbackground=GOLD,
            selectforeground=BG_DARK,
            relief=tk.FLAT,
            highlightthickness=0,
            border=0,
            activestyle="none",
            font=("Consolas", 10),
            selectmode=tk.EXTENDED,
        )
        listbox.pack(fill=tk.BOTH, expand=True)

        return pane, listbox, search_var

    def _build_local_quick_access(self):
        entries = [("Home", str(Path.home()))]
        for label, guid in (
            ("Desktop", FOLDERID_DESKTOP),
            ("Downloads", FOLDERID_DOWNLOADS),
            ("Documents", FOLDERID_DOCUMENTS),
        ):
            path = get_known_folder(guid)
            if path:
                entries.append((label, path))
        return entries

    def show_transfer(self):
        self._refresh_transfer_server_choices()
        self.transfer_frame.tkraise()
        # Local files don't require an SFTP connection to browse -- populate
        # the local pane as soon as the screen opens rather than waiting on
        # transfer_on_connect, which only ever needs to touch the remote side.
        self._local_refresh(self.local_dir)

    def transfer_on_back(self):
        self._close_server_dropdown()
        self.transfer_disconnect()
        self.show_picker()

    def _refresh_transfer_server_choices(self):
        names = [e.get("name", "(unnamed)") for e in self.all_entries]
        current = self.transfer_server_var.get()
        if names and current not in names:
            self._select_transfer_server(names[0])
        elif not names:
            self.transfer_server_var.set("")
            self.transfer_dropdown_btn.config(text="No servers configured")
        else:
            self.transfer_dropdown_btn.config(text=f"{current}  ▾")

    def _select_transfer_server(self, name):
        self.transfer_server_var.set(name)
        self.transfer_dropdown_btn.config(text=f"{name}  ▾")

    def _toggle_server_dropdown(self):
        if self._server_dropdown_popup is not None and self._server_dropdown_popup.winfo_exists():
            self._close_server_dropdown()
        elif self._dropdown_just_closed:
            # Clicking the toggle button while the popup is open shifts focus
            # away from it first, which fires the popup's own <FocusOut>
            # close handler *before* this button's command runs -- without
            # this guard, that already-closed state looks identical to
            # "nothing is open" and immediately reopens it, so the button
            # appears to do nothing on a second click.
            pass
        else:
            self._open_server_dropdown()

    def _open_server_dropdown(self):
        names = [e.get("name", "(unnamed)") for e in self.all_entries]
        if not names:
            return

        # Force a real layout pass first -- winfo_width()/winfo_rootx() can
        # return stale values (e.g. 1px) if queried before Tk has actually
        # computed geometry for this button yet.
        self.update_idletasks()
        btn = self.transfer_dropdown_btn
        x = btn.winfo_rootx()
        y = btn.winfo_rooty() + btn.winfo_height()
        width = btn.winfo_width()

        popup = tk.Toplevel(self)
        popup.title("Portkey Server Dropdown")
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=GOLD_DIM)

        inner = tk.Frame(popup, bg=BG_PANEL)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        search_var = tk.StringVar()
        search_wrap = tk.Frame(inner, bg=BG_PANEL_LIGHT)
        search_wrap.pack(fill=tk.X)
        search_entry = tk.Entry(
            search_wrap,
            textvariable=search_var,
            bg=BG_PANEL_LIGHT,
            fg=TEXT_LIGHT,
            insertbackground=GOLD,
            relief=tk.FLAT,
            font=("Segoe UI", 9),
            highlightthickness=0,
            border=0,
        )
        search_entry.pack(fill=tk.X, padx=6, pady=4)

        list_row = tk.Frame(inner, bg=BG_PANEL)
        list_row.pack(fill=tk.BOTH, expand=True)

        visible_rows = min(len(names), 8)
        listbox = tk.Listbox(
            list_row,
            bg=BG_PANEL,
            fg=TEXT_LIGHT,
            selectbackground=GOLD,
            selectforeground=BG_DARK,
            relief=tk.FLAT,
            highlightthickness=0,
            border=0,
            activestyle="none",
            font=("Segoe UI", 10),
            height=visible_rows,
            exportselection=False,
        )
        scrollbar = tk.Scrollbar(list_row, command=listbox.yview)
        listbox.config(yscrollcommand=scrollbar.set)
        listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        if len(names) > visible_rows:
            scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # the currently-displayed (possibly filtered) name list, kept in sync
        # with the listbox so a click maps back to the right server
        visible_names = list(names)

        def populate(filtered_names):
            listbox.delete(0, tk.END)
            for name in filtered_names:
                listbox.insert(tk.END, f"  {name}")
            current = self.transfer_server_var.get()
            if current in filtered_names:
                listbox.selection_set(filtered_names.index(current))

        def on_search_change(*_args):
            query = search_var.get().strip().lower()
            filtered = [n for n in names if query in n.lower()]
            visible_names[:] = filtered
            populate(filtered)

        search_var.trace_add("write", on_search_change)
        populate(names)

        # Listbox has no native hover state (only a selection color), so a
        # mouse-following highlight is faked by recoloring whichever row the
        # cursor is currently over, distinct from the gold selection color.
        hovered = {"index": None}

        def clear_hover():
            if hovered["index"] is not None:
                try:
                    listbox.itemconfig(hovered["index"], background=BG_PANEL)
                except tk.TclError:
                    pass
                hovered["index"] = None

        def on_motion(event):
            idx = listbox.nearest(event.y)
            if idx == hovered["index"] or not (0 <= idx < listbox.size()):
                return
            clear_hover()
            listbox.itemconfig(idx, background=BG_PANEL_LIGHT)
            hovered["index"] = idx

        listbox.bind("<Motion>", on_motion)
        listbox.bind("<Leave>", lambda _e: clear_hover())

        def pick(_event=None):
            selection = listbox.curselection()
            if selection and selection[0] < len(visible_names):
                self._select_transfer_server(visible_names[selection[0]])
                self.transfer_on_connect()
            self._close_server_dropdown()

        def select_first(_event=None):
            if visible_names:
                listbox.selection_clear(0, tk.END)
                listbox.selection_set(0)
                pick()

        listbox.bind("<<ListboxSelect>>", pick)
        search_entry.bind("<Return>", select_first)
        search_entry.bind("<Down>", lambda _e: listbox.focus_set())
        popup.bind("<Escape>", lambda _e: self._close_server_dropdown())
        popup.bind("<FocusOut>", lambda _e: self._close_server_dropdown())

        # size the popup to match the button's width exactly, but let its
        # height come from Tk's own layout of the packed search box + rows
        # instead of a hand-guessed pixel value (that guess previously left
        # a visible gap below the last row).
        popup.update_idletasks()
        height = popup.winfo_reqheight()
        popup.geometry(f"{width}x{height}+{x}+{y}")

        self._server_dropdown_popup = popup
        popup.focus_force()
        search_entry.focus_set()

    def _close_server_dropdown(self):
        if self._server_dropdown_popup is not None and self._server_dropdown_popup.winfo_exists():
            self._server_dropdown_popup.destroy()
            self._dropdown_just_closed = True
            self.after(150, lambda: setattr(self, "_dropdown_just_closed", False))
        self._server_dropdown_popup = None

    def _show_input_dialog(self, title, prompt, initial=""):
        # Reuses the server dropdown's borderless-Toplevel visual pattern
        # (dark/gold themed, no native OS chrome) but with different
        # semantics: truly modal, and does NOT close on FocusOut like the
        # dropdown does -- losing a half-typed rename/folder name to an
        # incidental focus change (e.g. alt-tab) would be bad UX, so only
        # Escape/Cancel/OK dismiss it.
        result = {"value": None}

        popup = tk.Toplevel(self)
        popup.title(title)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=GOLD_DIM)

        inner = tk.Frame(popup, bg=BG_PANEL)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        styled_label(inner, prompt, bg=BG_PANEL).pack(anchor="w", padx=12, pady=(12, 4))

        var = tk.StringVar(value=initial)
        wrap, entry = styled_entry(inner, var)
        wrap.pack(fill=tk.X, padx=12)
        entry.select_range(0, tk.END)
        entry.icursor(tk.END)

        btn_row = tk.Frame(inner, bg=BG_PANEL)
        btn_row.pack(fill=tk.X, padx=12, pady=12)

        def confirm(_event=None):
            result["value"] = var.get().strip() or None
            popup.grab_release()
            popup.destroy()

        def cancel(_event=None):
            popup.grab_release()
            popup.destroy()

        styled_button(btn_row, "OK", confirm).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        styled_button(btn_row, "Cancel", cancel, primary=False).pack(side=tk.LEFT, fill=tk.X, expand=True)

        popup.bind("<Return>", confirm)
        popup.bind("<Escape>", cancel)
        popup.protocol("WM_DELETE_WINDOW", cancel)

        self.update_idletasks()
        popup.update_idletasks()
        width, height = popup.winfo_reqwidth(), popup.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        popup.geometry(f"{width}x{height}+{x}+{y}")

        popup.grab_set()
        popup.focus_force()
        entry.focus_set()
        self.wait_window(popup)
        return result["value"]

    def _show_confirm_dialog(self, title, message, confirm_text="Delete"):
        # Same themed borderless-Toplevel shell as _show_input_dialog, just
        # without the text entry -- used for anything that used to be a
        # native messagebox.askyesno, which looked like jarring OS chrome
        # against this app's fully custom dark/gold theme.
        result = {"value": False}

        popup = tk.Toplevel(self)
        popup.title(title)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=GOLD_DIM)

        inner = tk.Frame(popup, bg=BG_PANEL)
        inner.pack(fill=tk.BOTH, expand=True, padx=1, pady=1)

        styled_label(
            inner, message, bg=BG_PANEL, wraplength=320, justify=tk.LEFT
        ).pack(anchor="w", padx=14, pady=(14, 10))

        btn_row = tk.Frame(inner, bg=BG_PANEL)
        btn_row.pack(fill=tk.X, padx=14, pady=(0, 14))

        def confirm(_event=None):
            result["value"] = True
            popup.grab_release()
            popup.destroy()

        def cancel(_event=None):
            popup.grab_release()
            popup.destroy()

        styled_button(btn_row, confirm_text, confirm).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        styled_button(btn_row, "Cancel", cancel, primary=False).pack(side=tk.LEFT, fill=tk.X, expand=True)

        popup.bind("<Return>", confirm)
        popup.bind("<Escape>", cancel)
        popup.protocol("WM_DELETE_WINDOW", cancel)

        self.update_idletasks()
        popup.update_idletasks()
        width, height = popup.winfo_reqwidth(), popup.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - width) // 2
        y = self.winfo_rooty() + (self.winfo_height() - height) // 2
        popup.geometry(f"{width}x{height}+{x}+{y}")

        popup.grab_set()
        popup.focus_force()
        self.wait_window(popup)
        return result["value"]

    # -- threading plumbing: background work never touches Tk widgets directly,
    # results are routed back through a queue drained on the main thread.
    def _submit(self, work_fn, on_success, on_error=None):
        def runner():
            try:
                result = work_fn()
            except Exception as exc:
                self._task_queue.put((on_error or self._default_task_error, exc))
            else:
                self._task_queue.put((on_success, result))

        threading.Thread(target=runner, daemon=True).start()

    def _default_task_error(self, exc):
        messagebox.showerror("Portkey", str(exc))

    def _poll_task_queue(self):
        try:
            while True:
                callback, payload = self._task_queue.get_nowait()
                callback(payload)
                if self._did_reconnect:
                    self._did_reconnect = False
                    self._flash_reconnected()
        except queue.Empty:
            pass
        self.after(50, self._poll_task_queue)

    def _flash_reconnected(self):
        original = self.transfer_status_var.get()
        self.transfer_status_var.set(f"Reconnected to {self.transfer_server_var.get()}")
        self.after(1500, lambda: self.transfer_status_var.set(original) if self.sftp_client else None)

    # -- connect / disconnect --
    def transfer_on_connect(self):
        name = self.transfer_server_var.get()
        entry = next((e for e in self.all_entries if e.get("name") == name), None)
        if not entry:
            messagebox.showerror("Portkey", "Select a server first.")
            return
        self.transfer_disconnect()
        self._transfer_entry = entry
        self.transfer_status_var.set("Connecting…")
        self._submit(lambda: self._do_connect(entry), self._on_connected, self._on_connect_error)

    def _do_connect(self, entry):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = dict(
            hostname=entry["host"],
            port=entry.get("port", 22),
            username=entry.get("user"),
            timeout=10,
            # `timeout` alone only bounds the initial TCP connect -- a stalled
            # SSH banner exchange or auth phase can otherwise hang forever.
            banner_timeout=10,
            auth_timeout=10,
        )
        key = entry.get("key")
        if key:
            connect_kwargs["key_filename"] = key
        client.connect(**connect_kwargs)
        sftp = client.open_sftp()
        remote_start = sftp.normalize(".")
        return client, sftp, remote_start

    def _on_connected(self, result):
        client, sftp, remote_start = result
        self.ssh_client = client
        self.sftp_client = sftp
        self._remote_home_dir = remote_start
        self.transfer_status_var.set(f"Connected to {self.transfer_server_var.get()}")
        self._set_connect_button_connected(True)
        self._remote_refresh(remote_start)

    def _on_connect_error(self, exc):
        self.transfer_status_var.set("Not connected")
        self._set_connect_button_connected(False)
        messagebox.showerror("Portkey", self._describe_connect_error(exc))

    def _set_connect_button_connected(self, connected):
        if connected:
            self.connect_server_btn.config(
                text="✓  Connected",
                bg=BG_PANEL_LIGHT,
                fg=GOLD,
                activebackground=BG_PANEL,
                activeforeground=GOLD,
            )
        else:
            self.connect_server_btn.config(
                text="Connect",
                bg=GOLD,
                fg=BG_DARK,
                activebackground=GOLD_DIM,
                activeforeground=BG_DARK,
            )

    def _describe_connect_error(self, exc):
        if isinstance(exc, paramiko.AuthenticationException):
            return "Authentication failed — check the username/key for this server."
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return "Connection timed out — check the host/port."
        if isinstance(exc, (paramiko.SSHException, OSError)):
            return f"Could not reach the server:\n{exc}"
        return f"Connection failed:\n{exc}"

    def transfer_disconnect(self):
        if self.sftp_client is not None:
            try:
                self.sftp_client.close()
            except Exception:
                pass
            self.sftp_client = None
        if self.ssh_client is not None:
            try:
                self.ssh_client.close()
            except Exception:
                pass
            self.ssh_client = None
        self._transfer_entry = None
        self.transfer_status_var.set("Not connected")
        if hasattr(self, "connect_server_btn"):
            self._set_connect_button_connected(False)
        # Local rows are deliberately left alone -- browsing the local
        # pane doesn't depend on the SSH/SFTP session (see show_transfer),
        # so disconnecting/reconnecting the remote side shouldn't blow away
        # local_listbox's already-rendered rows out from under it.
        self.remote_rows = []
        self.remote_filtered_rows = []
        self.remote_path_var.set("")
        if hasattr(self, "remote_listbox"):
            self.remote_listbox.delete(0, tk.END)

    def _reconnect_transfer_session(self):
        # Called from a _submit worker thread when an in-flight SFTP call
        # looks like the session died underneath it (idle timeout, network
        # blip, etc). One attempt, with a short cooldown against a truly
        # dead server turning a multi-file batch into a string of connect
        # timeouts -- one per file.
        if not self._transfer_entry:
            return False
        if time.monotonic() - self._reconnect_last_failed_at < 5:
            return False
        try:
            client, sftp, _ = self._do_connect(self._transfer_entry)
        except Exception:
            self._reconnect_last_failed_at = time.monotonic()
            return False
        if self.ssh_client is not None:
            try:
                self.ssh_client.close()
            except Exception:
                pass
        self.ssh_client = client
        self.sftp_client = sftp
        self._did_reconnect = True
        return True

    def _sftp_retry(self, fn):
        # fn is a zero-arg callable that uses self.sftp_client. Must only be
        # called from within a _submit worker thread (never the main thread),
        # since a failed attempt blocks on a fresh SSH connect.
        try:
            return fn()
        except (EOFError, OSError, paramiko.SSHException):
            if not self._reconnect_transfer_session():
                raise
            return fn()

    # -- listing / navigation --
    def _render_listbox(self, listbox, rows):
        listbox.delete(0, tk.END)
        for row in rows:
            name = row["name"] + ("/" if row["is_dir"] else "")
            size_str = "" if row["is_dir"] else format_size(row["size"])
            listbox.insert(tk.END, f"{name:<26.26} {size_str:>7}")

    def _apply_local_search(self):
        query = self.local_search_var.get().strip().lower()
        if query == FILE_SEARCH_PLACEHOLDER.lower():
            query = ""
        self.local_filtered_rows = [r for r in self.local_rows if query in r["name"].lower()]
        self._render_listbox(self.local_listbox, self.local_filtered_rows)

    def _apply_remote_search(self):
        query = self.remote_search_var.get().strip().lower()
        if query == FILE_SEARCH_PLACEHOLDER.lower():
            query = ""
        self.remote_filtered_rows = [r for r in self.remote_rows if query in r["name"].lower()]
        self._render_listbox(self.remote_listbox, self.remote_filtered_rows)

    def _local_refresh(self, path):
        try:
            target = Path(path)
            rows = []
            for item in target.iterdir():
                try:
                    st = item.stat()
                except OSError:
                    continue
                # Legacy per-user junctions like "Application Data", "Cookies",
                # "Local Settings" etc. are marked hidden+system -- Explorer
                # hides them by default, and for good reason: iterating INTO
                # one raises WinError 5 Access Denied even though listing the
                # parent folder that contains it succeeds fine. item.stat()
                # follows the reparse point and reports the *target's*
                # attributes (an ordinary, non-hidden folder) -- lstat() is
                # needed to see the junction's own flags instead.
                try:
                    link_attrs = item.lstat().st_file_attributes
                except OSError:
                    link_attrs = 0
                if link_attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM):
                    continue
                rows.append(
                    {
                        "name": item.name,
                        "is_dir": item.is_dir(),
                        "size": None if item.is_dir() else st.st_size,
                        "mtime": st.st_mtime,
                    }
                )
        except Exception as exc:
            messagebox.showerror("Portkey", f"Failed to list local folder:\n{exc}")
            return
        rows.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        self.local_dir = str(target)
        self.local_rows = rows
        self.local_path_var.set(self.local_dir)
        self._apply_local_search()

    def local_on_navigate(self, _event=None):
        selection = self.local_listbox.curselection()
        if not selection:
            return
        row = self.local_filtered_rows[selection[0]]
        if row["is_dir"]:
            self._local_refresh(str(Path(self.local_dir) / row["name"]))

    def local_on_up(self):
        self._local_refresh(str(Path(self.local_dir).parent))

    def _remote_join(self, base, name):
        return posixpath.normpath(posixpath.join(base, name))

    def _remote_parent(self, path):
        return posixpath.dirname(path.rstrip("/")) or "/"

    def _do_remote_list(self, path):
        with self._sftp_lock:
            attrs = self._sftp_retry(lambda: self.sftp_client.listdir_attr(path))
        rows = []
        for a in attrs:
            is_dir = stat.S_ISDIR(a.st_mode) if a.st_mode is not None else False
            rows.append(
                {
                    "name": a.filename,
                    "is_dir": is_dir,
                    "size": None if is_dir else a.st_size,
                    "mtime": a.st_mtime,
                }
            )
        rows.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return path, rows

    def _remote_refresh(self, path):
        if not self.sftp_client:
            return
        self._submit(lambda: self._do_remote_list(path), self._on_remote_listed, self._on_remote_list_error)

    def _on_remote_listed(self, result):
        path, rows = result
        self.remote_dir = path
        self.remote_rows = rows
        self.remote_path_var.set(self.remote_dir)
        self._apply_remote_search()

    def _on_remote_list_error(self, exc):
        messagebox.showerror("Portkey", f"Failed to list remote folder:\n{exc}")

    def remote_on_navigate(self, _event=None):
        selection = self.remote_listbox.curselection()
        if not selection:
            return
        row = self.remote_filtered_rows[selection[0]]
        if row["is_dir"]:
            self._remote_refresh(self._remote_join(self.remote_dir, row["name"]))

    def remote_on_up(self):
        if not self.sftp_client:
            return
        self._remote_refresh(self._remote_parent(self.remote_dir))

    def remote_on_home(self):
        if not self.sftp_client or not self._remote_home_dir:
            return
        self._remote_refresh(self._remote_home_dir)

    # -- remote mutations: rename / mkdir / delete --
    def _on_remote_mutate_done(self, remote_dir_snapshot):
        self._remote_refresh(remote_dir_snapshot)

    def _on_remote_mutate_error(self, exc):
        messagebox.showerror("Portkey", f"Operation failed:\n{exc}")

    def remote_on_new_folder(self):
        if not self.sftp_client:
            messagebox.showerror("Portkey", "Connect to a server first.")
            return
        name = self._show_input_dialog("New Folder", "Folder name:")
        if not name:
            return
        remote_dir_snapshot = self.remote_dir
        new_path = self._remote_join(remote_dir_snapshot, name)

        def work():
            with self._sftp_lock:
                self._sftp_retry(lambda: self.sftp_client.mkdir(new_path))
            return remote_dir_snapshot

        self._submit(work, self._on_remote_mutate_done, self._on_remote_mutate_error)

    def remote_on_rename(self):
        if not self.sftp_client:
            messagebox.showerror("Portkey", "Connect to a server first.")
            return
        selection = self.remote_listbox.curselection()
        if len(selection) != 1:
            messagebox.showerror("Portkey", "Select a single item to rename.")
            return
        row = self.remote_filtered_rows[selection[0]]
        new_name = self._show_input_dialog(
            "Rename", f"New name for {row['name']}:", initial=row["name"]
        )
        if not new_name or new_name == row["name"]:
            return
        remote_dir_snapshot = self.remote_dir
        old_path = self._remote_join(remote_dir_snapshot, row["name"])
        new_path = self._remote_join(remote_dir_snapshot, new_name)

        def work():
            with self._sftp_lock:
                self._sftp_retry(lambda: self.sftp_client.rename(old_path, new_path))
            return remote_dir_snapshot

        self._submit(work, self._on_remote_mutate_done, self._on_remote_mutate_error)

    def remote_on_delete(self):
        if not self.sftp_client:
            messagebox.showerror("Portkey", "Connect to a server first.")
            return
        selection = self.remote_listbox.curselection()
        if not selection:
            return
        rows = [self.remote_filtered_rows[i] for i in selection]
        if len(rows) == 1:
            prompt = f"Delete '{rows[0]['name']}'?"
        else:
            names = [r["name"] for r in rows[:5]]
            more = f"\n...and {len(rows) - 5} more" if len(rows) > 5 else ""
            prompt = f"Delete {len(rows)} selected items?\n\n" + "\n".join(names) + more
        if not self._show_confirm_dialog("Delete", prompt):
            return

        remote_dir_snapshot = self.remote_dir
        targets = [(self._remote_join(remote_dir_snapshot, r["name"]), r["is_dir"]) for r in rows]

        def work():
            failures = []
            with self._sftp_lock:
                for path, is_dir in targets:
                    try:
                        if is_dir:
                            self._sftp_retry(lambda p=path: self.sftp_client.rmdir(p))
                        else:
                            self._sftp_retry(lambda p=path: self.sftp_client.remove(p))
                    except (IOError, OSError) as exc:
                        failures.append((posixpath.basename(path), str(exc)))
            return remote_dir_snapshot, failures

        self._submit(work, self._on_remote_delete_done, self._on_remote_mutate_error)

    def _on_remote_delete_done(self, result):
        remote_dir_snapshot, failures = result
        self._remote_refresh(remote_dir_snapshot)
        if failures:
            lines = "\n".join(f"- {name}: {msg}" for name, msg in failures)
            messagebox.showwarning("Portkey", f"Some items couldn't be deleted:\n{lines}")

    # -- transfer --
    def _reset_progress(self):
        self.progress_fill.place_forget()
        self.progress_label.config(text="")

    def _make_progress_cb(self, file_index=1, file_total=1, filename=""):
        def cb(transferred, total):
            self._task_queue.put(
                (self._on_progress, (file_index, file_total, filename, transferred, total))
            )

        return cb

    def _on_progress(self, payload):
        file_index, file_total, filename, transferred, total = payload
        frac = 0 if not total else min(1.0, transferred / total)
        if frac <= 0:
            self.progress_fill.place_forget()
        else:
            self.progress_fill.place(x=0, y=0, relwidth=frac, relheight=1)
        prefix = f"({file_index}/{file_total}) {filename} — " if file_total > 1 else ""
        self.progress_label.config(
            text=f"{prefix}{format_size(transferred)} / {format_size(total)}" if total else ""
        )

    def _report_batch_issues(self, skipped_dirs, failures, verb):
        parts = []
        if skipped_dirs:
            noun = "folder" if skipped_dirs == 1 else "folders"
            parts.append(f"{skipped_dirs} {noun} skipped (folders aren't supported yet).")
        if failures:
            lines = "\n".join(f"- {name}: {msg}" for name, msg in failures)
            parts.append(f"Failed to {verb}:\n{lines}")
        if parts:
            messagebox.showwarning("Portkey", "\n\n".join(parts))

    def transfer_on_upload(self):
        if not self.sftp_client:
            messagebox.showerror("Portkey", "Connect to a server first.")
            return
        selection = self.local_listbox.curselection()
        if not selection:
            return
        rows = [self.local_filtered_rows[i] for i in selection]
        files = [r for r in rows if not r["is_dir"]]
        skipped_dirs = len(rows) - len(files)
        if not files:
            messagebox.showerror("Portkey", "Select at least one file to upload (folders aren't supported yet).")
            return
        local_dir_snapshot = self.local_dir
        remote_dir_snapshot = self.remote_dir
        self._reset_progress()

        def work():
            failures = []
            with self._sftp_lock:
                for idx, row in enumerate(files, start=1):
                    local_path = str(Path(local_dir_snapshot) / row["name"])
                    remote_path = self._remote_join(remote_dir_snapshot, row["name"])
                    try:
                        self._sftp_retry(lambda lp=local_path, rp=remote_path: self.sftp_client.put(
                            lp, rp,
                            callback=self._make_progress_cb(idx, len(files), row["name"]),
                        ))
                    except Exception as exc:
                        failures.append((row["name"], str(exc)))
            return remote_dir_snapshot, skipped_dirs, failures

        self._submit(work, self._on_upload_done, self._on_transfer_error)

    def _on_upload_done(self, result):
        remote_dir_snapshot, skipped_dirs, failures = result
        self._reset_progress()
        self._remote_refresh(remote_dir_snapshot)
        self._report_batch_issues(skipped_dirs, failures, "upload")

    def transfer_on_download(self):
        if not self.sftp_client:
            messagebox.showerror("Portkey", "Connect to a server first.")
            return
        selection = self.remote_listbox.curselection()
        if not selection:
            return
        rows = [self.remote_filtered_rows[i] for i in selection]
        files = [r for r in rows if not r["is_dir"]]
        skipped_dirs = len(rows) - len(files)
        if not files:
            messagebox.showerror("Portkey", "Select at least one file to download (folders aren't supported yet).")
            return
        remote_dir_snapshot = self.remote_dir
        local_dir_snapshot = self.local_dir
        self._reset_progress()

        def work():
            failures = []
            with self._sftp_lock:
                for idx, row in enumerate(files, start=1):
                    remote_path = self._remote_join(remote_dir_snapshot, row["name"])
                    local_path = str(Path(local_dir_snapshot) / row["name"])
                    try:
                        self._sftp_retry(lambda rp=remote_path, lp=local_path: self.sftp_client.get(
                            rp, lp,
                            callback=self._make_progress_cb(idx, len(files), row["name"]),
                        ))
                    except Exception as exc:
                        failures.append((row["name"], str(exc)))
            return local_dir_snapshot, skipped_dirs, failures

        self._submit(work, self._on_download_done, self._on_transfer_error)

    def _on_download_done(self, result):
        local_dir_snapshot, skipped_dirs, failures = result
        self._reset_progress()
        self._local_refresh(local_dir_snapshot)
        self._report_batch_issues(skipped_dirs, failures, "download")

    def _on_transfer_error(self, exc):
        self._reset_progress()
        messagebox.showerror("Portkey", f"Transfer failed:\n{exc}")

    # -- in-app pane-to-pane drag and drop (local<->remote only; dragging in
    # from the OS file explorer is a separate, deferred feature) --
    def _widget_is_or_within(self, widget, ancestor):
        while widget is not None:
            if widget is ancestor:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _on_pane_drag_press(self, event):
        self._drag_start = (event.x_root, event.y_root)
        self._drag_start_widget = event.widget
        self._drag_active = False

    def _drag_badge_text(self, source_widget):
        if source_widget is self.local_listbox:
            selection, rows = self.local_listbox.curselection(), self.local_filtered_rows
        else:
            selection, rows = self.remote_listbox.curselection(), self.remote_filtered_rows
        names = [rows[i]["name"] for i in selection if i < len(rows)]
        if not names:
            return None
        return f"→ {names[0]}" if len(names) == 1 else f"→ {len(names)} items"

    def _show_drag_badge(self, event, text):
        badge = tk.Toplevel(self)
        badge.title("Portkey Drag Badge")
        badge.overrideredirect(True)
        badge.attributes("-topmost", True)
        badge.configure(bg=GOLD_DIM)
        inner = tk.Frame(badge, bg=BG_PANEL)
        inner.pack(padx=1, pady=1)
        tk.Label(
            inner, text=text, bg=BG_PANEL, fg=GOLD, font=("Segoe UI", 9, "bold"), padx=8, pady=4
        ).pack()
        badge.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
        self._drag_badge = badge

    def _move_drag_badge(self, event):
        if self._drag_badge is not None and self._drag_badge.winfo_exists():
            self._drag_badge.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")

    def _hide_drag_badge(self):
        if self._drag_badge is not None:
            if self._drag_badge.winfo_exists():
                self._drag_badge.destroy()
            self._drag_badge = None

    def _on_pane_drag_motion(self, event):
        if self._drag_start is None:
            return
        if not self._drag_active:
            start_x, start_y = self._drag_start
            if abs(event.x_root - start_x) > 5 or abs(event.y_root - start_y) > 5:
                self._drag_active = True
                event.widget.config(cursor="hand2")
                text = self._drag_badge_text(event.widget)
                if text:
                    self._show_drag_badge(event, text)
        if self._drag_active:
            self._move_drag_badge(event)
            # Suppress the Listbox's own default drag-to-extend-selection
            # behavior once a real drag has taken over -- otherwise dragging
            # to move a file to the other pane also silently multi-selects
            # whatever rows the cursor passes over, which is confusing and
            # redundant now that Ctrl/Shift+click already handles multi-select.
            return "break"

    def _on_pane_drag_release(self, event):
        was_dragging = self._drag_active
        source_widget = self._drag_start_widget
        self._drag_start = None
        self._drag_active = False
        self._hide_drag_badge()
        if source_widget is not None:
            source_widget.config(cursor="")
        if not was_dragging or source_widget is None:
            return

        target = self.winfo_containing(event.x_root, event.y_root)
        if target is not None:
            # Check against the whole pane (header/listbox/buttons), not just
            # the raw Listbox widget -- a real mouse release can easily land
            # on the listbox's wrapper frame or padding a pixel or two off
            # the widget's exact bounds, and that should still count as
            # "dropped on this pane".
            if source_widget is self.local_listbox and self._widget_is_or_within(target, self.remote_pane):
                self.transfer_on_upload()
            elif source_widget is self.remote_listbox and self._widget_is_or_within(target, self.local_pane):
                self.transfer_on_download()
        return "break"


if __name__ == "__main__":
    app = PortkeyApp()
    app.mainloop()
