"""v15 §R — per-lead isolation (§R1), subprocess wall regression (§R2),
journal resume guard wiring (§R3)."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


def _load_run_module():
    if "yaml" not in sys.modules:
        sys.modules["yaml"] = types.SimpleNamespace(
            safe_dump=lambda obj, **kwargs: "{}\n",
            safe_load=lambda s: {},
        )
    root = Path(__file__).resolve().parent.parent.parent
    run_path = root / "jp-form-outreach" / "run.py"
    spec = importlib.util.spec_from_file_location("jp_form_outreach_run_v15rel", run_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _FakeHeartbeat:
    """Records start/tick/end so we can assert hb.end always runs."""

    instances: list["_FakeHeartbeat"] = []

    def __init__(self, *args, **kwargs):
        self.started = False
        self.ticks: list[int] = []
        self.ended = False
        _FakeHeartbeat.instances.append(self)

    def start(self, *a, **k):
        self.started = True

    def tick(self, n, *a, **k):
        self.ticks.append(n)

    def end(self, *a, **k):
        self.ended = True


def _write_drafts(path: Path, ids: list[str]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for tid in ids:
            f.write(json.dumps({
                "id": tid,
                "name": f"会社{tid}",
                "form_url": f"https://{tid}.example.co.jp/contact",
                "draft": {"subject": "ご提案", "body": "本文"},
            }, ensure_ascii=False) + "\n")


class TestSendPerLeadIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def _stage_send(self, tmp: Path, drafts: Path, send_one, ids=None):
        m = self.run_mod
        _FakeHeartbeat.instances = []
        # v20 primary-host guard must not depend on THIS machine's hostname /
        # data/primary_host — patch it open so the tests run on any host.
        from _outreach_core import host_role
        with patch.object(host_role, "is_send_allowed", return_value=(True, "test")), \
             patch.object(m, "DATA_DIR", tmp), \
             patch.object(m, "load_sent_set", return_value=set()), \
             patch.object(m, "HeartbeatSession", _FakeHeartbeat), \
             patch.object(m, "_send_one_target", side_effect=send_one), \
             patch.object(m, "_close_tab_safely"), \
             patch.object(m, "_emit_event"), \
             patch.object(m, "append_sent_history"), \
             patch.object(m.time, "sleep"):
            m.stage_send(drafts, ids or {1, 2}, mode="auto", config={"sender": {}})
        return _FakeHeartbeat.instances[-1]

    def test_crash_in_one_lead_does_not_kill_batch(self) -> None:
        """§R acceptance 1: an exception in lead 1 → batch continues, needs_attention recorded."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            drafts = tmp / "drafts.jsonl"
            _write_drafts(drafts, ["t1", "t2"])

            calls: list[str] = []

            def send_one(d, **kw):
                calls.append(d["id"])
                if d["id"] == "t1":
                    raise RuntimeError("browser exploded")
                return {"outcome": "done"}

            hb = self._stage_send(tmp, drafts, send_one)

            self.assertEqual(calls, ["t1", "t2"], "batch must continue past the crash")
            self.assertTrue(hb.ended, "hb.end must run")
            na = tmp / "needs_attention.jsonl"
            self.assertTrue(na.exists())
            rows = [json.loads(l) for l in na.read_text().splitlines() if l.strip()]
            self.assertTrue(any("lead_crashed" in str(r.get("reason")) for r in rows))

    def test_hb_end_runs_even_when_loop_raises(self) -> None:
        """§R acceptance 2: non-KeyboardInterrupt escaping the loop still ends hb."""
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            drafts = tmp / "drafts.jsonl"
            _write_drafts(drafts, ["t1"])

            m = self.run_mod
            _FakeHeartbeat.instances = []

            class _ExplodingHb(_FakeHeartbeat):
                def tick(self, n, *a, **k):
                    raise RuntimeError("slack down")

            from _outreach_core import host_role
            with patch.object(host_role, "is_send_allowed", return_value=(True, "test")), \
                 patch.object(m, "DATA_DIR", tmp), \
                 patch.object(m, "load_sent_set", return_value=set()), \
                 patch.object(m, "HeartbeatSession", _ExplodingHb), \
                 patch.object(m, "_send_one_target", return_value={"outcome": "done"}), \
                 patch.object(m, "_emit_event"), \
                 patch.object(m, "append_sent_history"), \
                 patch.object(m.time, "sleep"):
                with self.assertRaises(RuntimeError):
                    m.stage_send(drafts, {1}, mode="auto", config={"sender": {}})
            self.assertTrue(_FakeHeartbeat.instances[-1].ended)

    def test_unverified_prior_attempt_is_not_resent(self) -> None:
        """§R acceptance 3: journal submit_attempted w/o verified → no send, needs_attention."""
        from _outreach_core import send_journal as sj

        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            drafts = tmp / "drafts.jsonl"
            _write_drafts(drafts, ["t1", "t2"])
            sj.append_journal(tmp, "t1", sj.PHASE_SUBMIT_ATTEMPTED, form_url="https://t1.example.co.jp/contact")

            calls: list[str] = []

            def send_one(d, **kw):
                calls.append(d["id"])
                return {"outcome": "done"}

            self._stage_send(tmp, drafts, send_one)

            self.assertEqual(calls, ["t2"], "t1 must NOT be auto-resent")
            rows = [
                json.loads(l)
                for l in (tmp / "needs_attention.jsonl").read_text().splitlines()
                if l.strip()
            ]
            self.assertTrue(any("unverified_prior_attempt" in str(r.get("reason")) for r in rows))


class TestEnrichPerLeadIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.run_mod = _load_run_module()

    def test_crash_in_one_lead_keeps_enriching(self) -> None:
        m = self.run_mod
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            in_path = tmp / "leads.jsonl"
            out_path = tmp / "enriched.jsonl"
            with in_path.open("w") as f:
                for tid in ("a", "b"):
                    f.write(json.dumps({"id": tid, "name": tid, "form_url": f"https://{tid}.jp/c"}) + "\n")

            def one(t_, i, total, config, enriched):
                if t_["id"] == "a":
                    raise RuntimeError("dom boom")
                enriched.append({**t_, "form_fields": {}})
                return {"outcome": "enriched"}

            with patch.object(m, "_enrich_one_target", side_effect=one), \
                 patch.object(m, "_emit_event"):
                m.stage_enrich(in_path, out_path, config={})

            rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
            self.assertEqual(len(rows), 2)
            crashed = [r for r in rows if r.get("_enrich_skipped") == "lead_crashed"]
            self.assertEqual(len(crashed), 1)
            self.assertEqual(crashed[0]["id"], "a")


class TestInferSubprocTimeoutRegression(unittest.TestCase):
    """§R2 — oc_browser_json / oc_evaluate MUST route through _run (240s wall)."""

    def test_oc_browser_json_uses_run(self) -> None:
        from _outreach_core import infer

        with patch.object(infer, "_run", return_value=(0, '{"ok": true}', "")) as r:
            infer.oc_browser_json("tabs")
        self.assertTrue(r.called)

    def test_oc_evaluate_uses_run(self) -> None:
        from _outreach_core import infer

        with patch.object(infer, "_run", return_value=(0, "123", "")) as r:
            out = infer.oc_evaluate("() => 123")
        self.assertTrue(r.called)
        self.assertEqual(out, 123)


if __name__ == "__main__":
    unittest.main()
