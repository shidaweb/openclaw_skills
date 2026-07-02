"""v24 §S3 — _submission_loop state-machine scenarios against a fake browser.

The fake `_evaluate` serves scripted page states; a successful click advances
the script. This pins the three structural behaviours the closed-loop driver
was built for:

1. confirm flow completes by OBSERVING input → confirm → done (no assumed
   transitions, journal written exactly once before the real submit),
2. clicks that never change the page (cancelled confirm() dialog class) are
   detected as "ineffective" instead of surfacing as a verify failure,
3. a validation bounce that auto-fix cannot clear terminates as
   "validation_stuck" instead of final-submitting the invalid input page.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


INPUT_PAGE = {
    "url": "https://example.co.jp/contact/",
    "title": "お問い合わせ",
    "text": "お名前 メールアドレス お問い合わせ内容 入力内容を確認する",
    "visible_forms": 1,
    "visible_textareas": 1,
    "editable_visible": 8,
    "submit_controls": 1,
    "probe_text_hits": 0,
    "probe_field_hits": 3,
    "dialog_count": 0,
}
CONFIRM_PAGE = {
    "url": "https://example.co.jp/contact/confirm",
    "title": "入力内容の確認",
    "text": "上記の内容でよろしければ送信ボタンをクリックしてください 志田典道 株式会社トラーナ",
    "visible_forms": 1,
    "visible_textareas": 0,
    "editable_visible": 0,
    "submit_controls": 1,
    "probe_text_hits": 2,
    "probe_field_hits": 0,
    "dialog_count": 0,
}
DONE_PAGE = {
    "url": "https://example.co.jp/contact/thanks",
    "title": "送信完了",
    "text": "お問い合わせありがとうございました。送信完了しました。",
    "visible_forms": 0,
    "visible_textareas": 0,
    "editable_visible": 0,
    "submit_controls": 0,
    "probe_text_hits": 0,
    "probe_field_hits": 0,
    "dialog_count": 0,
}
CF7_STICKY_DONE_PAGE = {
    "url": "https://example.co.jp/contact/",
    "title": "お問い合わせ",
    "text": "お問い合わせフォーム",
    "visible_forms": 1,
    "visible_textareas": 1,
    "editable_visible": 8,
    "submit_controls": 1,
    "probe_text_hits": 0,
    "probe_field_hits": 0,
    "dialog_count": 0,
    "cf7_sent": True,
    "cf7_invalid": False,
    "cf7_statuses": ["sent wpcf7-form sent"],
    "cf7_response_text": "ありがとうございます。メッセージは送信されました。",
}
GENERIC_STICKY_DONE_PAGE = {
    "url": "https://example.co.jp/contact/",
    "title": "お問い合わせ",
    "text": "お問い合わせフォーム",
    "visible_forms": 1,
    "visible_textareas": 1,
    "editable_visible": 8,
    "submit_controls": 1,
    "probe_text_hits": 0,
    "probe_field_hits": 0,
    "dialog_count": 0,
    "submission_sent": True,
    "submission_statuses": ["success form-success submitted"],
    "submission_status_text": "お問い合わせを受け付けました。",
}


def _validation_page(n: int) -> dict:
    return {
        **INPUT_PAGE,
        "url": "https://example.co.jp/contact/",
        "text": f"入力エラーがあります（{n}件）。業界は必須です。",
        "probe_field_hits": 3,
    }


class FakeBrowser:
    """Scripted page sequence; a successful click advances to the next page."""

    def __init__(self, pages: list[dict], click_advances: bool = True) -> None:
        self.pages = pages
        self.idx = 0
        self.clicks = 0
        self.click_advances = click_advances

    @property
    def page(self) -> dict:
        return self.pages[min(self.idx, len(self.pages) - 1)]

    def evaluate(self, js: str):
        if "probe_text_hits" in js:  # send_state evidence probe (check FIRST)
            return dict(self.page)
        if "__ocDialogArmed" in js:
            return {"armed": True}
        if js.strip().startswith("() => (window.__ocDialogLog"):
            return []
        if "title: document.title" in js:  # verify.PAGE_EVIDENCE_JS
            return {"url": self.page["url"], "title": self.page["title"],
                    "text": self.page["text"]}
        if '"patterns"' in js and "const fn =" in js:  # click-button JS
            self.clicks += 1
            if self.click_advances and self.idx < len(self.pages) - 1:
                self.idx += 1
            return {"clicked": True, "text": "確認/送信", "scope": "form"}
        return []  # gate lists, text-field reads, etc.


def _wire(monkeypatch, fake: FakeBrowser) -> list:
    journal_calls: list = []
    monkeypatch.setattr(run, "_evaluate", fake.evaluate)
    monkeypatch.setattr(run, "oc_browser", lambda *a, **k: "")
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run.core_send_journal, "append_journal",
        lambda *a, **k: journal_calls.append((a, k)),
    )
    return journal_calls


def _target() -> dict:
    return {
        "id": "t1", "name": "テスト株式会社",
        "form_url": "https://example.co.jp/contact/",
        "draft": {"subject": "x", "body": "はじめまして。突然のご連絡で恐れ入ります。"},
    }


CONFIG = {"sender": {"name": "志田典道", "email": "shida@torana.co.jp",
                     "company": "株式会社トラーナ"}}


def test_confirm_flow_completes_via_observed_states(monkeypatch):
    fake = FakeBrowser([INPUT_PAGE, CONFIRM_PAGE, DONE_PAGE])
    journal = _wire(monkeypatch, fake)
    timeline: list = []
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。突然のご連絡で恐れ入ります。",
        flow="confirm", mode="auto", trace=None, tid="t1", timeline=timeline,
    )
    assert res["status"] == "done"
    assert res["clicks"] == 2
    # journal written exactly once — before the OBSERVED final submit
    assert len(journal) == 1
    # the confirm page was OBSERVED (not assumed) before the final click
    assert any(t.get("stage") == "confirm_page" for t in timeline)
    live = [
        (t.get("detail") or {}).get("state")
        for t in timeline if t.get("stage") == "live_state"
    ]
    assert live[:3] == ["input", "confirm", "done"]


def test_single_flow_mislabeled_as_confirm_still_completes(monkeypatch):
    # enrich said confirm but the form is single-step: input → done
    fake = FakeBrowser([INPUT_PAGE, DONE_PAGE])
    _wire(monkeypatch, fake)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="confirm", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "done"
    assert res["clicks"] == 1


def test_cf7_ajax_success_with_visible_form_completes(monkeypatch):
    # Bookoff-class: CF7 Ajax success leaves the form visible, so DOM visibility
    # alone would loop until wizard_too_deep.
    fake = FakeBrowser([INPUT_PAGE, CF7_STICKY_DONE_PAGE])
    _wire(monkeypatch, fake)
    timeline: list = []
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=timeline,
    )
    assert res["status"] == "done"
    assert res["clicks"] == 1
    live = [
        (t.get("detail") or {}).get("state")
        for t in timeline if t.get("stage") == "live_state"
    ]
    assert live[:2] == ["input", "done"]


def test_generic_ajax_success_with_visible_form_completes(monkeypatch):
    fake = FakeBrowser([INPUT_PAGE, GENERIC_STICKY_DONE_PAGE])
    _wire(monkeypatch, fake)
    timeline: list = []
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=timeline,
    )
    assert res["status"] == "done"
    assert res["clicks"] == 1
    live = [
        (t.get("detail") or {}).get("state")
        for t in timeline if t.get("stage") == "live_state"
    ]
    assert live[:2] == ["input", "done"]


def test_ineffective_clicks_detected_not_misreported(monkeypatch):
    # the cancelled-confirm()-dialog class: clicks "succeed", page never changes
    fake = FakeBrowser([INPUT_PAGE], click_advances=False)
    _wire(monkeypatch, fake)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "ineffective"
    assert res["clicks"] >= 2  # it retried before giving up


def test_silent_bounce_with_native_invalid_field_is_validation_stuck(monkeypatch):
    fake = FakeBrowser([INPUT_PAGE], click_advances=False)
    _wire(monkeypatch, fake)
    monkeypatch.setattr(
        run,
        "_snapshot_native_validation",
        lambda **kwargs: {
            "valid": False,
            "invalid_count": 1,
            "invalid": [{
                "name": "requestGroup[]",
                "label": "お問い合わせ種別",
                "type": "radio",
                "reasons": ["valueMissing"],
                "message": "このフィールドを入力してください。",
            }],
        },
    )
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "validation_stuck"
    assert res["errors"][0]["field"] == "お問い合わせ種別"
    assert "valueMissing" in res["errors"][0]["kind"]


def test_unfixable_validation_bounce_terminates_as_validation_stuck(monkeypatch):
    # distinct bounce pages each round (fingerprint changes) — never proceeds
    # to a blind final submit, terminates with the honest reason instead
    fake = FakeBrowser([_validation_page(1), _validation_page(2), _validation_page(3)])
    _wire(monkeypatch, fake)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "validation_stuck"


# v30 §WS-A — wizard same-button-streak gate.
#
# Production failure: Fujisoft 2026-06-29 clicked 次へ three times against an
# input page whose fingerprint flickered (validation banner re-rendered). The
# legacy `no_progress` gate did NOT catch this because the fingerprint was
# never identical twice in a row. The wizard's same-button gate must fire
# even when the fingerprint flickers, as long as the observation state stays
# the same and the same button is clicked repeatedly.


class FlickeringFingerprintBrowser:
    """Same observation_state across clicks, but a new fingerprint each time —
    every iteration returns slightly different text so the legacy no_progress
    counter never trips. Used to lock in the wizard's same-button-streak gate."""

    def __init__(self, base_page: dict) -> None:
        self.base = base_page
        self.clicks = 0
        self.observe_calls = 0

    def evaluate(self, js: str):
        if "probe_text_hits" in js:  # send_state evidence probe
            self.observe_calls += 1
            return {**self.base, "text": self.base["text"] + f" round={self.observe_calls}"}
        if "__ocDialogArmed" in js:
            return {"armed": True}
        if js.strip().startswith("() => (window.__ocDialogLog"):
            return []
        if "title: document.title" in js:
            return {"url": self.base["url"], "title": self.base["title"], "text": self.base["text"]}
        if '"patterns"' in js and "const fn =" in js:
            self.clicks += 1
            return {"clicked": True, "text": "次へ", "scope": "form"}
        return []


def test_wizard_same_button_streak_short_circuits_loop(monkeypatch):
    # Page state stays "input" through every observation; each click "succeeds"
    # but does nothing useful. The wizard fires REASON_SAME_BUTTON after 3
    # clicks of the same button text.
    fake = FlickeringFingerprintBrowser(INPUT_PAGE)
    _wire(monkeypatch, fake)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "ineffective"
    assert "wizard_stuck" in res
    stuck = res["wizard_stuck"]
    assert stuck["reason"] == "same_button_repeated"
    assert "次へ" in stuck["detail"]
    # The legacy MAX_FORM_STEPS+2=6 cap would have allowed up to 6 clicks; the
    # wizard short-circuits at exactly 3.
    assert res["clicks"] == 3


# v30 §WS-E — target_state checkpoint is written from the send loop.
#
# The runtime snapshot is a diagnostic breadcrumb: each observation updates
# hop / observation_state / last_button so a crashed process leaves enough
# information on disk for the next run to surface "this target was at wizard
# hop=N when the previous run died". send_journal owns the safety-critical
# double-send decision; this only enriches diagnostics.


def test_target_state_checkpoint_written_during_submission(monkeypatch, tmp_path):
    from _outreach_core import target_state as ts_mod

    fake = FakeBrowser([INPUT_PAGE, CONFIRM_PAGE, DONE_PAGE])
    _wire(monkeypatch, fake)
    captured: list[dict] = []
    original_merge = ts_mod.merge_update

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return original_merge(*args, **kwargs)

    monkeypatch.setattr(ts_mod, "merge_update", _capture)
    monkeypatch.setattr(run.core_target_state, "merge_update", _capture)
    monkeypatch.setattr(run, "DATA_DIR", tmp_path)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="confirm", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "done"
    # Each observation produced a merge_update with phase="send.observed".
    observed_phases = [c.get("phase") for c in captured]
    assert "send.observed" in observed_phases
    # The hop value progresses across observations (>=2 because confirm flow
    # observes input → confirm → done).
    hops = [c.get("hop") for c in captured if c.get("hop") is not None]
    assert max(hops) >= 2


def test_target_state_cleared_on_terminal_skipped(monkeypatch, tmp_path):
    # When _auto_skip_and_log fires for a target, the runtime snapshot is
    # removed — list_runtime_states should not show the skipped target.
    from _outreach_core import target_state as ts_mod

    monkeypatch.setattr(run, "DATA_DIR", tmp_path)
    monkeypatch.setattr(run, "append_skip_history", lambda *a, **k: None)
    monkeypatch.setattr(run, "close_needs_attention", lambda *a, **k: True)
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    # Stub the WS-D notification so the test does not depend on Slack config.
    from _outreach_core import notify as _notify
    monkeypatch.setattr(_notify, "post_target_event", lambda **kw: True)

    target = {"id": "t-skip", "name": "ABC株式会社",
              "draft": {"body": "はじめまして。"}}
    # Seed a snapshot to verify it gets cleared.
    ts_mod.merge_update(tmp_path, "t-skip", phase="send.observed", hop=2)
    assert ts_mod.read_state(tmp_path, "t-skip") is not None
    run._auto_skip_and_log(target, "captcha_human_required: v2")
    # Snapshot must be gone now.
    assert ts_mod.read_state(tmp_path, "t-skip") is None


# --- v31 §WS6 — ineffective-click diagnosis + gated disabled-submit retry ----

DIAG_DISABLED = {
    "submit_total": 1, "submit_visible": 1, "disabled": 1,
    "aria_disabled": 0, "covered": 0,
    "samples": [{"text": "送信する", "disabled": True,
                 "aria_disabled": False, "covered_by": ""}],
}
DIAG_OVERLAY = {
    "submit_total": 1, "submit_visible": 1, "disabled": 0,
    "aria_disabled": 0, "covered": 1,
    "samples": [{"text": "送信する", "disabled": False,
                 "aria_disabled": False, "covered_by": "DIV.cookie-banner"}],
}


class DiagFakeBrowser(FakeBrowser):
    """FakeBrowser that also answers the _SUBMIT_DIAG_JS probe."""

    def __init__(self, pages, click_advances=True, diag=None):
        super().__init__(pages, click_advances)
        self.diag = diag or DIAG_DISABLED

    def evaluate(self, js: str):
        if "elementFromPoint" in js:  # _SUBMIT_DIAG_JS
            return dict(self.diag)
        return super().evaluate(js)


def test_ineffective_result_carries_diagnostics(monkeypatch):
    fake = DiagFakeBrowser([INPUT_PAGE], click_advances=False, diag=DIAG_OVERLAY)
    _wire(monkeypatch, fake)
    res = run._submission_loop(
        _target(), CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "ineffective"
    assert res["diagnostics"]["blocker"] == "overlay"
    assert res["diagnostics"]["samples"][0]["covered_by"] == "DIV.cookie-banner"


def test_disabled_submit_retry_reapplies_enable_steps_then_succeeds(monkeypatch):
    # click does nothing while the submit stays disabled; re-applying the
    # plan's enable gates "arms" the button and the next round completes.
    fake = DiagFakeBrowser([INPUT_PAGE, DONE_PAGE], click_advances=False,
                           diag=DIAG_DISABLED)
    _wire(monkeypatch, fake)
    applied: list = []

    def _apply(name, action, value, selector=None):
        applied.append((name, action, value))
        fake.click_advances = True  # gates satisfied → button armed
        return {"ok": True}

    monkeypatch.setattr(run, "_apply_field_action", _apply)
    monkeypatch.setattr(run, "_rescan_form_fields", lambda *a, **k: None)
    monkeypatch.setattr(run, "_auto_fill_live_gates", lambda **k: {"changed": 0})

    target = _target()
    target["_llm_plan"] = {
        "next_step": "single",
        "enable_sequence": [
            {"action": "select_radio", "name": "route", "value": "法人のお客様"},
            {"action": "click", "value": "送信"},  # must be SKIPPED in retry
        ],
    }
    res = run._submission_loop(
        target, CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "done"
    assert target["_enable_retry_done"] is True
    # only the radio step was re-applied; the click step was skipped
    assert applied == [("route", "select_radio", "法人のお客様")]


def test_disabled_submit_retry_fires_only_once(monkeypatch):
    # gates re-apply but the button never arms → terminal ineffective, and
    # the retry flag prevents a second attempt.
    fake = DiagFakeBrowser([INPUT_PAGE], click_advances=False, diag=DIAG_DISABLED)
    _wire(monkeypatch, fake)
    apply_calls: list = []
    monkeypatch.setattr(
        run, "_apply_field_action",
        lambda name, action, value, selector=None:
            apply_calls.append(name) or {"ok": True},
    )
    monkeypatch.setattr(run, "_rescan_form_fields", lambda *a, **k: None)
    monkeypatch.setattr(run, "_auto_fill_live_gates", lambda **k: {"changed": 0})

    target = _target()
    target["_llm_plan"] = {
        "next_step": "single",
        "enable_sequence": [
            {"action": "select_radio", "name": "route", "value": "法人のお客様"},
        ],
    }
    res = run._submission_loop(
        target, CONFIG, "はじめまして。",
        flow="single", mode="auto", trace=None, tid="t1", timeline=[],
    )
    assert res["status"] == "ineffective"
    assert res["diagnostics"]["blocker"] == "disabled_submit"
    assert target["_enable_retry_done"] is True
    # the enable step re-applied exactly once across the whole loop
    assert apply_calls.count("route") == 1
