import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from core.semantic.rebuild import RebuildJournal, RebuildError, SCHEMA, canonical_json, fingerprint, write_json
from core.semantic.rebuild_payloads import STORAGE_SCHEMA, REF_KEY, opened


def inline_fixture(journal, *, records=2, width=1024):
    inventory = {"records": [{"text": '中\\文\n" 🐈' + 'x' * width}] * records}
    plan = {"schema": SCHEMA, "run_id": journal.run_id, "generation_id": "gen-1",
            "legacy_inventory": inventory}
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    digest = hashlib.sha256()
    for chunk in encoder.iterencode(plan):
        digest.update(chunk.encode())
    frozen = [{"receipt_id": "freeze-" + str(i), "legacy_inventory": inventory,
               "files": [{"path": "synthetic", "sha256": "a" * 64}]} for i in (1, 2)]
    document = {"schema": SCHEMA, "run_id": journal.run_id, "plan": plan,
                "plan_fingerprint": digest.hexdigest(), "revision": 8, "phase": "freeze-2",
                "errors": [], "runtime": {}, "freeze_receipts": frozen,
                "steps": {f["receipt_id"]: f for f in frozen}}
    journal.run_root.mkdir(parents=True, mode=0o700)
    with open(journal.path, 'w', encoding='utf-8') as out:
        for chunk in encoder.iterencode(document):
            out.write(chunk)
        out.write('\n')
    return document


class PayloadJournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.journal = RebuildJournal(self.root, "run-1")

    def convert(self):
        with self.journal.locked():
            return self.journal.read()

    def test_old_conversion_keeps_exact_original_and_logical_evidence(self):
        original = inline_fixture(self.journal)
        raw = self.journal.path.read_bytes()
        with self.assertRaisesRegex(RebuildError, 'upgrade_required'):
            self.journal.read()
        converted = self.convert()
        self.assertEqual(converted['schema'], STORAGE_SCHEMA)
        self.assertEqual(converted['original_plan_fingerprint'], original['plan_fingerprint'])
        self.assertEqual((self.journal.run_root / 'inline-v1.json').read_bytes(), raw)
        details = self.journal.payloads.expand(self.journal.read())
        for key in ('run_id', 'plan', 'freeze_receipts', 'steps', 'phase', 'revision', 'errors'):
            self.assertEqual(details[key], original[key])
        self.assertLess(self.journal.path.stat().st_size, 4096)
        self.assertEqual(len(list((self.journal.run_root / 'payloads').glob('*.json'))), 3)

    def test_phase_updates_do_not_read_or_hash_immutable_payloads_again(self):
        inline_fixture(self.journal, records=32, width=65536)
        self.convert()
        journal = RebuildJournal(self.root, 'run-1')
        journal.read()  # Verify immutable identities once for this reader.
        with patch('core.semantic.rebuild_payloads.opened', wraps=opened) as reads:
            for i in range(20):
                journal.update(lambda row: row.update(phase='test-' + str(i)))
                journal.read()
            self.assertEqual(reads.call_count, 0)
        self.assertLess(journal.path.stat().st_size, 4096)

    def test_changed_payload_is_rejected_even_on_warm_reader(self):
        inline_fixture(self.journal)
        run = self.convert()
        reference = run['plan']['legacy_inventory']
        path = self.journal.payloads.path(reference)
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b'x', b'y', 1))
        with self.assertRaisesRegex(RebuildError, 'payload_changed'):
            self.journal.read()

    def test_references_reject_different_run_missing_and_symlink(self):
        inline_fixture(self.journal)
        run = self.convert()
        reference = run['plan']['legacy_inventory']
        with self.assertRaisesRegex(RebuildError, 'binding_invalid'):
            self.journal.payloads.get(dict(reference, run_id='other'))
        path = self.journal.payloads.path(reference)
        retained = path.with_suffix('.retained')
        path.rename(retained)
        with self.assertRaisesRegex(RebuildError, 'payload_missing'):
            self.journal.read()
        path.symlink_to(retained)
        with self.assertRaisesRegex(RebuildError, 'symlink'):
            self.journal.read()

    def test_interrupted_publish_retries_without_replacing_original_backup(self):
        inline_fixture(self.journal)
        raw = self.journal.path.read_bytes()
        with patch('core.semantic.rebuild.write_json', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.convert()
        self.assertEqual(self.journal.path.read_bytes(), raw)
        self.assertEqual((self.journal.run_root / 'inline-v1.json').read_bytes(), raw)
        self.convert()
        self.assertEqual(self.journal.read()['phase'], 'freeze-2')

    def test_wrong_old_plan_fingerprint_and_truncated_json_preserve_original(self):
        document = inline_fixture(self.journal)
        document['plan']['generation_id'] = 'wrong'
        write_json(self.journal.path, document)
        raw = self.journal.path.read_bytes()
        with self.assertRaisesRegex(RebuildError, 'run_invalid'):
            self.convert()
        self.assertEqual(self.journal.path.read_bytes(), raw)
        self.journal.path.write_bytes(raw[:-100])
        with self.assertRaisesRegex(RebuildError, 'corrupt_json'):
            self.convert()
        self.assertEqual(self.journal.path.read_bytes(), raw[:-100])

    def test_step_retry_compares_immutable_receipt_identity(self):
        self.journal.create({'schema': SCHEMA, 'run_id': 'run-1', 'generation_id': 'gen-1'})
        evidence = {'receipt_id': 'same', 'details': 'retained'}
        self.journal.step('phase', evidence)
        self.journal.step('phase', evidence)
        with self.assertRaisesRegex(RebuildError, 'receipt_conflict'):
            self.journal.step('phase', dict(evidence, details='changed'))

    def test_scalar_and_metadata_limits_leave_inline_journal_unchanged(self):
        inline_fixture(self.journal)
        raw = self.journal.path.read_bytes()
        for setting, limit, code in (('MAX_SCALAR', 512, 'scalar_limit'),
                                     ('MAX_JOURNAL', 128, 'metadata_limit')):
            with patch('core.semantic.rebuild_payloads.' + setting, limit):
                with self.assertRaisesRegex(RebuildError, code):
                    self.convert()
            self.assertEqual(self.journal.path.read_bytes(), raw)

    def test_compact_command_does_not_construct_source_or_provider(self):
        from core.semantic.rebuild import RebuildCoordinator
        from unittest.mock import Mock
        inline_fixture(self.journal)
        config = Mock()
        config.inspect.side_effect = AssertionError('config not needed for local storage conversion')
        provider, source, managed = Mock(), Mock(), Mock()
        coordinator = RebuildCoordinator(self.root, config, roles_factory=provider,
                                         source_factory=source, managed_runtime=managed)
        result = coordinator.compact_journal('run-1')
        self.assertEqual(result['state'], 'journal_ready')
        source.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(managed.mock_calls, [])

    def test_large_inline_conversion_in_fresh_process_has_bounded_peak_memory(self):
        # 160 MiB on disk, generated in the parent with a reused 1 MiB scalar.
        # Child RSS excludes fixture construction and never loads the document.
        inline_fixture(self.journal, records=32, width=1024 * 1024)
        size = self.journal.path.stat().st_size
        code = '''
import json, resource, sys, tracemalloc
from core.semantic.rebuild import RebuildJournal
baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
tracemalloc.start()
j = RebuildJournal(sys.argv[1], 'run-1')
with j.locked():
    r = j.read()
    for i in range(10):
        j.update(lambda value: value.update(phase='bounded'))
peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform != 'darwin': peak *= 1024
print(json.dumps({'baseline_rss': baseline, 'peak_bytes': peak, 'traced_peak': tracemalloc.get_traced_memory()[1], 'journal_bytes': j.path.stat().st_size}))
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                capture_output=True, text=True, timeout=120, close_fds=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertGreater(size, 150 * 1024 * 1024)
        self.assertLess(evidence['peak_bytes'], 512 * 1024 * 1024)
        self.assertLess(evidence['traced_peak'], 64 * 1024 * 1024)
        self.assertLess(evidence['journal_bytes'], 4096)
        print('bounded journal conversion:', json.dumps(dict(evidence, input_bytes=size)))
        larger = RebuildJournal(self.root / 'larger', 'run-1')
        inline_fixture(larger, records=64, width=1024 * 1024)
        second = subprocess.run([sys.executable, '-c', code, str(self.root / 'larger')],
                                capture_output=True, text=True, timeout=120, close_fds=False)
        self.assertEqual(second.returncode, 0, second.stderr)
        larger_evidence = json.loads(second.stdout)
        self.assertLess(larger_evidence['peak_bytes'], 512 * 1024 * 1024)
        # macOS allocator resident high-water varies even when live Python
        # allocation stays bounded. Check both absolute RSS and actual retained
        # allocation, not an arbitrary delta between two allocator runs.
        self.assertLess(larger_evidence['traced_peak'], 64 * 1024 * 1024)
        print('double-input journal conversion:', json.dumps(dict(larger_evidence, input_bytes=(larger.run_root / 'inline-v1.json').stat().st_size)))


if __name__ == '__main__':
    unittest.main()
