"""Multi-account manager & simple GUI for `google-colab-cli` (OS keyring backed).

Lets you add multiple Google accounts, inspect live Compute Unit (CU) balances and active VMs,
switch the active `colab` CLI account in one click when Compute Units run out, and stop stray VMs.

Usage:
  python colab_accounts.py              # Launch the GUI
  python colab_accounts.py list         # Print saved accounts & live CU balances
  python colab_accounts.py switch EMAIL # Switch active account from CLI
  python colab_accounts.py active       # Show active account email & CU balance
  python colab_accounts.py stop-vms     # Stop all running Colab VMs on the active account
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


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


# ---------------------------------------------------------------------------
# Account store & Colab API helpers
# ---------------------------------------------------------------------------
def _safe_slug(email: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", email.lower()).strip("_") or "account"


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
    """Refresh token if needed and fetch email, CU balance, hourly rate, and active assignments."""
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
            {"endpoint": a.endpoint, "accelerator": a.accelerator.value}
            for a in assignments
        ],
        "token_json": creds.to_json() if refreshed else token_json,
        "checked_at": datetime.now().strftime("%H:%M:%S"),
    }


def stop_all_vms_for_account(email: str) -> int:
    """Unassign every active Colab VM on the specified account. Returns count stopped."""
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
    """If `colab` already has an active token in keyring, ensure it is indexed in saved accounts."""
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


# ---------------------------------------------------------------------------
# Tkinter GUI
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


def launch_gui() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Colab CLI — Account Switcher")
    root.geometry("820x580")
    root.minsize(720, 500)
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
        rowheight=34,
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
    style.map(
        "Dark.Treeview",
        background=[("selected", "#2a3954")],
        foreground=[("selected", TEXT)],
    )
    style.map("Dark.Treeview.Heading", background=[("active", CARD_ALT)])

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
    busy = {"flag": False}

    # --- Header ---
    header = tk.Frame(root, bg=BG)
    header.pack(fill="x", padx=20, pady=(16, 10))

    h_top = tk.Frame(header, bg=BG)
    h_top.pack(fill="x")
    h_right = tk.Frame(h_top, bg=BG)
    h_right.pack(side="right")
    tk.Label(h_top, text="Colab CLI Account Switcher", bg=BG, fg=TEXT, font=("Segoe UI", 15, "bold")).pack(
        side="left", anchor="w"
    )
    tk.Label(
        header,
        text="Switch Google accounts for Blender cloud renders when Compute Units run out. Tokens stay in your OS keyring.",
        bg=BG,
        fg=MUTED,
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(4, 0))

    # --- Active Account Card ---
    active_card = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    active_card.pack(fill="x", padx=20, pady=(0, 14))

    ac_top = tk.Frame(active_card, bg=CARD)
    ac_top.pack(fill="x", padx=16, pady=(12, 6))
    tk.Label(ac_top, text="ACTIVE ACCOUNT", bg=CARD, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")
    lbl_checked = tk.Label(ac_top, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8))
    lbl_checked.pack(side="right")

    ac_mid = tk.Frame(active_card, bg=CARD)
    ac_mid.pack(fill="x", padx=16, pady=(0, 14))

    ac_info = tk.Frame(ac_mid, bg=CARD)
    ac_info.pack(side="left", fill="x", expand=True)
    lbl_active_email = tk.Label(ac_info, text="Not signed in", bg=CARD, fg=TEXT, font=("Segoe UI", 13, "bold"))
    lbl_active_email.pack(anchor="w")
    lbl_active_meta = tk.Label(ac_info, text="Click '+ Add Account' below to sign in.", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
    lbl_active_meta.pack(anchor="w", pady=(3, 0))

    ac_metrics = tk.Frame(ac_mid, bg=CARD)
    ac_metrics.pack(side="right", padx=(12, 0))
    lbl_cu_val = tk.Label(ac_metrics, text="— CU", bg=CARD, fg=GREEN, font=("Segoe UI", 16, "bold"))
    lbl_cu_val.pack(anchor="e")
    lbl_vm_val = tk.Label(ac_metrics, text="0 active VMs", bg=CARD, fg=MUTED, font=("Segoe UI", 9))
    lbl_vm_val.pack(anchor="e")

    # --- Pack bottom bars FIRST so expanding Treeview never clips buttons ---
    status_var = tk.StringVar(value="Ready.")
    status_bar = tk.Label(root, textvariable=status_var, bg="#0f0f12", fg=MUTED, anchor="w", padx=14, pady=6, font=("Segoe UI", 9))
    status_bar.pack(fill="x", side="bottom")

    actions = tk.Frame(root, bg=BG)
    actions.pack(fill="x", side="bottom", padx=20, pady=(4, 12))

    # --- Saved Accounts Section ---
    sec_hdr = tk.Frame(root, bg=BG)
    sec_hdr.pack(fill="x", padx=20, pady=(0, 6))
    tk.Label(sec_hdr, text="SAVED ACCOUNTS (double-click to switch)", bg=BG, fg=MUTED, font=("Segoe UI", 8, "bold")).pack(side="left")

    tree_wrap = tk.Frame(root, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
    tree_wrap.pack(fill="both", expand=True, padx=20, pady=(0, 8))

    cols = ("status", "email", "balance", "rate", "vms")
    tree = ttk.Treeview(tree_wrap, columns=cols, show="headings", style="Dark.Treeview", selectmode="browse", height=5)
    tree.heading("status", text="Status")
    tree.heading("email", text="Google Account")
    tree.heading("balance", text="Compute Units")
    tree.heading("rate", text="Burn Rate")
    tree.heading("vms", text="Active VMs")

    tree.column("status", width=95, anchor="center", stretch=False)
    tree.column("email", width=290, anchor="w", stretch=True)
    tree.column("balance", width=120, anchor="e", stretch=False)
    tree.column("rate", width=95, anchor="e", stretch=False)
    tree.column("vms", width=95, anchor="center", stretch=False)
    tree.pack(fill="both", expand=True, padx=1, pady=1)

    def set_status(msg: str, color: str = MUTED) -> None:
        status_var.set(msg)
        status_bar.configure(fg=color)

    def render_ui() -> None:
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
            if bal is not None:
                cu_color = GREEN if bal >= 10 else (AMBER if bal > 0 else RED)
                lbl_cu_val.configure(text=f"{bal:.2f} CU", fg=cu_color)
                lbl_active_meta.configure(text=f"Usage rate: {rate:.2f} CU/hr   •   Ready for `python colab_render.py`")
                vm_text = f"{vms} active VM{'s' if vms != 1 else ''}"
                lbl_vm_val.configure(text=vm_text, fg=AMBER if vms else MUTED)
                btn_stop_vms.configure(state="normal" if vms else "disabled")
            else:
                lbl_cu_val.configure(text="… CU", fg=MUTED)
                lbl_active_meta.configure(text="Click 'Refresh All' to load live Compute Units balance.")
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

    def refresh_all(*, sync_first: bool = True) -> None:
        if busy["flag"]:
            return
        busy["flag"] = True
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
                root.after(0, lambda: _done_refresh(f"Refreshed {len(emails)} account(s) at {datetime.now().strftime('%H:%M:%S')}.", GREEN))
            except Exception as e:
                err_msg = str(e)
                root.after(0, lambda: _done_refresh(f"Refresh error: {err_msg}", RED))

        def _done_refresh(msg: str, color: str) -> None:
            busy["flag"] = False
            render_ui()
            set_status(msg, color)

        threading.Thread(target=worker, daemon=True).start()

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
            render_ui()
            set_status(f"Switched active account to {target_email}. Re-run your render command to resume!", GREEN)
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
        render_ui()
        set_status(f"Removed {email}.", MUTED)

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
        chk = tk.Checkbutton(
            dlg,
            text="Switch to this account immediately",
            variable=activate_var,
            bg=BG,
            fg=TEXT,
            selectcolor=CARD,
            activebackground=BG,
            activeforeground=TEXT,
            font=("Segoe UI", 9),
        )
        chk.pack(anchor="w", padx=18, pady=(0, 8))

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
                render_ui()
                set_status(
                    f"Connected {info['email']} ({info['balance']:.2f} CU). Ready to render!",
                    GREEN,
                )

            def _on_err(err: str) -> None:
                btn_connect.configure(state="normal", text="Connect Account")
                lbl_dlg_status.configure(text=f"Error: {err}", fg=RED)

            threading.Thread(target=worker, daemon=True).start()

        btn_connect = make_btn(btn_row, "Connect Account", submit_code, bg=ACCENT, hover=ACCENT_HOVER, bold=True)
        btn_connect.pack(side="right")
        make_btn(btn_row, "Cancel", dlg.destroy).pack(side="right", padx=(0, 8))
        entry.bind("<Return>", lambda _e: submit_code())

    make_btn(h_right, "Refresh All", lambda: refresh_all(sync_first=True)).pack(side="left", padx=(0, 8))
    make_btn(h_right, "Buy CUs", lambda: webbrowser.open("https://colab.research.google.com/signup")).pack(side="left")

    make_btn(actions, "+ Add Account", open_add_account_dialog, bg=ACCENT, hover=ACCENT_HOVER, bold=True).pack(side="left")
    make_btn(actions, "Switch to Selected", do_switch).pack(side="left", padx=(8, 0))
    btn_stop_vms = make_btn(actions, "Stop Active VMs", do_stop_vms, bg="#3b1d20", fg="#fca5a5", hover="#58262b")
    btn_stop_vms.pack(side="left", padx=(8, 0))
    make_btn(actions, "Remove Selected", do_remove).pack(side="right")

    tree.bind("<Double-1>", do_switch)

    render_ui()
    root.after(150, lambda: refresh_all(sync_first=True))
    root.mainloop()
    return 0


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    if not argv or argv[0] == "gui":
        return launch_gui()

    cmd = argv[0].lower()
    if cmd == "list":
        sync_current_cli_login()
        idx = _load_index()
        active = idx.get("active")
        accounts = sorted(idx.get("accounts", {}).keys())
        if not accounts:
            print("No saved Colab accounts. Run `python colab_accounts.py` to add one.")
            return 0
        for em in accounts:
            tok = _get_account_token_json(em)
            mark = "*" if em == active else " "
            if not tok:
                print(f"{mark} {em:38s}  [missing keyring token]")
                continue
            try:
                info = _query_account_live_status(tok)
                print(
                    f"{mark} {em:38s}  {info['balance']:7.2f} CU  "
                    f"({info['rate']:.2f}/hr, {info['assignments_count']} VM)"
                )
            except Exception as e:
                print(f"{mark} {em:38s}  [error: {e}]")
        return 0

    if cmd == "active":
        em = sync_current_cli_login()
        if not em:
            print("Not signed in.")
            return 1
        tok = _get_active_token_json()
        assert tok is not None
        info = _query_account_live_status(tok)
        print(f"Active account : {info['email']}")
        print(f"Compute units  : {info['balance']:.2f} CU")
        print(f"Usage rate     : {info['rate']:.2f}/hr")
        print(f"Active VMs     : {info['assignments_count']}")
        return 0

    if cmd == "switch":
        if len(argv) < 2:
            raise SystemExit("Usage: python colab_accounts.py switch <email>")
        em = switch_active_account(argv[1])
        print(f"Switched active Colab CLI account to: {em}")
        return 0

    if cmd == "stop-vms":
        em = argv[1] if len(argv) >= 2 else sync_current_cli_login()
        if not em:
            raise SystemExit("No active account.")
        n = stop_all_vms_for_account(em)
        print(f"Stopped {n} active VM(s) on {em}.")
        return 0

    raise SystemExit(f"Unknown command: {cmd}. Expected: gui | list | active | switch <email> | stop-vms [email]")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
