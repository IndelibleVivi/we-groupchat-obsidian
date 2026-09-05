"""Repository-native regression tests; all live/process entrypoints are mocked.

These tests exercise production orchestration and the actual receipt builder /
writer against temporary files. They never open WeChat, provider, vault, or
LaunchAgent state. Run with a temporary HOME before imports, as in AGENTS.md.
"""
import ast
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import key_extractor
from scripts import catch_up_monitor as catchup
from scripts import health_check


class TargetBindingTests(unittest.TestCase):
    def test_explicit_session_target_wins_over_default_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            default = Path(directory) / 'Default.app'
            selected = Path(directory) / 'Selected WeChat.app'
            default.mkdir()
            selected.mkdir()
            with (patch.object(key_extractor, 'DEFAULT_WECHAT_APP', str(default)),
                  patch.dict(os.environ, {'WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH': str(selected)}),
                  patch.object(key_extractor.subprocess, 'run') as run):
                self.assertEqual(key_extractor.get_wechat_app_path(), str(selected.resolve()))
                run.assert_not_called()

    def test_invalid_or_empty_binding_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            default = Path(directory) / 'Default.app'
            default.mkdir()
            for value in ('', '  ', str(Path(directory) / 'Missing.app')):
                with self.subTest(value=value):
                    with (patch.object(key_extractor, 'DEFAULT_WECHAT_APP', str(default)),
                          patch.dict(os.environ, {'WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH': value}),
                          patch.object(key_extractor.subprocess, 'run') as run):
                        self.assertIsNone(key_extractor.get_wechat_app_path())
                        run.assert_not_called()

    def test_unset_binding_retains_default_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            with (patch.object(key_extractor, 'DEFAULT_WECHAT_APP', directory),
                  patch.dict(os.environ, {}, clear=True)):
                self.assertEqual(key_extractor.get_wechat_app_path(), directory)

    def test_runtime_signature_uses_selected_path_and_strict_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            selected = Path(directory) / 'Selected.app'
            selected.mkdir()
            completed = [
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 0, '', 'CodeDirectory flags=0x2(adhoc)\nSignature=adhoc\n'),
            ]
            with (patch.dict(os.environ, {'WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH': str(selected)}),
                  patch.object(key_extractor.subprocess, 'run', side_effect=completed) as run):
                self.assertTrue(key_extractor.is_wechat_signed())
                self.assertEqual(run.call_count, 2)
                self.assertIn('--verify', run.call_args_list[0].args[0])
                self.assertIn('--strict', run.call_args_list[0].args[0])
                for call in run.call_args_list:
                    self.assertEqual(call.args[0][-1], str(selected.resolve()))

    def test_invalid_binding_stops_recovery_before_process_selection(self):
        with (patch.dict(os.environ, {'WE_GROUPCHAT_OBSIDIAN_WECHAT_APP_PATH': ''}),
              patch.object(key_extractor, '_read_keys_file', return_value={}),
              patch.object(key_extractor, 'select_wechat_scan_target', return_value=None) as select):
            result = key_extractor.recover_keys()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'target_identity_unavailable')
        select.assert_not_called()


class DrainClosureTests(unittest.TestCase):
    def test_source_and_unknown_failures_do_not_burn_page_budget(self):
        for code in ('source_generation_admission_required', 'source_page_invalid',
                     'source_cursor_stalled', 'source_envelope_invalid',
                     'knowledge_recovery_unavailable', 'future_stop_state'):
            with self.subTest(code=code):
                monitor = Mock()
                monitor.check_once.return_value = {'status': code}
                result = catchup.drain_monitors(
                    [({'username': 'fixture', 'name': 'Synthetic'}, monitor)], 10, 1)
                self.assertEqual(result['blocked'], {'fixture': code})
                self.assertEqual(result['pages'], {})
                monitor.check_once.assert_called_once()

    def test_real_progress_then_verified_eof_still_completes(self):
        monitor = Mock()
        monitor.check_once.side_effect = [
            {'status': 'no_match', 'raw_message_count': 1},
            {'status': 'no_messages', 'source_eof': True},
        ]
        result = catchup.drain_monitors(
            [({'username': 'fixture', 'name': 'Synthetic'}, monitor)], 10, 1)
        self.assertEqual(result['complete'], ['fixture'])
        self.assertEqual(result['pages'], {'fixture': 1})

    def test_invalid_result_stops_and_does_not_expose_message(self):
        for payload in (None, {'status': 'PRIVATE_SENTINEL: payload'}):
            with self.subTest(payload=payload):
                monitor = Mock()
                monitor.check_once.return_value = payload
                result = catchup.drain_monitors(
                    [({'username': 'fixture', 'name': 'Synthetic'}, monitor)], 10, 1)
                self.assertEqual(result['pages'], {})
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(result))
                monitor.check_once.assert_called_once()


class PreflightOutcomeTests(unittest.TestCase):
    def test_preflight_preserves_source_failure_without_launch_or_cursor_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chat = {'username': 'fixture', 'name': 'Synthetic'}
            for reason in ('source_generation_admission_required', 'source_page_invalid', '',
                           'PRIVATE_SENTINEL: payload'):
                with self.subTest(reason=reason), ExitStack() as stack:
                    audit = [{'username': 'fixture', 'name': 'Synthetic', 'checkpoint': 10,
                              'count': None, 'capped': False, 'reason': reason}]
                    receipts = []
                    real_write = catchup.write_reconciliation_receipt
                    def write(receipt):
                        path = real_write(receipt, receipts_dir=root / 'receipts')
                        receipts.append(json.loads(path.read_text(encoding='utf-8')))
                        return path
                    stack.enter_context(patch.object(catchup, 'audit_pending', return_value=audit))
                    stack.enter_context(patch.object(catchup, '_checkpoint_for_chat', return_value=10))
                    stack.enter_context(patch.object(catchup, 'state_file_for_chat', return_value=str(root / 'state.json')))
                    stack.enter_context(patch.object(catchup, 'write_reconciliation_receipt', side_effect=write))
                    launch = stack.enter_context(patch.object(catchup, 'launch_agent_report'))
                    drain = stack.enter_context(patch.object(catchup, 'drain_monitors'))
                    output = io.StringIO()
                    stack.enter_context(redirect_stdout(output))
                    code = catchup.apply_catch_up({}, [chat], object(), SimpleNamespace(audit_limit=100))
                    launch.assert_not_called()
                    drain.assert_not_called()
                    expected = reason or 'missing_checkpoint'
                    if ':' in expected:
                        expected = 'unknown'
                    self.assertEqual(code, 2)
                    self.assertEqual(receipts[-1]['state'], 'failed')
                    self.assertEqual(receipts[-1]['chats'][0]['blocked_reason'], expected)
                    self.assertEqual(receipts[-1]['chats'][0]['pages'], 0)
                    self.assertIn(expected, output.getvalue())
                    self.assertNotIn('PRIVATE_SENTINEL', output.getvalue())


class ReceiptPublicationTests(unittest.TestCase):
    def run_case(self, *, loaded=True, failed_writes=(), post_write_failures=(), restore_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chat = {'username': 'fixture', 'name': 'Synthetic'}
            audit = [{'username': 'fixture', 'name': 'Synthetic', 'checkpoint': 10,
                      'count': 1, 'capped': False}]
            drained = {'complete': ['fixture'], 'blocked': {}, 'pages': {'fixture': 1},
                       'statuses': {'no_match': 1}, 'affected_dates': [],
                       'per_chat': {'fixture': {'pages': 1, 'statuses': {'no_match': 1},
                           'event_ids': [], 'affected_dates': [], 'outcome': 'complete',
                           'blocked_reason': ''}}}
            lock_state = {'held': False}
            lock = Mock()
            def acquire():
                lock_state['held'] = True
                return lock
            def release():
                lock_state['held'] = False
            lock.acquire.side_effect = acquire
            lock.release.side_effect = release
            attempts, persisted = [], []
            real_write = catchup.write_reconciliation_receipt
            def write(receipt):
                attempts.append({'held': lock_state['held'], 'receipt': dict(receipt)})
                if len(attempts) in failed_writes:
                    raise OSError('PRIVATE_SENTINEL')
                path = real_write(receipt, receipts_dir=root / 'receipts')
                persisted.append(json.loads(path.read_text(encoding='utf-8')))
                if len(attempts) in post_write_failures:
                    raise OSError('PRIVATE_SENTINEL after publication')
                return path
            stdout = io.StringIO()
            with ExitStack() as stack:
                patches = {
                    'audit_pending': {'return_value': audit},
                    '_print_audit': {},
                    'launch_agent_report': {'return_value': (SimpleNamespace(label='fixture'), SimpleNamespace(loaded=loaded))},
                    '_stop_launch_agent': {},
                    '_restore_launch_agent': {'side_effect': OSError('restore failed')} if restore_fails else {},
                    'AppInstanceLock': {'return_value': lock},
                    'backup_runtime_state': {'return_value': root / 'backup'},
                    'TopicMonitor': {},
                    'drain_monitors': {'return_value': drained},
                    'rebuild_projections': {'return_value': None},
                    'validate_knowledge_db': {'return_value': {'ok': True}},
                    '_checkpoint_for_chat': {'return_value': 11},
                    'state_file_for_chat': {'return_value': str(root / 'fixture.json')},
                    'write_reconciliation_receipt': {'side_effect': write},
                }
                mocked = {name: stack.enter_context(patch.object(catchup, name, **kwargs))
                          for name, kwargs in patches.items()}
                stack.enter_context(redirect_stdout(stdout))
                code = catchup.apply_catch_up({}, [chat], object(), SimpleNamespace(
                    audit_limit=100, max_pages_per_chat=10, max_minutes=1))
                self.assertEqual(mocked['_restore_launch_agent'].call_count, 1 if loaded else 0)
            lock.release.assert_called_once()
            self.assertTrue(attempts[0]['held'])
            for attempt in attempts[1:]:
                self.assertFalse(attempt['held'])
            return code, attempts, persisted, stdout.getvalue()

    def test_provisional_failure_cannot_be_cleared_by_final_success(self):
        code, attempts, persisted, output = self.run_case(failed_writes={1})
        self.assertEqual(code, 1)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(persisted[-1]['state'], 'complete')
        self.assertEqual(persisted[-1]['transaction_error'], 'provisional_receipt_failed')
        self.assertEqual(persisted[-1]['outcome'], 'provisional_receipt_failed')
        self.assertFalse(persisted[-1]['resume_supported'])
        self.assertNotIn('PRIVATE_SENTINEL', output)

    def test_provisional_error_after_visible_publication_is_still_sticky(self):
        code, attempts, persisted, output = self.run_case(post_write_failures={1})
        self.assertEqual(code, 1)
        self.assertEqual(len(persisted), 2)
        self.assertNotEqual(persisted[-1]['state'], 'complete')
        self.assertEqual(persisted[-1]['transaction_error'], 'provisional_receipt_failed')
        self.assertNotIn('PRIVATE_SENTINEL', output)

    def test_unloaded_agent_also_records_sticky_publication_failure(self):
        code, attempts, persisted, output = self.run_case(loaded=False, failed_writes={1})
        self.assertEqual(code, 1)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(persisted[-1]['state'], 'complete')
        self.assertEqual(persisted[-1]['transaction_error'], 'provisional_receipt_failed')
        self.assertNotIn('state: complete', output)

    def test_total_publication_failure_never_prints_complete(self):
        code, attempts, persisted, output = self.run_case(failed_writes={1, 2})
        self.assertEqual(code, 1)
        self.assertEqual(persisted, [])
        self.assertNotIn('state: complete', output)

    def test_final_failure_keeps_durable_provisional_receipt(self):
        code, attempts, persisted, output = self.run_case(failed_writes={2})
        self.assertEqual(code, 1)
        self.assertEqual(persisted[-1]['finalization'], 'restore_pending')
        self.assertNotEqual(persisted[-1]['state'], 'complete')
        self.assertNotIn('state: complete', output)

    def test_success_still_finalizes_the_same_run(self):
        code, attempts, persisted, output = self.run_case()
        self.assertEqual(code, 0)
        self.assertEqual(persisted[-1]['state'], 'complete')
        self.assertEqual(persisted[0]['run_id'], persisted[-1]['run_id'])
        self.assertEqual(persisted[0]['finalization'], 'restore_pending')
        self.assertEqual(persisted[-1]['finalization'], 'finalized')

    def test_restore_failure_does_not_clear_provisional_failure(self):
        code, attempts, persisted, output = self.run_case(failed_writes={1}, restore_fails=True)
        self.assertEqual(code, 1)
        self.assertEqual(persisted[-1]['transaction_error'], 'provisional_receipt_failed')
        self.assertEqual(persisted[-1]['outcome'], 'launch_agent_restore_failed')


class HealthOutcomeTests(unittest.TestCase):
    def test_latest_new_stop_does_not_fall_back_to_old_success(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / 'synthetic.log'
            for status in ('source_generation_admission_required', 'knowledge_recovery_unavailable',
                           'future_stop_state', 'x' * 81):
                with self.subTest(status=status):
                    log.write_text('[monitor] no_messages: old success\n'
                                   f'[monitor] {status}: PRIVATE_SENTINEL\n', encoding='utf-8')
                    actual = health_check.latest_monitor_runtime_result(log)
                    self.assertEqual(actual, status if len(status) <= 80 else 'unknown')
                    self.assertNotIn('PRIVATE_SENTINEL', actual)

    def test_health_has_no_protected_source_scanner_call(self):
        source = Path(health_check.__file__).read_text(encoding='utf-8')
        tree = ast.parse(source)
        forbidden = {'check_new_databases', '_required_pages', 'recover_keys', 'extract_keys'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                called = getattr(node.func, 'id', getattr(node.func, 'attr', ''))
                self.assertNotIn(called, forbidden)


if __name__ == '__main__':
    unittest.main()
