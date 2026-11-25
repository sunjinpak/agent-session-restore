#!/usr/bin/env python3
"""Reopen your Claude Code and Codex CLI sessions in the same macOS Terminal
windows after a restart.

daemon    runs inside Terminal (started by launcher.command). After login it
          restores the last snapshot, verifies it, then snapshots every minute.
          Running inside Terminal means no extra Automation (TCC) permission.
keeper    run by launchd at login and every 5 minutes: if the daemon is not
          running, open launcher.command in Terminal to start it.
snapshot  record windows now (one shot).
restore   reopen windows from the last snapshot now.
verify    check the restored sessions, unstick prompts, relaunch missing ones.
status    print the snapshot and the last run.

Background sessions of the `claude agents` view are saved too and continued
with `claude --bg --resume <id>`; the agents window itself is reopened.
mark      mark the current boot as already restored (used by install.sh).
"""
import fcntl, glob, json, os, pty, select, shutil, socket, subprocess, sys, time

HOME = os.path.expanduser("~")
DIR = os.environ.get("ASR_DIR", os.path.join(HOME, ".agent-session-restore"))
STATE = os.path.join(DIR, "snapshot.json")
RUN = os.path.join(DIR, "lastrun.json")
LOCKFILE = os.path.join(DIR, "daemon.lock")
CONFIG = os.path.join(DIR, "config.json")
LAUNCHER = os.path.join(DIR, "launcher.command")  # written by install.sh

DEFAULTS = {
    "name": socket.gethostname().split(".")[0],
    "interval": 60,            # seconds between snapshots
    "auto_trust": True,        # answer Claude's "trust this folder" prompt for restored sessions
    "notify_cmd": None,        # e.g. ["/path/to/notify.sh"]; the message is appended as last arg
    "claude_cmd": "claude",
    "codex_cmd": "codex",
    "claude_extra_args": [],
    "codex_extra_args": [],
    "folder_wait": 300,        # seconds to wait for a missing cwd (network drive still mounting)
}

# Flags replayed from the original command line. Anything else (prompts,
# --print, session ids) is dropped so a restore never re-sends input.
CLAUDE_BOOL = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions",
               "--verbose", "--chrome", "--no-chrome", "--ide"}
CLAUDE_VAL = {"--permission-mode", "--model", "--add-dir", "--settings", "--mcp-config",
              "--agent", "--fallback-model", "--effort"}
CODEX_BOOL = {"--dangerously-bypass-approvals-and-sandbox", "--full-auto", "--search", "--yolo"}
CODEX_VAL = {"-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval", "-c", "--config",
             "-p", "--profile", "--enable", "--disable"}


def cfg():
    c = dict(DEFAULTS)
    try:
        c.update(json.load(open(CONFIG)))
    except FileNotFoundError:
        pass
    return c


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


# ---------- Terminal / process helpers ----------

def osa(script):
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr.strip())
    return r.stdout.strip()


def terminal_running():
    out = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True).stdout
    return any(l.endswith("Terminal.app/Contents/MacOS/Terminal") for l in out.splitlines())


def terminal_windows():
    """[(window_id, [l,t,r,b], tty, custom_title)] front to back."""
    out = osa('''tell application "Terminal"
set o to ""
set sep to character id 9
repeat with w in windows
try
set b to bounds of w
set t to tab 1 of w
set ct to ""
try
set ct to custom title of t
end try
set o to o & (id of w) & sep & (item 1 of b) & "," & (item 2 of b) & "," & (item 3 of b) & "," & (item 4 of b) & sep & (tty of t) & sep & ct & linefeed
end try
end repeat
return o
end tell''')
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 3:
            rows.append((int(p[0]), [int(x) for x in p[1].split(",")], p[2], p[3] if len(p) > 3 else ""))
    return rows


def tab_contents(tty):
    return osa(f'tell application "Terminal" to get contents of tab 1 of (first window whose tty of tab 1 is "{tty}")')


def type_into(tty, expr):
    """expr is an AppleScript string expression; do script adds Enter."""
    osa(f'tell application "Terminal" to do script {expr} in tab 1 of (first window whose tty of tab 1 is "{tty}")')


def procs():
    out = subprocess.run(["ps", "-axo", "pid=,tty=,lstart=,command="], capture_output=True, text=True).stdout
    res = []
    for line in out.splitlines():
        p = line.split(None, 7)
        if len(p) < 8:
            continue
        try:
            start = time.mktime(time.strptime(" ".join(p[2:7]), "%a %b %d %H:%M:%S %Y"))
        except ValueError:
            continue
        res.append((int(p[0]), "/dev/" + p[1], start, p[7]))
    return res


def proc_cwd(pid):
    out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return HOME


def boot_time():
    out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True).stdout
    return int(out.split("sec = ")[1].split(",")[0])


def keep_flags(args, bools, vals):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        key = a.split("=", 1)[0]
        if a in bools:
            out.append(a)
        elif key in vals and "=" in a:
            out.append(a)
        elif a in vals and i + 1 < len(args):
            out += [a, args[i + 1]]
            i += 1
        i += 1
    return out


def which_agent(cmd):
    toks = cmd.split()
    names = [os.path.basename(t) for t in toks[:2]]
    if names[0] == "claude":
        if [t for t in toks[1:] if not t.startswith("-")][:1] == ["agents"]:
            return "agents", [t for t in toks[1:] if t != "agents"]
        return "claude", toks[1:]
    if "codex" in names or "codex-darwin" in cmd or "codex-aarch64" in cmd:
        i = names.index("codex") + 1 if "codex" in names else 1
        rest = toks[i:]
        if rest[:1] in (["app-server"], ["exec"], ["mcp"], ["mcp-server"]):
            return None, []
        return "codex", rest
    return None, []


def claude_session(pid, start, args):
    """pid file written by Claude Code > --resume arg > newest transcript since start."""
    try:
        s = json.load(open(os.path.join(HOME, ".claude/sessions", f"{pid}.json")))
        return s["sessionId"], s.get("cwd") or proc_cwd(pid)
    except Exception:
        pass
    cwd = proc_cwd(pid)
    for flag in ("--resume", "-r"):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return os.path.basename(args[i + 1]).removesuffix(".jsonl"), cwd
    pdir = os.path.join(HOME, ".claude/projects", "".join(c if c.isalnum() else "-" for c in cwd))
    fs = [f for f in glob.glob(pdir + "/*.jsonl") if os.path.getmtime(f) >= start]
    if fs:
        return os.path.basename(max(fs, key=os.path.getmtime))[:-6], cwd
    return None, cwd


def codex_session(pid, start, args):
    root = os.path.join(HOME, ".codex/sessions")
    if "resume" in args:
        i = args.index("resume")
        rest = [a for a in args[i + 1:] if not a.startswith("-")]
        if rest:
            sid = rest[0]
            for f in glob.glob(f"{root}/*/*/*/rollout-*{sid}.jsonl"):
                try:
                    return sid, json.loads(open(f, errors="ignore").readline())["payload"]["cwd"]
                except Exception:
                    pass
            return sid, proc_cwd(pid)
    cwd = proc_cwd(pid)
    best = None
    for f in glob.glob(f"{root}/*/*/*/rollout-*.jsonl"):
        if os.path.getmtime(f) < start:
            continue
        try:
            meta = json.loads(open(f, errors="ignore").readline()).get("payload", {})
        except Exception:
            continue
        if meta.get("cwd") == cwd and (best is None or os.path.getmtime(f) > best[0]):
            best = (os.path.getmtime(f), meta.get("id"))
    return (best[1] if best else None), cwd


# ---------- run record (one per boot) ----------

def load_run():
    try:
        r = json.load(open(RUN))
        if r.get("boot") == boot_time():
            return r
    except Exception:
        pass
    return {"boot": boot_time()}


def save_run(**kw):
    r = load_run()
    r.update(kw)
    json.dump(r, open(RUN, "w"), ensure_ascii=False, indent=1)


# ---------- snapshot ----------

def claude_bin(c):
    return shutil.which(c["claude_cmd"]) or c["claude_cmd"]


def bg_sessions(c):
    """Live background sessions of the `claude agents` view (not done/failed)."""
    try:
        out = subprocess.run([claude_bin(c), "agents", "--json"], capture_output=True, text=True,
                             timeout=30, cwd=HOME).stdout
        return [{"id": x["sessionId"], "cwd": x.get("cwd", HOME), "name": x.get("name", "")}
                for x in json.loads(out)
                if x.get("kind") == "background" and x.get("state") in ("working", "blocked", "idle", "waiting")]
    except Exception:
        return []


def resume_bg(e, c):
    """`claude --bg --resume <id>` continues the same background session (extra
    flags would start a copy instead). Claude asks "trust this folder?" on a pty
    in some folders (home folder: every launch); answer Down+Enter if auto_trust."""
    cwd = e.get("cwd", HOME)
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd if os.path.isdir(cwd) else HOME)
        b = claude_bin(c)
        os.execv(b, [b, "--bg", "--resume", e["id"]])
    buf, sent, end = b"", False, time.time() + 90
    while time.time() < end:
        if select.select([fd], [], [], 1)[0]:
            try:
                d = os.read(fd, 4096)
            except OSError:
                break
            if not d:
                break
            buf += d
            if c["auto_trust"] and not sent and b"trust" in buf.lower():  # ANSI codes split the prompt text
                time.sleep(1.5)
                os.write(fd, b"\x1b[B")
                time.sleep(0.5)
                os.write(fd, b"\r")
                sent = True
        if os.waitpid(pid, os.WNOHANG)[0]:
            break
    return b"backgrounded" in buf


def snapshot():
    if not terminal_running():
        return
    ps = procs()
    entries = []
    for _, bounds, tty, title in terminal_windows():
        entry = {"bounds": bounds, "title": title, "kind": "shell"}
        for pid, ptty, start, cmd in ps:
            if ptty != tty:
                continue
            kind, args = which_agent(cmd)
            if kind == "agents":
                entry.update(kind=kind, id="agents", cwd=proc_cwd(pid), flags=keep_flags(args, CLAUDE_BOOL, CLAUDE_VAL))
                break
            if kind == "claude":
                sid, cwd = claude_session(pid, start, args)
                flags = keep_flags(args, CLAUDE_BOOL, CLAUDE_VAL)
            elif kind == "codex":
                sid, cwd = codex_session(pid, start, args)
                flags = keep_flags(args, CODEX_BOOL, CODEX_VAL)
            else:
                continue
            if sid:
                entry.update(kind=kind, id=sid, cwd=cwd, flags=flags)
                break
        entries.append(entry)
    bg = bg_sessions(cfg())
    if not bg and not any(e["kind"] != "shell" for e in entries):
        return  # keep the previous snapshot
    os.makedirs(DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump({"saved": time.strftime("%Y-%m-%d %H:%M:%S"), "windows": entries, "bg": bg}, open(tmp, "w"),
              ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


# ---------- restore ----------

def q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def asq(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launch_cmd(e, c):
    cwd = e.get("cwd", HOME)
    if e["kind"] == "claude":
        args = e.get("flags", []) + c["claude_extra_args"] + ["--resume", e["id"]]
        return f"cd {q(cwd)} && command {c['claude_cmd']} " + " ".join(q(a) for a in args)
    if e["kind"] == "agents":
        args = e.get("flags", []) + c["claude_extra_args"] + ["agents"]
        return f"cd {q(cwd)} && command {c['claude_cmd']} " + " ".join(q(a) for a in args)
    if e["kind"] == "codex":
        args = ["resume"] + e.get("flags", []) + c["codex_extra_args"] + [e["id"]]
        return f"cd {q(cwd)} && command {c['codex_cmd']} " + " ".join(q(a) for a in args)
    return "cd ~"


def open_window(e, c):
    l, t, r, b = e["bounds"]
    return osa(f'''tell application "Terminal"
set t to do script {asq(launch_cmd(e, c))}
delay 0.5
set w to window 1
set bounds of w to {{{l}, {t}, {r}, {b}}}
return tty of t
end tell''')


def accept_trust_prompts(ttys, wait=90):
    """Claude asks "Do you trust this folder?" again on launch in some folders
    (home folder: "Yes lasts for this session only"). Answer it for windows we
    just reopened: these are sessions the user already ran in that folder."""
    ttys = set(ttys)
    end = time.time() + wait
    while ttys and time.time() < end:
        time.sleep(3)
        for tty in list(ttys):
            try:
                c = tab_contents(tty)
            except Exception:
                ttys.discard(tty)
                continue
            if "trust this folder" in c and "Enter to confirm" in c:
                type_into(tty, '((character id 27) & "[B")')  # Down, then Enter
                ttys.discard(tty)
            elif "? for shortcuts" in c or "bypass permissions" in c:
                ttys.discard(tty)


def restore(exclude_tty=None):
    if not os.path.exists(STATE):
        return log("restore: no snapshot yet")
    c = cfg()
    snap = json.load(open(STATE))
    for _ in range(30):  # Terminal may still be launching
        if terminal_running():
            break
        time.sleep(2)
    time.sleep(8)  # let Terminal finish reopening its own windows
    busy = {t for _, t, _, cmd in procs() if not cmd.lstrip("-").startswith(("zsh", "login", "bash", "fish", "sh "))}
    for wid, _, tty, _ in terminal_windows():
        if tty != exclude_tty and tty not in busy:
            osa(f'tell application "Terminal" to close window id {wid} saving no')
    bg = snap.get("bg", [])
    back = [e["name"] or e["id"][:8] for e in bg if resume_bg(e, c)]
    if bg:
        log(f"restore: resumed {len(back)}/{len(bg)} background sessions: {', '.join(back)}")
    claude_ttys = []
    # open back to front so the original front window ends up on top;
    # windows whose folder is not there yet (network drive mounting) go last
    order = list(reversed(snap["windows"]))
    later = [e for e in order if not os.path.isdir(e.get("cwd", HOME))]
    for e in order:
        if e in later:
            continue
        tty = open_window(e, c)
        if e["kind"] in ("claude", "agents"):
            claude_ttys.append(tty)
        time.sleep(1)
    deadline = time.time() + c["folder_wait"]
    while later and time.time() < deadline:
        for e in [e for e in later if os.path.isdir(e["cwd"])]:
            tty = open_window(e, c)
            if e["kind"] in ("claude", "agents"):
                claude_ttys.append(tty)
            later.remove(e)
        if later:
            time.sleep(5)
    for e in later:
        log(f"restore: folder never appeared, skipped {e['cwd']}")
    if c["auto_trust"]:
        accept_trust_prompts(claude_ttys)
    save_run(restored=time.strftime("%Y-%m-%d %H:%M:%S"), snapshot=snap)
    log(f"restore: reopened {len(snap['windows'])} windows from snapshot {snap['saved']}")


# ---------- verify ----------

def find_session(e, ps):
    if e["kind"] == "agents":
        return next((tty for _, tty, _, cmd in ps if which_agent(cmd)[0] == "agents"), None)
    for pid, tty, _, cmd in ps:
        if e["id"] in cmd:
            return tty
    for pid, tty, _, cmd in ps:  # Claude may not carry the id in argv (e.g. after /clear)
        try:
            if json.load(open(os.path.join(HOME, ".claude/sessions", f"{pid}.json")))["sessionId"] == e["id"]:
                return tty
        except Exception:
            pass
    return None


def check(expected):
    ps = procs()
    wins = {tty: bounds for _, bounds, tty, _ in terminal_windows()}
    missing, moved, stuck = [], [], []
    for e in expected:
        label = f"{e['kind']} {(e.get('title') or e['id'][:8])[:30]} ({os.path.basename(e['cwd']) or '/'})"
        tty = find_session(e, ps)
        if not tty:
            missing.append((e, label))
            continue
        if tty in wins and any(abs(a - b) > 6 for a, b in zip(wins[tty], e["bounds"])):
            moved.append(label)
        try:
            text = tab_contents(tty)
        except Exception:
            text = ""
        if "Enter to confirm" in text and "trust this folder" in text:
            stuck.append((tty, "trust", label))
        elif "Working directory" in text and "resume" in text and "Enter" in text:
            stuck.append((tty, "codex-dir", label))
    return missing, moved, stuck


def notify(msg):
    c = cfg()
    if os.environ.get("ASR_NO_NOTIFY"):
        return
    try:
        if c["notify_cmd"]:
            subprocess.run(list(c["notify_cmd"]) + [msg], capture_output=True, timeout=30)
        else:
            subprocess.run(["osascript", "-e", f'display notification {asq(msg)} with title "agent-session-restore"'],
                           capture_output=True, timeout=30)
    except Exception as ex:
        log("notify failed:", ex)


def verify():
    run = load_run()
    snap = run.get("snapshot")
    if not snap:
        return log("verify: nothing restored this boot")
    c = cfg()
    expected = [e for e in snap["windows"] if e["kind"] != "shell"]
    fixed = []
    missing, moved, stuck = check(expected)
    for tty, kind, label in stuck:
        type_into(tty, '((character id 27) & "[B")' if kind == "trust" else '""')
        fixed.append(f"answered prompt: {label}")
    for e, label in missing:
        for _ in range(c["folder_wait"] // 5):
            if os.path.isdir(e.get("cwd", HOME)):
                break
            time.sleep(5)
        tty = open_window(e, c)
        if e["kind"] in ("claude", "agents") and c["auto_trust"]:
            accept_trust_prompts([tty], wait=60)
        fixed.append(f"relaunched: {label}")
    want_bg = snap.get("bg", [])
    alive = {x["id"] for x in bg_sessions(c)}
    for e in [e for e in want_bg if e["id"] not in alive]:
        if resume_bg(e, c):
            fixed.append(f"relaunched background: {e['name'] or e['id'][:8]}")
    if fixed:
        time.sleep(15)
        missing, moved, stuck = check(expected)
    alive = {x["id"] for x in bg_sessions(c)}
    bg_missing = [e["name"] or e["id"][:8] for e in want_bg if e["id"] not in alive]
    ok = len(expected) - len(missing) - len(stuck)
    good = not missing and not stuck and not bg_missing
    lines = [f"{'✅' if good else '⚠️'} {c['name']}: {ok}/{len(expected)} windows"
             + (f", {len(want_bg) - len(bg_missing)}/{len(want_bg)} background sessions" if want_bg else "")
             + " restored after restart"]
    lines += [f"- background session not running: {n}" for n in bg_missing]
    lines += [f"- fixed, {f}" for f in fixed]
    lines += [f"- missing: {label}" for _, label in missing]
    lines += [f"- stuck at a prompt: {label}" for _, _, label in stuck]
    lines += [f"- window moved: {label}" for label in moved]
    msg = "\n".join(lines)
    save_run(verified=time.strftime("%Y-%m-%d %H:%M:%S"), result=msg)
    log(msg)
    notify(msg)


# ---------- daemon / keeper ----------

def take_lock():
    os.makedirs(DIR, exist_ok=True)
    f = open(LOCKFILE, "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        return None
    f.seek(0); f.truncate(); f.write(str(os.getpid())); f.flush()
    return f


def close_window(tty):
    if tty and tty.startswith("/dev/"):
        try:
            osa(f'tell application "Terminal" to close (every window whose tty of tab 1 is "{tty}") saving no')
        except Exception:
            pass


def daemon(launcher_tty=None):
    lock = take_lock()
    if not lock:
        log("daemon: already running")
        return close_window(launcher_tty)
    try:
        os.setsid()  # survive the launcher window closing
    except OSError:
        pass
    log(f"daemon: started pid {os.getpid()}")
    run = load_run()
    if "restored" not in run and os.path.exists(STATE):
        try:
            restore(exclude_tty=launcher_tty)
            time.sleep(30)
            verify()
        except Exception as ex:
            log("daemon: restore failed:", ex)
            notify(f"⚠️ {cfg()['name']}: session restore failed: {ex}")
            save_run(restored="failed")
    elif "restored" not in run:
        save_run(restored="nothing to restore")
    close_window(launcher_tty)
    while True:
        try:
            snapshot()
        except Exception as ex:
            log("snapshot failed:", ex)
        time.sleep(cfg()["interval"])


def keeper():
    lock = take_lock()
    if not lock:
        return  # daemon alive
    lock.close()
    subprocess.run(["open", "-g", "-a", "Terminal", LAUNCHER])


def status():
    print(f"state dir: {DIR}")
    if os.path.exists(STATE):
        s = json.load(open(STATE))
        print(f"snapshot {s['saved']}:")
        for e in s["windows"]:
            print(f"  {e['kind']:6} {e.get('id', '')[:8]:8} {e.get('cwd', '')}  {e['bounds']}")
        for e in s.get("bg", []):
            print(f"  bg     {e['id'][:8]:8} {e['cwd']}  {e['name']}")
    else:
        print("no snapshot yet")
    print("this boot:", json.dumps({k: v for k, v in load_run().items() if k != "snapshot"}, ensure_ascii=False))
    print("daemon:", "not running" if take_lock() else "running")


if __name__ == "__main__":
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "daemon":
        daemon(a[1] if len(a) > 1 else None)
    elif cmd == "keeper":
        keeper()
    elif cmd == "snapshot":
        snapshot(); status()
    elif cmd == "restore":
        restore(); time.sleep(30); verify()
    elif cmd == "verify":
        verify()
    elif cmd == "status":
        status()
    elif cmd == "mark":
        save_run(restored="marked at install", verified="marked at install")
    else:
        sys.exit(__doc__)
