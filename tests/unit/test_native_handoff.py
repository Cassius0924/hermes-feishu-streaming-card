from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

import pytest

from hermes_feishu_card.native_handoff import (
    NativeHandoffStore,
    NativeHandoffStoreError,
    handoff_identity_key,
)
from hermes_feishu_card import native_handoff as native_handoff_module


def _identity(index: int = 1) -> str:
    return handoff_identity_key(
        profile_id="profile-test",
        chat_id=f"chat-private-{index}",
        conversation_id=f"conversation-private-{index}",
        message_id=f"message-private-{index}",
    )


def test_handoff_identity_is_stable_hash_and_does_not_contain_raw_ids():
    first = _identity()
    second = _identity()
    different = _identity(2)

    assert first == second
    assert first != different
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert "private" not in first


def test_store_rejects_filesystem_root_before_any_state_operation():
    filesystem_root = Path(Path.cwd().anchor)

    with pytest.raises(ValueError, match="filesystem root"):
        NativeHandoffStore(filesystem_root)


def test_store_persists_pending_then_committed_in_private_atomic_file(tmp_path):
    root = tmp_path / "state"
    store = NativeHandoffStore(root, now=lambda: 100.0)
    identity = _identity()

    pending, created = store.begin(
        identity,
        feishu_message_id="fake-card-message-1",
        bot_id="fake-bot-1",
        event_created_at=90.0,
    )

    assert created is True
    assert pending.state == "pending"
    assert pending.event_created_at == 90.0
    assert os.stat(root).st_mode & 0o777 == 0o700
    path = root / "native-handoffs.json"
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert list(root.glob("*.tmp")) == []
    serialized = path.read_text(encoding="utf-8")
    assert "chat-private" not in serialized
    assert "conversation-private" not in serialized
    assert "message-private" not in serialized

    reloaded = NativeHandoffStore(root, now=lambda: 101.0)
    assert reloaded.get(identity) == pending
    committed = reloaded.mark_committed(identity)

    assert committed is not None
    assert committed.state == "committed"
    assert NativeHandoffStore(root).get(identity).state == "committed"


def test_store_records_terminal_only_handoff_without_card_identifiers(tmp_path):
    store = NativeHandoffStore(tmp_path / "state")
    record, created = store.begin_no_card(_identity(), event_created_at=10.0)

    assert created is True
    assert record.state == "no_card"
    assert record.feishu_message_id == ""
    assert record.bot_id == ""


def test_store_reads_early_v1_record_without_event_timestamp(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    identity = _identity()
    path = root / "native-handoffs.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    identity: {
                        "state": "no_card",
                        "feishu_message_id": "",
                        "bot_id": "",
                        "created_at": 10.0,
                        "updated_at": 11.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    record = NativeHandoffStore(root).get(identity)

    assert record is not None
    assert record.event_created_at == 10.0


def test_store_is_bounded_and_evicts_committed_before_pending(tmp_path):
    clock = iter((1.0, 2.0, 3.0, 4.0, 5.0))
    store = NativeHandoffStore(
        tmp_path / "state",
        max_records=2,
        now=lambda: next(clock),
    )
    pending_key = _identity(1)
    committed_key = _identity(2)
    newest_key = _identity(3)
    store.begin(
        pending_key,
        feishu_message_id="fake-card-1",
        bot_id="fake-bot",
        event_created_at=1.0,
    )
    store.begin_no_card(committed_key, event_created_at=2.0)

    store.begin_no_card(newest_key, event_created_at=3.0)

    assert store.get(pending_key) is not None
    assert store.get(committed_key) is None
    assert store.get(newest_key) is not None
    payload = json.loads((tmp_path / "state" / "native-handoffs.json").read_text())
    assert len(payload["records"]) == 2


def test_store_clear_allows_same_identity_to_begin_new_lifecycle(tmp_path):
    store = NativeHandoffStore(tmp_path / "state")
    identity = _identity()
    store.begin_no_card(identity, event_created_at=1.0)

    assert store.clear(identity) is True
    _, created = store.begin_no_card(identity, event_created_at=2.0)

    assert created is True


def test_store_refuses_symlinked_root_or_file_without_touching_target(tmp_path):
    target_root = tmp_path / "target-root"
    target_root.mkdir(mode=0o700)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(target_root, target_is_directory=True)

    with pytest.raises(NativeHandoffStoreError, match="symbolic link"):
        NativeHandoffStore(linked_root).get(_identity())

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "target.json"
    target.write_text("do-not-touch", encoding="utf-8")
    (root / "native-handoffs.json").symlink_to(target)

    with pytest.raises(NativeHandoffStoreError, match="symbolic link"):
        NativeHandoffStore(root).begin_no_card(_identity(), event_created_at=1.0)
    assert target.read_text(encoding="utf-8") == "do-not-touch"


def test_store_refuses_insecure_or_oversized_existing_file(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "native-handoffs.json"
    path.write_text('{"version":1,"records":{}}', encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(NativeHandoffStoreError, match="permissions"):
        NativeHandoffStore(root).get(_identity())

    path.chmod(0o600)
    path.write_bytes(b"x" * 1025)
    with pytest.raises(NativeHandoffStoreError, match="size"):
        NativeHandoffStore(root, max_file_bytes=1024).get(_identity())


def test_failed_writes_do_not_leave_ghost_begin_commit_or_clear_state(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "state"
    store = NativeHandoffStore(root)
    identity = _identity()
    real_write = native_handoff_module._atomic_write_private

    def fail_write(*_args, **_kwargs):
        raise NativeHandoffStoreError("injected write failure")

    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", fail_write)
    with pytest.raises(NativeHandoffStoreError, match="injected"):
        store.begin_no_card(identity, event_created_at=10.0)
    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", real_write)

    record, created = store.begin(
        identity,
        feishu_message_id="fake-card-message-1",
        bot_id="fake-bot-1",
        event_created_at=10.0,
    )
    assert created is True
    assert record.state == "pending"

    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", fail_write)
    with pytest.raises(NativeHandoffStoreError, match="injected"):
        store.mark_committed(identity)
    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", real_write)
    assert store.get(identity).state == "pending"

    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", fail_write)
    with pytest.raises(NativeHandoffStoreError, match="injected"):
        store.clear(identity)
    monkeypatch.setattr(native_handoff_module, "_atomic_write_private", real_write)
    assert store.get(identity).state == "pending"


def test_two_store_instances_reload_and_merge_under_one_persistent_lock(tmp_path):
    root = tmp_path / "state"
    first = NativeHandoffStore(root)
    second = NativeHandoffStore(root)
    first_key = _identity(1)
    second_key = _identity(2)

    assert first.get(first_key) is None
    _, second_created = second.begin_no_card(second_key, event_created_at=2.0)
    _, first_created = first.begin_no_card(first_key, event_created_at=1.0)
    _, duplicate_created = second.begin_no_card(first_key, event_created_at=1.0)

    assert second_created is True
    assert first_created is True
    assert duplicate_created is False
    reloaded = NativeHandoffStore(root)
    assert reloaded.get(first_key) is not None
    assert reloaded.get(second_key) is not None


def test_private_file_lock_serializes_store_instances(tmp_path):
    root = tmp_path / "state"
    first = NativeHandoffStore(root)
    second = NativeHandoffStore(root)
    identity = _identity()

    with ThreadPoolExecutor(max_workers=1) as executor:
        with first._persistent_lock():
            future = executor.submit(
                second.begin_no_card,
                identity,
                event_created_at=1.0,
            )
            with pytest.raises(FutureTimeout):
                future.result(timeout=0.02)
        record, created = future.result(timeout=1.0)

    assert created is True
    assert record.state == "no_card"
    assert os.stat(root / "native-handoffs.lock").st_mode & 0o777 == 0o600


def test_private_file_lock_serializes_independent_processes(tmp_path):
    root = tmp_path / "state"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    completed = tmp_path / "completed"
    identity = _identity()
    holder_script = """
import sys
import time
from pathlib import Path
from hermes_feishu_card.native_handoff import NativeHandoffStore

root, ready, release = map(Path, sys.argv[1:4])
store = NativeHandoffStore(root)
with store._persistent_lock():
    ready.write_text("ready", encoding="utf-8")
    while not release.exists():
        time.sleep(0.01)
"""
    writer_script = """
import json
import sys
from pathlib import Path
from hermes_feishu_card.native_handoff import NativeHandoffStore

root, completed = map(Path, sys.argv[1:3])
_record, created = NativeHandoffStore(root).begin_no_card(
    sys.argv[3],
    event_created_at=1.0,
)
completed.write_text(json.dumps({"created": created}), encoding="utf-8")
"""

    holder = subprocess.Popen(
        [sys.executable, "-c", holder_script, str(root), str(ready), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    writer = None
    try:
        _wait_for_path(ready)
        writer = subprocess.Popen(
            [
                sys.executable,
                "-c",
                writer_script,
                str(root),
                str(completed),
                identity,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.05)
        assert not completed.exists()
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=5)
        writer_stdout, writer_stderr = writer.communicate(timeout=5)
    finally:
        for process in (holder, writer):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert holder.returncode == 0, holder_stdout + holder_stderr
    assert writer is not None and writer.returncode == 0, writer_stdout + writer_stderr
    assert json.loads(completed.read_text(encoding="utf-8")) == {"created": True}
    assert NativeHandoffStore(root).get(identity) is not None


def test_windows_locking_branch_uses_msvcrt_byte_range(tmp_path, monkeypatch):
    calls = []

    class FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(descriptor, mode, byte_count):
            calls.append((descriptor, mode, byte_count))

    monkeypatch.setattr(native_handoff_module, "_fcntl", None)
    monkeypatch.setattr(native_handoff_module, "_msvcrt", FakeMsvcrt)

    store = NativeHandoffStore(tmp_path / "state")
    record, created = store.begin_no_card(_identity(), event_created_at=1.0)

    assert created is True
    assert record.state == "no_card"
    assert [(mode, size) for _fd, mode, size in calls] == [(1, 1), (2, 1)]
    assert (tmp_path / "state" / "native-handoffs.lock").read_bytes() == b"\0"


def test_windows_unlock_failure_still_closes_descriptor(tmp_path, monkeypatch):
    class FailingUnlockMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(_descriptor, mode, _byte_count):
            if mode == FailingUnlockMsvcrt.LK_UNLCK:
                raise OSError("injected unlock failure")

    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    descriptor = os.open(root / "injected.lock", os.O_RDWR | os.O_CREAT, 0o600)
    store = NativeHandoffStore(root)
    monkeypatch.setattr(native_handoff_module, "_fcntl", None)
    monkeypatch.setattr(native_handoff_module, "_msvcrt", FailingUnlockMsvcrt)
    monkeypatch.setattr(
        native_handoff_module,
        "_open_private_lock_file",
        lambda *_args: descriptor,
    )

    with pytest.raises(OSError, match="injected unlock failure"):
        with store._persistent_lock():
            pass

    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_store_fails_closed_when_platform_has_no_persistent_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(native_handoff_module, "_fcntl", None)
    monkeypatch.setattr(native_handoff_module, "_msvcrt", None)

    with pytest.raises(NativeHandoffStoreError, match="locking is unavailable"):
        NativeHandoffStore(tmp_path / "state").begin_no_card(
            _identity(),
            event_created_at=1.0,
        )


def test_prepare_lifecycle_clears_only_strictly_newer_event(tmp_path):
    store = NativeHandoffStore(tmp_path / "state")
    identity = _identity()
    store.begin_no_card(identity, event_created_at=20.0)

    assert store.prepare_lifecycle(identity, event_created_at=19.0) == "stale"
    assert store.prepare_lifecycle(identity, event_created_at=20.0) == "stale"
    assert store.get(identity) is not None
    assert store.prepare_lifecycle(identity, event_created_at=21.0) == "cleared"
    lifecycle_floor = store.get(identity)
    assert lifecycle_floor is not None
    assert lifecycle_floor.state == "lifecycle"
    assert lifecycle_floor.event_created_at == 21.0


def test_begin_rejects_terminal_before_but_allows_at_lifecycle_floor(tmp_path):
    store = NativeHandoffStore(tmp_path / "state")
    identity = _identity()
    store.begin_no_card(identity, event_created_at=10.0)
    assert store.prepare_lifecycle(identity, event_created_at=20.0) == "cleared"

    stale, stale_created = store.begin_no_card(identity, event_created_at=19.0)
    fresh, fresh_created = store.begin_no_card(identity, event_created_at=20.0)

    assert stale.state == "lifecycle"
    assert stale_created is False
    assert fresh.state == "no_card"
    assert fresh_created is True


def test_capacity_refuses_to_evict_pending_or_lifecycle_records(tmp_path):
    store = NativeHandoffStore(tmp_path / "state", max_records=2)
    pending_key = _identity(1)
    lifecycle_key = _identity(2)
    rejected_key = _identity(3)
    store.begin(
        pending_key,
        feishu_message_id="fake-card-pending",
        bot_id="fake-bot",
        event_created_at=10.0,
    )
    store.begin_no_card(lifecycle_key, event_created_at=10.0)
    assert store.prepare_lifecycle(lifecycle_key, event_created_at=20.0) == "cleared"

    with pytest.raises(NativeHandoffStoreError, match="cannot be bounded"):
        store.begin_no_card(rejected_key, event_created_at=30.0)

    assert store.get(pending_key).state == "pending"
    assert store.get(lifecycle_key).state == "lifecycle"
    assert store.get(rejected_key) is None


def test_stale_repair_cannot_commit_reused_identity(tmp_path):
    store = NativeHandoffStore(tmp_path / "state")
    identity = _identity()
    old_record, _ = store.begin(
        identity,
        feishu_message_id="fake-card-old",
        bot_id="fake-bot",
        event_created_at=10.0,
    )
    assert store.prepare_lifecycle(identity, event_created_at=20.0) == "cleared"
    new_record, _ = store.begin(
        identity,
        feishu_message_id="fake-card-new",
        bot_id="fake-bot",
        event_created_at=21.0,
    )

    unchanged = store.mark_committed(identity, expected_record=old_record)

    assert unchanged == new_record
    assert store.get(identity).state == "pending"
    assert store.mark_committed(
        identity,
        expected_record=new_record,
    ).state == "committed"


def test_oversized_timestamp_is_wrapped_as_store_error(tmp_path):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    path = root / "native-handoffs.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "records": {
                    _identity(): {
                        "state": "no_card",
                        "feishu_message_id": "",
                        "bot_id": "",
                        "created_at": 10**1000,
                        "updated_at": 10**1000,
                        "event_created_at": 1.0,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)

    with pytest.raises(NativeHandoffStoreError, match="invalid"):
        NativeHandoffStore(root).get(_identity())


def _wait_for_path(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {path.name}")
