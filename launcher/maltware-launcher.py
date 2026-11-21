#!/usr/bin/env python3
# maltware launcher — see README.md for the license status.
"""maltware launcher for Sober (Roblox on Linux).

A small, dependency-light launcher for Sober, the community-built Linux
client that runs the official Roblox Android APK on Linux via Flatpak.

What it does
------------
* Launches Sober (``flatpak run org.vinegarhq.Sober``).
* Installs/removes itself into the user's Applications menu (one entry).
* Auto-checks this repository for updates when the launcher opens, with a
  button to check on demand and to apply available updates (git fetch +
  fast-forward pull).
* Stays easy to extend: new launch actions are added through a plain JSON
  config file -- no rebuild required.
* A menu bar and an in-window console: the *Liner* menu (or F5) sweeps an
  animated line down the window, lighting up each console line it crosses.
  It is pure Tkinter drawing inside this window -- it never touches the
  Sober or Roblox process.

What it does NOT do
-------------------
It does not inject, hook or modify anything inside Sober or Roblox. There
is deliberately no in-game/in-instance GUI here: placing an ImGUI inside
the Sober/Roblox process requires injecting code into that process -- the
architecture of Roblox script executors, cheat software for an online
game. That is out of scope. See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

VERSION = "1.0.0"
DESKTOP_FILE = "maltware-launcher.desktop"
DESKTOP_SOBER_FILE = "maltware-sober.desktop"
ICON_NAME = "maltware.svg"
ICON_DEST = Path.home() / ".local/share/icons/hicolor/scalable/apps" / ICON_NAME
ICON_SRC = Path(__file__).resolve().parent / ICON_NAME
CONFIG_DIR = Path.home() / ".config" / "maltware"
CONFIG_FILE = CONFIG_DIR / "install.json"
USER_ACTIONS_FILE = CONFIG_DIR / "actions.json"
DESKTOP_DIR = Path.home() / ".local/share/applications"

REPO_OVERRIDE = os.environ.get("MALTWARE_REPO")  # escape hatch (testing)
CLI_ENV = "MALTWARE_CLI"

FALLBACK_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect x="2" y="2" width="60" height="60" rx="12" fill="#1e1e2e"/>
  <rect x="6" y="6" width="52" height="8" rx="4" fill="#89b4fa"/>
  <rect x="6" y="20" width="24" height="38" rx="4" fill="#313244"/>
  <rect x="34" y="20" width="24" height="18" rx="4" fill="#313244"/>
  <rect x="34" y="42" width="24" height="16" rx="4" fill="#313244"/>
</svg>"""

DEFAULT_ACTIONS = [
    {
        "id": "sober",
        "label": "Launch Sober",
        "command": ["flatpak", "run", "org.vinegarhq.Sober"],
        "description": "Launch the Sober Flatpak (Roblox on Linux).",
        "is_default": True,
    }
]

# ---------------------------------------------------------------------------
# utilities
# ---------------------------------------------------------------------------

def die(msg, code=1):
    print(f"maltware: {msg}", file=sys.stderr)
    sys.exit(code)


def run(cmd, timeout=60, cwd=None):
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
        return (
            proc.returncode == 0,
            proc.stdout.decode(errors="replace"),
            proc.stderr.decode(errors="replace"),
        )
    except FileNotFoundError as exc:
        return False, "", str(exc)
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return False, "", str(exc)


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# repository / git helpers
# ---------------------------------------------------------------------------

def find_repo():
    """Locate the git checkout that contains this launcher."""
    resolved = Path(__file__).resolve()
    for directory in [resolved.parent, *resolved.parents]:
        if (directory / ".git").exists():
            return directory.resolve()
    cfg = load_json(CONFIG_FILE)
    if cfg and cfg.get("repo"):
        cand = Path(cfg["repo"]).resolve()
        if (cand / ".git").exists():
            return cand
    return None


def current_branch(repo):
    ok, out, _ = run(["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"], timeout=15)
    return out.strip() if ok and out.strip() else None


def head_sha(repo):
    ok, out, _ = run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], timeout=15)
    return out.strip() if ok else None


def remote_sha(repo, branch):
    ok, out, _ = run(
        ["git", "-C", str(repo), "rev-parse", "--short", f"origin/{branch}"], timeout=15
    )
    return out.strip() if ok and out.strip() else None


def fetch(repo, branch):
    return run(["git", "-C", str(repo), "fetch", "origin", branch], timeout=45)


def behind_count(repo, branch):
    ok, out, _ = run(
        ["git", "-C", str(repo), "rev-list", "--count", f"HEAD..origin/{branch}"], timeout=15
    )
    if not ok:
        return None
    try:
        return int(out.strip() or "0")
    except ValueError:
        return None


def is_tracked_clean(repo):
    """True when there are no uncommitted changes to tracked files."""
    ok, _, _ = run(["git", "-C", str(repo), "diff", "--quiet", "HEAD"], timeout=15)
    if not ok:
        return False
    ok, _, _ = run(["git", "-C", str(repo), "diff", "--cached", "--quiet", "HEAD"], timeout=15)
    return ok


def pull(repo, branch):
    return run(["git", "-C", str(repo), "pull", "--ff-only", "origin", branch], timeout=60)


# ---------------------------------------------------------------------------
# update engine
# ---------------------------------------------------------------------------

def check_updates(repo=None):
    repo = repo or find_repo()
    if repo is None:
        return {
            "status": "not-git",
            "reason": "not a git checkout (run build.sh --install to configure this launcher)",
            "repo": None,
        }
    branch = current_branch(repo)
    if not branch:
        return {"status": "error", "reason": "could not determine current branch", "repo": str(repo)}

    head = head_sha(repo)

    fetch_ok, _, fetch_err = fetch(repo, branch)
    remote = remote_sha(repo, branch)
    n = behind_count(repo, branch)
    if n is None:
        n = 0

    if not fetch_ok:
        reason = "could not reach origin (offline?); showing local state"
        if not remote:
            reason = "no origin/" + branch + " yet (offline, or branch not pushed)"
        return {
            "status": "offline",
            "reason": reason,
            "repo": str(repo),
            "branch": branch,
            "head": head,
            "remote": remote,
            "behind": n,
        }

    if remote is None:
        return {
            "status": "error",
            "reason": "no origin/" + branch + " found (branch not pushed yet?)",
            "repo": str(repo),
            "branch": branch,
            "head": head,
        }

    if n > 0:
        return {
            "status": "behind",
            "reason": f"{n} commit(s) behind origin/{branch}",
            "repo": str(repo),
            "branch": branch,
            "head": head,
            "remote": remote,
            "behind": n,
        }
    return {
        "status": "uptodate",
        "reason": "up to date",
        "repo": str(repo),
        "branch": branch,
        "head": head,
        "remote": remote,
        "behind": 0,
    }


def apply_update(repo=None):
    """Fetch and fast-forward the repo. Returns a status dict."""
    repo = repo or find_repo()
    if repo is None:
        return {"status": "not-git", "reason": "not a git checkout"}
    branch = current_branch(repo)
    if not branch:
        return {"status": "error", "reason": "could not determine current branch"}

    if not is_tracked_clean(repo):
        return {
            "status": "refused",
            "reason": "working tree has uncommitted changes — commit or stash first",
            "repo": str(repo),
            "branch": branch,
        }

    ok, _, err = fetch(repo, branch)
    if not ok:
        return {
            "status": "offline",
            "reason": "could not reach origin: " + (err.strip() or "unknown error"),
        }

    n = behind_count(repo, branch)
    if n is None:
        return {"status": "error", "reason": "could not compare against origin/" + branch}
    if n == 0:
        return {"status": "uptodate", "reason": "already up to date", "branch": branch}

    ok, out, err = pull(repo, branch)
    if not ok:
        return {
            "status": "error",
            "reason": "git pull failed: " + (err.strip() or out.strip() or "unknown error"),
        }
    return {
        "status": "updated",
        "reason": f"fast-forwarded {n} commit(s) — updated to {head_sha(repo)}",
        "repo": str(repo),
        "branch": branch,
        "head": head_sha(repo),
    }


# ---------------------------------------------------------------------------
# actions ("easy integration")
# ---------------------------------------------------------------------------

def load_actions():
    """User override (~/.config/maltware/actions.json) wins; then the repo's
    launcher/actions.json; then the embedded defaults."""
    for path in (USER_ACTIONS_FILE, Path(__file__).resolve().parent / "actions.json"):
        data = load_json(path)
        if data:
            actions = data.get("actions") if isinstance(data, dict) else data
            if isinstance(actions, list) and actions:
                return actions, str(path)
    return DEFAULT_ACTIONS, "<builtin>"


def validate_actions(actions):
    valid = []
    for a in actions:
        if not isinstance(a, dict):
            continue
        cmd = a.get("command")
        if not isinstance(cmd, list) or not cmd or not cmd[0]:
            continue
        if not isinstance(a.get("id"), str) or not a["id"]:
            continue
        valid.append(
            {
                "id": a["id"],
                "label": a.get("label", a["id"]),
                "description": a.get("description", ""),
                "command": [str(x) for x in cmd],
                "is_default": bool(a.get("is_default")),
            }
        )
    return valid or list(DEFAULT_ACTIONS)


def launch_action(action):
    # expand a leading ~ in any argument (no shell involved)
    cmd = [os.path.expanduser(x) if isinstance(x, str) else x for x in action["command"]]
    proc = subprocess.Popen(
        [str(x) for x in cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return proc.pid


# ---------------------------------------------------------------------------
# applications-menu integration
# ---------------------------------------------------------------------------

WRAPPER_TEMPLATE = """#!/bin/sh
# maltware launcher wrapper.
# Execs the launcher from the checkout recorded in the config file; falls
# back to the standalone copy installed by build.sh.
CONFIG=\"$HOME/.config/maltware/install.json\"
REPO=\"\"
if [ -f \"$CONFIG\" ]; then
  REPO=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get(\"repo\",\"\"))' \"$CONFIG\" 2>/dev/null)
fi
if [ -f \"$REPO/launcher/maltware-launcher.py\" ]; then
  exec python3 \"$REPO/launcher/maltware-launcher.py\" \"$@\"
fi
if [ -f \"$HOME/.local/share/maltware/maltware-launcher.py\" ]; then
  exec python3 \"$HOME/.local/share/maltware/maltware-launcher.py\" \"$@\"
fi
echo \"maltware: launcher checkout not found. Re-run build.sh --install.\" >&2
exit 1
"""


def wrapper_path():
    return Path.home() / ".local/bin/maltware-launcher"


def applications_installed():
    return wrapper_path().exists() and (DESKTOP_DIR / DESKTOP_FILE).exists()


def _desktop_entry(name, exec_line, icon_abs, description, categories="Utility;Game;"):
    return f"""[Desktop Entry]
Type=Application
Version=1.0
Name={name}
Comment={description}
Exec={exec_line}
Icon={icon_abs}
Terminal=false
Categories={categories}
StartupNotify=false

"""


def install_menu_entries(entries=True):
    """Write the wrapper, desktop entry(ies) and icon. Idempotent."""
    wrapper = wrapper_path()
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(WRAPPER_TEMPLATE, encoding="utf-8")
    wrapper.chmod(0o755)

    # record the repo this wrapper should run (first install does this)
    repo = find_repo()
    if not CONFIG_FILE.exists():
        write_json(CONFIG_FILE, {"repo": str(repo) if repo else None, "installedAt": time.time(), "branch": current_branch(repo) if repo else None})

    ICON_DEST.parent.mkdir(parents=True, exist_ok=True)
    if ICON_SRC.exists():
        ICON_DEST.write_bytes(ICON_SRC.read_bytes())
    else:
        ICON_DEST.write_text(FALLBACK_ICON_SVG, encoding="utf-8")

    launcher_desktop = None
    if entries:
        DESKTOP_DIR.mkdir(parents=True, exist_ok=True)
        launcher_desktop = DESKTOP_DIR / DESKTOP_FILE
        launcher_desktop.write_text(
            _desktop_entry(
                "Maltware Launcher",
                str(wrapper),
                str(ICON_DEST),
                "Launch Sober (Roblox on Linux) and manage maltware",
            ),
            encoding="utf-8",
        )
    # remove the stale Sober shortcut from older installs (no longer used)
    stale = DESKTOP_DIR / DESKTOP_SOBER_FILE
    if stale.exists():
        stale.unlink()
    return True, launcher_desktop, None


def remove_menu_entries():
    removed = []
    for name in (DESKTOP_FILE, DESKTOP_SOBER_FILE):
        p = DESKTOP_DIR / name
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed


# ---------------------------------------------------------------------------
# GUI / CLI
# ---------------------------------------------------------------------------

CLI_ENV_MSG = f"set {CLI_ENV}=1 in the environment to force CLI mode"


def force_cli():
    if os.environ.get(CLI_ENV) == "1":
        return True
    if "--cli" in sys.argv:
        return True
    if sys.platform.startswith("linux"):
        return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return False


def _sober_hint():
    ok, _, _ = run(["flatpak", "info", "org.vinegarhq.Sober"], timeout=15)
    if ok:
        return "installed"
    return "not found — install from Flathub: flatpak install flathub org.vinegarhq.Sober"


def print_update_result(result):
    print(f"status: {result.get('status')}")
    print(f"detail: {result.get('reason')}")
    if result.get("behind"):
        print(f"behind: {result['behind']} commit(s) on {result.get('branch')}")
    if result.get("repo"):
        print(f"repo:   {result.get('repo')}")


def cmd_status(_args):
    repo = find_repo()
    print(f"maltware {VERSION}")
    print(f"repository : {repo if repo else '(not found)'}")
    if repo:
        print(f"branch     : {current_branch(repo)}")
        print(f"HEAD       : {head_sha(repo)}")
    actions, src = load_actions()
    print(f"actions    : {len(validate_actions(actions))} (from {src})")
    print(f"applications entry: {'installed' if applications_installed() else 'not installed'}")
    print(f"Sober      : {_sober_hint()}")
    return 0


def cmd_check(_args):
    result = check_updates()
    print_update_result(result)
    return 0


def cmd_update(_args):
    print("checking this repository for changes…")
    result = apply_update()
    print_update_result(result)
    if result.get("status") == "updated":
        print("launcher updated — it runs from the repository, so this applies immediately")
    return 0 if result.get("status") in ("updated", "uptodate") else 1


def cmd_applications(args):
    if args.remove:
        removed = remove_menu_entries()
        names = ", ".join(p.name for p in removed) or "(none)"
        print(f"removed applications entries: {names}")
        print("the wrapper script was left in ~/.local/bin/maltware-launcher")
        return 0
    entries = not args.no_entries
    _, launcher_desktop, _ = install_menu_entries(entries=entries)
    if entries:
        print(f"installed desktop entry: {launcher_desktop}")
        print("Sober opens only from the Launch Sober button in the launcher")
        print("if your menu does not refresh immediately, log out/in or press refresh in your menu")
    else:
        print("installed wrapper + icon (desktop entries skipped)")
    return 0


def cmd_launch(args):
    actions = validate_actions(load_actions()[0])
    by_id = {a["id"]: a for a in actions}
    default = next((a for a in actions if a.get("is_default")), actions[0])
    target = by_id.get(args.action)
    if target is None and args.action == "default":
        target = default
    if target is None:
        known = ", ".join(by_id)
        die(f"unknown action '{args.action}'. known actions: {known}")
    try:
        pid = launch_action(target)
    except FileNotFoundError:
        return _launch_failed(target)
    except Exception as exc:  # noqa: BLE001
        print(f"maltware: {exc}", file=sys.stderr)
        return 1
    print(f"launched '{target['label']}' (pid {pid})")
    return 0


def _launch_failed(target):
    cmd0 = target["command"][0]
    print(f"maltware: could not launch '{target['label']}' — '{cmd0}' was not found.", file=sys.stderr)
    if cmd0 == "flatpak":
        print("Install flatpak first, then Sober from Flathub (see README.md).", file=sys.stderr)
    return 1


def cmd_list_actions(_args):
    actions, src = load_actions()
    print(f"actions (from {src}):")
    for a in validate_actions(actions):
        print(f"  {a['id']:<16} {a['label']}")
        print(f"      command: {' '.join(a['command'])}")
        if a.get("description"):
            print(f"      {a['description']}")
    return 0


# ---------------------------------------------------------------------------
# GUI (Tkinter) — window, menu bar, console, and the liner sweep
# ---------------------------------------------------------------------------

PALETTE = {
    "base": "#1e1e2e",
    "mantle": "#181825",
    "crust": "#11111b",
    "surface": "#313244",
    "overlay": "#45475a",
    "text": "#cdd6f4",
    "subtext": "#a6adc8",
    "blue": "#89b4fa",
    "lavender": "#b4befe",
    "green": "#a6e3a1",
    "mauve": "#cba6f7",
    "peach": "#fab387",
    "red": "#f38ba8",
    "yellow": "#f9e2af",
    "teal": "#94e2d5",
}

# the liner: a thin animated line that sweeps down the window
LINER_TICK_MS = 16                                   # ~60 fps
LINER_THICKNESS = 2                                  # px, the line itself
LINER_GLOW = 14                                      # px, the soft bar behind it
LINER_SPEEDS = (("slow", 2400), ("normal", 1400), ("fast", 750))
LINER_ACCENTS = {                                    # menu name -> palette key
    "blue": "blue",
    "green": "green",
    "mauve": "mauve",
    "peach": "peach",
}
CONSOLE_MAX_LINES = 400                              # trim the console past this
CONSOLE_KEEP_LINES = 200


def ease_in_out(t):
    """Smoothstep: 0 -> 1 with a soft start and a soft stop."""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return t * t * (3.0 - 2.0 * t)


def liner_offset(progress, height, thickness=LINER_THICKNESS):
    """Pixel y for the liner at eased *progress* (0..1) across *height* px."""
    span = max(0, int(height) - int(thickness))
    return int(round(ease_in_out(progress) * span))


def blend_colour(a, b, t):
    """Mix two #rrggbb colours: t=0 gives *a*, t=1 gives *b*."""
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{int(round(x + (y - x) * t)):02x}" for x, y in zip(ca, cb))


def run_gui():
    import tkinter as tk
    from tkinter import messagebox

    mono = "DejaVu Sans Mono"

    class LauncherApp(tk.Tk):
        """The launcher window.

        Layout: header, body (launch actions + setup/updates), and a console
        strip along the bottom. The liner is a thin frame — plus a soft glow
        frame behind it — placed over the whole window and animated down it;
        every line it crosses in the console lights up as it passes.
        """

        def __init__(self):
            super().__init__()
            self.title("maltware — Sober launcher")
            self.configure(bg=PALETTE["base"])
            self.geometry("920x740")
            self.minsize(780, 620)
            self._update_running = False

            actions, actions_src = load_actions()
            self.actions = validate_actions(actions)
            self.actions_src = actions_src

            # liner / console state
            self.var_speed = tk.StringVar(master=self, value="normal")
            self.var_accent = tk.StringVar(master=self, value="blue")
            self.var_loop = tk.BooleanVar(master=self, value=False)
            self.var_console = tk.BooleanVar(master=self, value=True)
            self._liner_job = None
            self._liner_active = False
            self._liner_progress = 0.0
            self._liner_sweeps = 0
            self._log_pos = 0

            self._build_ui()
            self._build_menu()
            self._bind_keys()
            self._boot_console()
            self._poll_log()
            self._schedule_update_check(auto=True)
            self.after(250, self.liner_sweep)   # intro sweep

        # -- layout ------------------------------------------------------
        def _build_ui(self):
            tk.Label(
                self, text="maltware", font=("Segoe UI", 28, "bold"),
                bg=PALETTE["base"], fg=PALETTE["blue"],
            ).pack(anchor="w", padx=24, pady=(18, 0))
            tk.Label(
                self, text="Sober launcher — Roblox on Linux",
                font=("Segoe UI", 11), bg=PALETTE["base"], fg=PALETTE["text"],
            ).pack(anchor="w", padx=24)
            tk.Frame(self, bg=PALETTE["blue"], height=2).pack(fill="x", padx=24, pady=(10, 12))

            self._build_console()          # packed first: it owns the bottom

            self.body = tk.Frame(self, bg=PALETTE["base"])
            self.body.pack(fill="both", expand=True, padx=24, pady=(0, 12))
            body = self.body

            # left column: launch actions
            left = tk.Frame(body, bg=PALETTE["base"])
            left.pack(side="left", fill="both", expand=True, padx=(0, 12))
            tk.Label(left, text="LAUNCH", font=("Segoe UI", 10, "bold"),
                     bg=PALETTE["base"], fg=PALETTE["subtext"]).pack(anchor="w", pady=(0, 6))

            for a in self.actions:
                btn = tk.Button(
                    left, text=a["label"], font=("Segoe UI", 13),
                    bg=PALETTE["blue"] if a.get("is_default") else PALETTE["surface"],
                    fg=PALETTE["base"] if a.get("is_default") else PALETTE["text"],
                    relief="flat", cursor="hand2", padx=18, pady=16,
                    activebackground=PALETTE["lavender"], activeforeground=PALETTE["base"],
                    command=lambda x=a: self._do_launch(x),
                )
                btn.pack(fill="x", pady=4)
                if a.get("is_default"):
                    btn.configure(text="▶  " + a["label"])

            tk.Label(
                left,
                text=("Runs Sober without injecting into or modifying it.\n"
                      "No in-game / no in-instance GUI is part of this launcher.\n"
                      "The liner only draws inside this window."),
                font=("Segoe UI", 8), bg=PALETTE["base"], fg=PALETTE["subtext"], justify="left",
            ).pack(anchor="w", pady=(14, 0))

            # right column: setup / updates / menu
            right = tk.Frame(body, bg=PALETTE["mantle"], highlightbackground=PALETTE["surface"],
                             highlightthickness=1, width=330)
            right.pack(side="right", fill="both", expand=False)
            right.pack_propagate(False)

            tk.Label(right, text="SETUP & UPDATES", font=("Segoe UI", 10, "bold"),
                     bg=PALETTE["mantle"], fg=PALETTE["subtext"]).pack(anchor="w", padx=16, pady=(14, 6))

            self.lbl_sober = tk.Label(right, text="Sober: checking…", bg=PALETTE["mantle"],
                                      fg=PALETTE["teal"], font=("Segoe UI", 9), anchor="w")
            self.lbl_sober.pack(fill="x", padx=16, pady=2)
            self.lbl_repo = tk.Label(right, text="Repo: …", bg=PALETTE["mantle"], fg=PALETTE["text"],
                                     font=("Segoe UI", 9), anchor="w")
            self.lbl_repo.pack(fill="x", padx=16, pady=2)
            self.lbl_update = tk.Label(right, text="Updates: checking…", bg=PALETTE["mantle"],
                                       fg=PALETTE["yellow"], font=("Segoe UI", 10), anchor="w")
            self.lbl_update.pack(fill="x", padx=16, pady=6)

            tk.Frame(right, bg=PALETTE["surface"], height=1).pack(fill="x", padx=16, pady=6)

            tk.Label(right, text="APPLICATION MENU", font=("Segoe UI", 9, "bold"),
                     bg=PALETTE["mantle"], fg=PALETTE["subtext"]).pack(anchor="w", padx=16, pady=(8, 2))

            self.btn_apps = tk.Button(
                right,
                text="Remove from Applications" if applications_installed() else "Add to Applications",
                font=("Segoe UI", 10), bg=PALETTE["surface"], fg=PALETTE["text"], relief="flat",
                cursor="hand2", padx=8, pady=6, activebackground=PALETTE["overlay"],
                command=self._toggle_applications,
            )
            self.btn_apps.pack(fill="x", padx=16, pady=(2, 6))

            tk.Label(right, text="CHECKS", font=("Segoe UI", 9, "bold"),
                     bg=PALETTE["mantle"], fg=PALETTE["subtext"]).pack(anchor="w", padx=16, pady=(4, 4))

            tk.Button(
                right, text="Check for updates", font=("Segoe UI", 10),
                bg=PALETTE["blue"], fg=PALETTE["base"], relief="flat", cursor="hand2", padx=8, pady=8,
                activebackground=PALETTE["lavender"],
                command=lambda: self._schedule_update_check(auto=False),
            ).pack(fill="x", padx=16, pady=(0, 6))

            self.btn_apply = tk.Button(
                right, text="Update now", font=("Segoe UI", 10, "bold"),
                bg=PALETTE["green"], fg=PALETTE["base"], relief="flat", cursor="hand2",
                padx=8, pady=8, state="disabled", command=self._apply_update,
            )
            self.btn_apply.pack(fill="x", padx=16, pady=(0, 12))

            tk.Label(
                right,
                text="Updates pull from this repository (git) and apply\nthe launcher immediately, without reinstalling.",
                font=("Segoe UI", 8), bg=PALETTE["mantle"], fg=PALETTE["subtext"], justify="left",
            ).pack(anchor="w", padx=16, pady=(0, 14))

            # the liner: placed over everything, never packed
            accent = PALETTE[LINER_ACCENTS[self.var_accent.get()]]
            self.liner_glow = tk.Frame(self, bg=blend_colour(accent, PALETTE["base"], 0.78), height=LINER_GLOW)
            self.liner_core = tk.Frame(self, bg=accent, height=LINER_THICKNESS)
            for f in (self.liner_glow, self.liner_core):
                f.place(x=0, y=-40, relwidth=1, height=f.cget("height"))

        def _build_console(self):
            self.console_frame = tk.Frame(self, bg=PALETTE["mantle"],
                                          highlightbackground=PALETTE["surface"], highlightthickness=1)
            self.console_frame.pack(side="bottom", fill="x", padx=24, pady=(0, 14))

            head = tk.Frame(self.console_frame, bg=PALETTE["mantle"])
            head.pack(fill="x")
            tk.Label(head, text="CONSOLE", font=("Segoe UI", 9, "bold"),
                     bg=PALETTE["mantle"], fg=PALETTE["subtext"]).pack(side="left", padx=12, pady=(6, 4))
            self.lbl_liner = tk.Label(head, text="liner · idle", font=(mono, 9),
                                      bg=PALETTE["mantle"], fg=PALETTE["blue"])
            self.lbl_liner.pack(side="right", padx=12)

            self.console = tk.Text(
                self.console_frame, height=9, bg=PALETTE["crust"], fg=PALETTE["text"],
                font=(mono, 9), relief="flat", highlightthickness=0, padx=10, pady=6,
                wrap="none", state="disabled", cursor="arrow", takefocus=0,
                selectbackground=PALETTE["overlay"], spacing1=1,
            )
            self.console.pack(fill="both", expand=True)
            self.console.tag_configure("liner", background=PALETTE["blue"], foreground=PALETTE["crust"])
            self.console.tag_configure("liner_trail", background=PALETTE["surface"])
            self.console.tag_configure("event", foreground=PALETTE["green"])
            self.console.tag_configure("warn", foreground=PALETTE["yellow"])
            self.console.tag_configure("dim", foreground=PALETTE["subtext"])

        def _build_menu(self):
            menubar = tk.Menu(self)

            default = next((a for a in self.actions if a.get("is_default")), self.actions[0])

            file_menu = tk.Menu(menubar, tearoff=0)
            file_menu.add_command(label=f"Launch {default['label']}", accelerator="Ctrl+L",
                                  command=lambda: self._do_launch(default))
            file_menu.add_separator()
            file_menu.add_command(
                label="Remove from Applications" if applications_installed() else "Add to Applications",
                command=self._toggle_applications,
            )
            file_menu.add_separator()
            file_menu.add_command(label="Quit", accelerator="Ctrl+Q", command=self.destroy)
            menubar.add_cascade(label="File", menu=file_menu)

            view_menu = tk.Menu(menubar, tearoff=0)
            view_menu.add_checkbutton(label="Console", variable=self.var_console,
                                      command=self._toggle_console)
            menubar.add_cascade(label="View", menu=view_menu)

            liner_menu = tk.Menu(menubar, tearoff=0)
            liner_menu.add_command(label="Sweep once", accelerator="F5", command=self.liner_sweep)
            liner_menu.add_checkbutton(label="Continuous sweep", variable=self.var_loop,
                                       command=self._liner_toggle_loop)
            liner_menu.add_command(label="Stop", accelerator="Esc",
                                   command=lambda: self._liner_stop("stopped"))
            liner_menu.add_separator()

            speed_menu = tk.Menu(liner_menu, tearoff=0)
            for name, ms in LINER_SPEEDS:
                speed_menu.add_radiobutton(label=f"{name}  ({ms} ms)", variable=self.var_speed,
                                           value=name, command=self._liner_status)
            liner_menu.add_cascade(label="Speed", menu=speed_menu)

            accent_menu = tk.Menu(liner_menu, tearoff=0)
            for name in LINER_ACCENTS:
                accent_menu.add_radiobutton(label=name, variable=self.var_accent,
                                            value=name, command=self._liner_recolour)
            liner_menu.add_cascade(label="Accent", menu=accent_menu)

            menubar.add_cascade(label="Liner", menu=liner_menu)

            help_menu = tk.Menu(menubar, tearoff=0)
            help_menu.add_command(label="About maltware", command=self._about)
            menubar.add_cascade(label="Help", menu=help_menu)

            self.config(menu=menubar)

        def _bind_keys(self):
            self.bind("<F5>", lambda _e: self.liner_sweep())
            self.bind("<Escape>", lambda _e: self._liner_stop("stopped"))
            self.bind("<Control-l>", lambda _e: self._launch_default())
            self.bind("<Control-q>", lambda _e: self.destroy())

        # -- console -----------------------------------------------------
        def console_write(self, text, tags=(), stamp=True):
            """Append one line to the in-window console (never to the log file)."""
            try:
                self.console.configure(state="normal")
                if stamp:
                    self.console.insert("end", time.strftime("%H:%M:%S") + "  ", ("dim",))
                self.console.insert("end", text + "\n", tags)
                if int(self.console.index("end-1c").split(".")[0]) > CONSOLE_MAX_LINES:
                    self.console.delete("1.0", f"{CONSOLE_MAX_LINES - CONSOLE_KEEP_LINES}.0")
                self.console.see("end")
                self.console.configure(state="disabled")
            except tk.TclError:
                pass

        def _boot_console(self):
            self.console_write(f"maltware {VERSION} — launcher console", ("event",))
            self.console_write(f"actions  {len(self.actions)} loaded from {self.actions_src}")
            self.console_write("liner    Liner ▸ Sweep once, or press F5", ("dim",))

        def _poll_log(self):
            """Stream new lines from ~/.local/share/maltware/launcher.log."""
            path = _launcher_log_path()
            try:
                if path.exists():
                    size = path.stat().st_size
                    if size < self._log_pos:      # truncated / rotated
                        self._log_pos = 0
                    if size > self._log_pos:
                        with open(path, "r", encoding="utf-8", errors="replace") as fh:
                            fh.seek(self._log_pos)
                            chunk = fh.read()
                            self._log_pos = fh.tell()
                        for line in chunk.splitlines():
                            if line.strip():
                                self.console_write(line, ("dim",), stamp=False)
            except OSError:
                pass
            if self.winfo_exists():
                self.after(2000, self._poll_log)

        def _toggle_console(self):
            if self.var_console.get():
                self.console_frame.pack(side="bottom", fill="x", padx=24, pady=(0, 14),
                                        before=self.body)
                self.console_write("console shown", ("dim",))
            else:
                self.console_frame.pack_forget()

        # -- the liner ---------------------------------------------------
        def liner_sweep(self, _event=None):
            """Start one sweep from the top of the window."""
            self._liner_cancel()
            self.update_idletasks()
            self._liner_active = True
            self._liner_progress = 0.0
            self._liner_sweeps += 1
            self.liner_glow.place(x=0, y=-LINER_GLOW, relwidth=1, height=LINER_GLOW)
            self.liner_core.place(x=0, y=-LINER_THICKNESS, relwidth=1, height=LINER_THICKNESS)
            self.liner_glow.lift()
            self.liner_core.lift()
            self._liner_status(active=True)
            self._liner_job = self.after(LINER_TICK_MS, self._liner_tick)

        def _liner_tick(self):
            self._liner_job = None
            if not self._liner_active:      # stopped between frames
                return
            duration = dict(LINER_SPEEDS).get(self.var_speed.get(), 1400) / 1000.0
            height = max(self.winfo_height(), LINER_THICKNESS + 1)
            y = liner_offset(self._liner_progress, height)
            self.liner_core.place_configure(y=y, height=LINER_THICKNESS)
            self.liner_glow.place_configure(y=max(0, y - (LINER_GLOW - LINER_THICKNESS)),
                                            height=LINER_GLOW)
            self._liner_highlight(y)

            self._liner_progress += (LINER_TICK_MS / 1000.0) / duration
            if self._liner_progress >= 1.0:
                if self.var_loop.get():
                    self._liner_progress = 0.0
                else:
                    self._liner_stop()
                    return
            self._liner_job = self.after(LINER_TICK_MS, self._liner_tick)

        def _liner_highlight(self, y):
            """Light the console line the liner is currently crossing."""
            try:
                self.console.tag_remove("liner", "1.0", "end")
                self.console.tag_remove("liner_trail", "1.0", "end")
                cy = y - self.console.winfo_rooty()
                if 0 <= cy < self.console.winfo_height():
                    idx = self.console.index(f"@{6},{cy}")
                    self.console.tag_add("liner", f"{idx} linestart", f"{idx} lineend +1c")
                    line = int(idx.split(".")[0])
                    if line > 1:
                        self.console.tag_add("liner_trail", f"{line - 1}.0", f"{line - 1}.0 lineend +1c")
            except tk.TclError:
                pass

        def _liner_cancel(self):
            if self._liner_job is not None:
                try:
                    self.after_cancel(self._liner_job)
                except tk.TclError:
                    pass
                self._liner_job = None

        def _liner_stop(self, reason=None):
            self._liner_cancel()
            self._liner_active = False
            self.liner_glow.place_forget()
            self.liner_core.place_forget()
            try:
                self.console.tag_remove("liner", "1.0", "end")
                self.console.tag_remove("liner_trail", "1.0", "end")
            except tk.TclError:
                pass
            self.var_loop.set(False)
            self._liner_status()
            if reason:
                self.console_write(f"liner    {reason}", ("dim",))

        def _liner_toggle_loop(self):
            if self.var_loop.get():
                if self._liner_job is None:
                    self.liner_sweep()
                else:
                    self._liner_status(active=True)
            else:
                self._liner_stop()

        def _liner_status(self, active=False):
            label = "looping" if active and self.var_loop.get() else ("sweeping" if active else "idle")
            self.lbl_liner.config(
                text=f"liner · {self.var_speed.get()} · {self.var_accent.get()} · {label}"
            )

        def _liner_recolour(self):
            accent = PALETTE[LINER_ACCENTS[self.var_accent.get()]]
            self.liner_core.configure(bg=accent)
            self.liner_glow.configure(bg=blend_colour(accent, PALETTE["base"], 0.78))
            self.console.tag_configure("liner", background=accent, foreground=PALETTE["crust"])
            self.lbl_liner.configure(fg=accent)
            self._liner_status(active=self._liner_job is not None)

        def _about(self):
            messagebox.showinfo(
                "about maltware",
                f"maltware {VERSION}\n\nA Sober launcher for Linux.\n\n"
                "The liner sweeps a line down this window and lights up the console "
                "line it crosses. It draws inside this window only — it never touches "
                "the Sober or Roblox process.",
            )

        # -- launcher logic ----------------------------------------------
        def _launch_default(self):
            default = next((a for a in self.actions if a.get("is_default")), self.actions[0])
            self._do_launch(default)

        def _do_launch(self, action):
            try:
                pid = launch_action(action)
                self.lbl_update.config(text=f"Launched \u201c{action['label']}\u201d", fg=PALETTE["text"])
                self.console_write(f"launched {action['id']} (pid {pid})", ("event",))
            except Exception as exc:  # noqa: BLE001
                self.console_write(f"launch failed: {exc}", ("warn",))
                messagebox.showerror("Launch failed", str(exc))

        def _toggle_applications(self):
            if applications_installed():
                remove_menu_entries()
                self.btn_apps.configure(text="Add to Applications")
                self.lbl_update.config(text="Removed Applications entry", fg=PALETTE["text"])
                self.console_write("applications entry removed", ("dim",))
            else:
                install_menu_entries(entries=True)
                self.btn_apps.configure(text="Remove from Applications")
                self.lbl_update.config(text="Installed Applications entry", fg=PALETTE["text"])
                self.console_write("applications entry installed", ("dim",))

        def _schedule_update_check(self, auto=False):
            self.lbl_update.config(text="Checking…")

            def worker():
                result = check_updates()
                sober = _sober_hint()
                self.after(0, lambda: self._show_update_result(result, sober))

            threading.Thread(target=worker, daemon=True).start()

        def _show_update_result(self, result, sober=None):
            status = result.get("status")
            repo = result.get("repo")
            branch = result.get("branch", "")
            short = "/".join(repo.split("/")[-2:]) if repo else "(not found)"
            self.lbl_repo.config(text=f"Repo: {short or '(not found)'}\nbranch: {branch}")
            self.lbl_sober.config(text=f"Sober: {sober if sober is not None else _sober_hint()}")

            if status == "uptodate":
                self.lbl_update.config(text="Up to date ✓", fg=PALETTE["green"])
                self.btn_apply.configure(state="disabled")
                self.console_write(f"updates  up to date on origin/{branch}", ("dim",))
            elif status == "behind":
                self.lbl_update.config(
                    text=f"Update available — {result.get('behind')} behind origin/{branch}",
                    fg=PALETTE["yellow"],
                )
                self.btn_apply.configure(state="normal")
                self.console_write(f"updates  {result.get('behind')} commit(s) behind origin/{branch}",
                                   ("warn",))
            elif status == "offline":
                self.lbl_update.config(text="Offline / repo unreachable", fg=PALETTE["red"])
                self.btn_apply.configure(state="disabled")
                self.console_write("updates  offline / repo unreachable", ("warn",))
            elif status == "not-git":
                self.lbl_update.config(text="Not a git checkout — updates unavailable", fg=PALETTE["red"])
                self.btn_apply.configure(state="disabled")
                self.console_write("updates  not a git checkout", ("warn",))
            else:
                self.lbl_update.config(text=str(result.get("reason", "Unknown status"))[:95],
                                       fg=PALETTE["red"])
                self.btn_apply.configure(state="disabled")
                self.console_write(f"updates  {result.get('reason', 'unknown')}", ("warn",))

        def _apply_update(self):
            if self._update_running:
                return
            self._update_running = True
            self.btn_apply.configure(state="disabled")
            self.lbl_update.config(text="Updating…")
            self.console_write("updates  applying…", ("dim",))

            def worker():
                result = apply_update()
                self.after(0, lambda: self._finish_update(result))

            threading.Thread(target=worker, daemon=True).start()

        def _finish_update(self, result):
            self._update_running = False
            status = result.get("status")
            if status == "updated":
                self.lbl_update.config(text="Updated ✓", fg=PALETTE["green"])
                self.console_write("updates  updated", ("event",))
                messagebox.showinfo("Update complete", result.get("reason", "Updated"))
            elif status == "refused":
                self.lbl_update.config(text="Update blocked", fg=PALETTE["red"])
                self.console_write(f"updates  blocked: {result.get('reason', '')}", ("warn",))
                messagebox.showwarning("Update blocked", result.get("reason", "Update blocked"))
            elif status == "uptodate":
                self.lbl_update.config(text="Up to date ✓", fg=PALETTE["green"])
                self.console_write("updates  already up to date", ("dim",))
            else:
                self.lbl_update.config(text=str(result.get("reason", "Update failed"))[:95],
                                       fg=PALETTE["red"])
                self.console_write(f"updates  failed: {result.get('reason', '')}", ("warn",))
                messagebox.showerror("Update failed", result.get("reason", "Update failed"))
            self.btn_apply.configure(state="disabled")
            self._schedule_update_check(auto=True)  # refresh button state

    app = LauncherApp()
    app.mainloop()


# ---------------------------------------------------------------------------
# fallback: keep the launcher visible even when the GUI cannot start
# ---------------------------------------------------------------------------

TERMINAL_COMMANDS = [
    ("xdg-terminal-exec", lambda cmd: ["xdg-terminal-exec", "sh", "-c", cmd]),
    ("x-terminal-emulator", lambda cmd: ["x-terminal-emulator", "-e", "sh", "-c", cmd]),
    ("gnome-terminal", lambda cmd: ["gnome-terminal", "--", "sh", "-c", cmd]),
    ("konsole", lambda cmd: ["konsole", "-e", "sh", "-c", cmd]),
    ("xfce4-terminal", lambda cmd: ["xfce4-terminal", "-x", "sh", "-c", cmd]),
    ("mate-terminal", lambda cmd: ["mate-terminal", "--", "sh", "-c", cmd]),
    ("lxterminal", lambda cmd: ["lxterminal", "-e", "sh", "-c", cmd]),
    ("kitty", lambda cmd: ["kitty", "sh", "-c", cmd]),
    ("alacritty", lambda cmd: ["alacritty", "-e", "sh", "-c", cmd]),
    ("xterm", lambda cmd: ["xterm", "-e", "sh", "-c", cmd]),
    ("urxvt", lambda cmd: ["urxvt", "-e", "sh", "-c", cmd]),
]


def _has_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _launcher_log_path():
    return Path.home() / ".local/share/maltware/launcher.log"


def _log(msg):
    try:
        path = _launcher_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        return path
    except Exception:
        return None


def _open_terminal(cmdline):
    for binary, build in TERMINAL_COMMANDS:
        if not shutil.which(binary):
            continue
        try:
            subprocess.Popen(build(cmdline), stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except Exception:
            continue
    return False


def _open_dialog(text):
    try:
        if shutil.which("zenity"):
            subprocess.Popen(["zenity", "--error", "--title=maltware", "--text=" + text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        if shutil.which("yad"):
            subprocess.Popen(["yad", "--error", "--title=maltware", "--text=" + text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        if shutil.which("kdialog"):
            subprocess.Popen(["kdialog", "--error", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
        if shutil.which("xmessage"):
            subprocess.Popen(["xmessage", text],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            return True
    except Exception:
        pass
    return False


def _fallback_visible(reason):
    """Ensure *something* visible happens when the GUI cannot start."""
    log_path = _log(reason)
    print(f"maltware: {reason}", file=sys.stderr)
    hint = "run `maltware-launcher status` from a terminal for details."
    if log_path:
        hint += f" (details in {log_path})"
    print(f"maltware: {hint}", file=sys.stderr)
    if _has_display():
        # try a terminal with the CLI first (it stays visible and lets you
        # see the error live), then a plain dialog as a last resort
        if _open_terminal("maltware-launcher --cli status; echo; echo 'Press Enter to close...'; read -r"):
            return 0
        _open_dialog(reason + "\n\n" + hint)
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(
        description="maltware — Sober launcher for Roblox on Linux. Launches Sober, "
                    "installs an Applications menu entry, checks this repository for "
                    "updates, and stays easy to extend. It does NOT inject into or "
                    "modify Sober/Roblox.",
        epilog=f"CLI tips: force CLI mode with --cli (or {CLI_ENV}=1).",
    )
    p.add_argument("--version", action="version", version=f"maltware {VERSION}")
    p.add_argument("--cli", action="store_true", help="force CLI mode even with a display")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="print a status summary")
    sub.add_parser("check", help="check this repository for updates")
    sub.add_parser("update", help="apply pending updates from this repository")

    p_launch = sub.add_parser("launch", help="launch a known action")
    p_launch.add_argument("action", nargs="?", default="default",
                          help="action id (default, sober, ...) — see list-actions")

    p_apps = sub.add_parser("applications", help="install/remove desktop entries")
    p_apps.add_argument("--remove", action="store_true", help="remove desktop entries")
    p_apps.add_argument("--no-entries", action="store_true",
                        help="install wrapper + icon, but no desktop entries")

    sub.add_parser("list-actions", help="list known launch actions")
    return p.parse_args()


def main():
    args = parse_args()
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "check":
        return cmd_check(args)
    if args.cmd == "update":
        return cmd_update(args)
    if args.cmd == "launch":
        return cmd_launch(args)
    if args.cmd == "applications":
        return cmd_applications(args)
    if args.cmd == "list-actions":
        return cmd_list_actions(args)

    if force_cli():
        print("maltware launcher is in CLI mode.")
        print("try:  maltware-launcher --help")
        print("       maltware-launcher status")
        print("       maltware-launcher check")
        print("       maltware-launcher launch sober")
        print("       maltware-launcher applications")
        return 0

    try:
        run_gui()
        return 0
    except ModuleNotFoundError as exc:
        return _fallback_visible(
            f"GUI unavailable ({exc}). Install tkinter (python3-tk on "
            "Debian/Ubuntu, or your distro's tkinter package) for the GUI."
        )
    except Exception as exc:  # noqa: BLE001
        return _fallback_visible(f"GUI failed to start: {exc}")


if __name__ == "__main__":
    sys.exit(main())
