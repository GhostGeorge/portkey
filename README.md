# Portkey

A small Windows desktop app for launching SSH sessions and transferring files to your VPS servers, without needing to remember SSH commands.

## Features

- Save a list of servers (name, host, user, port, private key) and connect with a double-click — opens SSH in Windows Terminal for you
- Dual-pane SFTP file browser: drag-and-drop between local and remote, multi-select batch upload/download, rename, delete, new folder
- Live green/red status dot per server showing whether it's currently reachable
- Search/filter across servers and files
- Everything runs locally — your server list and keys never leave your machine

## Getting started

Grab `Portkey.zip` from the latest release, unzip it, and run `Portkey.exe` — no install needed, and it can live anywhere (Desktop, a folder, wherever).

Requirements: Windows 10/11, the OpenSSH client (`ssh.exe`, usually preinstalled), and Windows Terminal (`wt.exe`, preinstalled on Windows 11).

First run: click the gear icon → "+ New Server" → fill in the host details → "Save Server". Double-click a server to connect, or use the ⇅ icon to transfer files.

## Building from source

```
pip install -r requirements.txt
python portkey.pyw                                    # run in dev mode
python -m PyInstaller Portkey.spec --noconfirm         # build Portkey.exe
```

See [AGENTS.md](AGENTS.md) for architecture notes and conventions if you're contributing.
