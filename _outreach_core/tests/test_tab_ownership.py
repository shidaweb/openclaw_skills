"""v32 FX3 — run.py tab ownership wiring (persist / cap scope / orphan sweep)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


@pytest.fixture()
def brief_env(tmp_path, monkeypatch):
    skill = tmp_path / "jp-form-outreach"
    own = skill / "data" / "briefs" / "own-brief"
    own.mkdir(parents=True)
    monkeypatch.setattr(run, "SKILL_DIR", skill)
    monkeypatch.setattr(run, "DATA_DIR", own)
    monkeypatch.setattr(run, "_OWNED_TAB_IDS", set())
    return skill, own


def test_open_and_close_maintain_ownership_record(brief_env, monkeypatch):
    _, own = brief_env

    class _Browser:
        def browser_json(self, cmd, *a):
            return {"targetId": "T1", "type": "page"}

    monkeypatch.setattr(run.core_adapters, "get_browser", lambda: _Browser())
    tid = run._open_tab("https://example.co.jp/contact")
    assert tid == "T1"
    record = json.loads((own / "owned_tabs.json").read_text())
    assert record["tab_ids"] == ["T1"]

    monkeypatch.setattr(run, "oc_browser", lambda *a, **k: "ok")
    run._close_tab("T1")
    record = json.loads((own / "owned_tabs.json").read_text())
    assert record["tab_ids"] == []


def test_enforce_tab_cap_only_touches_own_tabs(brief_env, monkeypatch):
    closed: list[str] = []
    monkeypatch.setattr(run, "_OWNED_TAB_IDS", {"OWN1", "OWN2"})
    monkeypatch.setattr(run, "_close_tab", lambda t: closed.append(t))
    payload = {"tabs": [
        {"targetId": t, "type": "page"}
        for t in ("SIB1", "SIB2", "OWN1", "SIB3", "OWN2")
    ]}
    monkeypatch.setattr(run, "_list_tabs_payload", lambda: payload)
    run._enforce_tab_cap(protect=set(), cap=1)
    assert closed == ["OWN1"]  # own oldest only; siblings untouched


def _write_sibling(skill: Path, name: str, tab_ids: list[str],
                   *, lock_alive: bool | None, resolver: list[dict] | None = None):
    d = skill / "data" / "briefs" / name
    d.mkdir(parents=True)
    (d / "owned_tabs.json").write_text(json.dumps({"tab_ids": tab_ids}))
    if lock_alive is not None:
        (d / "active_run.lock").write_text(json.dumps({"pid": 4242, "run_id": "x"}))
    if resolver:
        (d / "resolve_queue.jsonl").write_text(
            "\n".join(json.dumps(e) for e in resolver) + "\n"
        )
    return d


def test_sweep_closes_dead_runs_tabs_but_not_resolver_or_live(brief_env, monkeypatch):
    skill, _own = brief_env
    # dead sibling: no lock file at all → orphans minus resolver-pending
    _write_sibling(skill, "dead-brief", ["D1", "D2", "D3"], lock_alive=None,
                   resolver=[{"target_id": "x", "status": "pending", "tab_id": "D2"}])
    # live sibling: lock alive → untouched entirely
    _write_sibling(skill, "live-brief", ["L1"], lock_alive=True)

    from _outreach_core import active_run as ar
    monkeypatch.setattr(ar, "is_lock_alive", lambda lock: bool(lock))

    open_tabs = {"tabs": [
        {"targetId": t, "type": "page"} for t in ("D1", "D2", "D3", "L1", "UNKNOWN")
    ]}
    monkeypatch.setattr(run, "_list_tabs_payload", lambda: open_tabs)
    closed: list[str] = []
    monkeypatch.setattr(run, "_close_tab", lambda t: closed.append(t))
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)

    run._sweep_orphan_tabs()

    assert sorted(closed) == ["D1", "D3"]      # D2 = resolver-pending, kept
    assert "L1" not in closed                  # live sibling untouched
    assert "UNKNOWN" not in closed             # unrecorded tab untouched
    # dead sibling's record consumed; live sibling's kept
    assert not (skill / "data" / "briefs" / "dead-brief" / "owned_tabs.json").exists()
    assert (skill / "data" / "briefs" / "live-brief" / "owned_tabs.json").exists()


def test_sweep_never_raises_on_corrupt_record(brief_env, monkeypatch):
    skill, _own = brief_env
    d = skill / "data" / "briefs" / "corrupt-brief"
    d.mkdir(parents=True)
    (d / "owned_tabs.json").write_text("{not json")
    monkeypatch.setattr(
        run, "_list_tabs_payload",
        lambda: {"tabs": [{"targetId": "X", "type": "page"}]},
    )
    monkeypatch.setattr(run, "_close_tab", lambda t: None)
    run._sweep_orphan_tabs()  # must not raise
