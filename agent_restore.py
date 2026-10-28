#!/usr/bin/python3
"""agent-restore: reopen closed GNOME Terminal windows of Claude Code / Codex
sessions, with the same tabs in the same order.

  agent-restore                     pick closed windows to restore
  agent-restore list                show live windows and closed windows
  agent-restore restore ID...|all   restore closed windows (--print to dry-run)
  agent-restore forget ID...|all    drop closed windows
  agent-restore watch               background tracker (run by the systemd unit)
  agent-restore install|uninstall   set up / remove the tracker service

The tracker reads the window/tab layout from GNOME Terminal's accessibility
tree (AT-SPI) and identifies each tab's agent session from its title, which
Claude and Codex keep set to the session's name.
"""
import argparse
import contextlib
import fcntl
import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse

HOME = os.path.expanduser('~')
CLAUDE_DIR = os.path.join(HOME, '.claude')
CODEX_DIR = os.path.join(HOME, '.codex')
STATE_DIR = os.path.join(os.environ.get('XDG_STATE_HOME') or os.path.join(HOME, '.local', 'state'),
                         'agent-restore')
STATE_FILE = os.path.join(STATE_DIR, 'state.json')
LOCK_FILE = os.path.join(STATE_DIR, 'state.lock')
UNIT_NAME = 'agent-restore.service'
UNIT_FILE = os.path.join(HOME, '.config', 'systemd', 'user', UNIT_NAME)
BIN_LINK = os.path.join(HOME, '.local', 'bin', 'agent-restore')

POLL_SECS = 3.0
KEEP_SECS = 3 * 86400
MERGE_SECS = 15         # tabs of one window that die a few polls apart land in one entry
CLAUDE_GRACE_SECS = 30  # a vanished tab whose Claude process lives on was moved, not closed
EXIT_GRACE_SECS = 30    # agents can exit before their terminal tabs disappear at shutdown

# Flags worth carrying over to the resumed session.
CLAUDE_VALUE_FLAGS = {'--model', '--permission-mode', '--add-dir'}
CLAUDE_BOOL_FLAGS = {'--dangerously-skip-permissions'}
CODEX_VALUE_FLAGS = {'-m', '--model', '-s', '--sandbox', '-a', '--ask-for-approval',
                     '-c', '--config', '-p', '--profile', '--add-dir'}
CODEX_BOOL_FLAGS = {'--dangerously-bypass-approvals-and-sandbox', '--search', '--no-alt-screen'}
# Codex subcommands that aren't an interactive session.
CODEX_NON_TUI = {'exec', 'e', 'review', 'login', 'logout', 'mcp', 'mcp-server', 'plugin',
                 'app-server', 'remote-control', 'completion', 'update', 'doctor', 'sandbox',
                 'debug', 'apply', 'a', 'queue', 'archive', 'delete', 'unarchive',
                 'migrate-rollouts', 'cloud', 'exec-server', 'features', 'agents', 'help'}

# Set by a running agent for its own child processes. GNOME Terminal hands the launching
# environment to new tabs, so restoring from inside an agent would otherwise leak them.
AGENT_ENV = {'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_CHILD_SESSION', 'CLAUDE_CODE_SESSION_ID',
             'CLAUDE_CODE_SESSION_ATTENDED', 'CLAUDE_CODE_MESSAGING_SOCKET', 'CLAUDE_CODE_MESSAGING_TOKEN',
             'CLAUDE_CODE_EXECPATH', 'CLAUDE_CODE_SSE_PORT', 'CLAUDE_PID', 'CLAUDE_EFFORT',
             'CODEX_SANDBOX', 'CODEX_SANDBOX_NETWORK_DISABLED', 'CODEX_THREAD_ID', 'CODEX_SESSION_ID'}

# Claude prefixes its title with a status glyph (✳ idle, ◐◑◒◓ busy, braille spinners...).
LEADING_GLYPHS = re.compile(r'^[^\w\'"(\[<{]+')


def log(msg):
    print(time.strftime('%H:%M:%S'), msg, file=sys.stderr, flush=True)


def short_path(p):
    return '~' + p[len(HOME):] if p and (p == HOME or p.startswith(HOME + '/')) else (p or '?')


def clean_title(title):
    return LEADING_GLYPHS.sub('', title or '').strip()


# --- /proc ------------------------------------------------------------------

def read_stat(pid):
    """(ppid, tty_nr, starttime in clock ticks) for a pid, or None if it's gone."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            s = f.read()
    except OSError:
        return None
    rest = s[s.rfind(')') + 2:].split()
    return int(rest[1]), int(rest[4]), int(rest[19])


def read_cmdline(pid):
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return [a.decode(errors='replace') for a in f.read().split(b'\0') if a]
    except OSError:
        return []


def read_cwd(pid):
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except OSError:
        return None


def boot_time():
    with open('/proc/stat') as f:
        for line in f:
            if line.startswith('btime '):
                return int(line.split()[1])
    return 0


def boot_id():
    with open('/proc/sys/kernel/random/boot_id') as f:
        return f.read().strip()


def process_running(comm_prefix):
    uid = os.getuid()
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            if os.stat(f'/proc/{name}').st_uid != uid:
                continue
            with open(f'/proc/{name}/comm') as f:
                if f.read().startswith(comm_prefix):
                    return True
        except OSError:
            continue
    return False


def keep_flags(args, value_flags, bool_flags):
    out, i = [], 0
    while i < len(args):
        a = args[i]
        if a in bool_flags:
            out.append(a)
        elif a.split('=', 1)[0] in value_flags:
            if '=' in a:
                out.append(a)
            elif i + 1 < len(args):
                out += [a, args[i + 1]]
                i += 1
        i += 1
    return out


# --- agent sessions -----------------------------------------------------------

class JsonlTail:
    """Reads only the lines appended to JSONL files since the previous call."""

    def __init__(self):
        self.offsets = {}

    def new_lines(self, path):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        off = self.offsets.get(path, 0)
        if size < off:  # rewritten
            off = 0
        if size == off:
            return
        with open(path, 'rb') as f:
            f.seek(off)
            buf = b''
            while True:
                chunk = f.read(1 << 22)
                if not chunk:
                    break
                buf += chunk
                lines = buf.split(b'\n')
                buf = lines.pop()
                for line in lines:
                    off += len(line) + 1
                    yield line
                self.offsets[path] = off


class Sessions:
    """Finds live Claude/Codex sessions. Keeps caches, so reuse one instance."""

    def __init__(self):
        self.tail = JsonlTail()
        self.transcripts = {}   # claude session id -> transcript path
        self.claude_titles = {}  # transcript path -> set of titles
        self.codex_names = {}   # codex thread id -> thread name
        self.btime = boot_time()
        self.hz = os.sysconf('SC_CLK_TCK')

    def transcript(self, sid, cwd):
        path = self.transcripts.get(sid)
        if path and os.path.exists(path):
            return path
        path = os.path.join(CLAUDE_DIR, 'projects', re.sub(r'[^A-Za-z0-9]', '-', cwd), sid + '.jsonl')
        if not os.path.exists(path):
            hits = glob.glob(os.path.join(CLAUDE_DIR, 'projects', '*', sid + '.jsonl'))
            path = hits[0] if hits else None
        if path:
            self.transcripts[sid] = path
        return path

    def titles_of(self, path):
        titles = self.claude_titles.setdefault(path, set())
        for line in self.tail.new_lines(path):
            if b'-title"' not in line and b'"agent-name"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('type') in ('ai-title', 'custom-title', 'agent-name'):
                for k in ('aiTitle', 'customTitle', 'agentName', 'title'):
                    if isinstance(rec.get(k), str) and rec[k].strip():
                        titles.add(rec[k].strip())
        return titles

    def claude(self):
        out = []
        for f in glob.glob(os.path.join(CLAUDE_DIR, 'sessions', '*.json')):
            try:
                with open(f) as fh:
                    d = json.load(fh)
                pid, sid, cwd = int(d['pid']), d['sessionId'], d['cwd']
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if d.get('kind', 'interactive') != 'interactive':
                continue
            st = read_stat(pid)
            if not st or str(st[2]) != str(d.get('procStart', st[2])):
                continue  # dead, or the pid was reused
            path = self.transcript(sid, cwd)
            titles = set(self.titles_of(path)) if path else set()
            if d.get('name'):
                titles.add(d['name'])
            out.append({'session_id': sid, 'cwd': cwd, 'start': st[2], 'titles': titles,
                        'restorable': bool(path),
                        'flags': keep_flags(read_cmdline(pid)[1:], CLAUDE_VALUE_FLAGS, CLAUDE_BOOL_FLAGS)})
        return sorted(out, key=lambda s: s['start'])

    def codex(self):
        """Interactive Codex TUIs. The node wrapper and its native binary count once."""
        uid, found = os.getuid(), {}
        for name in os.listdir('/proc'):
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                if os.stat(f'/proc/{pid}').st_uid != uid:
                    continue
                with open(f'/proc/{pid}/comm') as f:
                    if f.read().strip() not in ('codex', 'node', 'nodejs'):
                        continue
            except OSError:
                continue
            argv = read_cmdline(pid)
            if not argv:
                continue
            if os.path.basename(argv[0]) == 'codex':
                args = argv[1:]
            elif len(argv) > 1 and os.path.basename(argv[1]) == 'codex':
                args = argv[2:]
            else:
                continue
            if next((a for a in args if not a.startswith('-')), None) in CODEX_NON_TUI:
                continue
            st, cwd = read_stat(pid), read_cwd(pid)
            if not st or not st[1] or not cwd:  # no controlling terminal
                continue
            found[pid] = {'ppid': st[0], 'cwd': cwd, 'started': self.btime + st[2] / self.hz,
                          'flags': keep_flags(args, CODEX_VALUE_FLAGS, CODEX_BOOL_FLAGS)}
        return [p for p in found.values() if p['ppid'] not in found]

    def codex_threads(self, cwds):
        """Resumable threads in the given folders, newest first."""
        for line in self.tail.new_lines(os.path.join(CODEX_DIR, 'session_index.jsonl')):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('id') and rec.get('thread_name'):
                self.codex_names[rec['id']] = rec['thread_name']
        dbs = glob.glob(os.path.join(CODEX_DIR, 'state_*.sqlite'))
        if not cwds or not dbs:
            return []
        db = max(dbs, key=lambda p: int(re.search(r'state_(\d+)', p).group(1)))
        marks = ','.join('?' * len(cwds))
        con = sqlite3.connect(f'file:{urllib.parse.quote(db)}?mode=ro', uri=True, timeout=2)
        try:
            try:
                rows = con.execute(f'select id, cwd, name, source, updated_at from threads where archived = 0 '
                                   f'and cwd in ({marks}) order by updated_at desc', list(cwds)).fetchall()
            except sqlite3.OperationalError:  # older schema without thread names
                rows = con.execute(f"select id, cwd, '', source, updated_at from threads where archived = 0 "
                                   f'and cwd in ({marks}) order by updated_at desc', list(cwds)).fetchall()
        finally:
            con.close()
        return [{'id': tid, 'cwd': cwd, 'name': self.codex_names.get(tid) or name or '', 'updated': updated}
                for tid, cwd, name, source, updated in rows
                if not (source or '').startswith('{')]  # {"subagent": ...} helper threads


# --- terminal layout ----------------------------------------------------------

_atspi = None


def atspi():
    global _atspi
    if _atspi is None:
        import gi
        gi.require_version('Atspi', '2.0')
        from gi.repository import Atspi
        Atspi.set_timeout(2000, 15000)  # don't let a hung terminal stall the watcher
        _atspi = Atspi
    return _atspi


def find_role(obj, role, skip, depth):
    for i in range(obj.get_child_count()):
        c = obj.get_child_at_index(i)
        if c is None:
            continue
        r = c.get_role()
        if r == role:
            return c
        if depth > 1 and r not in skip:
            hit = find_role(c, role, skip, depth - 1)
            if hit:
                return hit
    return None


_apps = {'found': [], 'at': 0.0}


def terminal_apps():
    """GNOME Terminal's accessible apps. Rescanning every app on the desktop is the
    costly part of a poll, so it's done only every 30s, when a cached app died, or
    while no terminal is open."""
    A = atspi()
    found = _apps['found']
    if not found or time.time() - _apps['at'] > 30 or not all(read_stat(pid) for pid, _ in found):
        desktop = A.get_desktop(0)
        desktop.clear_cache()
        found = []
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app is not None and app.get_name() == 'gnome-terminal-server':
                found.append((app.get_process_id(), app))
        _apps.update(found=found, at=time.time())
    return _apps['found']


def read_layout():
    """GNOME Terminal windows with their tabs in order, or None when the
    terminal is running but missing from the accessibility tree."""
    A = atspi()
    skip = {A.Role.MENU_BAR, A.Role.MENU, A.Role.PUSH_BUTTON, A.Role.TOGGLE_BUTTON,
            A.Role.LABEL, A.Role.SEPARATOR, A.Role.SCROLL_BAR}
    windows, seen_app = [], False
    for pid, app in terminal_apps():
        seen_app = True
        app.clear_cache()
        for j in range(app.get_child_count()):
            frame = app.get_child_at_index(j)
            if frame is None or frame.get_role() != A.Role.FRAME:
                continue
            tabs = []
            tab_list = find_role(frame, A.Role.PAGE_TAB_LIST, skip, 3)
            for k in range(tab_list.get_child_count() if tab_list else 0):
                t = tab_list.get_child_at_index(k)
                if t is not None:
                    tabs.append({'key': f'{pid}:{t.path}', 'title': clean_title(t.get_name()),
                                 'active': t.get_state_set().contains(A.StateType.SELECTED)})
            if not tabs:
                tabs.append({'key': f'{pid}:{frame.path}#1', 'title': '', 'active': True})
            # A lone tab has no visible label; the window title is the active tab's title.
            for t in tabs:
                if not t['title'] and (t['active'] or len(tabs) == 1):
                    t['title'] = clean_title(frame.get_name())
            e = frame.get_extents(A.CoordType.SCREEN)
            windows.append({'key': f'{pid}:{frame.path}', 'x': e.x, 'y': e.y, 'w': e.width, 'h': e.height,
                            'tabs': tabs})
    if not seen_app and process_running('gnome-terminal'):
        return None
    return windows


def title_hit(title, candidates):
    if title in candidates:
        return True
    # tolerate truncated titles
    return len(title) >= 20 and any(len(c) >= 20 and (c.startswith(title) or title.startswith(c))
                                    for c in candidates)


def same_flags(procs):
    flags = {tuple(p['flags']) for p in procs}
    return list(flags.pop()) if len(flags) == 1 else []


def match(layout, sessions, prev=None):
    """Set tab['agent'] to the resumable session running in each tab, or None."""
    claude = sessions.claude()
    procs = sessions.codex()
    by_cwd = {}
    for p in procs:
        by_cwd.setdefault(p['cwd'], []).append(p)
    threads = sessions.codex_threads(set(by_cwd))
    claimed, unmatched = set(), []
    tabs = [t for w in layout for t in w['tabs']]

    def take(t, tool, sid, cwd, flags, guess=False):
        claimed.add(sid)
        t['agent'] = {'tool': tool, 'session_id': sid, 'cwd': cwd, 'flags': flags, 'guess': guess}

    for t in tabs:
        t['agent'] = None
        s = next((s for s in claude if s['session_id'] not in claimed and title_hit(t['title'], s['titles'])), None)
        if s:
            if s['restorable']:
                take(t, 'claude', s['session_id'], s['cwd'], s['flags'])
            else:
                claimed.add(s['session_id'])
            continue
        # Codex titles read "<thread name> | <folder name>".
        name, sep, folder = t['title'].rpartition(' | ')
        th = sep and next((th for th in threads if th['id'] not in claimed and th['name'] == name
                           and os.path.basename(th['cwd']) == folder), None)
        if th:
            take(t, 'codex', th['id'], th['cwd'], same_flags(by_cwd[th['cwd']]))
        else:
            unmatched.append(t)

    # A Claude tab whose title we lost track of keeps its session while that session lives.
    live = {s['session_id']: s for s in claude}
    prev_agents = {t['key']: t.get('agent') for w in (prev or []) for t in w['tabs']}
    for t in list(unmatched):
        a = prev_agents.get(t['key'])
        if a and a['tool'] == 'claude' and a['session_id'] in live and a['session_id'] not in claimed:
            take(t, 'claude', a['session_id'], a['cwd'], a['flags'], a.get('guess', False))
            unmatched.remove(t)

    # Claude sessions without a title yet all show "Claude Code"; pair them up in start order,
    # untitled sessions first.
    untitled_first = sorted(claude, key=lambda s: (bool(s['titles']), s['start']))
    for t in list(unmatched):
        if t['title'] == 'Claude Code':
            s = next((s for s in untitled_first if s['session_id'] not in claimed), None)
            if s:
                if s['restorable']:
                    take(t, 'claude', s['session_id'], s['cwd'], s['flags'], guess=True)
                else:
                    claimed.add(s['session_id'])
                unmatched.remove(t)

    # A Codex thread without a name shows only the folder name. Give such tabs the newest
    # unnamed thread there, while that folder has more live Codex TUIs than matched tabs.
    for t in unmatched:
        for cwd, ps in by_cwd.items():
            if os.path.basename(cwd) != t['title']:
                continue
            used = sum(1 for x in tabs if x['agent'] and x['agent']['tool'] == 'codex' and x['agent']['cwd'] == cwd)
            since = min(p['started'] for p in ps) - 5
            th = used < len(ps) and next((th for th in threads if th['cwd'] == cwd and th['id'] not in claimed
                                          and not th['name'] and th['updated'] >= since), None)
            if th:
                take(t, 'codex', th['id'], cwd, same_flags(ps), guess=True)
                break
    return {s['session_id'] for s in claude}


# --- state --------------------------------------------------------------------

@contextlib.contextmanager
def state():
    """Locked read-modify-write of the state file."""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOCK_FILE, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with open(STATE_FILE) as f:
                st = json.load(f)
        except (OSError, ValueError):
            st = {}
        st.setdefault('closed', [])
        st.setdefault('next_id', 0)
        before = json.dumps(st, sort_keys=True)
        prune(st, set(), time.time())
        yield st
        if json.dumps(st, sort_keys=True) != before:
            tmp = STATE_FILE + '.tmp'
            with open(tmp, 'w') as f:
                json.dump(st, f, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, STATE_FILE)


def workarea():
    try:
        out = subprocess.run(['xprop', '-root', '_NET_WORKAREA'], capture_output=True, text=True,
                             timeout=2).stdout
        return [int(n) for n in re.findall(r'\d+', out.split('=', 1)[1])][:4]
    except (OSError, IndexError, subprocess.SubprocessError):
        return None


def prune(st, live_sids, now):
    for c in st['closed']:
        c['tabs'] = [t for t in c['tabs'] if t['session_id'] not in live_sids]
    st['closed'] = [c for c in st['closed'] if c['tabs'] and now - c['closed_at'] < KEEP_SECS]


# --- watcher ------------------------------------------------------------------

class Watcher:
    def __init__(self, layout):
        self.layout = layout or []  # last layout seen
        self.limbo = {}             # tab key -> vanished agent tab awaiting confirmation

    def step(self, cur, live_claude, now, final):
        """Agent tabs that are confirmed closed since the last call."""
        cur_tabs = {t['key']: t for w in cur for t in w['tabs']}
        cur_sids = {t['agent']['session_id'] for w in cur for t in w['tabs'] if t['agent']}
        for w in self.layout:
            for i, t in enumerate(w['tabs']):
                current = cur_tabs.get(t['key'])
                if (t.get('agent') and (current is None or not current.get('agent'))
                        and t['key'] not in self.limbo):
                    self.limbo[t['key']] = {'win': w, 'idx': i, 'tab': t,
                                            'since': t.get('unmatched_since', now)}
        self.layout = cur
        ready = []
        for key, e in list(self.limbo.items()):
            sid = e['tab']['agent']['session_id']
            current = cur_tabs.get(key)
            if sid in cur_sids or (current is not None and current.get('agent')):
                # Came back, moved, or this tab started a different session.
                del self.limbo[key]
            elif current is not None:
                # Losing a process/title match isn't proof of a normal exit: at
                # shutdown Codex can die while its tab still exists. Keep its
                # last association until the tab closes or stays a shell long
                # enough to confirm that the agent was exited intentionally.
                a = e['tab']['agent']
                if (a['tool'] == 'codex' and ' | ' in e['tab']['title']
                        and current['title'] == e['tab']['title']):
                    # An unchanged named Codex title still identifies the
                    # session, even if its process disappeared first.
                    e['since'] = now
                elif now - e['since'] >= EXIT_GRACE_SECS:
                    del self.limbo[key]
            elif sid in live_claude and not final:  # its process still runs, so the tab wasn't closed yet
                if now - e['since'] > CLAUDE_GRACE_SECS:
                    del self.limbo[key]
            elif final or now - e['since'] >= POLL_SECS * 0.9:  # gone two polls in a row
                ready.append(e)
                del self.limbo[key]
        return ready, cur_sids

    def saved_layout(self):
        """The layout plus vanished tabs not yet confirmed closed, so that a watcher
        killed mid-confirmation (e.g. at logout) still finds them after a restart."""
        windows = {w['key']: dict(w, tabs=list(w['tabs'])) for w in self.layout}
        for e in sorted(self.limbo.values(), key=lambda e: e['idx']):
            w = windows.setdefault(e['win']['key'], dict(e['win'], tabs=[]))
            tab = dict(e['tab'], unmatched_since=e['since'])
            idx = next((i for i, t in enumerate(w['tabs']) if t['key'] == tab['key']), None)
            if idx is None:
                w['tabs'].insert(e['idx'], tab)
            else:
                w['tabs'][idx] = tab
        return list(windows.values())

    @staticmethod
    def commit(st, ready, cur, now, reason):
        cur_wins = {w['key'] for w in cur}
        wa = workarea() if ready else None
        groups = {}
        for e in ready:
            groups.setdefault(e['win']['key'], []).append(e)
        for wkey, es in groups.items():
            w = es[0]['win']
            tabs = [dict(e['tab']['agent'], idx=e['idx'], title=e['tab']['title'], active=e['tab']['active'])
                    for e in es]
            entry = next((c for c in st['closed'] if c.get('src') == wkey and now - c['closed_at'] < MERGE_SECS),
                         None)
            if entry is None:
                st['next_id'] += 1
                maximized = bool(wa) and w['w'] >= wa[2] - 4 and w['h'] >= wa[3] - 4
                entry = {'id': st['next_id'], 'src': wkey, 'closed_at': now, 'reason': reason,
                         'geom': {'x': w['x'], 'y': w['y'], 'max': maximized}, 'tabs': []}
                st['closed'].append(entry)
            have = {t['session_id'] for t in entry['tabs']}
            entry['tabs'] = sorted(entry['tabs'] + [t for t in tabs if t['session_id'] not in have],
                                   key=lambda t: t['idx'])
            entry['whole'] = wkey not in cur_wins
            log(f"recorded {'window' if entry['whole'] else 'tab'} #{entry['id']}: "
                + ', '.join(f"{t['tool']}:{t['title']}" for t in tabs))


def cmd_watch(args):
    sessions = Sessions()
    boot = boot_id()
    with state() as st:
        watcher = Watcher(st.get('layout'))
        rebooted = st.get('boot_id') not in (None, boot)
        st['watcher_pid'] = os.getpid()
    reason = 'shutdown' if rebooted else 'closed'
    log(f'watching (previous layout: {len(watcher.layout)} windows{", from before reboot" if rebooted else ""})')
    while True:
        try:
            cur = read_layout()
            if cur is None:
                log('GNOME Terminal is running but not in the accessibility tree; skipping')
            else:
                live_claude = match(cur, sessions, watcher.layout)
                now = time.time()
                # Right after a reboot nothing from the old layout can come back: commit at once.
                ready, cur_sids = watcher.step(cur, live_claude, now, final=(reason == 'shutdown'))
                with state() as st:
                    st['boot_id'] = boot
                    st['layout'] = watcher.saved_layout()
                    if ready:
                        Watcher.commit(st, ready, cur, now, reason)
                    prune(st, cur_sids | live_claude, now)
                reason = 'closed'
        except Exception as e:  # keep tracking through transient AT-SPI / sqlite errors
            log(f'poll failed: {type(e).__name__}: {e}')
        time.sleep(POLL_SECS)


# --- restore --------------------------------------------------------------------

def resume_cmd(t):
    if t['tool'] == 'claude':
        argv = [shutil.which('claude') or 'claude', '--resume', t['session_id']]
    else:
        argv = [shutil.which('codex') or 'codex', 'resume', t['session_id']]
    # Keep the tab open on a shell once the agent exits, like a normal tab.
    return shlex.join(['bash', '-c', shlex.join(argv + t.get('flags', [])) + '; exec "${SHELL:-bash}"'])


def terminal_argv(entry):
    argv = ['gnome-terminal', '--window']
    g = entry.get('geom') or {}
    if g.get('max'):
        argv.append('--maximize')
    elif 'x' in g:
        argv.append(f"--geometry=+{g['x']}+{g['y']}")
    for i, t in enumerate(entry['tabs']):
        if i:
            argv.append('--tab')
        cwd = t['cwd'] if os.path.isdir(t['cwd']) else HOME
        argv += [f'--working-directory={cwd}', '-e', resume_cmd(t)]
        if t.get('active'):
            argv.append('--active')
    return argv


def live_session_ids():
    """Sessions running right now, so a restore never opens one twice."""
    sessions = Sessions()
    try:
        layout = read_layout() or []
        live = match(layout, sessions)
        return live | {t['agent']['session_id'] for w in layout for t in w['tabs'] if t['agent']}
    except Exception:
        return {s['session_id'] for s in sessions.claude()}


def restore(entries, dry):
    env = {k: v for k, v in os.environ.items() if k not in AGENT_ENV}
    if env.get('GIT_EDITOR') == 'true' and 'CLAUDECODE' in os.environ:  # Claude's, not the user's
        del env['GIT_EDITOR']
    live = live_session_ids()
    done = []
    for c in entries:
        running = [t for t in c['tabs'] if t['session_id'] in live]
        for t in running:
            print(f"  skipping {t['tool']} '{t['title']}': already running", file=sys.stderr)
        c = dict(c, tabs=[t for t in c['tabs'] if t not in running])
        if not c['tabs']:
            if not dry:
                done.append(c['id'])
            continue
        argv = terminal_argv(c)
        if dry:
            print(shlex.join(argv))
            continue
        for t in c['tabs']:
            if not os.path.isdir(t['cwd']):
                print(f"  warning: {t['cwd']} no longer exists; opening in ~", file=sys.stderr)
        r = subprocess.run(argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        noise = [l for l in r.stderr.splitlines() if 'deprecated' not in l and 'Use “-- ”' not in l and l.strip()]
        if r.returncode == 0:
            print(f"restored #{c['id']} ({len(c['tabs'])} tab{'s' * (len(c['tabs']) != 1)})")
            done.append(c['id'])
        else:
            print(f"failed to restore #{c['id']}: {' '.join(noise) or r.returncode}", file=sys.stderr)
    if done:
        with state() as st:
            st['closed'] = [c for c in st['closed'] if c['id'] not in done]
    return bool(done) or dry


def closed_entries():
    with state() as st:
        return sorted(st['closed'], key=lambda c: c['closed_at'], reverse=True)


def print_closed(entries):
    if not entries:
        print('No closed windows to restore.')
        return
    for c in entries:
        when = time.strftime('%a %H:%M', time.localtime(c['closed_at']))
        what = {'shutdown': 'before reboot/logout'}.get(c['reason'], 'window closed' if c.get('whole') else 'tab closed')
        print(f"  #{c['id']:<3} {when}  {what}, {len(c['tabs'])} tab{'s' * (len(c['tabs']) != 1)}")
        for t in c['tabs']:
            print(f"        {'*' if t.get('active') else ' '} {t['tool']:<6} {t['title'][:48]:<48}  "
                  f"{short_path(t['cwd'])}{'  (guess)' if t.get('guess') else ''}")


def select(entries, ids):
    if any(i in ('a', 'all') for i in ids):
        return entries
    want = {int(i.lstrip('#')) for i in ids if i.lstrip('#').isdigit()}
    unknown = want - {c['id'] for c in entries}
    if unknown:
        print(f"no closed window {', '.join('#%d' % i for i in sorted(unknown))}", file=sys.stderr)
    return [c for c in entries if c['id'] in want]


def cmd_pick(args):
    entries = closed_entries()
    print_closed(entries)
    if not entries:
        return 0
    try:
        ans = input('\nRestore which? (ids, a = all, Enter = cancel): ').replace(',', ' ').split()
    except (EOFError, KeyboardInterrupt):
        print()
        return 1
    chosen = select(entries, ans)
    return 0 if chosen and restore(chosen, args.print) else 1


def cmd_restore(args):
    chosen = select(closed_entries(), args.ids)
    return 0 if chosen and restore(chosen, args.print) else 1


def cmd_forget(args):
    with state() as st:
        drop = {c['id'] for c in select(st['closed'], args.ids)}
        st['closed'] = [c for c in st['closed'] if c['id'] not in drop]
    print(f'forgot {len(drop)} window{"s" * (len(drop) != 1)}')
    return 0


def watcher_status():
    with state() as st:
        pid = st.get('watcher_pid')
    if pid and 'watch' in read_cmdline(pid):
        return f'running (pid {pid})'
    return 'NOT running - start it with: agent-restore install'


def cmd_list(args):
    try:
        layout = read_layout()
    except Exception as e:
        layout = None
        print(f'cannot read GNOME Terminal windows: {e}')
    if layout is not None:
        match(layout, Sessions())
        print('Live windows:')
        if not layout:
            print('  (none)')
        for n, w in enumerate(layout, 1):
            print(f"  window {n}, {len(w['tabs'])} tabs")
            for t in w['tabs']:
                a = t['agent']
                if a:
                    print(f"    {'*' if t['active'] else ' '} {a['tool']:<6} {t['title'][:48]:<48}  "
                          f"{short_path(a['cwd'])}  {a['session_id'][:8]}{'?' if a['guess'] else ''}")
                else:
                    print(f"    {'*' if t['active'] else ' '} {'-':<6} {t['title'][:48]}")
    print('\nClosed windows:')
    print_closed(closed_entries())
    print(f'\nWatcher: {watcher_status()}')
    return 0


# --- install ----------------------------------------------------------------------

def systemctl(*args):
    return subprocess.run(['systemctl', '--user', *args], check=False).returncode


def cmd_install(args):
    script = os.path.realpath(__file__)
    os.chmod(script, os.stat(script).st_mode | 0o111)
    os.makedirs(os.path.dirname(BIN_LINK), exist_ok=True)
    if os.path.lexists(BIN_LINK) and not os.path.islink(BIN_LINK):
        print(f'{BIN_LINK} exists and is not a symlink; leaving it alone', file=sys.stderr)
    else:
        if os.path.lexists(BIN_LINK):
            os.remove(BIN_LINK)
        os.symlink(script, BIN_LINK)
    os.makedirs(os.path.dirname(UNIT_FILE), exist_ok=True)
    with open(UNIT_FILE, 'w') as f:
        f.write(f"""[Unit]
Description=Track Claude/Codex terminal windows so agent-restore can reopen them
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart=/usr/bin/python3 {script} watch
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
""")
    systemctl('daemon-reload')
    rc = systemctl('enable', '--now', UNIT_NAME)
    print(f'installed {BIN_LINK} and {UNIT_FILE}' if rc == 0 else 'failed to start the service')
    return rc


def cmd_uninstall(args):
    systemctl('disable', '--now', UNIT_NAME)
    if os.path.exists(UNIT_FILE):
        os.remove(UNIT_FILE)
    systemctl('daemon-reload')
    if os.path.islink(BIN_LINK) and os.path.realpath(BIN_LINK) == os.path.realpath(__file__):
        os.remove(BIN_LINK)
    print(f'uninstalled (state kept in {STATE_DIR})')
    return 0


def main():
    ap = argparse.ArgumentParser(prog='agent-restore', description=__doc__.split('\n\n')[0])
    ap.add_argument('--print', action='store_true', help='print the terminal commands instead of running them')
    sub = ap.add_subparsers(dest='cmd')
    sub.add_parser('list', help='show live and closed windows')
    for name, help_ in (('restore', 'restore closed windows'), ('forget', 'drop closed windows')):
        p = sub.add_parser(name, help=help_)
        p.add_argument('ids', nargs='+', help="window ids from 'list', or 'all'")
        p.add_argument('--print', action='store_true', default=argparse.SUPPRESS)
    sub.add_parser('watch', help='track windows (run by the systemd unit)')
    sub.add_parser('install', help='install the command and start the tracker service')
    sub.add_parser('uninstall', help='stop and remove the tracker service')
    args = ap.parse_args()
    handler = {None: cmd_pick, 'list': cmd_list, 'restore': cmd_restore, 'forget': cmd_forget,
               'watch': cmd_watch, 'install': cmd_install, 'uninstall': cmd_uninstall}[args.cmd]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
