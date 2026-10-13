import concurrent.futures
from datetime import datetime, timezone
import hashlib
import multiprocessing
from pathlib import Path
import tempfile
import unittest

from core.provider_usage import ProviderUsageError, ProviderUsageStore


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc).timestamp()
DAY = "2026-10-13"


def _reserve_process(path, request_id):
    store = ProviderUsageStore(path, clock=lambda: NOW)
    try:
        store.reserve("account", request_id=request_id, route="decision:synthetic",
                      generation_id=request_id, upper_bound_units=60,
                      request_fingerprint=hashlib.sha256(request_id.encode()).hexdigest())
        return "reserved"
    except ProviderUsageError as exc:
        return exc.code


class ProviderUsageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = NOW
        self.path = Path(self.tmp.name) / "usage.sqlite3"
        self.store = ProviderUsageStore(self.path, clock=lambda: self.now)

    def budget(self, limit=100, external=0, **kwargs):
        self.store.confirm_budget("account", utc_day=DAY, daily_limit_units=limit,
                                  external_usage_units=external, confirmation_id="evidence-1",
                                  valid_until=kwargs.get("valid_until", NOW + 3600))

    def reserve(self, request="r1", bound=60, generation="g1"):
        return self.store.reserve("account", request_id=request, route="decision:synthetic",
                                  upper_bound_units=bound, request_fingerprint="a" * 64,
                                  generation_id=generation)

    def test_unconfirmed_or_expired_external_evidence_blocks(self):
        with self.assertRaisesRegex(ProviderUsageError, "external_budget_unconfirmed"):
            self.reserve()
        self.budget(valid_until=NOW + 1)
        self.reserve()
        self.now += 2
        with self.assertRaisesRegex(ProviderUsageError, "external_budget_unconfirmed"):
            self.store.mark_sent("r1")
        self.assertEqual(self.store.get("r1").status, "reserved")

    def test_preflight_budget_uses_fresh_evidence_and_reserved_units(self):
        self.assertEqual(self.store.budget_status("account")["code"], "external_budget_unconfirmed")
        self.budget(limit=100, external=10, valid_until=NOW + 1)
        self.reserve(bound=60)
        self.assertEqual(self.store.budget_status("account")["remaining_units"], 30)
        self.assertEqual(self.store.day_usage("account", DAY), 60)
        self.now += 2
        self.assertFalse(self.store.budget_status("account")["ready"])

    def test_concurrent_process_reservations_share_one_account_across_generations(self):
        self.budget()
        with concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(_reserve_process, [str(self.path)] * 4, ["g1", "g2", "g3", "g4"]))
        self.assertEqual(results.count("reserved"), 1)
        self.assertEqual(results.count("usage_budget_exhausted"), 3)
        self.assertEqual(self.store.day_usage("account", DAY), 60)

    def test_unknown_retains_reservation_and_blocks_duplicate_send_and_cancel(self):
        self.budget()
        self.reserve()
        self.store.mark_sent("r1")
        self.store.mark_unknown("r1")
        self.assertEqual(self.store.day_usage("account", DAY), 60)
        for action in (lambda: self.reserve(), lambda: self.store.mark_sent("r1"), lambda: self.store.cancel("r1")):
            with self.assertRaises(ProviderUsageError):
                action()
        with self.assertRaisesRegex(ProviderUsageError, "usage_budget_exhausted"):
            self.reserve("r2", generation="other-generation")

    def test_unique_receipt_and_idempotent_settlement(self):
        self.budget(200)
        self.reserve()
        self.reserve("r2")
        self.store.mark_sent("r1")
        self.store.mark_sent("r2")
        self.store.settle("r1", actual_units=20, receipt_id="receipt1")
        self.store.settle("r1", actual_units=20, receipt_id="receipt1")
        self.assertEqual(self.store.day_usage("account", DAY), 80)
        for request, amount, receipt in (("r2", 20, "receipt1"), ("r1", 21, "receipt1"), ("r1", 20, "receipt2")):
            with self.assertRaisesRegex(ProviderUsageError, "usage_receipt_conflict"):
                self.store.settle(request, actual_units=amount, receipt_id=receipt)

    def test_midnight_rechecked_immediately_before_sending(self):
        self.budget()
        self.reserve()
        self.now += 86400
        with self.assertRaisesRegex(ProviderUsageError, "usage_day_changed"):
            self.store.mark_sent("r1")
        self.assertEqual(self.store.get("r1").status, "reserved")

    def test_reconfirmation_does_not_clear_local_usage_or_bound_violation(self):
        self.budget()
        self.reserve()
        self.store.mark_sent("r1")
        with self.assertRaisesRegex(ProviderUsageError, "usage_bound_exceeded"):
            self.store.settle("r1", actual_units=61, receipt_id="receipt1")
        self.budget(200)
        self.assertEqual(self.store.day_usage("account", DAY), 61)
        with self.assertRaisesRegex(ProviderUsageError, "usage_bound_exceeded"):
            self.reserve("r2")

    def test_new_budget_can_stop_previously_reserved_send(self):
        self.budget()
        self.reserve()
        self.budget(external=50)
        with self.assertRaisesRegex(ProviderUsageError, "usage_budget_exhausted"):
            self.store.mark_sent("r1")
        self.store.cancel("r1")
        self.assertEqual(self.store.day_usage("account", DAY), 0)

    def test_reusing_request_id_for_different_input_or_generation_fails(self):
        self.budget()
        self.reserve()
        with self.assertRaisesRegex(ProviderUsageError, "usage_request_conflict"):
            self.reserve(generation="g2")

    def test_unsent_cancelled_reservation_can_be_readmitted_under_same_intent(self):
        self.budget()
        self.reserve()
        self.store.cancel("r1")
        renewed = self.reserve()
        self.assertEqual(renewed.status, "reserved")
        self.store.mark_sent("r1")
        self.assertEqual(self.store.get("r1").status, "sent")


if __name__ == "__main__":
    unittest.main()
