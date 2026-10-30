import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import agent_restore as app


def tab(key, tool='codex'):
    return {'key': key, 'title': key + ' | project', 'active': key == 'first',
            'agent': {'tool': tool, 'session_id': key, 'cwd': '/project',
                      'flags': [], 'guess': False}}


def window(*tabs, key='window'):
    return {'key': key, 'x': 0, 'y': 0, 'w': 800, 'h': 600, 'tabs': list(tabs)}


def unmatched(layout, *keys):
    cur = copy.deepcopy(layout)
    for w in cur:
        for t in w['tabs']:
            if t['key'] in keys:
                t['agent'] = None
                t['title'] = 'shell'
    return cur


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self.layout = [window(tab('first'), tab('claude', 'claude'), tab('last'))]
        self.watcher = app.Watcher(copy.deepcopy(self.layout))
        self.now = 1000

    def test_codex_exits_before_window_closes(self):
        cur = unmatched(self.layout, 'first', 'last')
        ready, _ = self.watcher.step(cur, {'claude'}, self.now, final=False)
        self.assertEqual(ready, [])
        ready, _ = self.watcher.step([], set(), self.now + app.POLL_SECS, final=False)
        self.assertEqual([e['tab']['agent']['session_id'] for e in ready], ['first', 'last'])
        ready, _ = self.watcher.step([], set(), self.now + 2 * app.POLL_SECS, final=False)
        self.assertEqual([e['tab']['agent']['session_id'] for e in ready], ['claude'])

    def test_reboot_retains_codex_and_claude_in_original_order(self):
        self.watcher.step(unmatched(self.layout, 'first', 'last'), {'claude'}, self.now, final=False)
        saved = self.watcher.saved_layout()
        self.assertEqual([t['agent']['tool'] for t in saved[0]['tabs']], ['codex', 'claude', 'codex'])
        # The pending snapshot has to survive JSON serialization and a fresh process.
        restarted = app.Watcher(json.loads(json.dumps(saved)))
        ready, _ = restarted.step([], set(), self.now + 5, final=True)
        st = {'closed': [], 'next_id': 0}
        with patch.object(app, 'workarea', return_value=None), patch.object(app, 'log'):
            app.Watcher.commit(st, ready, [], self.now + 5, 'shutdown')
        self.assertEqual(len(st['closed']), 1)
        self.assertEqual([t['session_id'] for t in st['closed'][0]['tabs']], ['first', 'claude', 'last'])
        self.assertEqual(st['closed'][0]['reason'], 'shutdown')

    def test_normal_exit_is_forgotten_after_grace(self):
        cur = unmatched(self.layout, 'first')
        self.watcher.step(cur, {'claude'}, self.now, final=False)
        ready, _ = self.watcher.step(cur, {'claude'}, self.now + app.EXIT_GRACE_SECS, final=False)
        self.assertEqual(ready, [])
        self.assertEqual(self.watcher.limbo, {})
        self.assertIsNone(self.watcher.saved_layout()[0]['tabs'][0]['agent'])
        # Closing the shell later must not bring that exited conversation back.
        cur[0]['tabs'].pop(0)
        self.watcher.step(cur, {'claude'}, self.now + 40, final=False)
        ready, _ = self.watcher.step(cur, {'claude'}, self.now + 45, final=False)
        self.assertEqual(ready, [])

    def test_watcher_restart_does_not_reset_exit_grace(self):
        cur = unmatched(self.layout, 'first')
        self.watcher.step(cur, {'claude'}, self.now, final=False)
        restarted = app.Watcher(self.watcher.saved_layout())
        restarted.step(cur, {'claude'}, self.now + app.EXIT_GRACE_SECS, final=False)
        self.assertEqual(restarted.limbo, {})

    def test_unchanged_codex_title_preserves_session_without_process(self):
        cur = copy.deepcopy(self.layout)
        cur[0]['tabs'][0]['agent'] = None
        self.watcher.step(cur, {'claude'}, self.now, final=False)
        self.watcher.step(cur, {'claude'}, self.now + 120, final=False)
        self.assertEqual(self.watcher.saved_layout()[0]['tabs'][0]['agent']['session_id'], 'first')
        # The normal-exit grace starts when the title returns to a shell.
        cur[0]['tabs'][0]['title'] = 'shell'
        self.watcher.step(cur, {'claude'}, self.now + 123, final=False)
        self.assertIn('first', self.watcher.limbo)
        self.watcher.step(cur, {'claude'}, self.now + 150, final=False)
        self.assertEqual(self.watcher.limbo, {})

    def test_transient_match_failure_clears_when_session_returns(self):
        self.watcher.step(unmatched(self.layout, 'first'), {'claude'}, self.now, final=False)
        ready, _ = self.watcher.step(copy.deepcopy(self.layout), {'claude'}, self.now + 3, final=False)
        self.assertEqual(ready, [])
        self.assertEqual(self.watcher.limbo, {})

    def test_different_session_in_same_tab_cancels_old_pending_session(self):
        self.watcher.step(unmatched(self.layout, 'first'), {'claude'}, self.now, final=False)
        cur = copy.deepcopy(self.layout)
        cur[0]['tabs'][0]['agent']['session_id'] = 'replacement'
        self.watcher.step(cur, {'claude'}, self.now + 3, final=False)
        self.assertEqual(self.watcher.limbo, {})

    def test_move_to_another_window_is_not_recorded_as_closed(self):
        self.watcher.step(unmatched(self.layout, 'first'), {'claude'}, self.now, final=False)
        cur = copy.deepcopy(self.layout)
        moved = cur[0]['tabs'].pop(0)
        moved['key'] = 'moved-key'
        cur.append(window(moved, key='other-window'))
        ready, _ = self.watcher.step(cur, {'claude'}, self.now + 3, final=False)
        self.assertEqual(ready, [])
        self.assertEqual(self.watcher.limbo, {})

    def test_snapshot_merges_pending_tabs_without_duplicate_windows(self):
        cur = copy.deepcopy(self.layout)
        cur[0]['tabs'].pop(0)
        self.watcher.step(cur, {'claude'}, self.now, final=False)
        saved = self.watcher.saved_layout()
        self.assertEqual(len(saved), 1)
        self.assertEqual([t['key'] for t in saved[0]['tabs']], ['first', 'claude', 'last'])
        self.assertEqual([t['key'] for t in cur[0]['tabs']], ['claude', 'last'])

    def test_closed_tab_still_needs_two_polls(self):
        cur = copy.deepcopy(self.layout)
        cur[0]['tabs'].pop(0)
        ready, _ = self.watcher.step(cur, {'claude'}, self.now, final=False)
        self.assertEqual(ready, [])
        ready, _ = self.watcher.step(cur, {'claude'}, self.now + app.POLL_SECS, final=False)
        self.assertEqual([e['tab']['agent']['session_id'] for e in ready], ['first'])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = patch.multiple(app, STATE_DIR=self.tmp.name,
                                 STATE_FILE=os.path.join(self.tmp.name, 'state.json'),
                                 LOCK_FILE=os.path.join(self.tmp.name, 'state.lock'))
        patches.start()
        self.addCleanup(patches.stop)
        self.now = 1000000

    def entry(self, id_, age):
        return {'id': id_, 'closed_at': self.now - age, 'tabs': [{'session_id': str(id_)}]}

    def write_state(self, entries):
        with open(app.STATE_FILE, 'w') as f:
            json.dump({'closed': entries, 'next_id': 10}, f)

    def test_expires_at_three_days_even_without_watcher(self):
        self.write_state([self.entry(1, 2 * 86400), self.entry(2, 3 * 86400), self.entry(3, 5 * 86400)])
        with patch.object(app.time, 'time', return_value=self.now):
            self.assertEqual([c['id'] for c in app.closed_entries()], [1])
        with open(app.STATE_FILE) as f:
            st = json.load(f)
        self.assertEqual([c['id'] for c in st['closed']], [1])
        self.assertEqual(st['next_id'], 10)

    def test_prune_preserves_closed_tabs_that_are_not_running(self):
        st = {'closed': [self.entry(1, 10), self.entry(2, 10)]}
        app.prune(st, {'1'}, self.now)
        self.assertEqual([c['id'] for c in st['closed']], [2])

    def test_state_prunes_before_mutations(self):
        self.write_state([self.entry(1, 4 * 86400)])
        with patch.object(app.time, 'time', return_value=self.now), app.state() as st:
            self.assertEqual(st['closed'], [])
            st['closed'].append(self.entry(2, 0))
        with open(app.STATE_FILE) as f:
            self.assertEqual([c['id'] for c in json.load(f)['closed']], [2])


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patches = patch.multiple(app, STATE_DIR=self.tmp.name,
                                 STATE_FILE=os.path.join(self.tmp.name, 'state.json'),
                                 LOCK_FILE=os.path.join(self.tmp.name, 'state.lock'))
        patches.start()
        self.addCleanup(patches.stop)
        self.entry = {'id': 1, 'closed_at': app.time.time(),
                      'tabs': [dict(tab('codex')['agent'], title='Codex'),
                               dict(tab('claude', 'claude')['agent'], title='Claude')]}
        with open(app.STATE_FILE, 'w') as f:
            json.dump({'closed': [self.entry], 'next_id': 1}, f)

    def restore(self, live=(), rc=0, dry=False):
        result = app.subprocess.CompletedProcess([], rc, stderr='')
        with patch.object(app, 'live_session_ids', return_value=set(live)), \
                patch.object(app.subprocess, 'run', return_value=result) as run, \
                patch.object(app.os.path, 'isdir', return_value=True), \
                patch('builtins.print'):
            restored = app.restore([self.entry], dry)
        return restored, run

    def saved_sids(self):
        with open(app.STATE_FILE) as f:
            return [t['session_id'] for c in json.load(f)['closed'] for t in c['tabs']]

    def test_terminal_success_keeps_unconfirmed_sessions_restorable(self):
        restored, run = self.restore()
        self.assertTrue(restored)
        run.assert_called_once()
        self.assertEqual(self.saved_sids(), ['codex', 'claude'])

    def test_partial_startup_only_removes_confirmed_sessions(self):
        self.restore(live={'claude'})
        self.assertEqual(self.saved_sids(), ['codex'])

    def test_already_running_sessions_are_removed_without_opening_tabs(self):
        restored, run = self.restore(live={'codex', 'claude'})
        self.assertTrue(restored)
        run.assert_not_called()
        self.assertEqual(self.saved_sids(), [])

    def test_terminal_launch_failure_keeps_saved_entries(self):
        restored, _ = self.restore(rc=1)
        self.assertFalse(restored)
        self.assertEqual(self.saved_sids(), ['codex', 'claude'])

    def test_dry_run_does_not_launch_or_remove_entries(self):
        restored, run = self.restore(live={'claude'}, dry=True)
        self.assertTrue(restored)
        run.assert_not_called()
        self.assertEqual(self.saved_sids(), ['codex', 'claude'])


class CodexStartupTests(unittest.TestCase):
    def run_codex(self, statuses, elapsed=1):
        results = [app.subprocess.CompletedProcess([], rc) for rc in statuses]
        times = [v for i in range(len(statuses)) for v in (i * 200, i * 200 + elapsed)]
        with patch.object(app.subprocess, 'run', side_effect=results) as run, \
                patch.object(app.time, 'monotonic', side_effect=times), \
                patch.object(app.time, 'sleep') as sleep, \
                patch.object(app.shutil, 'which', return_value='/bin/codex'), \
                patch('builtins.print'):
            rc = app.run_codex('saved-thread', ['--model', 'selected-model'])
        return rc, run, sleep

    def test_transient_startup_failure_retries_same_thread_and_flags(self):
        rc, run, sleep = self.run_codex([1, 1, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(run.call_count, 3)
        for invocation in run.call_args_list:
            self.assertEqual(invocation.args[0], ['/bin/codex', 'resume', 'saved-thread',
                                                 '--model', 'selected-model'])
            self.assertEqual(invocation.kwargs, {})  # stdin/out/err keep their terminal
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 10])

    def test_persistent_failure_is_bounded(self):
        rc, run, sleep = self.run_codex([1, 1, 1])
        self.assertEqual(rc, 1)
        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_normal_exit_and_interrupts_do_not_retry(self):
        for status in (0, -2, -15, 130, 137, 143):
            with self.subTest(status=status):
                rc, run, sleep = self.run_codex([status])
                self.assertEqual(rc, status)
                run.assert_called_once()
                sleep.assert_not_called()

    def test_failure_after_long_session_does_not_retry(self):
        rc, run, sleep = self.run_codex([1], elapsed=app.CODEX_START_SECS)
        self.assertEqual(rc, 1)
        run.assert_called_once()
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
