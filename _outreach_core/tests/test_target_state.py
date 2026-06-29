"""v30 §WS-E — target_state per-target snapshot read/write/clear."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import target_state as ts


class TestTargetStateIO(unittest.TestCase):
    def setUp(self) -> None:
        # Use tmp dir per test for isolation.
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_then_read_round_trip(self) -> None:
        state = ts.TargetState(
            target_id="legalon",
            run_id="20260630-090000",
            name="株式会社LegalOn Technologies",
            form_url="https://legalontech.jp/contact/",
            phase="send.submit_attempted",
            hop=2,
            observation_state="confirm",
            last_button="送信",
            captcha="v3",
        )
        path = ts.write_state(self.data_dir, state)
        self.assertTrue(path.exists())
        loaded = ts.read_state(self.data_dir, "legalon")
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.target_id, "legalon")
        self.assertEqual(loaded.name, "株式会社LegalOn Technologies")
        self.assertEqual(loaded.phase, "send.submit_attempted")
        self.assertEqual(loaded.hop, 2)
        self.assertEqual(loaded.observation_state, "confirm")
        self.assertEqual(loaded.last_button, "送信")
        self.assertEqual(loaded.captcha, "v3")
        self.assertNotEqual(loaded.updated_at, "")
        self.assertNotEqual(loaded.started_at, "")

    def test_read_returns_none_for_missing_target(self) -> None:
        self.assertIsNone(ts.read_state(self.data_dir, "ghost"))

    def test_read_returns_none_for_malformed_file(self) -> None:
        # Hand-write garbage where the snapshot should be — must not raise.
        d = ts.runtime_dir(self.data_dir, "bad")
        d.mkdir(parents=True, exist_ok=True)
        (d / ts.STATE_FILENAME).write_text("{not json", encoding="utf-8")
        self.assertIsNone(ts.read_state(self.data_dir, "bad"))

    def test_clear_state_removes_file(self) -> None:
        ts.write_state(self.data_dir, ts.TargetState(target_id="x", phase="send.opened"))
        self.assertTrue(ts.state_path(self.data_dir, "x").exists())
        self.assertTrue(ts.clear_state(self.data_dir, "x"))
        self.assertFalse(ts.state_path(self.data_dir, "x").exists())
        # Idempotent: removing twice is fine.
        self.assertFalse(ts.clear_state(self.data_dir, "x"))

    def test_write_with_empty_target_id_does_nothing(self) -> None:
        # Defensive: callers occasionally pass "" for a freshly-loaded
        # target that hasn't been assigned an id yet. Don't write a file
        # called .../runtime//last_state.json.
        ts.write_state(self.data_dir, ts.TargetState(target_id=""))
        # Nothing under runtime/.
        runtime = self.data_dir / ts.RUNTIME_DIRNAME
        if runtime.exists():
            self.assertEqual(list(runtime.iterdir()), [])


class TestMergeUpdate(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_merge_creates_then_updates_one_field(self) -> None:
        ts.merge_update(self.data_dir, "t", phase="send.opened",
                        name="株式会社X", form_url="https://x.example/")
        ts.merge_update(self.data_dir, "t", hop=1, observation_state="input")
        loaded = ts.read_state(self.data_dir, "t")
        assert loaded is not None
        # Fields from both updates are preserved.
        self.assertEqual(loaded.phase, "send.opened")
        self.assertEqual(loaded.name, "株式会社X")
        self.assertEqual(loaded.form_url, "https://x.example/")
        self.assertEqual(loaded.hop, 1)
        self.assertEqual(loaded.observation_state, "input")

    def test_merge_updates_button_streak(self) -> None:
        ts.merge_update(self.data_dir, "t", last_button="次へ", same_button_count=1)
        ts.merge_update(self.data_dir, "t", same_button_count=2)
        ts.merge_update(self.data_dir, "t", same_button_count=3,
                        wizard_stuck="same_button_repeated")
        loaded = ts.read_state(self.data_dir, "t")
        assert loaded is not None
        self.assertEqual(loaded.last_button, "次へ")
        self.assertEqual(loaded.same_button_count, 3)
        self.assertEqual(loaded.wizard_stuck, "same_button_repeated")

    def test_merge_preserves_extras_across_updates(self) -> None:
        ts.merge_update(self.data_dir, "t",
                        extras={"iframe_takeover_to": "https://share.hsforms.com/abc"})
        ts.merge_update(self.data_dir, "t", phase="send.filled")
        loaded = ts.read_state(self.data_dir, "t")
        assert loaded is not None
        self.assertEqual(
            loaded.extras.get("iframe_takeover_to"),
            "https://share.hsforms.com/abc",
        )
        self.assertEqual(loaded.phase, "send.filled")


class TestListRuntimeStates(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lists_only_targets_with_snapshots(self) -> None:
        ts.write_state(self.data_dir, ts.TargetState(target_id="a", phase="send.opened"))
        ts.write_state(self.data_dir, ts.TargetState(target_id="b", phase="send.submit_attempted"))
        # A target whose runtime dir exists but the snapshot file is missing.
        ts.runtime_dir(self.data_dir, "stale").mkdir(parents=True, exist_ok=True)
        listed = ts.list_runtime_states(self.data_dir)
        ids = sorted(s.target_id for s in listed)
        self.assertEqual(ids, ["a", "b"])

    def test_list_handles_missing_runtime_dir(self) -> None:
        # No runtime/ directory at all — return [] cleanly.
        self.assertEqual(ts.list_runtime_states(self.data_dir), [])

    def test_list_skips_malformed_files(self) -> None:
        good = ts.runtime_dir(self.data_dir, "good")
        good.mkdir(parents=True, exist_ok=True)
        (good / ts.STATE_FILENAME).write_text(
            json.dumps({"target_id": "good", "phase": "send.opened"}),
            encoding="utf-8",
        )
        bad = ts.runtime_dir(self.data_dir, "bad")
        bad.mkdir(parents=True, exist_ok=True)
        (bad / ts.STATE_FILENAME).write_text("garbage", encoding="utf-8")
        listed = ts.list_runtime_states(self.data_dir)
        self.assertEqual([s.target_id for s in listed], ["good"])


if __name__ == "__main__":
    unittest.main()
