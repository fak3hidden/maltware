# maltware

A small, dependency-light **Linux launcher for Sober** — the community-built
client that runs the official Roblox Android APK on Linux (via Flatpak,
`org.vinegarhq.Sober`, from Flathub).

![launcher](launcher/maltware.svg)

## What it is

* **A launcher** — launches Sober and shows its status at a glance.
* **An "Add to Applications" option** — installs a desktop entry (and icon)
  into your applications menu, and an optional Sober shortcut.
* **An auto-updater** — checks this repository for changes automatically when
  the launcher opens (background check), plus a *Check for updates* button.
  Clicking *Update now* fetches the repository and fast-forwards the checkout.
  Because the installed wrapper execs the launcher straight out of the
  checkout, updates apply immediately — no reinstall step.
* **Build script** — `build.sh` builds and sets up the launcher (validates the
  checkout, byte-compiles the launcher, installs the wrapper, desktop
  entries, icon, fallback copy, and writes the install config).
* **Easy integration of actions** — new launch actions are added through a
  plain JSON config file (`launcher/actions.json` or a user override at
  `~/.config/maltware/actions.json`). No rebuild required.

## What it is NOT

**It does not inject into, hook, or modify Sober or Roblox — and it does not
add an in-game / in-instance ImGUI.** Those are the same thing as a Roblox
script executor, and that is deliberately out of scope here:

* Sober is a closed-source runtime. Embedding an ImGUI "in the instance"
  means placing a payload inside the Sober/Roblox process (LD_PRELOAD,
  ptrace, memory hooks) — that is exactly what Roblox executors/injectors do.
* Such a GUI in the instance is cheat software for an online game. It
  violates [Roblox's Terms of Service](https://create.roblox.com/docs/terms)
  and enables unfair advantage in games, which this project does not do.
* This launcher launches and manages Sober — nothing else. It does not modify
  the game, its memory, or its UI at any point.

If you want an in-game UI, the legitimate path is **Roblox Studio** — build
your own experience with Roblox's official UI/Service APIs (ScreenGui,
Frames, UIListLayout, etc.). Sober has no plugin API and Roblox has no
official in-client injection hooks.

## Requirements

* Linux (x86-64), Python 3.8+, git
* Sober, installed via Flatpak:
  `flatpak install flathub org.vinegarhq.Sober`
* For the GUI: Tkinter (`python3-tk` on Debian/Ubuntu, `python3` with
  `tkinter` on Fedora/Arch). On a headless machine the launcher falls back to
  CLI mode automatically.

## Install / setup

```sh
git clone <this repo> && cd maltware
./build.sh                 # build and set up the launcher
./build.sh --check          # show status
```

This installs:

| Path | What |
| --- | --- |
| `~/.local/bin/maltware-launcher` | wrapper that execs the launcher from the checkout |
| `~/.local/share/applications/maltware-launcher.desktop` | Applications menu entry |
| `~/.local/share/applications/maltware-sober.desktop` | optional Sober shortcut |
| `~/.local/share/icons/hicolor/scalable/apps/maltware.svg` | icon |
| `~/.config/maltware/install.json` | install config (repo pointer) |
| `~/.local/share/maltware/` | fallback copy of the launcher (used only if the checkout moves) |

Run it with `maltware-launcher` or open the Applications menu (it may take a
screen refresh / log out — log in cycle to appear).

## CLI usage

```sh
maltware-launcher                 # open the GUI (or CLI hints when headless)
maltware-launcher status          # repo, branch, HEAD, actions, Sober status
maltware-launcher check           # check this repository for updates
maltware-launcher update          # fast-forward the repository (if behind)
maltware-launcher launch sober    # launch an action (default: sober)
maltware-launcher launch <id>     # launch any action from actions.json
maltware-launcher list-actions    # list configured actions
maltware-launcher applications     # install wrapper + desktop entries + icon
maltware-launcher applications --remove   # remove the desktop entries
maltware-launcher --version
```

## How auto-updates work

On every launch the launcher spawns a background check that runs
`git fetch origin <branch>` against the configured repository and compares
`origin/<branch>` with your HEAD. The UI shows the result and, when you are
behind, enables **Update now**.

Applying an update performs, in order:

1. Refuses if the working tree has uncommitted changes to tracked files
   (commit or stash first — nothing is silently overwritten or discarded).
2. `git fetch origin <branch>`.
3. `git pull --ff-only` — a fast-forward only; never force-push, never
   merge history, never touches anything outside the checkout.

Since the installed wrapper execs the launcher from the checkout, the
updated files take effect immediately.

> The updater never pushes anything and never modifies remote state. It only
> fast-forwards the local checkout through your own remote.

## Easy integration of actions

New launch actions are added in one JSON file — no code changes, no rebuild.

The launcher looks at, in priority order:

1. `~/.config/maltware/actions.json` — user override (kept on updates)
2. `launcher/actions.json` — the repo's default
3. built-in defaults

Example user override (`~/.config/maltware/actions.json`):

```json
{
  "actions": [
    {
      "id": "sober",
      "label": "Launch Sober",
      "command": ["flatpak", "run", "org.vinegarhq.Sober"],
      "description": "Launch Roblox via Sober",
      "is_default": true
    },
    {
      "id": "data-dir",
      "label": "Open Sober data dir",
      "command": ["xdg-open", "~/.var/app/org.vinegarhq.Sober"],
      "description": "Browse Sober's Flatpak data directory",
      "is_default": false
    }
  ]
}
```

Each action is a button in the GUI and a `launch <id>` entry in the CLI.
Only actions listed in the file run — there is no arbitrary code execution
from untrusted sources.

> Note: this is an *actions* system for launching things. It is not a
> "feature module" system for game scripts, hooks, or overlays — that would
> be the executor path this project refuses to build.

## Uninstall

```sh
./build.sh --uninstall
```

This removes the wrapper, desktop entries, icon, and fallback copy. It leaves
`~/.config/maltware/install.json` in place (remove it with
`rm -rf ~/.config/maltware`).

## Project layout

```
maltware/
├── build.sh                      # build / install / uninstall / check
├── launcher/
│   ├── maltware-launcher.py      # the launcher (GUI + CLI + updater)
│   ├── actions.json              # default launch actions
│   └── maltware.svg              # icon
└── README.md                     # this file
```

## Troubleshooting

* **No GUI / Tkinter missing** — use the CLI subcommands; install
  `python3-tk` (Debian/Ubuntu) or your distro's `tkinter` package.
* **Menu entry missing** — log out/in or press the refresh button in your
  applications menu; the entry lives in `~/.local/share/applications/`.
* **Update says "offline"** — the launcher could not reach the origin. Check
  your network and the remote URL (`git remote -v`); updates will apply once
  the repository is reachable.
* **Update refuses** — you have uncommitted changes to tracked files; commit
  or stash them.
* **Sober launches but shows no game** — Sober does not include Roblox
  itself; first run downloads the official APK (see the Sober docs).

## Disclaimer

Sober is a community-built, unofficial Linux client. Roblox Corporation
does not endorse it, and using it is at your own discretion. This project is
a launcher around Sober, not Roblox, not Sober, not an extension of either.

*License: to be decided by the repo owner.*
