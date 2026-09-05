"""Runs both in the repository and in the explicitly isolated offline harness."""
import contextlib
import hashlib
import hmac
import io
import json
import multiprocessing
import os
from pathlib import Path
import plistlib
import struct
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch

from core import key_extractor as k

KEY_A='12'*32
KEY_B='ab'*32
BAD='ef'*32
DB_A='message/message_0.db'
DB_B='message/message_1.db'


def write_page(root, rel, key=KEY_A, salt=b'0123456789abcdef'):
    path=Path(root)/rel; path.parent.mkdir(parents=True, exist_ok=True)
    page=bytearray(4096); page[:16]=salt
    mac_key=hashlib.pbkdf2_hmac('sha512',bytes.fromhex(key),bytes(x^0x3a for x in salt),2,dklen=32)
    hm=hmac.new(mac_key,page[16:4032],hashlib.sha512)
    hm.update(struct.pack('<I',1)); page[-64:]=hm.digest()
    path.write_bytes(page)
    return path


def target():
    if hasattr(k,'ScanTarget'):
        return k.ScanTarget(42,'/fixture/WeChat.app','/fixture/WeChat',1.,('4.1.11','269136','arm64'),(1,2,3,4))
    return object()


class RecoveryFixtures(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.db=self.root/'db_storage'; self.db.mkdir()
        self.cache=self.root/'all_keys.json'
        self.stack=contextlib.ExitStack(); self.addCleanup(self.stack.close); self.addCleanup(self.tmp.cleanup)
        for name,value in [('DATA_DIR',str(self.root)),('KEYS_FILE',str(self.cache))]:
            self.stack.enter_context(patch.object(k,name,value))
        self.stack.enter_context(patch.object(k,'get_wechat_pid',return_value=42))
        self.stack.enter_context(patch.object(
            k,
            'compile_scanner',
            return_value=types.SimpleNamespace(binary_path='/fixture/verified-scanner'),
        ))
        self.stack.enter_context(patch.object(k,'get_wechat_scan_target',return_value=target(),create=True))
        self.stack.enter_context(patch.object(k,'select_wechat_scan_target',return_value=target(),create=True))
        self.stack.enter_context(patch.object(k,'_protected_key_memory_mask',return_value=b'\0'*32))
        self.stack.enter_context(patch.object(k,'load_config',return_value={'db_dir':str(self.db)}))

    def cached(self,entries=None):
        entries=entries or {DB_A:{'enc_key':KEY_A}}
        self.cache.write_text(json.dumps(entries))
        return self.cache.read_bytes()

    def scanner(self, mapping=None, output='', code=0, stderr=''):
        mapping=mapping or {}
        def run(args,**kwargs):
            (Path(kwargs['cwd'])/'all_keys.json').write_text(json.dumps(mapping))
            return subprocess.CompletedProcess(args,code,output,stderr)
        return patch.object(k.subprocess,'run',side_effect=run)



class RecoveryContract(RecoveryFixtures):
    def test_unverified_staged_json_never_enters_canonical_cache(self):
        before=self.cached()
        write_page(self.db,DB_A)
        with self.scanner({DB_A:{'enc_key':BAD}}):
            result=k.extract_keys()
        self.assertIsNone(result)
        self.assertEqual(self.cache.read_bytes(),before)

    def test_short_page_does_not_promote_staged_wrong_key(self):
        before=self.cached(); p=write_page(self.db,DB_A); p.write_bytes(b'x'*16)
        with self.scanner({DB_A:{'enc_key':BAD}},f'WGO_KEY {BAD} -\n'):
            result=k.extract_keys()
        self.assertIsNone(result); self.assertEqual(self.cache.read_bytes(),before)

    def test_empty_scan_keeps_bytes_but_is_not_refresh_success(self):
        before=self.cached(); write_page(self.db,DB_A)
        with self.scanner(): result=k.extract_keys()
        self.assertIsNone(result); self.assertEqual(self.cache.read_bytes(),before)

    def test_timeout_keeps_cache_but_is_not_refresh_success(self):
        before=self.cached(); write_page(self.db,DB_A)
        with patch.object(k.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',60)):
            result=k.extract_keys()
        self.assertIsNone(result); self.assertEqual(self.cache.read_bytes(),before)

    def test_generic_scanner_error_does_not_prompt_for_admin(self):
        self.cached(); write_page(self.db,DB_A)
        with self.scanner(code=1,stderr='fixture generic failure') as run:
            result=k.extract_keys()
        self.assertIsNone(result); self.assertEqual(run.call_count,1)

    def test_unattributed_scanner_success_is_rejected_before_execution(self):
        before=self.cached(); write_page(self.db,DB_A)
        with patch.object(k,'compile_scanner',return_value=True):
            with self.scanner({},f'WGO_KEY {KEY_A} -\n') as run:
                result=k.recover_keys()
        self.assertEqual(result.reason,'scanner_identity_unavailable')
        self.assertEqual(run.call_count,0)
        self.assertEqual(self.cache.read_bytes(),before)

    def test_invalid_existing_key_is_reported_missing(self):
        write_page(self.db,DB_A)
        self.assertEqual(k.check_new_databases(str(self.db),{DB_A:{'enc_key':BAD}}),[DB_A])

    def test_truncated_current_source_is_not_reported_healthy(self):
        p=write_page(self.db,DB_A); p.write_bytes(b'x'*16)
        self.assertIn(DB_A,k.check_new_databases(str(self.db),{DB_A:{'enc_key':KEY_A}}))

    def test_symlink_source_is_not_reported_healthy(self):
        p=write_page(self.db,DB_A); other=self.root/'outside.db'; p.rename(other); p.symlink_to(other)
        self.assertIn(DB_A,k.check_new_databases(str(self.db),{DB_A:{'enc_key':KEY_A}}))

    def test_corrupt_cache_is_not_silently_replaced(self):
        self.cache.write_bytes(b'{broken'); before=self.cache.read_bytes(); write_page(self.db,DB_A)
        with self.scanner({DB_A:{'enc_key':KEY_A}},f'WGO_KEY {KEY_A} -\n'):
            result=k.extract_keys()
        self.assertIsNone(result); self.assertEqual(self.cache.read_bytes(),before)

    def test_wrong_top_level_cache_type_fails_without_exception(self):
        self.cache.write_text('[]')
        self.assertIsNone(k.get_cached_keys())

    def test_successful_current_hmac_round_trip_is_private(self):
        write_page(self.db,DB_A)
        with self.scanner({},f'WGO_KEY {KEY_A} -\n') as run:
            result=k.extract_keys()
        self.assertEqual(result,{DB_A:{'enc_key':KEY_A}})
        self.assertEqual(run.call_args.args[0][0], '/fixture/verified-scanner')
        self.assertEqual(self.cache.stat().st_mode&0o777,0o600)
        self.assertFalse((self.root/'extract_keys.log').exists())

    def test_both_same_salt_candidates_choose_verified_key(self):
        write_page(self.db,DB_A)
        salt=b'0123456789abcdef'.hex()
        with self.scanner({DB_A:{'enc_key':BAD}},f'WGO_KEY {KEY_A} {salt}\nWGO_KEY {BAD} {salt}\n'):
            result=k.extract_keys()
        self.assertEqual(result,{DB_A:{'enc_key':KEY_A}})

    def test_partial_scan_preserves_unobserved_historical_cache(self):
        self.cached({DB_B:{'enc_key':KEY_B}}); write_page(self.db,DB_A)
        with self.scanner({},f'WGO_KEY {KEY_A} -\n'):
            k.extract_keys()
        saved=json.loads(self.cache.read_text())
        self.assertEqual(saved,{DB_A:{'enc_key':KEY_A},DB_B:{'enc_key':KEY_B}})

    @unittest.skipUnless(os.name=='posix','POSIX process test; native Windows is a separate gate')
    def test_overlapping_process_scans_preserve_both_discoveries(self):
        write_page(self.db,DB_A); write_page(self.db,DB_B,KEY_B,salt=b'fedcba9876543210')
        self.cache.write_text('{}')
        ctx=multiprocessing.get_context('fork'); barrier=ctx.Barrier(2)
        def worker(rel,key):
            def run(args,**kwargs):
                (Path(kwargs['cwd'])/'all_keys.json').write_text(json.dumps({rel:{'enc_key':key}}))
                barrier.wait(timeout=10)
                return subprocess.CompletedProcess(args,0,f'WGO_KEY {key} -\n','')
            with patch.object(k.subprocess,'run',side_effect=run):
                with contextlib.redirect_stdout(io.StringIO()): k.extract_keys()
        children=[ctx.Process(target=worker,args=(DB_A,KEY_A)),ctx.Process(target=worker,args=(DB_B,KEY_B))]
        for p in children:p.start()
        try:
            for p in children:p.join(timeout=15)
            self.assertEqual([p.exitcode for p in children],[0,0])
            self.assertEqual(json.loads(self.cache.read_text()),{DB_A:{'enc_key':KEY_A},DB_B:{'enc_key':KEY_B}})
        finally:
            for p in children:
                if p.is_alive():p.terminate(); p.join()


@unittest.skipUnless(hasattr(k,'KeyRecoveryResult'),'candidate-only new contract')
class NewBoundaryTests(RecoveryFixtures):
    def test_direct_publication_reverifies_candidate(self):
        before=self.cached(); write_page(self.db,DB_A)
        _merged,fresh=k._publish_verified_keys(str(self.db),{DB_A:{'enc_key':BAD}})
        self.assertEqual(fresh,{}); self.assertEqual(self.cache.read_bytes(),before)

    def test_cache_json_duplicates_are_not_silently_collapsed(self):
        self.cache.write_text('{"x":{"enc_key":"'+KEY_A+'"},"x":{"enc_key":"'+BAD+'"}}')
        self.assertIsNone(k.get_cached_keys())

    def test_result_repr_never_contains_keys(self):
        value=k.KeyRecoveryResult('fresh_verified',{DB_A:{'enc_key':KEY_A}},1)
        self.assertNotIn(KEY_A,repr(value)); self.assertNotIn(DB_A,repr(value))
        partial = k.KeyRecoveryResult('partial', verified_count=1, missing_databases=(DB_A,))
        self.assertNotIn(DB_A, repr(partial))

    def test_target_change_drops_scan_without_cache_write(self):
        before=self.cached(); write_page(self.db,DB_A)
        original=target()
        with patch.object(k,'select_wechat_scan_target',side_effect=[original,original,None]):
            with self.scanner({},f'WGO_KEY {KEY_A} -\n'): result=k.recover_keys()
        self.assertFalse(result.ok); self.assertEqual(result.reason,'target_changed')
        self.assertEqual(self.cache.read_bytes(),before)

    def test_directory_fsync_follows_atomic_publication(self):
        with patch.object(k.os,'fsync',wraps=os.fsync) as fsync:
            k._atomic_write_keys(str(self.cache),{DB_A:{'enc_key':KEY_A}})
        self.assertEqual(fsync.call_count,2)

    def test_cache_symlink_is_left_untouched(self):
        target_path=self.root/'victim.json'; target_path.write_text(json.dumps({DB_A:{'enc_key':KEY_A}}))
        self.cache.symlink_to(target_path); before=target_path.read_bytes(); write_page(self.db,DB_A)
        with self.scanner({},f'WGO_KEY {KEY_A} -\n'): result=k.recover_keys()
        self.assertFalse(result.ok); self.assertTrue(self.cache.is_symlink()); self.assertEqual(target_path.read_bytes(),before)

    def test_write_failure_preserves_old_cache(self):
        before=self.cached(); write_page(self.db,DB_B,KEY_B)
        with patch.object(k.os,'replace',side_effect=OSError('fixture')):
            with self.scanner({},f'WGO_KEY {KEY_B} -\n'): result=k.recover_keys()
        self.assertFalse(result.ok); self.assertEqual(self.cache.read_bytes(),before)

    def test_missing_key_produces_partial_not_success(self):
        write_page(self.db,DB_A); write_page(self.db,DB_B,KEY_B)
        with self.scanner({},f'WGO_KEY {KEY_A} -\n'): result=k.recover_keys()
        self.assertEqual(result.status,'partial'); self.assertFalse(result.ok); self.assertIn(DB_B,result.missing_databases)

@unittest.skipUnless(hasattr(k, 'KeyRecoveryResult'), 'candidate-only new contract')
class FinalAdversarialTests(RecoveryFixtures):
    def test_configuration_exception_is_structured(self):
        with patch.object(k, 'load_config', side_effect=OSError('private/path')):
            result = k.recover_keys()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'recovery_setup_failed')
        self.assertNotIn('private/path', repr(result))

    def test_page_vanishes_after_publication_cannot_claim_success(self):
        page = write_page(self.db, DB_A)
        original = k._publish_verified_keys
        def publish(*args):
            result = original(*args)
            page.unlink()
            return result
        with patch.object(k, '_publish_verified_keys', side_effect=publish):
            with self.scanner({}, f'WGO_KEY {KEY_A} -\n'):
                result = k.recover_keys()
        self.assertFalse(result.ok)
        self.assertEqual(result.reason, 'source_changed_before_publication')

    def test_page_changes_key_after_match_cannot_publish_stale_candidate(self):
        before = self.cached()
        write_page(self.db, DB_A)
        original = k._publish_verified_keys
        def publish(*args):
            write_page(self.db, DB_A, KEY_B)
            return original(*args)
        with patch.object(k, '_publish_verified_keys', side_effect=publish):
            with self.scanner({}, f'WGO_KEY {KEY_A} -\n'):
                result = k.recover_keys()
        self.assertFalse(result.ok)
        self.assertEqual(self.cache.read_bytes(), before)

    def test_unknown_profile_with_no_verified_candidate_preserves_cache(self):
        before = self.cached()
        write_page(self.db, DB_A)
        with patch.object(k, '_protected_key_memory_mask', return_value=None):
            with self.scanner():
                result = k.recover_keys()
        self.assertEqual(result.status, 'unsupported_build')
        self.assertEqual(self.cache.read_bytes(), before)

    def test_unknown_profile_can_still_verify_a_legacy_literal_without_mask(self):
        write_page(self.db, DB_A)
        with patch.object(k, '_protected_key_memory_mask', return_value=None):
            with self.scanner({}, f'WGO_KEY {KEY_A} -\n') as run:
                result = k.recover_keys()
        self.assertTrue(result.ok)
        self.assertEqual(len(run.call_args.args[0]), 4)

    def test_sudo_scanner_error_does_not_trigger_administrator_dialog(self):
        self.cached()
        calls = [
            subprocess.CompletedProcess([], 1, '', 'task_for_pid failed: 5'),
            subprocess.CompletedProcess([], 1, '', 'scanner format error'),
        ]
        with patch.object(k.subprocess, 'run', side_effect=calls) as run:
            result = k.recover_keys()
        self.assertFalse(result.ok)
        self.assertEqual(run.call_count, 2)

    def test_cache_traversal_name_is_rejected_without_mutation(self):
        before = self.cached({'../message/outside.db': {'enc_key': KEY_A}})
        self.assertIsNone(k.get_cached_keys())
        self.assertEqual(self.cache.read_bytes(), before)


@unittest.skipUnless(hasattr(k, 'ScanTarget'), 'candidate-only target adapter')
class TargetIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Path(self.tmp.name) / 'Selected WeChat.app'
        contents = self.app / 'Contents'
        (contents / 'MacOS').mkdir(parents=True)
        self.executable = contents / 'MacOS' / 'WeChat'
        self.executable.write_bytes(b'synthetic executable identity only')
        self.info = {
            'CFBundleIdentifier': 'com.tencent.xinWeChat',
            'CFBundleExecutable': 'WeChat',
            'CFBundleShortVersionString': '4.1.11',
            'CFBundleVersion': '269136',
        }
        self.plist = contents / 'Info.plist'
        self.plist.write_bytes(plistlib.dumps(self.info))
        self.running = types.SimpleNamespace(
            isTerminated=lambda: False,
            bundleURL=lambda: types.SimpleNamespace(path=lambda: str(self.app)),
            executableURL=lambda: types.SimpleNamespace(path=lambda: str(self.executable)),
            launchDate=lambda: types.SimpleNamespace(timeIntervalSince1970=lambda: 123.0),
            bundleIdentifier=lambda: 'com.tencent.xinWeChat',
            executableArchitecture=lambda: 0x0100000C,
        )

    def resolve(self):
        import sys
        bridge = types.ModuleType('AppKit')
        bridge.NSRunningApplication = types.SimpleNamespace(
            runningApplicationWithProcessIdentifier_=lambda pid: self.running if pid == 42 else None
        )
        with patch.dict(sys.modules, {'AppKit': bridge}):
            return k.get_wechat_scan_target(42)

    def test_selected_running_app_wins_over_default_install(self):
        with patch.object(k, 'get_wechat_app_path', side_effect=AssertionError('default app must not be queried')):
            result = self.resolve()
        self.assertEqual(result.app_path, os.path.realpath(self.app))
        self.assertEqual(result.pid, 42)
        self.assertEqual(result.build_identity, ('4.1.11', '269136', 'arm64'))

    def test_target_cpu_does_not_inherit_python_rosetta_cpu(self):
        with patch.object(k.platform, 'machine', return_value='x86_64'):
            result = self.resolve()
        self.assertEqual(result.build_identity[2], 'arm64')

    def test_missing_launch_identity_fails_closed(self):
        self.running.launchDate = lambda: None
        self.assertIsNone(self.resolve())

    def test_terminated_target_fails_closed(self):
        self.running.isTerminated = lambda: True
        self.assertIsNone(self.resolve())

    def test_wrong_running_bundle_id_fails_closed(self):
        self.running.bundleIdentifier = lambda: 'com.example.helper'
        self.assertIsNone(self.resolve())

    def test_different_executable_fails_closed(self):
        self.running.executableURL = lambda: types.SimpleNamespace(path=lambda: '/other/WeChat')
        self.assertIsNone(self.resolve())

    def test_scan_selection_rejects_ambiguous_running_copies(self):
        first = target()
        second = k.ScanTarget(
            43,
            '/fixture/Other WeChat.app',
            '/fixture/Other WeChat.app/Contents/MacOS/WeChat',
            2.0,
            ('4.1.11', '269136', 'arm64'),
            (1, 3, 3, 4),
        )
        with patch.object(k, 'get_wechat_scan_targets', return_value=(first, second), create=True):
            self.assertIsNone(k.select_wechat_scan_target())

    def test_scan_selection_honors_exact_canonical_app_path(self):
        first = target()
        second = k.ScanTarget(
            43,
            '/fixture/Other WeChat.app',
            '/fixture/Other WeChat.app/Contents/MacOS/WeChat',
            2.0,
            ('4.1.11', '269136', 'arm64'),
            (1, 3, 3, 4),
        )
        with patch.object(k, 'get_wechat_scan_targets', return_value=(first, second), create=True):
            selected = k.select_wechat_scan_target('/fixture/Other WeChat.app')
        self.assertEqual(selected, second)


class ScannerBinaryIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'find_keys_macos.c'
        self.pointer = self.root / 'scanner-current.json'
        self.source.write_text('int main(void) { return 0; }\n')
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(k, 'DATA_DIR', str(self.root)))
        self.stack.enter_context(patch.object(k, 'C_SOURCE', str(self.source)))
        self.stack.enter_context(patch.object(
            k,
            '_compiler_identity',
            return_value={
                'path': '/fixture/cc',
                'version': 'Fixture clang 1.0',
                'target': 'arm64-apple-darwin',
            },
            create=True,
        ))

    def compiler(self, payload=b'compiled scanner', returncode=0):
        def run(args, **_kwargs):
            if returncode == 0:
                output = Path(args[args.index('-o') + 1])
                output.write_bytes(payload)
            return subprocess.CompletedProcess(args, returncode, '', 'fixture compile error')
        return patch.object(k.subprocess, 'run', side_effect=run)

    def test_newer_mtime_without_identity_receipt_is_rebuilt(self):
        legacy = self.root / 'find_keys_macos'
        legacy.write_bytes(b'unattributed old binary')
        os.utime(legacy, (4_000_000_000, 4_000_000_000))

        with self.compiler() as run:
            build = k.compile_scanner('arm64')

        self.assertIsNotNone(build)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(Path(build.binary_path).read_bytes(), b'compiled scanner')
        receipt = json.loads(Path(build.receipt_path).read_text())
        self.assertEqual(receipt['source_sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(receipt['binary_sha256'], hashlib.sha256(Path(build.binary_path).read_bytes()).hexdigest())
        self.assertEqual(receipt['target_architecture'], 'arm64')
        self.assertEqual(json.loads(self.pointer.read_text())['build_directory'], Path(build.binary_path).parent.name)

    def test_source_change_rebuilds_even_when_binary_mtime_is_newer(self):
        with self.compiler(b'first'):
            first = k.compile_scanner('arm64')
        self.source.write_text('int main(void) { return 1; }\n')
        os.utime(first.binary_path, (4_000_000_000, 4_000_000_000))

        with self.compiler(b'second') as run:
            second = k.compile_scanner('arm64')

        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(second.binary_path, first.binary_path)
        self.assertEqual(Path(second.binary_path).read_bytes(), b'second')

    def test_compile_failure_preserves_previous_binary_and_receipt(self):
        with self.compiler(b'known-good'):
            build = k.compile_scanner('arm64')
        binary_before = Path(build.binary_path).read_bytes()
        pointer_before = self.pointer.read_bytes()
        self.source.write_text('int main(void) { return 2; }\n')

        with self.compiler(returncode=1):
            self.assertIsNone(k.compile_scanner('arm64'))

        self.assertEqual(Path(build.binary_path).read_bytes(), binary_before)
        self.assertEqual(self.pointer.read_bytes(), pointer_before)

    def test_tampered_binary_is_not_accepted_by_matching_build_metadata(self):
        with self.compiler(b'known-good'):
            first = k.compile_scanner('arm64')
        Path(first.binary_path).write_bytes(b'tampered')

        with self.compiler(b'rebuilt') as run:
            rebuilt = k.compile_scanner('arm64')

        self.assertIsNotNone(rebuilt)
        self.assertEqual(run.call_count, 1)
        self.assertNotEqual(rebuilt.binary_path, first.binary_path)
        self.assertEqual(Path(rebuilt.binary_path).read_bytes(), b'rebuilt')

    def test_pointer_publish_failure_preserves_previous_current_build(self):
        with self.compiler(b'known-good'):
            first = k.compile_scanner('arm64')
        pointer_before = self.pointer.read_bytes()
        binary_before = Path(first.binary_path).read_bytes()
        self.source.write_text('int main(void) { return 3; }\n')
        real_replace = os.replace

        def fail_pointer(source, destination):
            if os.path.abspath(destination) == os.path.abspath(self.pointer):
                raise OSError('pointer publish failed')
            return real_replace(source, destination)

        with patch.object(k.os, 'replace', side_effect=fail_pointer):
            with self.compiler(b'new-but-unpublished'):
                self.assertIsNone(k.compile_scanner('arm64'))

        self.assertEqual(self.pointer.read_bytes(), pointer_before)
        self.assertEqual(Path(first.binary_path).read_bytes(), binary_before)

    def test_target_architecture_is_part_of_build_identity_and_argv(self):
        with self.compiler(b'arm64') as arm_run:
            arm = k.compile_scanner('arm64')
        with self.compiler(b'x86') as x86_run:
            x86 = k.compile_scanner('x86_64')

        self.assertNotEqual(arm.input_identity, x86.input_identity)
        self.assertIn('arm64', arm_run.call_args.args[0])
        self.assertIn('x86_64', x86_run.call_args.args[0])
