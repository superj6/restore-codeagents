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


if __name__ == '__main__':
    unittest.main()
