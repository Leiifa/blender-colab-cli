"""Colab Render — Account switcher, live GPU/render monitor, and Blender launcher.

Runs windowless on Windows (pythonw.exe) and monitors renders launched from the GUI,
terminal, or AI agents via ~/.config/colab-cli/jobs/.

CLI usage:
  python colab_accounts.py                     # Open GUI
  python colab_accounts.py monitor             # Open GUI detached on Monitor tab
  python colab_accounts.py list [--json]       # List saved accounts & live balances
  python colab_accounts.py active [--json]     # Show active account & balance
  python colab_accounts.py vms [--json]        # List running VMs across all accounts
  python colab_accounts.py switch EMAIL        # Switch active account
  python colab_accounts.py stop-vms [EMAIL]    # Stop all running VMs on an account
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
RENDER_SCRIPT = SCRIPT_DIR / "colab_render.py"
CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# Auto-relaunch inside google-colab-cli's uv venv when invoked with system python
# ---------------------------------------------------------------------------
def _find_uv_colab_python(*, windowless: bool = False) -> Optional[Path]:
    exe_name = "pythonw.exe" if (windowless and os.name == "nt") else ("python.exe" if os.name == "nt" else "python")
    candidates = [
        Path(os.environ.get("APPDATA", "")) / "uv" / "tools" / "google-colab-cli" / "Scripts" / exe_name,
        Path(os.environ.get("LOCALAPPDATA", "")) / "uv" / "tools" / "google-colab-cli" / "Scripts" / exe_name,
        Path.home() / ".local" / "share" / "uv" / "tools" / "google-colab-cli" / "bin" / exe_name,
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


try:
    import keyring  # noqa: F401
    import colab_cli.auth  # noqa: F401
except ImportError:
    uv_py = _find_uv_colab_python(windowless=False)
    if uv_py and Path(sys.executable).resolve() != uv_py.resolve():
        raise SystemExit(subprocess.call([str(uv_py), str(Path(__file__).resolve()), *sys.argv[1:]]))
    raise SystemExit("google-colab-cli is not installed. Run: uv tool install google-colab-cli")

from importlib import resources
import keyring
from google.auth.transport.requests import AuthorizedSession, Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from colab_cli.auth import (
    KEYRING_SERVICE_NAME,
    KEYRING_USERNAME,
    PUBLIC_SCOPES,
    REMOTE_REDIRECT_URI,
)
from colab_cli.client import Client, Prod

ACCOUNTS_KEYRING_SERVICE = "google-colab-cli-accounts"
CONFIG_DIR = Path.home() / ".config" / "colab-cli"
ACCOUNTS_INDEX_PATH = CONFIG_DIR / "accounts.json"
SESSIONS_PATH = CONFIG_DIR / "sessions.json"
PROFILES_DIR = CONFIG_DIR / "profiles"
JOBS_DIR = CONFIG_DIR / "jobs"
INBOX_DIR = CONFIG_DIR / "inbox"
GUI_PID_PATH = CONFIG_DIR / "gui.pid"
GUI_HEARTBEAT_PATH = CONFIG_DIR / "gui_heartbeat.json"


# ---------------------------------------------------------------------------
# Account store & Colab API helpers
# ---------------------------------------------------------------------------
def _safe_slug(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", email.lower()).strip("_") or "account"


def _is_pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == 259  # STILL_ACTIVE
            return False
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _load_oauth_client_config() -> dict:
    custom = Path.home() / ".colab-cli-oauth-config.json"
    if custom.is_file():
        return json.loads(custom.read_text(encoding="utf-8"))
    ref = resources.files("colab_cli").joinpath("oauth_config.json")
    return json.loads(ref.read_text(encoding="utf-8"))


def _load_index() -> dict[str, Any]:
    if ACCOUNTS_INDEX_PATH.is_file():
        try:
            data = json.loads(ACCOUNTS_INDEX_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("accounts"), dict):
                return data
        except Exception:
            pass
    return {"active": None, "accounts": {}}


def _save_index(data: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ACCOUNTS_INDEX_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, ACCOUNTS_INDEX_PATH)


def _get_active_token_json() -> Optional[str]:
    return keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)


def _set_active_token_json(token_json: Optional[str]) -> None:
    if token_json is None:
        try:
            keyring.delete_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME)
        except Exception:
            pass
    else:
        keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_USERNAME, token_json)


def _get_account_token_json(email: str) -> Optional[str]:
    return keyring.get_password(ACCOUNTS_KEYRING_SERVICE, email.lower())


def _set_account_token_json(email: str, token_json: str) -> None:
    keyring.set_password(ACCOUNTS_KEYRING_SERVICE, email.lower(), token_json)


def _delete_account_token(email: str) -> None:
    try:
        keyring.delete_password(ACCOUNTS_KEYRING_SERVICE, email.lower())
    except Exception:
        pass


def _credentials_from_json(token_json: str) -> Credentials:
    return Credentials.from_authorized_user_info(json.loads(token_json), PUBLIC_SCOPES)


def _fetch_email_for_credentials(creds: Credentials) -> str:
    if not creds.valid or not creds.token:
        creds.refresh(Request())
    qs = urllib.parse.urlencode({"access_token": creds.token})
    url = f"https://oauth2.googleapis.com/tokeninfo?{qs}"
    with urllib.request.urlopen(url, timeout=12) as resp:
        info = json.loads(resp.read().decode("utf-8"))
    email = info.get("email")
    if not email:
        raise RuntimeError("Could not read account email from Google token.")
    return email.lower()


def _query_account_live_status(token_json: str) -> dict[str, Any]:
    creds = _credentials_from_json(token_json)
    refreshed = False
    if not creds.valid or not creds.token:
        creds.refresh(Request())
        refreshed = True

    email = _fetch_email_for_credentials(creds)
    sess = AuthorizedSession(creds)
    client = Client(Prod(), sess)
    ccu = client.get_consumption_user_info()
    assignments = client.list_assignments()
    return {
        "email": email,
        "balance": float(ccu.paid_compute_units_balance),
        "rate": float(ccu.consumption_rate_hourly),
        "assignments_count": len(assignments),
        "assignments": [
            {
                "endpoint": a.endpoint,
                "accelerator": a.accelerator.value,
                "shape": getattr(a.machine_shape, "name", str(a.machine_shape)),
            }
            for a in assignments
        ],
        "token_json": creds.to_json() if refreshed else token_json,
        "checked_at": datetime.now().strftime("%H:%M"),
    }


def _endpoint_session_map(email: str, *, is_active: bool) -> dict[str, str]:
    ep_map: dict[str, str] = {}
    paths = [SESSIONS_PATH] if is_active else []
    paths.append(PROFILES_DIR / f"{_safe_slug(email)}_sessions.json")
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for sname, sdata in data.items():
                    if isinstance(sdata, dict) and sdata.get("endpoint"):
                        ep_map.setdefault(str(sdata["endpoint"]), str(sname))
        except Exception:
            pass
    return ep_map


def stop_one_vm(email: str, endpoint: str) -> None:
    email = email.lower().strip()
    token_json = _get_account_token_json(email)
    if not token_json:
        idx = _load_index()
        if idx.get("active") == email:
            token_json = _get_active_token_json()
    if not token_json:
        raise RuntimeError(f"No stored token for {email}")

    creds = _credentials_from_json(token_json)
    if not creds.valid or not creds.token:
        creds.refresh(Request())
    client = Client(Prod(), AuthorizedSession(creds))
    client.unassign(endpoint)

    idx = _load_index()
    paths = [PROFILES_DIR / f"{_safe_slug(email)}_sessions.json"]
    if idx.get("active") == email:
        paths.append(SESSIONS_PATH)
    for p in paths:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cleaned = {k: v for k, v in data.items() if not (isinstance(v, dict) and v.get("endpoint") == endpoint)}
                if len(cleaned) != len(data):
                    p.write_text(json.dumps(cleaned, indent=2), encoding="utf-8")
        except Exception:
            pass


def stop_all_vms_for_account(email: str) -> int:
    token_json = _get_account_token_json(email)
    if not token_json:
        idx = _load_index()
        if idx.get("active") == email.lower():
            token_json = _get_active_token_json()
    if not token_json:
        raise RuntimeError(f"No stored token for {email}")

    creds = _credentials_from_json(token_json)
    if not creds.valid or not creds.token:
        creds.refresh(Request())
    client = Client(Prod(), AuthorizedSession(creds))
    assignments = client.list_assignments()
    stopped = 0
    for a in assignments:
        client.unassign(a.endpoint)
        stopped += 1

    idx = _load_index()
    if idx.get("active") == email.lower() and SESSIONS_PATH.is_file():
        SESSIONS_PATH.write_text("{}", encoding="utf-8")
    prof_sess = PROFILES_DIR / f"{_safe_slug(email)}_sessions.json"
    if prof_sess.is_file():
        prof_sess.write_text("{}", encoding="utf-8")
    return stopped


def sync_current_cli_login() -> Optional[str]:
    idx = _load_index()
    active_tok = _get_active_token_json()
    if not active_tok:
        if idx.get("active"):
            idx["active"] = None
            _save_index(idx)
        return None

    try:
        active_rt = json.loads(active_tok).get("refresh_token")
    except Exception:
        active_rt = None

    if active_rt:
        for em in list(idx["accounts"].keys()):
            stored = _get_account_token_json(em)
            if stored:
                try:
                    if json.loads(stored).get("refresh_token") == active_rt:
                        idx["active"] = em
                        _save_index(idx)
                        return em
                except Exception:
                    pass

    try:
        creds = _credentials_from_json(active_tok)
        email = _fetch_email_for_credentials(creds)
        updated_tok = creds.to_json()
        _set_account_token_json(email, updated_tok)
        _set_active_token_json(updated_tok)
        idx["accounts"].setdefault(email, {"added_at": datetime.now().isoformat(timespec="seconds")})
        idx["active"] = email
        _save_index(idx)
        return email
    except Exception:
        return idx.get("active")


def _swap_sessions_file(old_email: Optional[str], new_email: Optional[str]) -> None:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    if old_email and SESSIONS_PATH.is_file():
        shutil.copy2(SESSIONS_PATH, PROFILES_DIR / f"{_safe_slug(old_email)}_sessions.json")
    if new_email:
        target = PROFILES_DIR / f"{_safe_slug(new_email)}_sessions.json"
        if target.is_file():
            shutil.copy2(target, SESSIONS_PATH)
        elif SESSIONS_PATH.is_file():
            SESSIONS_PATH.write_text("{}", encoding="utf-8")
    elif SESSIONS_PATH.is_file():
        SESSIONS_PATH.write_text("{}", encoding="utf-8")


def switch_active_account(email: str) -> str:
    email = email.lower().strip()
    idx = _load_index()
    if email not in idx["accounts"]:
        matches = [e for e in idx["accounts"] if email in e]
        if len(matches) == 1:
            email = matches[0]
        else:
            raise RuntimeError(f"Account '{email}' not found. Saved: {', '.join(idx['accounts']) or 'none'}")

    tok = _get_account_token_json(email)
    if not tok:
        raise RuntimeError(f"Saved sign-in for '{email}' is missing. Please add the account again.")

    old_active = idx.get("active")
    if old_active and old_active != email:
        cur_tok = _get_active_token_json()
        if cur_tok:
            _set_account_token_json(old_active, cur_tok)
    _swap_sessions_file(old_active, email)
    _set_active_token_json(tok)
    idx["active"] = email
    _save_index(idx)
    return email


def remove_saved_account(email: str) -> None:
    email = email.lower().strip()
    idx = _load_index()
    _delete_account_token(email)
    idx["accounts"].pop(email, None)
    prof_sess = PROFILES_DIR / f"{_safe_slug(email)}_sessions.json"
    if prof_sess.is_file():
        try:
            prof_sess.unlink()
        except OSError:
            pass
    if idx.get("active") == email:
        _set_active_token_json(None)
        if SESSIONS_PATH.is_file():
            SESSIONS_PATH.write_text("{}", encoding="utf-8")
        idx["active"] = None
    _save_index(idx)


def begin_oauth_flow() -> tuple[InstalledAppFlow, str]:
    client_config = _load_oauth_client_config()
    flow = InstalledAppFlow.from_client_config(client_config, PUBLIC_SCOPES)
    flow.redirect_uri = REMOTE_REDIRECT_URI
    auth_url, _ = flow.authorization_url(prompt="select_account consent", token_usage="remote")
    return flow, auth_url


def finish_oauth_flow(flow: InstalledAppFlow, code: str, *, activate: bool = True) -> dict[str, Any]:
    flow.fetch_token(code=code.strip())
    creds = flow.credentials
    tok_json = creds.to_json()
    info = _query_account_live_status(tok_json)
    email = info["email"]
    final_tok = info["token_json"]

    idx = _load_index()
    old_active = idx.get("active")
    if activate and old_active and old_active != email:
        cur_tok = _get_active_token_json()
        if cur_tok:
            _set_account_token_json(old_active, cur_tok)

    _set_account_token_json(email, final_tok)
    idx["accounts"][email] = {
        "added_at": idx["accounts"].get(email, {}).get("added_at") or datetime.now().isoformat(timespec="seconds"),
        "balance": info["balance"],
        "rate": info["rate"],
        "assignments_count": info["assignments_count"],
        "checked_at": info["checked_at"],
    }
    if activate or not old_active:
        _swap_sessions_file(old_active, email)
        _set_active_token_json(final_tok)
        idx["active"] = email
    _save_index(idx)
    return info


def _fmt_eta(seconds: float) -> str:
    if seconds <= 0:
        return ""
    s = int(seconds)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"~{h}h {m:02d}m left"
    if m:
        return f"~{m}m {sec:02d}s left"
    return f"~{sec}s left"


def _open_path_in_explorer(path: str) -> None:
    if not path or not os.path.exists(path):
        return
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


# ---------------------------------------------------------------------------
# Single-instance window detection (Windows HWND-aware)
# ---------------------------------------------------------------------------
def _find_gui_hwnd(pid: int) -> Optional[int]:
    if os.name != "nt" or not pid or pid <= 0:
        return None
    try:
        import ctypes.wintypes as wt

        user32 = ctypes.windll.user32
        found: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        def _cb(hwnd: int, _lp: int) -> bool:
            wpid = wt.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(_cb, 0)
        return found[0] if found else None
    except Exception:
        return None


def _focus_hwnd(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def _read_gui_pid() -> Optional[int]:
    if not GUI_PID_PATH.is_file():
        return None
    try:
        return int(GUI_PID_PATH.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def is_gui_running(*, cleanup_zombie: bool = False) -> bool:
    pid = _read_gui_pid()
    if not pid or not _is_pid_alive(pid):
        if cleanup_zombie and GUI_PID_PATH.is_file():
            GUI_PID_PATH.unlink(missing_ok=True)
        return False
    if os.name == "nt":
        if _find_gui_hwnd(pid) is not None:
            return True
        try:
            age = time.time() - GUI_PID_PATH.stat().st_mtime
        except OSError:
            age = 999.0
        if age < 5.0:
            return True
        if cleanup_zombie:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATION_FLAGS,
                check=False,
            )
            GUI_PID_PATH.unlink(missing_ok=True)
        return False
    return True


def ensure_gui_open(*, tab: str = "monitor", focus_if_open: bool = False) -> bool:
    if os.environ.get("COLAB_NO_GUI", "").strip().lower() in ("1", "true", "yes"):
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    if is_gui_running(cleanup_zombie=True):
        if focus_if_open:
            pid = _read_gui_pid()
            if pid:
                hwnd = _find_gui_hwnd(pid)
                if hwnd:
                    _focus_hwnd(hwnd)
        return False

    py_exe = _find_uv_colab_python(windowless=True) or _find_uv_colab_python(windowless=False)
    if not py_exe:
        exe = Path(sys.executable)
        if os.name == "nt" and exe.name.lower() == "python.exe" and exe.with_name("pythonw.exe").is_file():
            py_exe = exe.with_name("pythonw.exe")
        else:
            py_exe = exe

    cmd = [str(py_exe), str(Path(__file__).resolve()), "gui", "--tab", tab]
    kwargs: dict[str, Any] = dict(
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 1  # SW_SHOWNORMAL
        kwargs["startupinfo"] = si
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# GUI — Clean, native-feeling dark theme
# ---------------------------------------------------------------------------
BG = "#111215"
SURFACE = "#181a1f"
CONTROL = "#22252c"
HOVER = "#2c303a"
BORDER = "#252830"
TEXT = "#eceef2"
MUTED = "#8b91a0"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
GREEN = "#34d399"
AMBER = "#fbbf24"
RED = "#f87171"
DANGER_BG = "#2e1a1d"
DANGER_HOVER = "#432227"


def launch_gui(*, initial_tab: str = "monitor") -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    if is_gui_running(cleanup_zombie=True):
        pid = _read_gui_pid()
        if pid:
            hwnd = _find_gui_hwnd(pid)
            if hwnd:
                _focus_hwnd(hwnd)
        return 0
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        GUI_PID_PATH.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    root = tk.Tk()
    root.title("Colab Render")
    root.geometry("1040x660")
    root.minsize(900, 560)
    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(
        "Clean.Treeview",
        background=SURFACE,
        foreground=TEXT,
        fieldbackground=SURFACE,
        borderwidth=0,
        rowheight=32,
        font=("Segoe UI", 9),
    )
    style.configure(
        "Clean.Treeview.Heading",
        background=SURFACE,
        foreground=MUTED,
        borderwidth=0,
        relief="flat",
        font=("Segoe UI", 9),
        padding=(8, 6),
    )
    style.map("Clean.Treeview", background=[("selected", "#233047")], foreground=[("selected", TEXT)])
    style.map("Clean.Treeview.Heading", background=[("active", SURFACE)])

    style.configure("Clean.TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "Clean.TNotebook.Tab",
        background=BG,
        foreground=MUTED,
        padding=(16, 8),
        font=("Segoe UI", 9, "bold"),
        borderwidth=0,
    )
    style.map(
        "Clean.TNotebook.Tab",
        background=[("selected", SURFACE), ("active", "#1c1f26")],
        foreground=[("selected", TEXT), ("active", TEXT)],
    )
    style.configure(
        "Clean.Horizontal.TProgressbar",
        troughcolor=CONTROL,
        background=ACCENT,
        bordercolor=CONTROL,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        thickness=8,
    )
    style.configure(
        "Clean.TCombobox",
        fieldbackground=CONTROL,
        background=CONTROL,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=BORDER,
        padding=4,
    )

    def make_btn(parent, text, command, *, bg=CONTROL, fg=TEXT, hover=HOVER, padx=12, pady=5, font_size=9, bold=False):
        weight = "bold" if bold else "normal"
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
            disabledforeground="#555b69",
            relief="flat",
            bd=0,
            cursor="hand2",
            padx=padx,
            pady=pady,
            font=("Segoe UI", font_size, weight),
        )
        b.bind("<Enter>", lambda _e: b.configure(bg=hover) if b["state"] != "disabled" else None)
        b.bind("<Leave>", lambda _e: b.configure(bg=bg) if b["state"] != "disabled" else None)
        return b

    live_cache: dict[str, dict[str, Any]] = {}
    busy = {"refresh": False}
    log_state: dict[str, Any] = {
        "selected_run_id": None,
        "tailed_run_id": None,
        "offset": 0,
        "last_out_dir": "",
        "thumb_path": "",
        "thumb_img": None,
        "jobs_by_label": {},
        "jobs_by_id": {},
    }
    gui_procs: list[subprocess.Popen] = []

    # --- Subtle bottom status strip (packed first so nothing ever clips) ---
    status_var = tk.StringVar(value="")
    status_bar = tk.Label(root, textvariable=status_var, bg=BG, fg=MUTED, anchor="w", padx=18, pady=6, font=("Segoe UI", 8))
    status_bar.pack(fill="x", side="bottom")

    # --- Compact single-row Top Bar ---
    topbar = tk.Frame(root, bg=SURFACE)
    topbar.pack(fill="x")
    tb_in = tk.Frame(topbar, bg=SURFACE)
    tb_in.pack(fill="x", padx=18, pady=10)

    tk.Label(tb_in, text="Colab Render", bg=SURFACE, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(side="left")

    # Clickable active-account summary right in the top bar
    pill = tk.Frame(tb_in, bg=CONTROL, cursor="hand2")
    pill.pack(side="left", padx=(16, 0))
    lbl_top_account = tk.Label(pill, text="Not signed in", bg=CONTROL, fg=AMBER, font=("Segoe UI", 9), padx=10, pady=4, cursor="hand2")
    lbl_top_account.pack(side="left")
    lbl_top_cu = tk.Label(pill, text="", bg=CONTROL, fg=GREEN, font=("Segoe UI", 9, "bold"), padx=0, pady=4, cursor="hand2")
    lbl_top_cu.pack(side="left", padx=(0, 10))
    lbl_top_vm = tk.Label(pill, text="", bg=CONTROL, fg=MUTED, font=("Segoe UI", 9), padx=0, pady=4, cursor="hand2")
    lbl_top_vm.pack(side="left", padx=(0, 10))

    tb_right = tk.Frame(tb_in, bg=SURFACE)
    tb_right.pack(side="right")

    # Hairline divider under top bar
    tk.Frame(root, bg=BORDER, height=1).pack(fill="x")

    # --- Tabs ---
    nb = ttk.Notebook(root, style="Clean.TNotebook")
    nb.pack(fill="both", expand=True, padx=18, pady=(12, 4))

    tab_monitor = tk.Frame(nb, bg=BG)
    tab_render = tk.Frame(nb, bg=BG)
    tab_accounts = tk.Frame(nb, bg=BG)
    nb.add(tab_monitor, text="Monitor")
    nb.add(tab_render, text="Render")
    nb.add(tab_accounts, text="Accounts")

    for w in (pill, lbl_top_account, lbl_top_cu, lbl_top_vm):
        w.bind("<Button-1>", lambda _e: nb.select(tab_accounts))

    # =========================================================================
    # TAB 1: MONITOR (left Jobs sidebar + right Progress / Log / Preview)
    # =========================================================================
    # Bottom VM strip packed FIRST so main content always gets remaining space
    vm_section = tk.Frame(tab_monitor, bg=SURFACE)
    vm_section.pack(fill="x", side="bottom", pady=(8, 0))

    vm_top = tk.Frame(vm_section, bg=SURFACE)
    vm_top.pack(fill="x", padx=14, pady=(8, 4))
    tk.Label(vm_top, text="Running VMs", bg=SURFACE, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left")
    lbl_total_vms = tk.Label(vm_top, text="None", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9))
    lbl_total_vms.pack(side="left", padx=(8, 0))
    vm_hdr_btns = tk.Frame(vm_top, bg=SURFACE)
    vm_hdr_btns.pack(side="right")

    vm_cols = ("account", "session", "gpu", "rate")
    vm_tree = ttk.Treeview(vm_section, columns=vm_cols, show="headings", style="Clean.Treeview", selectmode="browse", height=2)
    vm_tree.heading("account", text="Account")
    vm_tree.heading("session", text="Session")
    vm_tree.heading("gpu", text="GPU")
    vm_tree.heading("rate", text="Rate")
    vm_tree.column("account", width=260, anchor="w", stretch=True)
    vm_tree.column("session", width=180, anchor="w", stretch=False)
    vm_tree.column("gpu", width=90, anchor="center", stretch=False)
    vm_tree.column("rate", width=100, anchor="e", stretch=False)
    vm_tree.pack(fill="x", padx=10, pady=(0, 8))
    vm_row_map: dict[str, tuple[str, str]] = {}

    # Main Master-Detail split
    mon_body = tk.Frame(tab_monitor, bg=BG)
    mon_body.pack(fill="both", expand=True, pady=(10, 0))

    # Left Sidebar: live Jobs list (zero white dropdowns, all jobs visible at once!)
    jobs_sidebar = tk.Frame(mon_body, bg=SURFACE, width=260)
    jobs_sidebar.pack(side="left", fill="y", padx=(0, 10))
    jobs_sidebar.pack_propagate(False)

    js_hdr = tk.Frame(jobs_sidebar, bg=SURFACE)
    js_hdr.pack(fill="x", padx=12, pady=(10, 6))
    tk.Label(js_hdr, text="Jobs", bg=SURFACE, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left")
    btn_clear_hist = make_btn(js_hdr, "Clear", lambda: do_clear_history(), bg=SURFACE, hover=CONTROL, fg=MUTED, padx=8, pady=2, font_size=8)
    btn_clear_hist.pack(side="right")

    jobs_tree = ttk.Treeview(
        jobs_sidebar,
        columns=("name", "prog", "state"),
        show="headings",
        style="Clean.Treeview",
        selectmode="browse",
    )
    jobs_tree.heading("name", text="Shot")
    jobs_tree.heading("prog", text="Frames")
    jobs_tree.heading("state", text="Status")
    jobs_tree.column("name", width=104, anchor="w", stretch=True)
    jobs_tree.column("prog", width=66, anchor="e", stretch=False)
    jobs_tree.column("state", width=72, anchor="e", stretch=False)
    jobs_tree.tag_configure("active", foreground=GREEN)
    jobs_tree.tag_configure("busy", foreground=AMBER)
    jobs_tree.tag_configure("done", foreground=TEXT)
    jobs_tree.tag_configure("error", foreground=RED)
    jobs_tree.tag_configure("stopped", foreground=MUTED)
    jobs_tree.pack(fill="both", expand=True, padx=6, pady=(0, 8))

    # Right Detail Pane: Progress card + Log + Frame Preview
    mon_detail = tk.Frame(mon_body, bg=BG)
    mon_detail.pack(side="left", fill="both", expand=True)

    mon_top = tk.Frame(mon_detail, bg=SURFACE)
    mon_top.pack(fill="x", pady=(0, 8))

    mon_row1 = tk.Frame(mon_top, bg=SURFACE)
    mon_row1.pack(fill="x", padx=14, pady=(10, 6))
    lbl_job_title = tk.Label(mon_row1, text="No job selected", bg=SURFACE, fg=TEXT, font=("Segoe UI", 11, "bold"))
    lbl_job_title.pack(side="left")
    lbl_job_meta = tk.Label(mon_row1, text="", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9))
    lbl_job_meta.pack(side="left", padx=(10, 0))
    lbl_job_badge = tk.Label(mon_row1, text="Idle", bg=CONTROL, fg=MUTED, padx=10, pady=3, font=("Segoe UI", 8, "bold"))
    lbl_job_badge.pack(side="right")

    pbar = ttk.Progressbar(mon_top, style="Clean.Horizontal.TProgressbar", mode="determinate", maximum=100, value=0)
    pbar.pack(fill="x", padx=14, pady=(0, 8))

    mon_row2 = tk.Frame(mon_top, bg=SURFACE)
    mon_row2.pack(fill="x", padx=14, pady=(0, 10))
    lbl_job_progress = tk.Label(mon_row2, text="Ready to render", bg=SURFACE, fg=TEXT, font=("Segoe UI", 9))
    lbl_job_progress.pack(side="left")
    mon_btns = tk.Frame(mon_row2, bg=SURFACE)
    mon_btns.pack(side="right")

    # Detail Bottom Split: Log (left) + Frame Preview (right)
    mon_mid = tk.Frame(mon_detail, bg=BG)
    mon_mid.pack(fill="both", expand=True)

    preview_card = tk.Frame(mon_mid, bg=SURFACE, width=265)
    preview_card.pack(side="right", fill="y", padx=(8, 0))
    preview_card.pack_propagate(False)
    lbl_preview_img = tk.Label(
        preview_card,
        text="Latest frame",
        bg=SURFACE,
        fg=MUTED,
        font=("Segoe UI", 9),
        justify="center",
        cursor="hand2",
    )
    lbl_preview_img.pack(fill="both", expand=True, padx=10, pady=(10, 4))
    lbl_preview_img.bind("<Button-1>", lambda _e: _open_path_in_explorer(log_state["thumb_path"]))
    lbl_preview_cap = tk.Label(preview_card, text="", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8))
    lbl_preview_cap.pack(side="bottom", pady=(0, 8))

    log_card = tk.Frame(mon_mid, bg="#0d0e11")
    log_card.pack(side="left", fill="both", expand=True)
    log_scroll = tk.Scrollbar(log_card)
    log_scroll.pack(side="right", fill="y")
    log_text = tk.Text(
        log_card,
        bg="#0d0e11",
        fg="#c9cdd6",
        insertbackground=TEXT,
        relief="flat",
        bd=0,
        wrap="none",
        font=("Consolas", 9),
        yscrollcommand=log_scroll.set,
        state="disabled",
    )
    log_text.pack(fill="both", expand=True, padx=10, pady=8)
    log_scroll.config(command=log_text.yview)
    log_text.tag_configure("frame", foreground=GREEN)
    log_text.tag_configure("err", foreground=RED)
    log_text.tag_configure("info", foreground="#60a5fa")

    def append_log_lines(text: str) -> None:
        if not text:
            return
        log_text.configure(state="normal")
        for line in text.splitlines(keepends=True):
            tag = None
            if "FRAME " in line or "DONE " in line or "[summary]" in line:
                tag = "frame"
            elif "Error" in line or "Traceback" in line or "UNRESOLVED" in line or "[error]" in line:
                tag = "err"
            elif line.startswith(("[colab]", "[prepare]", "GPU:", "BLENDER:", "LAUNCH ", "UNPACKED:")):
                tag = "info"
            log_text.insert("end", line, tag) if tag else log_text.insert("end", line)
        log_text.see("end")
        log_text.configure(state="disabled")

    def clear_log_box() -> None:
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")

    # =========================================================================
    # TAB 2: RENDER (clean form; auto-detects .blend vs .json queue)
    # =========================================================================
    ren_actions = tk.Frame(tab_render, bg=BG)
    ren_actions.pack(fill="x", side="bottom", pady=(10, 2))

    ren_card = tk.Frame(tab_render, bg=SURFACE)
    ren_card.pack(fill="both", expand=True, pady=(10, 0))
    rf = tk.Frame(ren_card, bg=SURFACE)
    rf.pack(fill="x", padx=22, pady=18)

    blend_var = tk.StringVar(value="")
    out_var = tk.StringVar(value="")
    prefix_var = tk.StringVar(value="f_")
    f0_var = tk.StringVar(value="1")
    f1_var = tk.StringVar(value="1")
    samples_var = tk.StringVar(value="0")
    gpu_var = tk.StringVar(value="L4")
    vms_var = tk.StringVar(value="1")
    par_var = tk.StringVar(value="3")
    session_var = tk.StringVar(value="fetch-render")
    extras_var = tk.StringVar(value="")
    keep_var = tk.BooleanVar(value=False)

    def _entry(parent, var, width=None):
        kw: dict[str, Any] = dict(
            textvariable=var,
            bg=BG,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Segoe UI", 9),
        )
        if width:
            kw["width"] = width
        return tk.Entry(parent, **kw)

    def _row(label: str):
        r = tk.Frame(rf, bg=SURFACE)
        r.pack(fill="x", pady=6)
        tk.Label(r, text=label, bg=SURFACE, fg=MUTED, width=12, anchor="w", font=("Segoe UI", 9)).pack(side="left")
        return r

    r1 = _row("Scene file")
    _entry(r1, blend_var).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))

    def browse_input() -> None:
        p = filedialog.askopenfilename(
            title="Select Blender scene or jobs.json",
            filetypes=[("Blender or Queue", "*.blend *.json"), ("Blender files", "*.blend"), ("JSON queues", "*.json")],
        )
        if p:
            blend_var.set(p)
            if p.lower().endswith(".blend") and not out_var.get().strip():
                bp = Path(p)
                out_var.set(str(bp.parent.parent / "comp" / bp.stem if bp.parent.name.lower() == "blender" else bp.parent / "renders" / bp.stem))

    make_btn(r1, "Browse", browse_input, pady=5).pack(side="right")

    r2 = _row("Output folder")
    _entry(r2, out_var).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))

    def browse_out() -> None:
        p = filedialog.askdirectory(title="Select output folder")
        if p:
            out_var.set(p)

    make_btn(r2, "Browse", browse_out, pady=5).pack(side="right")

    r3 = _row("Frames")
    _entry(r3, f0_var, width=7).pack(side="left", ipady=5)
    tk.Label(r3, text="to", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=8)
    _entry(r3, f1_var, width=7).pack(side="left", ipady=5)
    tk.Label(r3, text="Prefix", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(22, 8))
    _entry(r3, prefix_var, width=8).pack(side="left", ipady=5)
    tk.Label(r3, text="Samples", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(22, 8))
    _entry(r3, samples_var, width=7).pack(side="left", ipady=5)
    tk.Label(r3, text="(0 = default)", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(side="left", padx=(6, 0))

    def _segmented(
        parent,
        var: tk.StringVar,
        options: tuple[str, ...],
        *,
        labels: Optional[dict[str, str]] = None,
        on_change=None,
    ) -> None:
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="left")
        btns: dict[str, tk.Button] = {}

        def _refresh_seg() -> None:
            cur = var.get()
            for val, b in btns.items():
                if val == cur:
                    b.configure(bg=ACCENT, fg=TEXT, activebackground=ACCENT_HOVER)
                else:
                    b.configure(bg=CONTROL, fg=MUTED, activebackground=HOVER)
            if on_change is not None:
                on_change()

        for val in options:
            b = tk.Button(
                wrap,
                text=(labels[val] if labels and val in labels else val),
                justify="center",
                command=lambda v=val: (var.set(v), _refresh_seg()),
                relief="flat",
                bd=0,
                cursor="hand2",
                padx=10,
                pady=4,
                font=("Segoe UI", 9),
            )
            b.pack(side="left", padx=(0, 2))
            btns[val] = b
        _refresh_seg()

    # Approximate Google Colab Compute Unit rates per hour (1 CU ≈ $0.10).
    # Actual rates can vary slightly by region, RAM tier, and Colab updates.
    GPU_PRICING: dict[str, tuple[str, float, str]] = {
        "L4": ("~1.94 CU/hr", 1.94, "NVIDIA L4 · 24 GB VRAM"),
        "T4": ("~1.1 CU/hr", 1.10, "NVIDIA T4 · 16 GB VRAM"),
        "A100": ("~6.8 CU/hr", 6.77, "NVIDIA A100 · 40–80 GB VRAM"),
        "H100": ("~18.1 CU/hr", 18.05, "NVIDIA H100 · 80 GB VRAM (Pro+)"),
        "CPU": ("~0.1 CU/hr", 0.10, "CPU only · no GPU"),
    }
    gpu_btn_labels = {k: f"{k}\n{v[0]}" for k, v in GPU_PRICING.items()}
    gpu_cost_lbl = tk.Label(rf, text="", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8), anchor="w")

    def _update_gpu_cost_label() -> None:
        g = gpu_var.get() or "L4"
        rate_str, rate_val, desc = GPU_PRICING.get(g, ("?", 0.0, g))
        try:
            n_vms = max(1, int(vms_var.get() or "1"))
        except ValueError:
            n_vms = 1
        total_rate = rate_val * n_vms
        usd_hr = total_rate * 0.10
        idx = _load_index()
        active_email = idx.get("active")
        info = (live_cache.get(active_email) or idx.get("accounts", {}).get(active_email, {})) if active_email else {}
        bal = info.get("balance")
        est = ""
        if isinstance(bal, (int, float)) and bal > 0 and total_rate > 0:
            hrs = bal / total_rate
            est = f" · ~{hrs:.1f}h on balance" if hrs < 100 else f" · ~{int(hrs)}h on balance"
        rate_part = (
            f"{n_vms} VMs × {rate_str} = ~{total_rate:.2f} CU/hr (~${usd_hr:.2f}/hr)"
            if n_vms > 1
            else f"{rate_str} (~${usd_hr:.2f}/hr)"
        )
        gpu_cost_lbl.configure(text=f"{desc} · {rate_part}{est} · approx., set by Colab")

    r4 = _row("GPU")
    _segmented(r4, gpu_var, ("L4", "T4", "A100", "H100", "CPU"), labels=gpu_btn_labels, on_change=_update_gpu_cost_label)
    gpu_cost_lbl.pack(anchor="w", padx=(92, 0), pady=(0, 4))

    r4_par = _row("Parallel VMs")
    _segmented(r4_par, vms_var, ("1", "2", "3", "4"), on_change=_update_gpu_cost_label)
    tk.Label(r4_par, text="Workers / VM", bg=SURFACE, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(22, 8))
    _segmented(r4_par, par_var, ("1", "2", "3", "4"))

    r4b = _row("Session")
    _entry(r4b, session_var, width=20).pack(side="left", ipady=5)
    tk.Checkbutton(
        r4b,
        text="Keep VM open after render",
        variable=keep_var,
        bg=SURFACE,
        fg=TEXT,
        selectcolor=CONTROL,
        activebackground=SURFACE,
        activeforeground=TEXT,
        font=("Segoe UI", 9),
    ).pack(side="left", padx=(18, 0))

    r5 = _row("Extra options")
    _entry(r5, extras_var).pack(side="left", fill="x", expand=True, ipady=5)
    tk.Label(
        rf,
        text="Optional: fmt=exr, fmt=v7, pct=50, engine=cycles, camera=Name. Choosing a .json file runs it as a queue.",
        bg=SURFACE,
        fg=MUTED,
        font=("Segoe UI", 8),
    ).pack(anchor="w", padx=(92, 0), pady=(2, 0))

    # =========================================================================
    # TAB 3: ACCOUNTS
    # =========================================================================
    acc_actions = tk.Frame(tab_accounts, bg=BG)
    acc_actions.pack(fill="x", side="bottom", pady=(10, 2))

    acc_card = tk.Frame(tab_accounts, bg=SURFACE)
    acc_card.pack(fill="both", expand=True, pady=(10, 0))

    cols = ("email", "status", "balance", "rate", "vms")
    tree = ttk.Treeview(acc_card, columns=cols, show="headings", style="Clean.Treeview", selectmode="browse", height=7)
    tree.heading("email", text="Account")
    tree.heading("status", text="Status")
    tree.heading("balance", text="Balance")
    tree.heading("rate", text="Rate")
    tree.heading("vms", text="VMs")
    tree.column("email", width=330, anchor="w", stretch=True)
    tree.column("status", width=90, anchor="center", stretch=False)
    tree.column("balance", width=120, anchor="e", stretch=False)
    tree.column("rate", width=95, anchor="e", stretch=False)
    tree.column("vms", width=70, anchor="center", stretch=False)
    tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))

    tk.Label(acc_card, text="Double-click an account to switch.", bg=SURFACE, fg=MUTED, font=("Segoe UI", 8)).pack(
        anchor="w", padx=14, pady=(0, 8)
    )

    # =========================================================================
    # Helpers & live polling
    # =========================================================================
    def set_status(msg: str, color: str = MUTED) -> None:
        status_var.set(msg)
        status_bar.configure(fg=color)

    def render_accounts_ui() -> None:
        idx = _load_index()
        active_email = idx.get("active")
        accounts = idx.get("accounts", {})

        if active_email:
            info = live_cache.get(active_email) or accounts.get(active_email, {})
            bal = info.get("balance")
            vms = info.get("assignments_count") or 0
            lbl_top_account.configure(text=active_email, fg=TEXT)
            if bal is not None:
                cu_color = GREEN if bal >= 10 else (AMBER if bal > 0 else RED)
                lbl_top_cu.configure(text=f"{bal:.2f} CU", fg=cu_color)
                lbl_top_vm.configure(text=f"{vms} VM{'s' if vms != 1 else ''}" if vms else "Idle", fg=AMBER if vms else MUTED)
                btn_stop_vms.configure(state="normal" if vms else "disabled")
            else:
                lbl_top_cu.configure(text="...", fg=MUTED)
                lbl_top_vm.configure(text="", fg=MUTED)
                btn_stop_vms.configure(state="normal")
        else:
            lbl_top_account.configure(text="Not signed in", fg=AMBER)
            lbl_top_cu.configure(text="", fg=MUTED)
            lbl_top_vm.configure(text="Click to add", fg=MUTED)
            btn_stop_vms.configure(state="disabled")

        selected = tree.selection()
        prev_sel = selected[0] if selected else None
        for item in tree.get_children():
            tree.delete(item)

        for email in sorted(accounts.keys()):
            meta = live_cache.get(email) or accounts.get(email, {})
            st_txt = "Active" if email == active_email else ""
            if meta.get("error"):
                bal_txt, rate_txt, vm_txt = "Sign-in expired", "-", "-"
            elif meta.get("balance") is not None:
                bal_txt = f"{meta['balance']:.2f} CU"
                rate_txt = f"{meta.get('rate', 0.0):.2f}/hr"
                vm_txt = str(meta.get("assignments_count", 0))
            else:
                bal_txt, rate_txt, vm_txt = "-", "-", "-"
            tree.insert("", "end", iid=email, values=(email, st_txt, bal_txt, rate_txt, vm_txt))

        if prev_sel and prev_sel in accounts:
            tree.selection_set(prev_sel)
        elif active_email and active_email in accounts:
            tree.selection_set(active_email)

        # Populate Running VMs table across all saved accounts
        vm_sel = vm_tree.selection()
        prev_vm_sel = vm_sel[0] if vm_sel else None
        for item in vm_tree.get_children():
            vm_tree.delete(item)
        vm_row_map.clear()
        total_running = 0
        for email in sorted(accounts.keys()):
            meta = live_cache.get(email) or {}
            assigns = meta.get("assignments") or []
            if not assigns:
                continue
            ep_map = _endpoint_session_map(email, is_active=(email == active_email))
            rate_val = meta.get("rate", 0.0)
            for a in assigns:
                ep = str(a.get("endpoint", ""))
                sname = ep_map.get(ep, "Browser / external")
                row_id = f"{email}::{ep}"
                vm_row_map[row_id] = (email, ep)
                vm_tree.insert("", "end", iid=row_id, values=(email, sname, a.get("accelerator", "?"), f"{rate_val:.2f}/hr"))
                total_running += 1
        if prev_vm_sel and prev_vm_sel in vm_row_map:
            vm_tree.selection_set(prev_vm_sel)
        lbl_total_vms.configure(
            text=f"{total_running} active" if total_running else "None",
            fg=AMBER if total_running else MUTED,
        )
        btn_stop_one_vm.configure(state="normal" if total_running else "disabled")
        _update_gpu_cost_label()

    def _on_job_selected(_event=None) -> None:
        sel = jobs_tree.selection()
        if sel and sel[0] != log_state["selected_run_id"]:
            log_state["selected_run_id"] = sel[0]
            log_state["thumb_path"] = ""
            log_state["thumb_img"] = None
            lbl_preview_img.configure(image="", text="Latest frame")
            lbl_preview_cap.configure(text="")
            poll_render_monitor(reschedule=False)

    jobs_tree.bind("<<TreeviewSelect>>", _on_job_selected)

    def poll_render_monitor(*, reschedule: bool = True) -> None:
        try:
            active_states = {"starting", "preparing", "provisioning", "uploading", "rendering"}
            job_entries: list[tuple[str, dict[str, Any], Path]] = []
            if JOBS_DIR.is_dir():
                for jp in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:25]:
                    if jp.name.endswith("_queue.json"):
                        continue
                    try:
                        d = json.loads(jp.read_text(encoding="utf-8"))
                        rid = str(d.get("run_id") or jp.stem)
                        st = str(d.get("status", "idle"))
                        pid = int(d.get("pid") or 0)
                        if st in active_states and pid and not _is_pid_alive(pid):
                            d["status"] = "stopped"
                        job_entries.append((rid, d, jp.with_suffix(".log")))
                    except Exception:
                        pass

            job_entries.sort(key=lambda x: (0 if x[1].get("status") in active_states else 1, -float(x[1].get("updated_at") or 0)))
            by_id: dict[str, tuple[dict[str, Any], Path]] = {}
            wanted_ids: list[str] = []
            for rid, d, lp in job_entries:
                by_id[rid] = (d, lp)
                wanted_ids.append(rid)
            log_state["jobs_by_id"] = by_id

            # Update left sidebar tree without losing selection or causing flicker
            existing_ids = list(jobs_tree.get_children())
            if existing_ids != wanted_ids:
                for item in existing_ids:
                    jobs_tree.delete(item)
                for rid in wanted_ids:
                    jobs_tree.insert("", "end", iid=rid, values=("", "", ""))

            state_tag_map = {
                "rendering": ("Rendering", "active"),
                "uploading": ("Uploading", "busy"),
                "provisioning": ("Starting", "busy"),
                "preparing": ("Preparing", "busy"),
                "done": ("Done", "done"),
                "error": ("Error", "error"),
                "stopped": ("Stopped", "stopped"),
            }
            for rid in wanted_ids:
                d, _ = by_id[rid]
                jname = d.get("job_name") or Path(d.get("blend") or rid).name
                done_f = int(d.get("done_frames") or 0)
                tot_f = int(d.get("total_frames") or 0)
                st_label, st_tag = state_tag_map.get(str(d.get("status", "idle")), ("Idle", "stopped"))
                jobs_tree.item(rid, values=(jname, f"{done_f}/{tot_f}", st_label), tags=(st_tag,))

            if wanted_ids:
                sel_rid = log_state["selected_run_id"]
                if not sel_rid or sel_rid not in by_id:
                    sel_rid = wanted_ids[0]
                    log_state["selected_run_id"] = sel_rid
                if jobs_tree.selection() != (sel_rid,):
                    jobs_tree.selection_set(sel_rid)
                data, log_path = by_id[sel_rid]

                jname = data.get("job_name") or Path(data.get("blend") or sel_rid).name
                gpu = data.get("gpu") or "?"
                sess = data.get("session") or ""
                par = max(1, int(data.get("par") or 1))
                lbl_job_title.configure(text=jname)
                lbl_job_meta.configure(text=f"{gpu} · {par}x · {sess}" if sess else f"{gpu} · {par}x")

                st = str(data.get("status", "idle"))
                badge_map = {
                    "rendering": ("Rendering", GREEN, "#142a22"),
                    "uploading": ("Uploading", "#60a5fa", "#17253d"),
                    "provisioning": ("Starting VM", AMBER, "#2e2412"),
                    "preparing": ("Preparing", AMBER, "#2e2412"),
                    "done": ("Done", GREEN, "#142a22"),
                    "error": ("Error", RED, DANGER_BG),
                    "stopped": ("Stopped", MUTED, CONTROL),
                }
                b_txt, fg_c, bg_c = badge_map.get(st, (st.capitalize(), MUTED, CONTROL))
                lbl_job_badge.configure(text=b_txt, fg=fg_c, bg=bg_c)

                total = max(0, int(data.get("total_frames") or 0))
                done = max(0, int(data.get("done_frames") or 0))
                pct = (100.0 * done / total) if total > 0 else 0.0
                pbar.configure(value=min(100.0, pct))

                avg_s = float(data.get("avg_frame_sec") or 0.0)
                rem = max(0, total - done)
                eta = _fmt_eta((rem * avg_s) / par) if (avg_s > 0 and rem > 0 and st == "rendering") else ""
                err_txt = data.get("error") or ""
                if err_txt:
                    lbl_job_progress.configure(text=err_txt[:95], fg=RED)
                else:
                    parts = [f"{done} / {total} frames ({pct:.0f}%)"]
                    if eta:
                        parts.append(eta)
                    if avg_s > 0:
                        parts.append(f"{avg_s:.1f}s / frame")
                    lbl_job_progress.configure(text="   ·   ".join(parts), fg=TEXT)

                out_dir = data.get("out_dir") or ""
                log_state["last_out_dir"] = out_dir
                btn_open_out.configure(state="normal" if out_dir and os.path.exists(out_dir) else "disabled")
                btn_cancel_render.configure(state="normal" if st in active_states else "disabled")

                # Update frame thumbnail
                fp = data.get("last_frame_path") or ""
                if fp and fp != log_state["thumb_path"] and os.path.isfile(fp) and fp.lower().endswith(".png"):
                    try:
                        img = tk.PhotoImage(file=fp)
                        w_i, h_i = img.width(), img.height()
                        scale = max(1, int(max(w_i / 245, h_i / 200)))
                        if scale > 1:
                            img = img.subsample(scale, scale)
                        log_state["thumb_img"] = img
                        log_state["thumb_path"] = fp
                        lbl_preview_img.configure(image=img, text="")
                        lbl_preview_cap.configure(text=f"{os.path.basename(fp)}  (click to open)")
                    except Exception:
                        pass

                # Tail selected job's log
                if sel_rid != log_state["tailed_run_id"]:
                    log_state["tailed_run_id"] = sel_rid
                    log_state["offset"] = 0
                    clear_log_box()
                if log_path.is_file():
                    sz = log_path.stat().st_size
                    if sz < log_state["offset"]:
                        log_state["offset"] = 0
                        clear_log_box()
                    if sz > log_state["offset"]:
                        with open(log_path, "rb") as fh:
                            fh.seek(log_state["offset"])
                            chunk = fh.read()
                            log_state["offset"] = fh.tell()
                        append_log_lines(chunk.decode("utf-8", errors="replace"))
        except Exception:
            pass
        if reschedule:
            root.after(1500, poll_render_monitor)

    def refresh_all(*, sync_first: bool = True, silent: bool = False) -> None:
        if busy["refresh"]:
            return
        busy["refresh"] = True
        if not silent:
            set_status("Refreshing...", MUTED)

        def worker() -> None:
            try:
                if sync_first:
                    sync_current_cli_login()
                idx = _load_index()
                emails = sorted(idx.get("accounts", {}).keys())
                for em in emails:
                    tok = _get_account_token_json(em)
                    if not tok:
                        live_cache[em] = {"error": "Missing credential"}
                        continue
                    try:
                        info = _query_account_live_status(tok)
                        live_cache[em] = info
                        if info["token_json"] != tok:
                            _set_account_token_json(em, info["token_json"])
                            if idx.get("active") == em:
                                _set_active_token_json(info["token_json"])
                        idx["accounts"][em].update(
                            {
                                "balance": info["balance"],
                                "rate": info["rate"],
                                "assignments_count": info["assignments_count"],
                                "checked_at": info["checked_at"],
                            }
                        )
                    except Exception as e:
                        live_cache[em] = {"error": str(e)}
                _save_index(idx)
                msg = f"Updated {datetime.now().strftime('%H:%M:%S')}"
                root.after(0, lambda: _done_refresh(msg, MUTED))
            except Exception as e:
                err_msg = str(e)
                root.after(0, lambda: _done_refresh(f"Refresh failed: {err_msg}", RED))

        def _done_refresh(msg: str, color: str) -> None:
            busy["refresh"] = False
            render_accounts_ui()
            if not silent or color == RED:
                set_status(msg, color)

        threading.Thread(target=worker, daemon=True).start()

    def schedule_auto_refresh() -> None:
        refresh_all(sync_first=False, silent=True)
        root.after(15000, schedule_auto_refresh)

    def do_switch(_event=None) -> None:
        sel = tree.selection()
        if not sel:
            return
        target_email = sel[0]
        idx = _load_index()
        old_active = idx.get("active")
        if old_active == target_email:
            set_status(f"Already using {target_email}", MUTED)
            return

        old_vms = (live_cache.get(old_active) or idx.get("accounts", {}).get(old_active or "", {})).get("assignments_count", 0)
        if old_active and old_vms:
            ans = messagebox.askyesnocancel(
                "Running VM",
                f"{old_active} still has {old_vms} running VM(s).\nStop them before switching?",
            )
            if ans is None:
                return
            if ans is True:
                set_status(f"Stopping VMs on {old_active}...", AMBER)
                root.update_idletasks()
                try:
                    stop_all_vms_for_account(old_active)
                except Exception as e:
                    messagebox.showerror("Error", str(e))
                    return

        try:
            switch_active_account(target_email)
            render_accounts_ui()
            set_status(f"Switched to {target_email}", GREEN)
            refresh_all(sync_first=False)
        except Exception as e:
            messagebox.showerror("Switch Failed", str(e))
            set_status(str(e), RED)

    def do_stop_vms() -> None:
        idx = _load_index()
        active_email = idx.get("active")
        if not active_email:
            return
        if not messagebox.askyesno("Stop VMs", f"Stop all running Colab VMs on {active_email}?"):
            return
        set_status(f"Stopping VMs on {active_email}...", AMBER)

        def worker() -> None:
            try:
                n = stop_all_vms_for_account(active_email)
                root.after(0, lambda: _after_stop(n, None))
            except Exception as e:
                err_msg = str(e)
                root.after(0, lambda: _after_stop(0, err_msg))

        def _after_stop(n: int, err: Optional[str]) -> None:
            if err:
                messagebox.showerror("Stop Failed", err)
                set_status(err, RED)
            else:
                set_status(f"Stopped {n} VM(s)", GREEN)
                refresh_all(sync_first=False)

        threading.Thread(target=worker, daemon=True).start()

    def do_remove() -> None:
        sel = tree.selection()
        if not sel:
            return
        email = sel[0]
        if not messagebox.askyesno("Remove Account", f"Remove saved sign-in for {email}?"):
            return
        remove_saved_account(email)
        live_cache.pop(email, None)
        render_accounts_ui()
        set_status(f"Removed {email}", MUTED)

    def do_clear_history() -> None:
        active_states = {"starting", "preparing", "provisioning", "uploading", "rendering"}
        removed = 0
        if JOBS_DIR.is_dir():
            for jp in list(JOBS_DIR.glob("*.json")):
                try:
                    d = json.loads(jp.read_text(encoding="utf-8"))
                    st = str(d.get("status", "idle"))
                    pid = int(d.get("pid") or 0)
                    if st in active_states and pid and _is_pid_alive(pid):
                        continue
                    jp.unlink(missing_ok=True)
                    jp.with_suffix(".log").unlink(missing_ok=True)
                    removed += 1
                except Exception:
                    pass
        log_state["selected_run_id"] = None
        log_state["tailed_run_id"] = None
        log_state["thumb_path"] = ""
        log_state["thumb_img"] = None
        lbl_preview_img.configure(image="", text="Latest frame")
        lbl_preview_cap.configure(text="")
        clear_log_box()
        for item in jobs_tree.get_children():
            jobs_tree.delete(item)
        lbl_job_title.configure(text="No job selected")
        lbl_job_meta.configure(text="")
        lbl_job_badge.configure(text="Idle", fg=MUTED, bg=CONTROL)
        lbl_job_progress.configure(text="Ready to render", fg=TEXT)
        pbar.configure(value=0)
        set_status(f"Cleared {removed} past job(s)", MUTED)
        poll_render_monitor(reschedule=False)

    def do_stop_one_vm() -> None:
        sel = vm_tree.selection()
        if not sel or sel[0] not in vm_row_map:
            return
        email, ep = vm_row_map[sel[0]]
        if not messagebox.askyesno("Stop VM", f"Stop this VM on {email}?"):
            return
        set_status(f"Stopping VM on {email}...", AMBER)

        def worker() -> None:
            try:
                stop_one_vm(email, ep)
                root.after(0, lambda: _done(None))
            except Exception as e:
                err_s = str(e)
                root.after(0, lambda: _done(err_s))

        def _done(err: Optional[str]) -> None:
            if err:
                messagebox.showerror("Stop VM Failed", err)
                set_status(err, RED)
            else:
                set_status(f"Stopped VM on {email}", GREEN)
                refresh_all(sync_first=False)

        threading.Thread(target=worker, daemon=True).start()

    def do_launch_render(*, dry_run: bool) -> None:
        if not RENDER_SCRIPT.is_file():
            messagebox.showerror("Missing Script", f"Could not find {RENDER_SCRIPT}")
            return
        target = blend_var.get().strip()
        if not target or not Path(target).is_file():
            messagebox.showerror("Select File", "Choose a .blend or jobs.json file first.")
            return

        py_exe = sys.executable
        if os.name == "nt" and py_exe.lower().endswith("pythonw.exe"):
            cand = Path(py_exe).with_name("python.exe")
            if cand.is_file():
                py_exe = str(cand)

        is_queue = target.lower().endswith(".json")
        cmd = [py_exe, str(RENDER_SCRIPT)]
        if is_queue:
            cmd += ["queue", target]
        else:
            cmd += ["shot", target]
            if out_var.get().strip():
                cmd += ["--out", out_var.get().strip()]
            cmd += ["--prefix", prefix_var.get().strip() or "f_"]
            cmd += ["--f0", f0_var.get().strip() or "1"]
            cmd += ["--f1", f1_var.get().strip() or f0_var.get().strip() or "1"]
            cmd += ["--samples", samples_var.get().strip() or "0"]
            if extras_var.get().strip():
                for tok in shlex.split(extras_var.get().strip()):
                    cmd += ["--extra", tok]

        sess_name = session_var.get().strip() or "fetch-render"
        cmd += [
            "--gpu",
            gpu_var.get() or "L4",
            "--vms",
            vms_var.get() or "1",
            "--par",
            par_var.get() or "3",
            "-s",
            sess_name,
        ]
        if keep_var.get():
            cmd.append("--keep")
        if dry_run:
            cmd.append("--dry-run")
        else:
            cmd.append("--wait")

        nb.select(tab_monitor)
        if dry_run:
            log_state["selected_run_id"] = None
            log_state["tailed_run_id"] = "__dry__"
            clear_log_box()
        else:
            log_state["selected_run_id"] = None
        set_status("Checking scene..." if dry_run else f"Starting render on {gpu_var.get()}...", ACCENT)

        def worker() -> None:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            proc = None
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=CREATION_FLAGS,
                )
                gui_procs.append(proc)
                assert proc.stdout is not None
                for line in proc.stdout:
                    if dry_run:
                        ln = line
                        root.after(0, lambda l=ln: append_log_lines(l))
                rc = proc.wait()
            except Exception as e:
                err_s = str(e)
                root.after(0, lambda: append_log_lines(f"[error] {err_s}\n"))
                rc = 1
            finally:
                if proc in gui_procs:
                    gui_procs.remove(proc)

            def _done() -> None:
                if dry_run:
                    set_status("Scene check passed" if rc == 0 else f"Scene check failed (code {rc})", GREEN if rc == 0 else RED)
                else:
                    set_status("Render finished" if rc == 0 else f"Render ended (code {rc})", GREEN if rc == 0 else AMBER)
                    refresh_all(sync_first=False)

            root.after(0, _done)

        threading.Thread(target=worker, daemon=True).start()

    def do_cancel_render() -> None:
        sel_rid = log_state.get("selected_run_id")
        entry = log_state["jobs_by_id"].get(sel_rid) if sel_rid else None
        if not entry:
            do_stop_vms()
            return
        data = entry[0]
        pid = int(data.get("pid") or 0)
        sess = data.get("session") or ""
        if not messagebox.askyesno("Stop Render", f"Stop '{data.get('job_name', '')}' and its Colab VM?"):
            return
        if pid and _is_pid_alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATION_FLAGS, check=False)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        if sess:
            idx = _load_index()
            active_em = idx.get("active")
            if active_em:
                ep_map = _endpoint_session_map(active_em, is_active=True)
                for ep, sname in ep_map.items():
                    if sname == sess:
                        try:
                            stop_one_vm(active_em, ep)
                        except Exception:
                            pass
        set_status(f"Stopped {data.get('job_name', 'render')}", AMBER)
        refresh_all(sync_first=False)

    def open_add_account_dialog() -> None:
        try:
            flow, auth_url = begin_oauth_flow()
        except Exception as e:
            messagebox.showerror("Sign-In Error", str(e))
            return

        dlg = tk.Toplevel(root)
        dlg.title("Add Google Account")
        dlg.geometry("540x320")
        dlg.minsize(500, 300)
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.grab_set()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", side="bottom", padx=20, pady=(6, 16))

        tk.Label(dlg, text="Sign in with Google", bg=BG, fg=TEXT, font=("Segoe UI", 12, "bold")).pack(
            anchor="w", padx=20, pady=(16, 4)
        )
        tk.Label(
            dlg,
            text="Open the sign-in page (or copy the link for another browser profile), approve access, then paste the code below.",
            bg=BG,
            fg=MUTED,
            wraplength=490,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=20, pady=(0, 14))

        url_btns = tk.Frame(dlg, bg=BG)
        url_btns.pack(fill="x", padx=20, pady=(0, 14))

        def copy_url() -> None:
            dlg.clipboard_clear()
            dlg.clipboard_append(auth_url)
            lbl_dlg_status.configure(text="Link copied to clipboard.", fg=GREEN)

        make_btn(
            url_btns,
            "Open Google Sign-In",
            lambda: (webbrowser.open(auth_url), lbl_dlg_status.configure(text="Opened in browser.", fg=MUTED)),
            bg=ACCENT,
            hover=ACCENT_HOVER,
            bold=True,
        ).pack(side="left")
        make_btn(url_btns, "Copy Link", copy_url).pack(side="left", padx=(8, 0))

        tk.Label(dlg, text="Authorization code", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=20)
        code_var = tk.StringVar()
        entry = tk.Entry(
            dlg,
            textvariable=code_var,
            bg=SURFACE,
            fg=TEXT,
            insertbackground=TEXT,
            relief="flat",
            highlightbackground=BORDER,
            highlightthickness=1,
            font=("Consolas", 10),
        )
        entry.pack(fill="x", padx=20, pady=(4, 10), ipady=6)
        entry.focus_set()

        activate_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            dlg,
            text="Set as active account",
            variable=activate_var,
            bg=BG,
            fg=TEXT,
            selectcolor=SURFACE,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=18)

        lbl_dlg_status = tk.Label(dlg, text="", bg=BG, fg=MUTED, font=("Segoe UI", 8), wraplength=490, justify="left")
        lbl_dlg_status.pack(anchor="w", padx=20, pady=(4, 0))

        def submit_code() -> None:
            code = code_var.get().strip()
            if not code:
                lbl_dlg_status.configure(text="Paste the code from Google first.", fg=AMBER)
                return
            btn_connect.configure(state="disabled", text="Signing in...")
            lbl_dlg_status.configure(text="Connecting account...", fg=MUTED)

            def worker() -> None:
                try:
                    info = finish_oauth_flow(flow, code, activate=activate_var.get())
                    live_cache[info["email"]] = info
                    root.after(0, lambda: _on_ok(info))
                except Exception as e:
                    err_msg = str(e)
                    root.after(0, lambda: _on_err(err_msg))

            def _on_ok(info: dict[str, Any]) -> None:
                dlg.destroy()
                render_accounts_ui()
                set_status(f"Signed in as {info['email']} ({info['balance']:.2f} CU)", GREEN)

            def _on_err(err: str) -> None:
                btn_connect.configure(state="normal", text="Sign In")
                lbl_dlg_status.configure(text=err, fg=RED)

            threading.Thread(target=worker, daemon=True).start()

        btn_connect = make_btn(btn_row, "Sign In", submit_code, bg=ACCENT, hover=ACCENT_HOVER, bold=True)
        btn_connect.pack(side="right")
        make_btn(btn_row, "Cancel", dlg.destroy).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", lambda _e: submit_code())

    # --- Wire buttons ---
    make_btn(tb_right, "Refresh", lambda: refresh_all(sync_first=True)).pack(side="left", padx=(0, 8))
    make_btn(tb_right, "Buy units", lambda: webbrowser.open("https://colab.research.google.com/signup"), bg=SURFACE, hover=CONTROL, fg=MUTED).pack(side="left")

    btn_stop_one_vm = make_btn(vm_hdr_btns, "Stop VM", do_stop_one_vm, bg=DANGER_BG, fg=RED, hover=DANGER_HOVER, padx=10, pady=3, font_size=8)
    btn_stop_one_vm.pack(side="left")
    btn_stop_one_vm.configure(state="disabled")

    btn_open_out = make_btn(mon_btns, "Open Folder", lambda: _open_path_in_explorer(log_state["last_out_dir"]), pady=4)
    btn_open_out.pack(side="left", padx=(0, 8))
    btn_open_out.configure(state="disabled")
    btn_cancel_render = make_btn(mon_btns, "Stop", do_cancel_render, bg=DANGER_BG, fg=RED, hover=DANGER_HOVER, pady=4)
    btn_cancel_render.pack(side="left")
    btn_cancel_render.configure(state="disabled")

    make_btn(ren_actions, "Check Scene", lambda: do_launch_render(dry_run=True)).pack(side="left")
    make_btn(ren_actions, "Start Render", lambda: do_launch_render(dry_run=False), bg=ACCENT, hover=ACCENT_HOVER, bold=True).pack(side="left", padx=(8, 0))

    make_btn(acc_actions, "Add Account", open_add_account_dialog, bg=ACCENT, hover=ACCENT_HOVER, bold=True).pack(side="left")
    make_btn(acc_actions, "Use Selected", do_switch).pack(side="left", padx=(8, 0))
    btn_stop_vms = make_btn(acc_actions, "Stop VMs", do_stop_vms, bg=DANGER_BG, fg=RED, hover=DANGER_HOVER)
    btn_stop_vms.pack(side="left", padx=(8, 0))
    make_btn(acc_actions, "Remove", do_remove, bg=SURFACE, hover=CONTROL, fg=MUTED).pack(side="right")

    tree.bind("<Double-1>", do_switch)

    tab_map = {"monitor": tab_monitor, "render": tab_render, "accounts": tab_accounts}
    if initial_tab in tab_map:
        nb.select(tab_map[initial_tab])

    def _cleanup_pid() -> None:
        try:
            if GUI_PID_PATH.is_file() and GUI_PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                GUI_PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if GUI_HEARTBEAT_PATH.is_file():
                hb = json.loads(GUI_HEARTBEAT_PATH.read_text(encoding="utf-8"))
                if int(hb.get("pid") or 0) == os.getpid():
                    GUI_HEARTBEAT_PATH.unlink(missing_ok=True)
        except Exception:
            pass

    hb_last = {"t": 0.0}

    def _write_heartbeat() -> None:
        now = time.time()
        if now - hb_last["t"] < 1.5:
            return
        hb_last["t"] = now
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = GUI_HEARTBEAT_PATH.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"pid": os.getpid(), "ts": now, "inbox": True}),
                encoding="utf-8",
            )
            os.replace(tmp, GUI_HEARTBEAT_PATH)
        except OSError:
            pass

    def _spawn_inbox_request(req: dict[str, Any], ack_path: Path) -> None:
        jobs = req.get("jobs") or []
        cwd = req.get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            cwd = None
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        local_bin = str(Path.home() / ".local" / "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = local_bin + os.pathsep + env.get("PATH", "")

        pids: dict[str, int] = {}
        err_msg = ""
        try:
            for idx, item in enumerate(jobs):
                if idx > 0:
                    time.sleep(0.5)
                cmd = item.get("cmd") or []
                rid = str(item.get("run_id") or f"job_{idx}")
                if not cmd:
                    continue
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    env=env,
                    creationflags=CREATION_FLAGS,
                )
                gui_procs.append(proc)
                pids[rid] = proc.pid

                def _reap(p: subprocess.Popen) -> None:
                    try:
                        p.wait()
                    finally:
                        if p in gui_procs:
                            gui_procs.remove(p)
                        try:
                            root.after(0, lambda: refresh_all(sync_first=False))
                        except Exception:
                            pass

                threading.Thread(target=_reap, args=(proc,), daemon=True).start()
        except Exception as e:
            err_msg = str(e)

        try:
            tmp = ack_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({"ok": not err_msg, "pids": pids, "error": err_msg}),
                encoding="utf-8",
            )
            os.replace(tmp, ack_path)
        except OSError:
            pass

        def _ui_notify() -> None:
            if err_msg:
                set_status(f"Inbox launch error: {err_msg}", RED)
            else:
                set_status(f"Started {len(pids)} render job(s)", ACCENT)
                log_state["selected_run_id"] = None
                poll_render_monitor(reschedule=False)

        try:
            root.after(0, _ui_notify)
        except Exception:
            pass

    def poll_inbox() -> None:
        try:
            _write_heartbeat()
            if INBOX_DIR.is_dir():
                for req_path in sorted(INBOX_DIR.glob("*.req.json")):
                    try:
                        raw = req_path.read_text(encoding="utf-8")
                        req_path.unlink(missing_ok=True)
                        req = json.loads(raw)
                        req_id = str(req.get("req_id") or req_path.name.removesuffix(".req.json"))
                        ack_path = INBOX_DIR / f"{req_id}.ack.json"
                        threading.Thread(
                            target=_spawn_inbox_request, args=(req, ack_path), daemon=True
                        ).start()
                    except Exception:
                        try:
                            req_path.unlink(missing_ok=True)
                        except OSError:
                            pass
        finally:
            try:
                root.after(400, poll_inbox)
            except Exception:
                pass

    def _on_close() -> None:
        _cleanup_pid()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    render_accounts_ui()
    _write_heartbeat()
    root.after(120, lambda: refresh_all(sync_first=True))
    root.after(200, poll_inbox)
    root.after(250, poll_render_monitor)
    root.after(15000, schedule_auto_refresh)
    try:
        root.mainloop()
    finally:
        _cleanup_pid()
    return 0


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    argv = [a for a in argv if a != "--json"]
    initial_tab = "monitor"
    if "--tab" in argv:
        ti = argv.index("--tab")
        if ti + 1 < len(argv):
            initial_tab = argv[ti + 1].lower()
            argv = argv[:ti] + argv[ti + 2 :]
    if not argv or argv[0] == "gui":
        return launch_gui(initial_tab=initial_tab)

    cmd = argv[0].lower()
    if cmd == "monitor":
        launched = ensure_gui_open(tab="monitor")
        if as_json:
            print(json.dumps({"launched": launched, "running": True}))
        else:
            print("Opened Colab Render." if launched else "Colab Render is already open.")
        return 0

    if cmd == "list":
        sync_current_cli_login()
        idx = _load_index()
        active = idx.get("active")
        accounts = sorted(idx.get("accounts", {}).keys())
        rows = []
        for em in accounts:
            tok = _get_account_token_json(em)
            if not tok:
                rows.append({"email": em, "active": em == active, "error": "missing keyring token"})
                continue
            try:
                info = _query_account_live_status(tok)
                rows.append(
                    {
                        "email": em,
                        "active": em == active,
                        "balance": info["balance"],
                        "rate": info["rate"],
                        "assignments_count": info["assignments_count"],
                        "assignments": info["assignments"],
                    }
                )
            except Exception as e:
                rows.append({"email": em, "active": em == active, "error": str(e)})
        if as_json:
            print(json.dumps({"active": active, "accounts": rows}, indent=2))
            return 0
        if not rows:
            print("No saved accounts. Run `python colab_accounts.py` to sign in.")
            return 0
        for r in rows:
            mark = "*" if r["active"] else " "
            if "error" in r:
                print(f"{mark} {r['email']:38s}  [error: {r['error']}]")
            else:
                print(f"{mark} {r['email']:38s}  {r['balance']:7.2f} CU  ({r['rate']:.2f}/hr, {r['assignments_count']} VM)")
        return 0

    if cmd == "active":
        em = sync_current_cli_login()
        if not em:
            print(json.dumps({"active": None}) if as_json else "Not signed in.")
            return 1
        tok = _get_active_token_json()
        assert tok is not None
        info = _query_account_live_status(tok)
        if as_json:
            print(
                json.dumps(
                    {
                        "email": info["email"],
                        "balance": info["balance"],
                        "rate": info["rate"],
                        "assignments_count": info["assignments_count"],
                        "assignments": info["assignments"],
                    },
                    indent=2,
                )
            )
            return 0
        print(f"Account : {info['email']}")
        print(f"Balance : {info['balance']:.2f} CU ({info['rate']:.2f}/hr, {info['assignments_count']} VM)")
        return 0

    if cmd == "switch":
        if len(argv) < 2:
            raise SystemExit("Usage: python colab_accounts.py switch <email>")
        em = switch_active_account(argv[1])
        print(json.dumps({"switched_to": em}) if as_json else f"Switched to {em}")
        return 0

    if cmd == "stop-vms":
        em = argv[1] if len(argv) >= 2 else sync_current_cli_login()
        if not em:
            raise SystemExit("No active account.")
        n = stop_all_vms_for_account(em)
        print(json.dumps({"email": em, "stopped_vms": n}) if as_json else f"Stopped {n} VM(s) on {em}")
        return 0

    if cmd == "vms":
        sync_current_cli_login()
        idx = _load_index()
        active = idx.get("active")
        all_vms = []
        for em in sorted(idx.get("accounts", {}).keys()):
            tok = _get_account_token_json(em)
            if not tok:
                continue
            try:
                info = _query_account_live_status(tok)
                ep_map = _endpoint_session_map(em, is_active=(em == active))
                for a in info["assignments"]:
                    all_vms.append(
                        {
                            "account": em,
                            "active_account": em == active,
                            "session": ep_map.get(a["endpoint"], "Browser / external"),
                            "accelerator": a["accelerator"],
                            "shape": a["shape"],
                            "endpoint": a["endpoint"],
                            "rate": info["rate"],
                        }
                    )
            except Exception:
                pass
        if as_json:
            print(json.dumps({"vms": all_vms}, indent=2))
            return 0
        if not all_vms:
            print("No active VMs.")
            return 0
        for v in all_vms:
            mark = "*" if v["active_account"] else " "
            print(f"{mark} {v['account']:32s}  {v['session']:18s}  {v['accelerator']:5s}  ({v['rate']:.2f}/hr)")
        return 0

    raise SystemExit(f"Unknown command: {cmd}. Expected: gui | monitor | list | active | vms | switch <email> | stop-vms [email]")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
