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
