from dataclasses import replace

from core.semantic.controls import ReaderControls, SearchAnswer
from core.semantic.delivery import (
    AttentionEvent, ContentBinding, DeliveryService, EvidenceTarget, NotifyResult,
)
from core.semantic.projections import ReadingError
from tests.test_semantic_projections import ReadingHarness


class RecordingNotifier:
    def __init__(self, status="accepted"):
        self.status = status
        self.notifications = []

    def send(self, envelope):
        self.notifications.append(envelope)
        if self.status == "raise":
            raise RuntimeError("unknown local outcome")
        return NotifyResult(self.status, envelope.receipt_id)


class DeliveryTests(ReadingHarness):
    def setUp(self):
        super().setUp()
        self.controls = ReaderControls(self.ledger, self.publisher, search=lambda _: SearchAnswer("空", ()))
        self.delivery = DeliveryService(self.ledger, self.publisher, self.controls)
        self.notifier = RecordingNotifier()

    def event(self, *, kind="wait_met", target="box-cat", revision=1):
        pref = self.controls.preference("box-cat")
        return AttentionEvent("story-window", target, revision, "猫回到窗边了", "等着的那段日常有了后续，可以接着读。", kind,
                              (ContentBinding("box-cat", revision, pref.revision),), (EvidenceTarget("box-cat", "e1"),))

    def prepare(self):
        self.publish()
        self.controls.set_preference("box-cat", expected_revision=0, wait="猫回窗边")

    def send(self, id_, **kwargs):
        return self.delivery.deliver(id_, device_id="synthetic-device", channel="capture", notifier=self.notifier, **kwargs)

    def test_rebuild_default_silent_and_device_channel_dedup(self):
        self.prepare()
        result = self.delivery.register(self.event())
        self.assertEqual(self.send(result.event_id).status, "silent")
        self.assertEqual(self.notifier.notifications, [])
        accepted = self.send(result.event_id, live=True)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(self.send(result.event_id, live=True), accepted)
        self.assertEqual(len(self.notifier.notifications), 1)
        self.delivery.deliver(result.event_id, device_id="second-device", channel="capture",
                              notifier=self.notifier, live=True)
        self.assertEqual(len(self.notifier.notifications), 2)

    def test_target_and_evidence_must_be_published_before_notifier(self):
        self.prepare()
        target_missing = self.delivery.register(self.event(target="missing"))
        self.assertEqual(self.send(target_missing.event_id, live=True).code, "attention_target_not_ready")
        self.assertEqual(self.notifier.notifications, [])
        valid = self.delivery.register(self.event())
        source = self.publisher.root / "来源/box-cat-0001.md"
        source.unlink()
        self.assertEqual(self.send(valid.event_id, live=True).code, "evidence_projection_missing")
        self.assertEqual(self.notifier.notifications, [])

    def test_os_accepted_click_and_mtime_do_not_mark_read_or_known(self):
        self.prepare()
        event = self.delivery.register(self.event())
        self.assertEqual(self.send(event.event_id, live=True).status, "accepted")
        target = self.delivery.resolve_click(event.event_id)
        self.assertEqual(target, str(self.publisher.root / "条目/箱猫.md"))
        self.assertEqual(self.controls.preference("box-cat").known_revision, 0)
        self.assertEqual(self.ledger.all("SELECT * FROM reader_open_observations"), [])

    def test_click_resolves_rename_and_rejects_symlink_escape(self):
        self.prepare()
        event = self.delivery.register(self.event())
        source = self.publisher.root / self.publisher.resolve("box-cat")
        renamed = self.publisher.root / "猫的新名字.md"
        source.rename(renamed)
        self.assertEqual(self.delivery.resolve_click(event.event_id), str(renamed))
        self.assertFalse(source.exists())
        renamed.unlink()
        renamed.symlink_to(self.base / "outside.md")
        with self.assertRaisesRegex(ReadingError, "projection_symlink"):
            self.delivery.resolve_click(event.event_id)

    def test_unknown_never_resends_and_requires_exact_receipt(self):
        self.prepare()
        event = self.delivery.register(self.event())
        self.notifier.status = "raise"
        result = self.send(event.event_id, live=True)
        self.assertEqual(result.status, "unknown")
        self.assertEqual(self.send(event.event_id, live=True).status, "unknown")
        self.assertEqual(len(self.notifier.notifications), 1)
        with self.assertRaisesRegex(ReadingError, "delivery_receipt_mismatch"):
            self.delivery.reconcile(event_id=event.event_id, device_id="synthetic-device", channel="capture",
                                    receipt_id="wrong", observed_status="accepted")
        self.delivery.reconcile(event_id=event.event_id, device_id="synthetic-device", channel="capture",
                                receipt_id=result.receipt_id, observed_status="accepted")
        self.assertEqual(self.send(event.event_id, live=True).status, "accepted")
        self.assertEqual(len(self.notifier.notifications), 1)

    def test_mute_beats_follow_wait_and_latest_preferences_are_checked(self):
        self.prepare()
        event = self.delivery.register(self.event())
        self.controls.set_preference("box-cat", expected_revision=1, follow=True, mute=True)
        self.assertEqual(self.send(event.event_id, live=True).code, "attention_muted")
        self.assertEqual(self.notifier.notifications, [])
        muted = self.delivery.register(replace(self.event(), source_event_id="second"))
        self.assertEqual(muted.status, "suppressed")

    def test_follow_ordinary_change_enters_brief_correction_can_be_immediate(self):
        self.publish()
        self.controls.set_preference("box-cat", expected_revision=0, follow=True)
        ordinary = self.delivery.register(self.event(kind="change"))
        self.assertEqual(ordinary.status, "brief")
        self.assertEqual(self.send(ordinary.event_id, live=True).code, "attention_requires_brief")
        correction = self.delivery.register(self.event(kind="correction"))
        self.assertEqual(correction.status, "candidate")
        self.assertEqual(self.send(correction.event_id, live=True).status, "accepted")

    def test_new_content_and_known_version_block_old_attention(self):
        self.prepare()
        event = self.delivery.register(self.event())
        self.revisions["box-cat"] = 2
        self.assertEqual(self.send(event.event_id, live=True).code, "attention_binding_changed")
        self.revisions["box-cat"] = 1
        self.controls.mark_known("box-cat", 1, known_at="now")
        self.assertEqual(self.send(event.event_id, live=True).code, "attention_already_known")
        self.assertEqual(self.notifier.notifications, [])
