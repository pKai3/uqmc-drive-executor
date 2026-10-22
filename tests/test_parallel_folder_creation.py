import copy
import tempfile
import signal
import threading
import unittest
from collections import Counter
from unittest.mock import patch

from uqmc_recovery.direct_transport import TransportError
from uqmc_recovery.folder_creation import prepare_required_folders
from uqmc_recovery.journal import Journal, JournalBusy
from uqmc_recovery.model import FOLDER, SafetyError


def spec(name, parent='anchor'):
    return {'logical_path': name, 'logical_key': 'key:' + name,
            'folder_name': name.rsplit('/', 1)[-1], 'parent_path': parent}


class Backend:
    def __init__(self):
        self.gate = threading.RLock()
        self.files = {'parent': {'id': 'parent', 'name': 'Parent', 'mimeType': FOLDER,
                      'parents': ['root'], 'trashed': False, 'capabilities': {'canAddChildren': True}}}
        self.calls = []
        self.counter = 0
        self.active = 0
        self.max_active = 0
        self.clients = []
        self.failures = {}
        self.barrier = None
        self.before_create = None

    def factory(self, allowed, mutations, excluded, enabled, *, creation_parent_ids):
        assert allowed == creation_parent_ids and not mutations and enabled
        client = Client(self, allowed)
        with self.gate:
            self.clients.append(client)
        return client


class Client:
    def __init__(self, backend, allowed):
        self.b = backend
        self.allowed = set(allowed)
        self.owner = threading.get_ident()
        self.reserved = set()

    def call(self, action, file_id):
        assert threading.get_ident() == self.owner
        with self.b.gate:
            self.b.calls.append((action, file_id))

    def get_file(self, file_id):
        self.call('get', file_id)
        with self.b.gate:
            return copy.deepcopy(self.b.files[file_id])

    def list_children(self, parent):
        self.call('list', parent)
        with self.b.gate:
            return [copy.deepcopy(x) for x in self.b.files.values() if x['parents'] == [parent]]

    def reserve_folder_id(self, parent):
        self.call('allocate', parent)
        with self.b.gate:
            self.b.counter += 1
            file_id = 'allocated-' + str(self.b.counter)
            self.reserved.add(file_id)
            return file_id

    def register_reserved_folder_id(self, file_id, parent):
        self.call('register', file_id)
        self.reserved.add(file_id)

    def get_reserved_folder(self, file_id):
        self.call('read', file_id)
        with self.b.gate:
            if file_id not in self.b.files:
                raise TransportError('DRIVE_HTTP_ERROR', status=404)
            return copy.deepcopy(self.b.files[file_id])

    def create_reserved_folder(self, file_id, *, name, parent_id, stable_key):
        assert file_id in self.reserved and parent_id in self.allowed
        self.call('create', file_id)
        with self.b.gate:
            self.b.active += 1
            self.b.max_active = max(self.b.max_active, self.b.active)
        try:
            if self.b.before_create:
                self.b.before_create(file_id, name, parent_id)
            if self.b.barrier:
                self.b.barrier.wait(timeout=3)
            mode = self.b.failures.get(name)
            if mode == 'refuse':
                raise TransportError('DRIVE_HTTP_ERROR', status=403)
            if mode == 'before':
                raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
            with self.b.gate:
                self.b.files[file_id] = {'id': file_id, 'name': name, 'mimeType': FOLDER,
                    'parents': [parent_id], 'trashed': False, 'capabilities': {'canAddChildren': True},
                    'appProperties': {'uqmcFolderKey': stable_key}}
            if mode == 'after':
                raise TransportError('DRIVE_REQUEST_TIMEOUT', outcome='UNKNOWN')
            return self.get_reserved_folder(file_id)
        finally:
            with self.b.gate:
                self.b.active -= 1


class ParallelFolderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.j = Journal(self.tmp.name)
        self.b = Backend()
        self.paths = {'anchor': 'parent'}
        self.events = []

    def authority(self, specs):
        keys = {s['logical_path']: s['logical_key'] for s in specs}
        folders = []
        for s in specs:
            parent = {'parent_key': keys[s['parent_path']]} if s['parent_path'] in keys else {'parent_id': self.paths[s['parent_path']]}
            record = {'key': s['logical_key'], 'name': s['folder_name'], **parent}
            if record not in folders:
                folders.append(record)
        instruction = self.j.evidence({'kind': 'TRUSTED_USER_INSTRUCTION', 'text': 'Fixture section approval'})
        return self.j.evidence({'schema_version': 'uqmc.folder-create-authority.v1',
            'section_id': 'fixture-section', 'approval_id': 'fixture-approval', 'decision': 'APPROVED',
            'provenance_ref': instruction, 'access_policy': 'SCOPED_INHERITANCE_ACCEPTED', 'folders': folders})

    def run_folders(self, specs, *, workers=2, authority=None):
        return prepare_required_folders(self.j, self.b.factory, specs=specs, paths=self.paths,
            excluded_ids={'fixture-excluded'}, authority_ref=authority or self.authority(specs),
            workers=workers, report=self.events.append)

    def test_remote_calls_overlap_with_independent_thread_bound_transports(self):
        self.b.barrier = threading.Barrier(2)
        result = self.run_folders([spec('A'), spec('B')])
        self.assertEqual(set(result), {'A', 'B'})
        self.assertEqual(self.b.max_active, 2)
        self.assertEqual(len(self.b.clients), 2)
        self.assertEqual(len({id(c) for c in self.b.clients}), 2)
        self.assertEqual(len([r for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_VERIFIED']), 2)
        # A separate instance can read the complete hash-chain after the pool ends.
        self.assertEqual(Journal(self.tmp.name).records(), self.j.records())

    def test_process_writer_lock_spans_remote_work_and_wal_precedes_each_create(self):
        def check(file_id, name, parent_id):
            with self.assertRaises(JournalBusy):
                Journal(self.tmp.name).records()
            records = self.j.records()
            attempts = [r for r in records if r['kind'] == 'REQUIRED_FOLDER_ATTEMPTED' and r['payload']['file_id'] == file_id]
            self.assertEqual(len(attempts), 1)
        self.b.before_create = check
        self.run_folders([spec('A'), spec('B')])

    def test_nested_dependencies_wait_for_verified_parent_with_one_or_many_workers(self):
        for workers in (1, 3):
            with self.subTest(workers=workers), tempfile.TemporaryDirectory() as directory:
                self.j = Journal(directory)
                self.b = Backend()
                specs = [spec('A/B', 'A'), spec('A/B/C', 'A/B'), spec('A')]
                def check(file_id, name, parent_id):
                    if parent_id != 'parent':
                        self.assertTrue(any(r['kind'] == 'REQUIRED_FOLDER_VERIFIED' and r['payload']['file_id'] == parent_id for r in self.j.records()))
                self.b.before_create = check
                result = self.run_folders(specs, workers=workers)
                self.assertEqual(result['A/B']['parents'], [result['A']['id']])
                self.assertEqual(result['A/B/C']['parents'], [result['A/B']['id']])

    def test_identical_duplicate_requests_are_single_flight_and_reuse_verified_id(self):
        s = spec('A')
        first = self.run_folders([s, dict(s)])
        second = self.run_folders([s])
        self.assertEqual(first, second)
        self.assertEqual(self.b.counter, 1)
        self.assertEqual(len([x for x in self.b.calls if x[0] == 'create']), 1)

    def test_conflicting_logical_keys_or_same_parent_names_fail_before_transport(self):
        for second in ({**spec('B'), 'logical_key': 'key:A'},
                       {**spec('B'), 'folder_name': 'A'},
                       {**spec('B'), 'folder_name': 'A '}):
            with self.subTest(second=second), self.assertRaisesRegex(SafetyError, 'IDENTITY_CONFLICT|NAME_COLLISION'):
                self.run_folders([spec('A'), second])
        self.assertEqual(self.b.calls, [])

    def test_parent_graph_failure_precedes_all_remote_calls(self):
        specs = [spec('A', 'B'), spec('B', 'A')]
        with self.assertRaisesRegex(SafetyError, 'DEPENDENCY_UNRESOLVED'):
            self.run_folders(specs)
        self.assertEqual(self.b.calls, [])

    def test_different_id_collision_is_not_adopted(self):
        self.b.files['other'] = {'id': 'other', 'name': 'A', 'parents': ['parent']}
        with self.assertRaisesRegex(SafetyError, 'NAME_COLLISION'):
            self.run_folders([spec('A')])
        self.assertEqual(self.b.counter, 0)
        self.assertFalse(any(c[0] == 'create' for c in self.b.calls))

    def test_partial_failure_stops_new_scheduling_and_restart_reuses_allocations(self):
        self.b.failures['A'] = 'before'
        specs = [spec('A'), spec('B'), spec('C')]
        authority = self.authority(specs)
        with self.assertRaises(TransportError):
            self.run_folders(specs, workers=1, authority=authority)
        self.assertEqual(self.b.counter, 1)
        self.assertEqual([r['payload']['name'] for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_ATTEMPTED'], ['A'])
        self.b.failures.clear()
        self.j = Journal(self.tmp.name)
        result = self.run_folders(specs, authority=authority)
        self.assertEqual(len(result), 3)
        self.assertEqual(self.b.counter, 3)
        self.assertEqual(result['A']['id'], 'allocated-1')
        attempts = Counter(c[1] for c in self.b.calls if c[0] == 'create')
        self.assertEqual(attempts['allocated-1'], 2)

    def test_lost_create_response_reconciles_without_replay(self):
        self.b.failures['A'] = 'after'
        first = self.run_folders([spec('A'), spec('B')])
        self.run_folders([spec('A'), spec('B')])
        self.assertEqual(self.b.counter, 2)
        self.assertEqual(len([x for x in self.b.calls if x[0] == 'create']), 2)
        self.assertTrue(first['A']['id'] in self.b.files)

    def test_refusal_remains_blocked_on_restart(self):
        self.b.failures['A'] = 'refuse'
        with self.assertRaises(TransportError):
            self.run_folders([spec('A')])
        with self.assertRaisesRegex(SafetyError, 'PRIOR_REFUSAL'):
            self.run_folders([spec('A')])
        self.assertEqual(len([x for x in self.b.calls if x[0] == 'create']), 1)

    def test_crash_after_success_before_receipt_reconciles_same_id(self):
        append = self.j.append
        def crash(kind, *args, **kwargs):
            if kind == 'REQUIRED_FOLDER_RECEIPT':
                raise KeyboardInterrupt()
            return append(kind, *args, **kwargs)
        with patch.object(self.j, 'append', crash), self.assertRaises(KeyboardInterrupt):
            self.run_folders([spec('A')])
        self.run_folders([spec('A')])
        self.assertEqual(len([x for x in self.b.calls if x[0] == 'create']), 1)

    def test_failed_worker_drains_inflight_and_does_not_start_dependent_child(self):
        failed = threading.Event()
        append = self.j.append
        def append_and_signal(kind, *args, **kwargs):
            value = append(kind, *args, **kwargs)
            if kind == 'REQUIRED_FOLDER_RECONCILIATION_FAILED':
                failed.set()
            return value
        def hold_sibling(file_id, name, parent_id):
            if name == 'B':
                self.assertTrue(failed.wait(timeout=3))
        self.b.before_create = hold_sibling
        self.b.failures['A'] = 'before'
        with patch.object(self.j, 'append', append_and_signal), self.assertRaises(TransportError):
            self.run_folders([spec('A'), spec('B'), spec('B/C', 'B')])
        self.assertEqual(self.b.active, 0)
        self.assertTrue(any(x['name'] == 'B' for x in self.b.files.values()))
        self.assertFalse(any(x['name'] == 'C' for x in self.b.files.values()))
        self.assertTrue(any(r['kind'] == 'REQUIRED_FOLDER_VERIFIED' and r['payload']['name'] == 'B' for r in self.j.records()))
        prior_b = next(x['id'] for x in self.b.files.values() if x['name'] == 'B')
        self.b.failures.clear()
        self.b.before_create = None
        self.j = Journal(self.tmp.name)
        resumed = self.run_folders([spec('A'), spec('B'), spec('B/C', 'B')])
        self.assertEqual(resumed['B']['id'], prior_b)
        self.assertEqual(len([c for c in self.b.calls if c == ('create', prior_b)]), 1)
        self.assertEqual(resumed['B/C']['parents'], [prior_b])

    def test_parent_refusal_never_dispatches_its_child(self):
        self.b.failures['A'] = 'refuse'
        with self.assertRaises(TransportError):
            self.run_folders([spec('A'), spec('A/B', 'A')], workers=4)
        self.assertEqual(self.b.counter, 1)
        self.assertEqual([r['payload']['name'] for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_ATTEMPTED'], ['A'])

    def test_cancellation_drains_dispatched_workers_without_scheduling_more(self):
        self.b.barrier = threading.Barrier(2)
        specs = [spec('A'), spec('B'), spec('C')]
        def cancel(event):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            prepare_required_folders(self.j, self.b.factory, specs=specs, paths=self.paths,
                excluded_ids=set(), authority_ref=self.authority(specs), workers=2, report=cancel)
        self.assertEqual(self.b.active, 0)
        self.assertEqual({r['payload']['name'] for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_VERIFIED'}, {'A', 'B'})
        self.assertFalse(any(x['name'] == 'C' for x in self.b.files.values()))

    def test_sigterm_drains_inflight_restores_handler_and_does_not_schedule_more(self):
        original_handler = signal.getsignal(signal.SIGTERM)
        self.b.barrier = threading.Barrier(2)
        specs = [spec('A'), spec('B'), spec('C')]
        first = [True]
        def terminate(event):
            if first[0]:
                first[0] = False
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        with self.assertRaises(KeyboardInterrupt):
            prepare_required_folders(self.j, self.b.factory, specs=specs, paths=self.paths,
                excluded_ids=set(), authority_ref=self.authority(specs), workers=2, report=terminate)
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)
        self.assertEqual(self.b.active, 0)
        self.assertEqual({r['payload']['name'] for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_VERIFIED'}, {'A', 'B'})
        self.assertFalse(any(x['name'] == 'C' for x in self.b.files.values()))

    def test_sigint_stops_scheduling_without_interrupting_active_journal_work(self):
        previous = signal.getsignal(signal.SIGINT)
        def interrupt(event):
            # The handler itself only requests cancellation; no exception can
            # interrupt an append/evidence write at the signal boundary.
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
        with self.assertRaises(KeyboardInterrupt):
            prepare_required_folders(self.j, self.b.factory, specs=[spec('A'), spec('B')],
                paths=self.paths, excluded_ids=set(), authority_ref=self.authority([spec('A'), spec('B')]),
                workers=1, report=interrupt)
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.assertEqual([r['payload']['name'] for r in self.j.records() if r['kind'] == 'REQUIRED_FOLDER_VERIFIED'], ['A'])

    def test_sigterm_handler_restored_after_success_and_failure(self):
        previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        self.run_folders([spec('A')])
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)
        self.b.failures['B'] = 'refuse'
        with self.assertRaises(TransportError):
            self.run_folders([spec('B')])
        self.assertEqual({sig: signal.getsignal(sig) for sig in previous}, previous)

    def test_alias_parent_paths_cannot_schedule_duplicate_child_names(self):
        self.paths['alias'] = self.paths['anchor']
        with self.assertRaisesRegex(SafetyError, 'NAME_COLLISION'):
            self.run_folders([spec('A'), {**spec('B', 'alias'), 'folder_name': 'A'}])
        self.assertEqual(self.b.calls, [])

    def test_workers_are_bounded_before_any_remote_call(self):
        for count in (0, 33, True):
            with self.subTest(workers=count), self.assertRaisesRegex(SafetyError, 'WORKERS_OUT_OF_RANGE'):
                self.run_folders([spec('A')], workers=count)
        self.assertEqual(self.b.calls, [])


if __name__ == '__main__':
    unittest.main()
