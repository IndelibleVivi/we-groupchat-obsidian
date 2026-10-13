"""Explicit device-renumber recovery for the existing menu-app source identity.

Only config identity is published. Inventory, monitor checkpoints and capture
ledgers remain owned by their existing stores and are never translated here.
"""
from __future__ import annotations

from .config import ConfigStore, active_monitor_chats
from .monitor import STATE_DIR, state_file_for_chat
from .monitor_state import MonitorStateStore
from .resource_capture import resource_capture_operation_lock
from .source_device_binding import (
    SourceDeviceBindingError, check_binding, resolve_root_identity,
    source_namespace, validate_runtime_identity,
)
from .source_inventory import SourceInventoryStore
from .wechat_db import WeChatDB


class SourceRecoveryError(RuntimeError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(self.code)


def recover_source_device(*, previous_device, expected_namespace, keys,
                          apply=False, config_store=None, inventory_store=None,
                          state_dir=STATE_DIR, source_factory=WeChatDB):
    """Prove full existing lineage while the caller holds AppInstanceLock.

    A dry run still reads source metadata and requires the same explicit source
    grant and stopped-app precondition as apply. No source messages are read.
    """
    if (isinstance(previous_device, bool) or not isinstance(previous_device, int)
            or previous_device < 0):
        raise SourceRecoveryError("source_recovery_device_invalid")
    config_store = config_store or ConfigStore()
    inventory_store = inventory_store or SourceInventoryStore()
    config = config_store.read()
    if not config or not config.get("db_dir"):
        raise SourceRecoveryError("source_not_configured")
    with resource_capture_operation_lock(config):
        if config_store.read() != config:
            raise SourceRecoveryError("source_recovery_config_changed")
        try:
            observed_root = resolve_root_identity(config["db_dir"])
            binding = {"root_inode": observed_root["root_inode"],
                       "volume_identity": observed_root["volume_identity"],
                       "original_device": previous_device}
            namespace = source_namespace(observed_root["source_db_dir"], previous_device,
                                         observed_root["root_inode"])
            if namespace != expected_namespace:
                raise SourceRecoveryError("source_recovery_namespace_mismatch")
            identity = validate_runtime_identity({
                "source_db_dir": observed_root["source_db_dir"],
                "source_namespace": namespace, "binding": binding}, config["db_dir"])
            installed = config.get("wechat_source_identity")
            if installed is not None and installed != identity:
                raise SourceRecoveryError("source_device_binding_conflict")
            inventory = inventory_store.inspect(namespace)
            expected = {r["relative_path"]: r for r in inventory.shards
                        if r["state"] != "explicitly_retired"}
            if not inventory.complete or inventory.error_codes or not expected:
                raise SourceRecoveryError("source_device_lineage_unproven")
            expected_cursors = {r["logical_shard_id"]: r["generation_id"]
                                for r in expected.values()}
            chats = active_monitor_chats(config)
            if not chats:
                raise SourceRecoveryError("source_recovery_monitor_unconfigured")
            checkpoint_snapshots = []
            for chat in chats:
                store = MonitorStateStore(state_file_for_chat(chat["username"], state_dir=state_dir))
                snapshot = store.inspect()
                cursors = snapshot.data.get("source_cursors") or {}
                if (not snapshot.existed or set(cursors) != set(expected_cursors) or
                        any(k not in expected_cursors or
                            v.get("generation_id") != expected_cursors[k]
                            for k, v in cursors.items())):
                    raise SourceRecoveryError("source_device_monitor_lineage_unproven")
                checkpoint_snapshots.append((store, snapshot))
            source = source_factory(config["db_dir"], keys,
                                    source_inventory_store=inventory_store,
                                    source_binding=binding)
            with source._source_heavy_section():
                observations, _, codes = source._message_shard_observations(expected)
                present = {r["relative_path"]: r for r in observations
                           if r["state"] != "explicitly_retired"}
                if codes or set(present) != set(expected) or any(
                        r.get("state") != "present" or not r.get("generation_id")
                        or r["generation_id"] != expected[path]["generation_id"]
                        for path, r in present.items()):
                    raise SourceRecoveryError("source_device_lineage_mismatch")
                if check_binding(config["db_dir"], binding) != observed_root:
                    raise SourceRecoveryError("source_device_binding_changed")
                if inventory_store.inspect(namespace).inventory_digest != inventory.inventory_digest:
                    raise SourceRecoveryError("source_recovery_inventory_changed")
                if any(store.inspect() != snapshot for store, snapshot in checkpoint_snapshots):
                    raise SourceRecoveryError("source_recovery_checkpoint_changed")
                if apply:
                    def install(current):
                        if (current.get("db_dir") != config["db_dir"] or
                                current.get("wechat_source_identity") != installed or
                                active_monitor_chats(current) != chats):
                            raise SourceRecoveryError("source_recovery_config_changed")
                        current["wechat_source_identity"] = identity
                    config_store.update(install)
            return {"state": ("already_recovered" if installed else "recovered")
                    if apply else "verified", "verified_shards": len(expected),
                    "monitor_chats": len(chats), "checkpoint_changes": 0}
        except SourceDeviceBindingError as exc:
            raise SourceRecoveryError(exc.code) from exc
