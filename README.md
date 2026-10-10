# agent-restore

Reopen closed GNOME Terminal windows of Claude Code and Codex sessions: same
windows, same tabs in the same order, each conversation resumed in its folder.

A small background watcher reads the window/tab layout from GNOME Terminal's
accessibility tree and recognizes each tab's session from its title (Claude and
Codex set it to the session name). When a window or tab closes, or the machine
reboots, its agent tabs become restorable. Nothing pops up; you restore when you
want.

## Install

```sh
./agent_restore.py install     # links ~/.local/bin/agent-restore, starts agent-restore.service
```

Needs GNOME Terminal on Ubuntu/GNOME and the system Python's `gi` + AT-SPI
bindings (preinstalled there). Re-run `install` if you move this folder.

## Use

```sh
agent-restore                  # pick closed windows to reopen (ids, or a = all)
agent-restore list             # live windows with matched sessions, plus closed windows
agent-restore restore 3 5      # or: restore all;  add --print to just show the commands
agent-restore forget 3         # or: forget all
agent-restore uninstall        # stop and remove the service (keeps state)
```

In `list`, `*` marks the active tab and `?` marks a session matched by a guess.
Matching uses the tab title. A guess happens when the title doesn't name the
session: a Claude session that has no title yet ("Claude Code"), or a Codex
thread without a name, whose tab shows only the folder name.

Only Claude/Codex tabs are restored; plain shell tabs are skipped. A tab whose
agent you exited normally (`/exit`, Ctrl-C) isn't restorable. Closed windows are
kept for 14 days, and dropped as soon as their sessions are running again.

State lives in `~/.local/state/agent-restore/state.json`; watcher logs go to
`journalctl --user -u agent-restore`.
