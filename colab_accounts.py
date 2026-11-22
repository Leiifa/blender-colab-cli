"""Colab CLI Studio — Account Switcher, Live GPU/Render Monitor & Launcher GUI + CLI.

Works natively without opening CMD windows (via pythonw.exe / CREATE_NO_WINDOW).
Monitors BOTH GUI-launched renders AND renders started by AI agents or terminals
(by reading live telemetry from ~/.config/colab-cli/active_render.json & .log).

Usage:
  python colab_accounts.py                     # Launch the GUI
  python colab_accounts.py list [--json]       # Print saved accounts & live CU balances
  python colab_accounts.py active [--json]     # Show active account email & CU balance
  python colab_accounts.py switch EMAIL        # Switch active account from CLI
  python colab_accounts.py stop-vms [EMAIL]    # Stop all running Colab VMs on account
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
# Auto-relaunch inside google-colab-cli's uv tool venv if run with system python
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
    raise SystemExit(
        "ERROR: google-colab-cli environment not found. Run: uv tool install google-colab-cli"
    )

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
GUI_PID_PATH = CONFIG_DIR / "gui.pid"


# ---------------------------------------------------------------------------
# Account store & Colab API helpers
# ---------------------------------------------------------------------------
def _safe_slug(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", email.lower()).strip("_") or "account"


def _is_pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
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
        raise RuntimeError("Token did not return an email address (missing userinfo.email scope).")
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
        "checked_at": datetime.now().strftime("%H:%M:%S"),
    }


def _endpoint_session_map(email: str, *, is_active: bool) -> dict[str, str]:
    """Map VM endpoint -> local CLI session name for an account."""
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
    """Unassign a single Colab VM endpoint on any saved account."""
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

    # Prune this endpoint from local session files if present
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
        raise RuntimeError(f"Credential for '{email}' is missing from your OS keyring. Re-add the account.")

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
        return "—"
    s = int(seconds)
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"~{h}h {m:02d}m"
    if m:
        return f"~{m}m {sec:02d}s"
    return f"~{sec}s"


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
# Tkinter GUI (Accounts + Live Monitor + Launch Render)
# ---------------------------------------------------------------------------
BG = "#141417"
CARD = "#1e1e24"
CARD_ALT = "#26262e"
BORDER = "#34343f"
TEXT = "#f4f4f5"
MUTED = "#a1a1aa"
ACCENT = "#3b82f6"
ACCENT_HOVER = "#2563eb"
GREEN = "#22c55e"
AMBER = "#f59e0b"
RED = "#ef4444"


def _find_gui_hwnd(pid: int) -> Optional[int]:
    """On Windows, return a top-level HWND belonging to `pid` (if any)."""
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
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
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
        # A pythonw.exe with no visible window after startup is a zombie; clean it up if requested.
        if _find_gui_hwnd(pid) is not None:
            return True
        # Give a freshly spawned process up to 5 seconds to create its window before treating as zombie.
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
    """Spawn the Studio GUI detached (windowless) if not already running. Returns True if launched."""
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
        # Note: Do NOT pass CREATE_NO_WINDOW or DETACHED_PROCESS to pythonw.exe — pythonw is already
        # a GUI-subsystem binary (no CMD window) and needs SW_SHOWNORMAL to display its Tk window cleanly.
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


def launch_gui(*, initial_tab: str = "accounts") -> int:
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
    root.title("Colab CLI Studio — Accounts, Live Monitor & Blender Render")
    root.geometry("920x660")
    root.minsize(820, 580)
    root.configure(bg=BG)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except Exception:
        pass

    style.configure(
        "Dark.Treeview",
        background=CARD,
        foreground=TEXT,
        fieldbackground=CARD,
        bordercolor=BORDER,
        borderwidth=0,
        rowheight=32,
        font=("Segoe UI", 10),
    )
    style.configure(
        "Dark.Treeview.Heading",
        background=CARD_ALT,
        foreground=MUTED,
        borderwidth=0,
        font=("Segoe UI", 9, "bold"),
        padding=(8, 6),
    )
    style.map("Dark.Treeview", background=[("selected", "#2a3954")], foreground=[("selected", TEXT)])
    style.map("Dark.Treeview.Heading", background=[("active", CARD_ALT)])

    style.configure("Dark.TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "Dark.TNotebook.Tab",
        background=CARD,
        foreground=MUTED,
        padding=(16, 8),
        font=("Segoe UI", 9, "bold"),
        borderwidth=0,
    )
    style.map(
        "Dark.TNotebook.Tab",
        background=[("selected", CARD_ALT), ("active", "#2c2c36")],
        foreground=[("selected", TEXT), ("active", TEXT)],
    )
    style.configure(
        "Render.Horizontal.TProgressbar",
        troughcolor=CARD_ALT,
        background=ACCENT,
        bordercolor=BORDER,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        thickness=12,
    )
    style.configure(
        "Dark.TCombobox",
        fieldbackground=CARD,
        background=CARD_ALT,
        foreground=TEXT,
        arrowcolor=TEXT,
        bordercolor=BORDER,
    )

    def make_btn(parent, text, command, *, bg=CARD_ALT, fg=TEXT, hover=BORDER, padx=12, pady=6, font_size=9, bold=False):
        weight = "bold" if bold else "normal"
        b = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover,
            activeforeground=fg,
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
    auto_refresh_var = tk.BooleanVar(value=True)
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

    # --- Status bar packed FIRST at bottom ---
    status_var = tk.StringVar(value="Ready.")
    status_bar = tk.Label(root, textvariable=status_var, bg="#0f0f12", fg=MUTED, anchor="w", padx=14, pady=6, font=("Segoe UI", 9))
    status_bar.pack(fill="x", side="bottom")

    # --- Top Header ---
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=20, pady=(14, 8))

    h_top = tk.Frame(header, bg=BG)
    h_top.pack(fill="x")
    h_right = tk.Frame(h_top, bg=BG)
    h_right.pack(side="right")
    tk.Label(h_top, text="Colab CLI Studio", bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(side="left", anchor="w")
    tk.Label(
        header,
        text="Zero-CMD GPU monitor, multi-account Compute Unit switcher, and Blender cloud render launcher.",
        bg=BG,
        fg=MUTED,
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(2, 0))

    # --- Always-visible Active Account Strip ---
    active_card = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    active_card.pack(fill="x", padx=20, pady=(0, 10))

    ac_inner = tk.Frame(active_card, bg=CARD)
    ac_inner.pack(fill="x", padx=16, pady=10)

    ac_left = tk.Frame(ac_inner, bg=CARD)
    ac_left.pack(side="left", fill="x", expand=True)
    ac_lbl_row = tk.Frame(ac_left, bg=CARD)
    ac_lbl_row.pack(anchor="w")
    tk.Label(ac_lbl_row, text="ACTIVE ACCOUNT", bg=CARD, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
    lbl_checked = tk.Label(ac_lbl_row, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
    lbl_checked.pack(side="left", padx=(10, 0))
    lbl_active_email = tk.Label(ac_left, text="Not signed in", bg=CARD, fg=TEXT, font=("Segoe UI", 12, "bold"))
    lbl_active_email.pack(anchor="w", pady=(2, 0))
    lbl_active_meta = tk.Label(ac_left, text="Click '+ Add Account' to connect a Google account.", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
    lbl_active_meta.pack(anchor="w")

    ac_right = tk.Frame(ac_inner, bg=CARD)
    ac_right.pack(side="right", padx=(12, 0))
    lbl_cu_val = tk.Label(ac_right, text="— CU", bg=CARD, fg=GREEN, font=("Segoe UI", 16, "bold"))
    lbl_cu_val.pack(anchor="e")
    lbl_vm_val = tk.Label(ac_right, text="0 active VMs", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
    lbl_vm_val.pack(anchor="e")

    # --- Notebook Tabs ---
    nb = ttk.Notebook(root, style="Dark.TNotebook")
    nb.pack(fill="both", expand=True, padx=20, pady=(0, 10))

    tab_accounts = tk.Frame(nb, bg=BG)
    tab_monitor = tk.Frame(nb, bg=BG)
    tab_render = tk.Frame(nb, bg=BG)
    nb.add(tab_accounts, text="  Accounts & CUs  ")
    nb.add(tab_monitor, text="  Live Monitor  ")
    nb.add(tab_render, text="  Launch Render  ")

    # =========================================================================
    # TAB 1: ACCOUNTS & CUs
    # =========================================================================
    acc_actions = tk.Frame(tab_accounts, bg=BG)
    acc_actions.pack(fill="x", side="bottom", pady=(8, 4))

    sec_hdr = tk.Frame(tab_accounts, bg=BG)
    sec_hdr.pack(fill="x", pady=(10, 6))
    tk.Label(sec_hdr, text="SAVED ACCOUNTS (double-click to switch)", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")

    tree_wrap = tk.Frame(tab_accounts, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    tree_wrap.pack(fill="both", expand=True)

    cols = ("status", "email", "balance", "rate", "vms")
    tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", style="Dark.Treeview", selectmode="browse", height=5)
    tree.heading("status", text="Status")
    tree.heading("email", text="Google Account")
    tree.heading("balance", text="Compute Units")
    tree.heading("rate", text="Burn Rate")
    tree.heading("vms", text="Active VMs")
    tree.column("status", width=95, anchor="center", stretch=False)
    tree.column("email", width=310, anchor="w", stretch=True)
    tree.column("balance", width=125, anchor="e", stretch=False)
    tree.column("rate", width=95, anchor="e", stretch=False)
    tree.column("vms", width=95, anchor="center", stretch=False)
    tree.pack(fill="both", expand=True, padx=1, pady=1)

    # =========================================================================
    # TAB 2: LIVE MONITOR (monitors ALL active VMs across accounts + multiple concurrent render jobs)
    # =========================================================================
    # 1) All Active Colab VMs table (across ALL saved accounts)
    vm_hdr = tk.Frame(tab_monitor, bg=BG)
    vm_hdr.pack(fill="x", pady=(8, 4))
    tk.Label(vm_hdr, text="ACTIVE COLAB VMs (ALL SAVED ACCOUNTS)", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
    lbl_total_vms = tk.Label(vm_hdr, text="0 running", bg=BG, fg=GREEN, font=("Segoe UI", 8, "bold"))
    lbl_total_vms.pack(side="left", padx=(10, 0))
    vm_hdr_btns = tk.Frame(vm_hdr, bg=BG)
    vm_hdr_btns.pack(side="right")

    vm_tree_wrap = tk.Frame(tab_monitor, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    vm_tree_wrap.pack(fill="x", pady=(0, 8))
    vm_cols = ("account", "session", "gpu", "shape", "endpoint", "rate")
    vm_tree = ttk.Treeview(vm_tree_wrap, columns=vm_cols, show="headings", style="Dark.Treeview", selectmode="browse", height=3)
    vm_tree.heading("account", text="Account")
    vm_tree.heading("session", text="Session")
    vm_tree.heading("gpu", text="GPU")
    vm_tree.heading("shape", text="Shape")
    vm_tree.heading("endpoint", text="Endpoint")
    vm_tree.heading("rate", text="Acct Burn")
    vm_tree.column("account", width=220, anchor="w", stretch=True)
    vm_tree.column("session", width=130, anchor="w", stretch=False)
    vm_tree.column("gpu", width=70, anchor="center", stretch=False)
    vm_tree.column("shape", width=85, anchor="center", stretch=False)
    vm_tree.column("endpoint", width=170, anchor="w", stretch=False)
    vm_tree.column("rate", width=85, anchor="e", stretch=False)
    vm_tree.pack(fill="x", padx=1, pady=1)
    vm_row_map: dict[str, tuple[str, str]] = {}

    # 2) Render Job selector + live progress card
    mon_top = tk.Frame(tab_monitor, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    mon_top.pack(fill="x", pady=(0, 8))

    mon_sel_row = tk.Frame(mon_top, bg=CARD)
    mon_sel_row.pack(fill="x", padx=14, pady=(8, 4))
    lbl_jobs_hdr = tk.Label(mon_sel_row, text="RENDER JOB:", bg=CARD, fg=MUTED, font=("Segoe UI", 8, "bold"))
    lbl_jobs_hdr.pack(side="left")
    job_sel_var = tk.StringVar(value="No render jobs yet")
    job_combo = ttk.Combobox(mon_sel_row, textvariable=job_sel_var, state="readonly", style="Dark.TCombobox", width=46)
    job_combo.pack(side="left", padx=(8, 8), fill="x", expand=True)
    btn_clear_hist = make_btn(mon_sel_row, "Clear History", lambda: do_clear_history(), pady=2, padx=8, font_size=8)
    btn_clear_hist.pack(side="right", padx=(6, 0))
    lbl_job_badge = tk.Label(mon_sel_row, text="IDLE", bg=CARD_ALT, fg=MUTED, padx=8, pady=2, font=("Segoe UI", 8, "bold"))
    lbl_job_badge.pack(side="right")

    lbl_job_detail = tk.Label(
        mon_top,
        text="Start a render from 'Launch Render' or via CLI/agent — multiple concurrent jobs appear in the dropdown above.",
        bg=CARD,
        fg=MUTED,
        font=("Segoe UI", 9),
    )
    lbl_job_detail.pack(anchor="w", padx=14, pady=(0, 6))

    pbar = ttk.Progressbar(mon_top, style="Render.Horizontal.TProgressbar", mode="determinate", maximum=100, value=0)
    pbar.pack(fill="x", padx=14, pady=(0, 6))

    mon_bar_row = tk.Frame(mon_top, bg=CARD)
    mon_bar_row.pack(fill="x", padx=14, pady=(0, 8))
    lbl_job_progress = tk.Label(mon_bar_row, text="0 / 0 frames", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
    lbl_job_progress.pack(side="left")
    mon_btns = tk.Frame(mon_bar_row, bg=CARD)
    mon_btns.pack(side="right")

    # 3) Scrolling Live Render Log + Latest Frame Thumbnail Preview side-by-side
    log_hdr = tk.Frame(tab_monitor, bg=BG)
    log_hdr.pack(fill="x", pady=(0, 4))
    tk.Label(log_hdr, text="LIVE RENDER LOG (SELECTED JOB)", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
    tk.Label(log_hdr, text="LATEST FRAME PREVIEW", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="right", padx=(0, 8))

    mon_bottom = tk.Frame(tab_monitor, bg=BG)
    mon_bottom.pack(fill="both", expand=True, pady=(0, 4))

    preview_wrap = tk.Frame(mon_bottom, bg=CARD, highlightbackground=BORDER, highlightthickness=1, width=260)
    preview_wrap.pack(side="right", fill="y", padx=(8, 0))
    preview_wrap.pack_propagate(False)
    lbl_preview_img = tk.Label(preview_wrap, text="Latest downloaded\nPNG frame appears\nhere automatically", bg=CARD, fg=MUTED, font=("Segoe UI", 9), justify="center")
    lbl_preview_img.pack(fill="both", expand=True, padx=6, pady=6)
    lbl_preview_cap = tk.Label(preview_wrap, text="", bg=CARD, fg=MUTED, font=("Consolas", 8))
    lbl_preview_cap.pack(side="bottom", pady=(0, 6))

    log_wrap = tk.Frame(mon_bottom, bg="#0d0d10", highlightbackground=BORDER, highlightthickness=1)
    log_wrap.pack(side="left", fill="both", expand=True)

    log_scroll = tk.Scrollbar(log_wrap)
    log_scroll.pack(side="right", fill="y")
    log_text = tk.Text(
        log_wrap,
        bg="#0d0d10",
        fg="#d4d4d8",
        insertbackground=TEXT,
        relief="flat",
        bd=0,
        wrap="none",
        font=("Consolas", 9),
        yscrollcommand=log_scroll.set,
        state="disabled",
    )
    log_text.pack(fill="both", expand=True, padx=8, pady=6)
    log_scroll.config(command=log_text.yview)
    log_text.tag_configure("frame", foreground="#4ade80")
    log_text.tag_configure("err", foreground="#f87171")
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
            elif line.startswith(("[colab]", "[prepare]", "GPU:", "BLENDER:", "LAUNCH ")):
                tag = "info"
            if tag:
                log_text.insert("end", line, tag)
            else:
                log_text.insert("end", line)
        log_text.see("end")
        log_text.configure(state="disabled")

    def clear_log_box() -> None:
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")

    # =========================================================================
    # TAB 3: LAUNCH RENDER (zero-CMD GUI launcher for Blender shots & queues)
    # =========================================================================
    ren_actions = tk.Frame(tab_render, bg=BG)
    ren_actions.pack(fill="x", side="bottom", pady=(8, 4))

    ren_card = tk.Frame(tab_render, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    ren_card.pack(fill="both", expand=True, pady=(10, 4))
    rf = tk.Frame(ren_card, bg=CARD)
    rf.pack(fill="both", expand=True, padx=18, pady=14)

    mode_var = tk.StringVar(value="shot")
    blend_var = tk.StringVar(value="")
    out_var = tk.StringVar(value="")
    prefix_var = tk.StringVar(value="f_")
    f0_var = tk.StringVar(value="1")
    f1_var = tk.StringVar(value="1")
    samples_var = tk.StringVar(value="0")
    gpu_var = tk.StringVar(value="L4")
    par_var = tk.StringVar(value="3")
    session_var = tk.StringVar(value="blender-render")
    extras_var = tk.StringVar(value="")
    keep_var = tk.BooleanVar(value=False)

    def _entry(parent, var, width=None):
        kw: dict[str, Any] = dict(
            textvariable=var,
            bg="#141418",
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

    r0 = tk.Frame(rf, bg=CARD)
    r0.pack(fill="x", pady=(0, 10))
    tk.Label(r0, text="Mode:", bg=CARD, fg=MUTED, font=("Segoe UI", 9, "bold")).pack(side="left")
    for val, lbl in (("shot", "Single .blend Shot"), ("queue", "Batch Queue (jobs.json)")):
        tk.Radiobutton(
            r0,
            text=lbl,
            variable=mode_var,
            value=val,
            bg=CARD,
            fg=TEXT,
            selectcolor=CARD_ALT,
            activebackground=CARD,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        ).pack(side="left", padx=(12, 0))

    r1 = tk.Frame(rf, bg=CARD)
    r1.pack(fill="x", pady=(0, 8))
    tk.Label(r1, text="Scene / Queue:", bg=CARD, fg=MUTED, width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
    _entry(r1, blend_var).pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))

    def browse_input() -> None:
        if mode_var.get() == "queue":
            p = filedialog.askopenfilename(title="Select jobs.json", filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        else:
            p = filedialog.askopenfilename(title="Select Blender scene", filetypes=[("Blender files", "*.blend"), ("All files", "*.*")])
        if p:
            blend_var.set(p)
            if mode_var.get() == "shot" and not out_var.get().strip():
                bp = Path(p)
                out_var.set(str(bp.parent.parent / "comp" / bp.stem if bp.parent.name.lower() == "blender" else bp.parent / "renders" / bp.stem))

    make_btn(r1, "Browse…", browse_input, pady=4).pack(side="right")

    r2 = tk.Frame(rf, bg=CARD)
    r2.pack(fill="x", pady=(0, 10))
    tk.Label(r2, text="Output Folder:", bg=CARD, fg=MUTED, width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
    _entry(r2, out_var).pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))

    def browse_out() -> None:
        p = filedialog.askdirectory(title="Select output folder for rendered frames")
        if p:
            out_var.set(p)

    make_btn(r2, "Browse…", browse_out, pady=4).pack(side="right")

    r3 = tk.Frame(rf, bg=CARD)
    r3.pack(fill="x", pady=(0, 10))
    tk.Label(r3, text="Frames:", bg=CARD, fg=MUTED, width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
    _entry(r3, f0_var, width=7).pack(side="left", ipady=4)
    tk.Label(r3, text="to", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=6)
    _entry(r3, f1_var, width=7).pack(side="left", ipady=4)
    tk.Label(r3, text="Prefix:", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(18, 6))
    _entry(r3, prefix_var, width=8).pack(side="left", ipady=4)
    tk.Label(r3, text="Samples (0=default):", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(18, 6))
    _entry(r3, samples_var, width=7).pack(side="left", ipady=4)

    r4 = tk.Frame(rf, bg=CARD)
    r4.pack(fill="x", pady=(0, 10))
    tk.Label(r4, text="GPU / Workers:", bg=CARD, fg=MUTED, width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
    ttk.Combobox(r4, textvariable=gpu_var, values=("L4", "T4", "A100", "H100", "G4", "CPU"), width=7, state="readonly", style="Dark.TCombobox").pack(side="left")
    tk.Label(r4, text="Parallel:", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(14, 6))
    ttk.Combobox(r4, textvariable=par_var, values=("1", "2", "3", "4"), width=4, state="readonly", style="Dark.TCombobox").pack(side="left")
    tk.Label(r4, text="Session (-s):", bg=CARD, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=(14, 6))
    _entry(r4, session_var, width=16).pack(side="left", ipady=4)
    tk.Checkbutton(
        r4,
        text="Keep VM (--keep)",
        variable=keep_var,
        bg=CARD,
        fg=TEXT,
        selectcolor=CARD_ALT,
        activebackground=CARD,
        activeforeground=TEXT,
        font=("Segoe UI", 9),
    ).pack(side="left", padx=(14, 0))

    r5 = tk.Frame(rf, bg=CARD)
    r5.pack(fill="x", pady=(0, 6))
    tk.Label(r5, text="Extras:", bg=CARD, fg=MUTED, width=14, anchor="w", font=("Segoe UI", 9)).pack(side="left")
    _entry(r5, extras_var).pack(side="left", fill="x", expand=True, ipady=4)
    tk.Label(
        rf,
        text="Optional space-separated cloud_render.py extras, e.g. `fmt=exr pct=50` or `fmt=v7 camera=CAM_1`.",
        bg=CARD,
        fg=MUTED,
        font=("Segoe UI", 8),
    ).pack(anchor="w", padx=(106, 0))

    # =========================================================================
    # Shared helpers & live polling
    # =========================================================================
    def set_status(msg: str, color: str = MUTED) -> None:
        status_var.set(msg)
        status_bar.configure(fg=color)

    def render_accounts_ui() -> None:
        idx = _load_index()
        active_email = idx.get("active")
        accounts = idx.get("accounts", {})

        if active_email:
            lbl_active_email.configure(text=active_email, fg=TEXT)
            info = live_cache.get(active_email) or accounts.get(active_email, {})
            bal = info.get("balance")
            rate = info.get("rate")
            vms = info.get("assignments_count")
            chk = info.get("checked_at", "")
            assigns = info.get("assignments") or []
            if bal is not None:
                cu_color = GREEN if bal >= 10 else (AMBER if bal > 0 else RED)
                lbl_cu_val.configure(text=f"{bal:.2f} CU", fg=cu_color)
                lbl_active_meta.configure(text=f"Usage rate: {rate:.2f} CU/hr   •   OS keyring synced with `colab` CLI")
                vm_text = f"{vms} active VM{'s' if vms != 1 else ''}"
                lbl_vm_val.configure(text=vm_text, fg=AMBER if vms else MUTED)
                btn_stop_vms.configure(state="normal" if vms else "disabled")
            else:
                lbl_cu_val.configure(text="… CU", fg=MUTED)
                lbl_active_meta.configure(text="Loading live Compute Units balance…")
                lbl_vm_val.configure(text="")
                btn_stop_vms.configure(state="normal")
            lbl_checked.configure(text=f"Updated {chk}" if chk else "")
        else:
            lbl_active_email.configure(text="Not signed in", fg=AMBER)
            lbl_active_meta.configure(text="Click '+ Add Account' below to connect a Google account.")
            lbl_cu_val.configure(text="— CU", fg=MUTED)
            lbl_vm_val.configure(text="0 active VMs", fg=MUTED)
            lbl_checked.configure(text="")
            btn_stop_vms.configure(state="disabled")

        selected = tree.selection()
        prev_sel = selected[0] if selected else None
        for item in tree.get_children():
            tree.delete(item)

        for email in sorted(accounts.keys()):
            meta = live_cache.get(email) or accounts.get(email, {})
            is_active = email == active_email
            st_txt = "● ACTIVE" if is_active else ""
            err = meta.get("error")
            if err:
                bal_txt, rate_txt, vm_txt = "Auth error", "—", "—"
            elif meta.get("balance") is not None:
                bal_txt = f"{meta['balance']:.2f} CU"
                rate_txt = f"{meta.get('rate', 0.0):.2f}/hr"
                vm_txt = str(meta.get("assignments_count", 0))
            else:
                bal_txt, rate_txt, vm_txt = "—", "—", "—"
            tree.insert("", "end", iid=email, values=(st_txt, email, bal_txt, rate_txt, vm_txt))

        if prev_sel and prev_sel in accounts:
            tree.selection_set(prev_sel)
        elif active_email and active_email in accounts:
            tree.selection_set(active_email)

        # Populate Live Monitor VM table across ALL saved accounts
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
                sname = ep_map.get(ep, "(browser / external)")
                acc_lbl = f"● {email}" if email == active_email else email
                row_id = f"{email}::{ep}"
                vm_row_map[row_id] = (email, ep)
                vm_tree.insert(
                    "",
                    "end",
                    iid=row_id,
                    values=(acc_lbl, sname, a.get("accelerator", "?"), a.get("shape", "STD"), ep[:22], f"{rate_val:.2f}/hr"),
                )
                total_running += 1
        if prev_vm_sel and prev_vm_sel in vm_row_map:
            vm_tree.selection_set(prev_vm_sel)
        lbl_total_vms.configure(
            text=f"{total_running} running across all accounts" if total_running else "0 running (0.00 CU/hr)",
            fg=AMBER if total_running else GREEN,
        )
        btn_stop_one_vm.configure(state="normal" if total_running else "disabled")

    def _on_job_selected(_event=None) -> None:
        lbl = job_sel_var.get()
        rid = log_state["jobs_by_label"].get(lbl)
        if rid:
            log_state["selected_run_id"] = rid
            poll_render_monitor(reschedule=False)

    job_combo.bind("<<ComboboxSelected>>", _on_job_selected)

    def poll_render_monitor(*, reschedule: bool = True) -> None:
        try:
            active_states = {"starting", "preparing", "provisioning", "uploading", "rendering"}
            job_entries: list[tuple[str, dict[str, Any], Path]] = []
            if JOBS_DIR.is_dir():
                for jp in sorted(JOBS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:15]:
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

            # Build dropdown labels (active jobs first, then recent finished)
            job_entries.sort(key=lambda x: (0 if x[1].get("status") in active_states else 1, -float(x[1].get("updated_at") or 0)))
            labels = []
            by_label: dict[str, str] = {}
            by_id: dict[str, tuple[dict[str, Any], Path, str]] = {}
            for rid, d, lp in job_entries:
                st = str(d.get("status", "idle")).upper()
                jname = d.get("job_name") or Path(d.get("blend") or rid).name
                done_f = int(d.get("done_frames") or 0)
                tot_f = int(d.get("total_frames") or 0)
                gpu = d.get("gpu") or "?"
                sess = d.get("session") or ""
                lbl = f"[{st}] {jname} ({done_f}/{tot_f}) — {gpu} / {sess} [{rid[-5:]}]"
                labels.append(lbl)
                by_label[lbl] = rid
                by_id[rid] = (d, lp, lbl)

            log_state["jobs_by_label"] = by_label
            log_state["jobs_by_id"] = by_id
            active_cnt = sum(1 for _, d, _ in job_entries if d.get("status") in active_states)
            hist_cnt = len(job_entries) - active_cnt
            lbl_jobs_hdr.configure(text=f"JOBS ({active_cnt} live, {hist_cnt} past):")

            if labels:
                job_combo.configure(values=labels)
                sel_rid = log_state["selected_run_id"]
                if not sel_rid or sel_rid not in by_id:
                    sel_rid = job_entries[0][0]
                    log_state["selected_run_id"] = sel_rid
                data, log_path, cur_lbl = by_id[sel_rid]
                if job_sel_var.get() != cur_lbl:
                    job_sel_var.set(cur_lbl)

                st = str(data.get("status", "idle"))
                badge_colors = {
                    "rendering": (GREEN, "#132a1c"),
                    "uploading": (ACCENT, "#172554"),
                    "provisioning": (AMBER, "#3b270c"),
                    "preparing": (AMBER, "#3b270c"),
                    "done": (GREEN, "#132a1c"),
                    "error": (RED, "#3b1518"),
                    "stopped": (MUTED, CARD_ALT),
                }
                fg_c, bg_c = badge_colors.get(st, (MUTED, CARD_ALT))
                lbl_job_badge.configure(text=st.upper(), fg=fg_c, bg=bg_c)

                total = max(0, int(data.get("total_frames") or 0))
                done = max(0, int(data.get("done_frames") or 0))
                pct = (100.0 * done / total) if total > 0 else 0.0
                pbar.configure(value=min(100.0, pct))

                avg_s = float(data.get("avg_frame_sec") or 0.0)
                par = max(1, int(data.get("par") or 1))
                rem = max(0, total - done)
                eta = _fmt_eta((rem * avg_s) / par) if (avg_s > 0 and rem > 0 and st == "rendering") else "—"
                last_f = data.get("last_frame") or ""
                last_s = float(data.get("last_frame_sec") or 0.0)
                last_part = f" • last: {last_f} ({last_s:.1f}s)" if last_f else ""
                lbl_job_progress.configure(text=f"{done} / {total} frames ({pct:.0f}%) • ETA {eta}{last_part}")

                out_dir = data.get("out_dir") or ""
                log_state["last_out_dir"] = out_dir
                gpu = data.get("gpu") or "?"
                sess = data.get("session") or ""
                err_txt = data.get("error") or ""
                if err_txt:
                    lbl_job_detail.configure(text=f"Error: {err_txt}", fg=RED)
                else:
                    lbl_job_detail.configure(text=f"GPU: {gpu} • {par} worker(s) • session: {sess} • out: {out_dir}", fg=MUTED)
                btn_open_out.configure(state="normal" if out_dir and os.path.exists(out_dir) else "disabled")
                btn_cancel_render.configure(state="normal" if st in active_states else "disabled")

                # Update live frame preview thumbnail
                fp = data.get("last_frame_path") or ""
                if fp and fp != log_state["thumb_path"] and os.path.isfile(fp) and fp.lower().endswith(".png"):
                    try:
                        img = tk.PhotoImage(file=fp)
                        w_i, h_i = img.width(), img.height()
                        scale = max(1, int(max(w_i / 242, h_i / 150)))
                        if scale > 1:
                            img = img.subsample(scale, scale)
                        log_state["thumb_img"] = img
                        log_state["thumb_path"] = fp
                        lbl_preview_img.configure(image=img, text="")
                        lbl_preview_cap.configure(text=os.path.basename(fp))
                    except Exception:
                        pass

                # Tail selected job's log incrementally
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
            set_status("Checking saved accounts and live Compute Unit balances...", MUTED)

        def worker() -> None:
            try:
                if sync_first:
                    sync_current_cli_login()
                idx = _load_index()
                emails = sorted(idx.get("accounts", {}).keys())
                for em in emails:
                    tok = _get_account_token_json(em)
                    if not tok:
                        live_cache[em] = {"error": "Missing keyring token"}
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
                msg = f"Updated {len(emails)} account(s) at {datetime.now().strftime('%H:%M:%S')}."
                root.after(0, lambda: _done_refresh(msg, GREEN if not silent else MUTED))
            except Exception as e:
                err_msg = str(e)
                root.after(0, lambda: _done_refresh(f"Refresh error: {err_msg}", RED))

        def _done_refresh(msg: str, color: str) -> None:
            busy["refresh"] = False
            render_accounts_ui()
            if not silent or color == RED:
                set_status(msg, color)

        threading.Thread(target=worker, daemon=True).start()

    def schedule_auto_refresh() -> None:
        if auto_refresh_var.get():
            refresh_all(sync_first=False, silent=True)
        root.after(15000, schedule_auto_refresh)

    def do_switch(_event=None) -> None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Select Account", "Select an account from the list first.")
            return
        target_email = sel[0]
        idx = _load_index()
        old_active = idx.get("active")
        if old_active == target_email:
            set_status(f"{target_email} is already the active account.", MUTED)
            return

        old_vms = (live_cache.get(old_active) or idx.get("accounts", {}).get(old_active or "", {})).get("assignments_count", 0)
        if old_active and old_vms:
            ans = messagebox.askyesnocancel(
                "Active GPU VM Running",
                f"{old_active} currently has {old_vms} active Colab VM(s).\n\n"
                "Stop those VM(s) before switching so you don't burn Compute Units?",
            )
            if ans is None:
                return
            if ans is True:
                set_status(f"Stopping active VM(s) on {old_active}...", AMBER)
                root.update_idletasks()
                try:
                    stop_all_vms_for_account(old_active)
                except Exception as e:
                    messagebox.showerror("Stop VM Failed", str(e))
                    return

        try:
            switch_active_account(target_email)
            render_accounts_ui()
            set_status(f"Switched active account to {target_email}.", GREEN)
            refresh_all(sync_first=False)
        except Exception as e:
            messagebox.showerror("Switch Failed", str(e))
            set_status(f"Switch failed: {e}", RED)

    def do_stop_vms() -> None:
        idx = _load_index()
        active_email = idx.get("active")
        if not active_email:
            return
        if not messagebox.askyesno(
            "Stop Active VMs",
            f"Disconnect and delete all running Colab VMs on {active_email}?",
        ):
            return
        set_status(f"Stopping running VMs on {active_email}...", AMBER)

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
                set_status(f"Failed to stop VMs: {err}", RED)
            else:
                set_status(f"Stopped {n} VM(s) on {active_email}.", GREEN)
                refresh_all(sync_first=False)

        threading.Thread(target=worker, daemon=True).start()

    def do_remove() -> None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Select Account", "Select an account to remove.")
            return
        email = sel[0]
        if not messagebox.askyesno(
            "Remove Account",
            f"Remove saved credentials for {email} from this machine?\n(Your Google account and Colab data are untouched.)",
        ):
            return
        remove_saved_account(email)
        live_cache.pop(email, None)
        render_accounts_ui()
        set_status(f"Removed {email}.", MUTED)

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
        clear_log_box()
        job_combo.configure(values=[])
        job_sel_var.set("No render jobs yet")
        lbl_job_badge.configure(text="IDLE", fg=MUTED, bg=CARD_ALT)
        lbl_job_progress.configure(text="0 / 0 frames")
        pbar.configure(value=0)
        set_status(f"Cleared {removed} finished/errored job record(s).", MUTED)
        poll_render_monitor(reschedule=False)

    def do_stop_one_vm() -> None:
        sel = vm_tree.selection()
        if not sel or sel[0] not in vm_row_map:
            messagebox.showinfo("Select VM", "Select a running VM in the Active Colab VMs table first.")
            return
        email, ep = vm_row_map[sel[0]]
        if not messagebox.askyesno("Stop Selected VM", f"Disconnect and delete VM `{ep[:22]}…` on {email}?"):
            return
        set_status(f"Stopping VM {ep[:14]}… on {email}...", AMBER)

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
                set_status(f"Stop VM failed: {err}", RED)
            else:
                set_status(f"Stopped VM on {email}.", GREEN)
                refresh_all(sync_first=False)

        threading.Thread(target=worker, daemon=True).start()

    def do_launch_render(*, dry_run: bool) -> None:
        if not RENDER_SCRIPT.is_file():
            messagebox.showerror("Missing Script", f"Could not find {RENDER_SCRIPT}")
            return
        target = blend_var.get().strip()
        if not target or not Path(target).is_file():
            messagebox.showerror("Select File", "Please choose an existing .blend or jobs.json file first.")
            return

        py_exe = sys.executable
        if os.name == "nt" and py_exe.lower().endswith("pythonw.exe"):
            cand = Path(py_exe).with_name("python.exe")
            if cand.is_file():
                py_exe = str(cand)

        cmd = [py_exe, str(RENDER_SCRIPT)]
        if mode_var.get() == "queue":
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

        sess_name = session_var.get().strip() or "blender-render"
        cmd += ["--gpu", gpu_var.get() or "L4", "--par", par_var.get() or "3", "-s", sess_name]
        if keep_var.get():
            cmd.append("--keep")
        if dry_run:
            cmd.append("--dry-run")

        nb.select(tab_monitor)
        if dry_run:
            log_state["selected_run_id"] = None
            log_state["tailed_run_id"] = "__dry__"
            clear_log_box()
            append_log_lines(f"[gui] Dry run: {' '.join(cmd)}\n")
        else:
            # Auto-select the newly spawned job once its telemetry file appears
            log_state["selected_run_id"] = None
        set_status("Dry-running shot..." if dry_run else f"Render started on session '{sess_name}' (no CMD window).", ACCENT)

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
                    set_status("Dry run finished." if rc == 0 else f"Dry run failed (rc={rc}).", GREEN if rc == 0 else RED)
                else:
                    set_status(
                        f"Render '{sess_name}' finished!" if rc == 0 else f"Render '{sess_name}' ended (rc={rc}).",
                        GREEN if rc == 0 else AMBER,
                    )
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
        if not messagebox.askyesno(
            "Stop Selected Render",
            f"Terminate render job '{data.get('job_name', '')}' (session: {sess}, pid: {pid})?",
        ):
            return
        if pid and _is_pid_alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], creationflags=CREATION_FLAGS, check=False)
            else:
                try:
                    os.kill(pid, 15)
                except OSError:
                    pass
        # Also stop that specific session if it exists on the active account
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
        set_status(f"Stopped job '{data.get('job_name', '')}'.", AMBER)
        refresh_all(sync_first=False)

    def open_add_account_dialog() -> None:
        try:
            flow, auth_url = begin_oauth_flow()
        except Exception as e:
            messagebox.showerror("OAuth Error", str(e))
            return

        dlg = tk.Toplevel(root)
        dlg.title("Add Google Account")
        dlg.geometry("600x360")
        dlg.minsize(560, 340)
        dlg.configure(bg=BG)
        dlg.transient(root)
        dlg.grab_set()

        btn_row = tk.Frame(dlg, bg=BG)
        btn_row.pack(fill="x", side="bottom", padx=20, pady=(6, 16))

        tk.Label(dlg, text="Sign in with a Google Account", bg=BG, fg=TEXT, font=("Segoe UI", 13, "bold")).pack(
            anchor="w", padx=20, pady=(16, 4)
        )
        tk.Label(
            dlg,
            text="1. Open the sign-in link in your browser (or copy it into an Incognito / other Chrome profile).\n"
                 "2. Choose the Google account you want to add and approve access.\n"
                 "3. Copy the authorization code Google displays and paste it below.",
            bg=BG,
            fg=MUTED,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=20, pady=(0, 12))

        url_btns = tk.Frame(dlg, bg=BG)
        url_btns.pack(fill="x", padx=20, pady=(0, 12))

        def copy_url() -> None:
            dlg.clipboard_clear()
            dlg.clipboard_append(auth_url)
            lbl_dlg_status.configure(text="Sign-in URL copied to clipboard! Paste into any browser profile.", fg=GREEN)

        make_btn(
            url_btns,
            "Open Sign-in Page in Browser",
            lambda: (webbrowser.open(auth_url), lbl_dlg_status.configure(text="Opened sign-in page in default browser.", fg=MUTED)),
            bg=ACCENT,
            hover=ACCENT_HOVER,
            bold=True,
        ).pack(side="left")
        make_btn(url_btns, "Copy URL (for Incognito / other profile)", copy_url).pack(side="left", padx=(8, 0))

        tk.Label(dlg, text="Authorization code:", bg=BG, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=20)
        code_var = tk.StringVar()
        entry = tk.Entry(
            dlg,
            textvariable=code_var,
            bg=CARD,
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
            text="Switch to this account immediately",
            variable=activate_var,
            bg=BG,
            fg=TEXT,
            selectcolor=CARD,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=18, pady=(0, 8))

        lbl_dlg_status = tk.Label(dlg, text="", bg=BG, fg=MUTED, font=("Segoe UI", 9), wraplength=540, justify="left")
        lbl_dlg_status.pack(anchor="w", padx=20, pady=(0, 6))

        def submit_code() -> None:
            code = code_var.get().strip()
            if not code:
                lbl_dlg_status.configure(text="Paste the authorization code from Google first.", fg=AMBER)
                return
            btn_connect.configure(state="disabled", text="Connecting…")
            lbl_dlg_status.configure(text="Exchanging code & checking Compute Unit balance…", fg=MUTED)

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
                set_status(f"Connected {info['email']} ({info['balance']:.2f} CU). Ready!", GREEN)

            def _on_err(err: str) -> None:
                btn_connect.configure(state="normal", text="Connect Account")
                lbl_dlg_status.configure(text=f"Error: {err}", fg=RED)

            threading.Thread(target=worker, daemon=True).start()

        btn_connect = make_btn(btn_row, "Connect Account", submit_code, bg=ACCENT, hover=ACCENT_HOVER, bold=True)
        btn_connect.pack(side="right")
        make_btn(btn_row, "Cancel", dlg.destroy).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", lambda _e: submit_code())

    # --- Wire Header, Tab 1, Tab 2, Tab 3 buttons ---
    tk.Checkbutton(
        h_right,
        text="Auto-refresh (15s)",
        variable=auto_refresh_var,
        bg=BG,
        fg=MUTED,
        selectcolor=CARD,
        activebackground=BG,
        activeforeground=TEXT,
        font=("Segoe UI", 9),
    ).pack(side="left", padx=(0, 10))
    make_btn(h_right, "Refresh All", lambda: refresh_all(sync_first=True)).pack(side="left", padx=(0, 8))
    make_btn(h_right, "Buy CUs", lambda: webbrowser.open("https://colab.research.google.com/signup")).pack(side="left")

    make_btn(acc_actions, "+ Add Account", open_add_account_dialog, bg=ACCENT, hover=ACCENT_HOVER, bold=True).pack(side="left")
    make_btn(acc_actions, "Switch to Selected", do_switch).pack(side="left", padx=(8, 0))
    btn_stop_vms = make_btn(acc_actions, "Stop Active VMs", do_stop_vms, bg="#3b1d20", fg="#fca5a5", hover="#58262b")
    btn_stop_vms.pack(side="left", padx=(8, 0))
    make_btn(acc_actions, "Remove Selected", do_remove).pack(side="right")

    btn_stop_one_vm = make_btn(vm_hdr_btns, "Stop Selected VM", do_stop_one_vm, bg="#3b1d20", fg="#fca5a5", hover="#58262b", pady=3)
    btn_stop_one_vm.pack(side="left")
    btn_stop_one_vm.configure(state="disabled")

    btn_open_out = make_btn(mon_btns, "Open Output Folder", lambda: _open_path_in_explorer(log_state["last_out_dir"]), pady=4)
    btn_open_out.pack(side="left", padx=(0, 8))
    btn_open_out.configure(state="disabled")
    btn_cancel_render = make_btn(mon_btns, "Stop Selected Job", do_cancel_render, bg="#3b1d20", fg="#fca5a5", hover="#58262b", pady=4)
    btn_cancel_render.pack(side="left")
    btn_cancel_render.configure(state="disabled")

    btn_dry_run = make_btn(ren_actions, "Dry Run (Check Assets)", lambda: do_launch_render(dry_run=True))
    btn_dry_run.pack(side="left")
    btn_start_render = make_btn(ren_actions, "▶ Start Render", lambda: do_launch_render(dry_run=False), bg=ACCENT, hover=ACCENT_HOVER, bold=True)
    btn_start_render.pack(side="left", padx=(8, 0))

    tree.bind("<Double-1>", do_switch)

    tab_map = {"accounts": tab_accounts, "monitor": tab_monitor, "render": tab_render}
    if initial_tab in tab_map:
        nb.select(tab_map[initial_tab])

    def _cleanup_pid() -> None:
        try:
            if GUI_PID_PATH.is_file() and GUI_PID_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                GUI_PID_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    def _on_close() -> None:
        _cleanup_pid()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)

    render_accounts_ui()
    root.after(150, lambda: refresh_all(sync_first=True))
    root.after(300, poll_render_monitor)
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
    initial_tab = "accounts"
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
            print("Opened Colab CLI Studio on Live Monitor." if launched else "Colab CLI Studio is already open.")
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
            print("No saved Colab accounts. Run `python colab_accounts.py` to add one.")
            return 0
        for r in rows:
            mark = "*" if r["active"] else " "
            if "error" in r:
                print(f"{mark} {r['email']:38s}  [error: {r['error']}]")
            else:
                print(
                    f"{mark} {r['email']:38s}  {r['balance']:7.2f} CU  "
                    f"({r['rate']:.2f}/hr, {r['assignments_count']} VM)"
                )
        return 0

    if cmd == "active":
        em = sync_current_cli_login()
        if not em:
            if as_json:
                print(json.dumps({"active": None}))
            else:
                print("Not signed in.")
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
        print(f"Active account : {info['email']}")
        print(f"Compute units  : {info['balance']:.2f} CU")
        print(f"Usage rate     : {info['rate']:.2f}/hr")
        print(f"Active VMs     : {info['assignments_count']}")
        return 0

    if cmd == "switch":
        if len(argv) < 2:
            raise SystemExit("Usage: python colab_accounts.py switch <email>")
        em = switch_active_account(argv[1])
        if as_json:
            print(json.dumps({"switched_to": em}))
        else:
            print(f"Switched active Colab CLI account to: {em}")
        return 0

    if cmd == "stop-vms":
        em = argv[1] if len(argv) >= 2 else sync_current_cli_login()
        if not em:
            raise SystemExit("No active account.")
        n = stop_all_vms_for_account(em)
        if as_json:
            print(json.dumps({"email": em, "stopped_vms": n}))
        else:
            print(f"Stopped {n} active VM(s) on {em}.")
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
                            "session": ep_map.get(a["endpoint"], "(browser / external)"),
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
            print("No active Colab VMs across any saved accounts.")
            return 0
        for v in all_vms:
            mark = "*" if v["active_account"] else " "
            print(f"{mark} {v['account']:32s}  {v['session']:18s}  {v['accelerator']:5s}  {v['endpoint'][:22]}  ({v['rate']:.2f}/hr)")
        return 0

    raise SystemExit(f"Unknown command: {cmd}. Expected: gui | list | active | vms | switch <email> | stop-vms [email]")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
