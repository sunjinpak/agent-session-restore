# agent-session-restore

Reopen your **Claude Code** and **Codex CLI** sessions after a macOS restart, each in
the same Terminal window, at the same position, resumed where you left off.

macOS brings Terminal windows back after a restart, but they come back as empty
shells. If you run several agent sessions side by side, or you control the Mac
remotely and cannot click through prompts, that means rebuilding your workspace by
hand. This tool does it for you and tells you whether it worked.

## What it does

1. **Snapshot** every minute: for each Terminal window, its bounds and the Claude Code
   or Codex session running in it (session id, working folder, and safe launch flags
   such as `--model` or `--dangerously-skip-permissions`).
2. **Restore** after login: closes the empty windows Terminal reopened, then opens one
   window per saved session with `claude --resume <id>` or `codex resume <id>` from the
   session's own folder, at the saved position, back to front so stacking order holds.
   Background sessions of the `claude agents` view are continued with
   `claude --bg --resume <id>` (same session id), and the agents window is reopened.
3. **Unstick prompts** so a remote Mac does not sit waiting: answers Claude's
   "Do you trust this folder?" prompt (asked again on every launch in some folders,
   such as your home folder) and Codex's working-directory prompt.
4. **Verify** 30 seconds later: checks every session is running at its position,
   relaunches any that are missing once, and sends a report
   (macOS notification by default, or any command you configure, e.g. Telegram).

Folders on network drives that mount late after login are waited for (5 minutes by
default) instead of failing.

## Install

```sh
git clone https://github.com/sunjinpak/agent-session-restore.git
cd agent-session-restore
./install.sh
```

A Terminal window flashes once to start the background process. Check it with:

```sh
python3 agent_session_restore.py status
```

Requirements: macOS, Terminal.app, Python 3.9+ (`/usr/bin/python3` from the Command
Line Tools is fine). Keep the cloned folder where it is; the launch agent points to it.

### Why no permission prompts

Controlling Terminal from a background job normally triggers a macOS Automation
(TCC) prompt, which nobody can click on a headless or remote Mac. Here, launchd only
runs `open launcher.command`, and the daemon then runs **inside Terminal**, so it
talks to Terminal with Terminal's own rights. No prompt appears.

For the restore to run unattended you also need the Mac to log in by itself after a
restart (Users & Groups > Automatically log in, which requires FileVault off).

## Configure

`~/.agent-session-restore/config.json`:

| key | default | meaning |
|---|---|---|
| `name` | host name | label used in reports |
| `interval` | `60` | seconds between snapshots |
| `auto_trust` | `true` | answer Claude's folder-trust prompt for restored sessions |
| `notify_cmd` | `null` | command list; the report is appended as the last argument |
| `claude_cmd` / `codex_cmd` | `claude` / `codex` | executables to launch |
| `claude_extra_args` / `codex_extra_args` | `[]` | extra flags added on restore |
| `folder_wait` | `300` | seconds to wait for a missing working folder |

Telegram reports: see `examples/telegram-notify.sh`.

`auto_trust` only touches windows the tool itself just reopened, for sessions you
already ran in that folder. Turn it off if you want to answer that prompt yourself.

## Commands

```
agent_session_restore.py status     show snapshot, this boot's record, daemon state
agent_session_restore.py snapshot   take a snapshot now
agent_session_restore.py restore    reopen windows from the snapshot now, then verify
agent_session_restore.py verify     check sessions, fix prompts, relaunch missing
```

Logs: `~/.agent-session-restore/daemon.log`.

## Limits

- Terminal.app only (not iTerm2, Ghostty, tmux). Only the first tab of each window.
- Plain shell windows are restored as a new shell in the home folder, not their old
  folder or history.
- Restore types the resume command into a new window. Avoid typing into a window that
  is being restored during the first seconds after login.

## Uninstall

```sh
./uninstall.sh           # keeps ~/.agent-session-restore
./uninstall.sh --purge   # removes it too
```

## License

MIT
