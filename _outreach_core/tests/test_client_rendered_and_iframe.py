"""v30 §WS-B — _assess_page_and_recover handles client-rendered SPAs and
iframe-hosted forms.

Two production failure modes:

  * **Medley / ROXX** (2026-06-29): real contact pages where React + code
    splitting takes 5–8s to populate the DOM. The legacy single 3s wait was
    a magic number that fired too early; the page was misread as
    ``empty_render`` and routed to ``page_has_no_form``.
  * **LegalOn** (2026-06-29): top-level /contact/ embeds an iframe hosting
    the real form (lp.legalforce-cloud.com). The LLM analyzer noted this in
    its warnings but the send pipeline never switched into the iframe, so
    the form's content never landed at submit time.

These tests lock in the progressive poll + iframe takeover behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402


_PAGE_OK = {
    "state": "form_ok", "inputs": 5, "textareas": 1,
    "submit_buttons": 1, "radio_groups": 0, "checkboxes": 0,
}
_PAGE_EMPTY = {
    "state": "empty_render", "inputs": 0, "textareas": 0,
    "submit_buttons": 0, "radio_groups": 0, "checkboxes": 0,
}
_PAGE_NO_FORM = {
    "state": "no_form", "inputs": 0, "textareas": 0,
    "submit_buttons": 0, "radio_groups": 0, "checkboxes": 0,
}


class FakeScans:
    """Counter-driven script of scan results.

    Each call to ``classify_page_form_state`` consumes the next entry in
    ``scans``. After the script is exhausted, the last entry keeps repeating
    so tests that don't care about the tail won't raise IndexError."""

    def __init__(self, scans: list[dict]) -> None:
        self.scans = scans
        self.calls = 0

    def classify(self, fields, text) -> dict:
        idx = min(self.calls, len(self.scans) - 1)
        self.calls += 1
        return self.scans[idx]


def _wire(monkeypatch, scan_results: list[dict], **kwargs):
    """Wire the helpers _assess_page_and_recover depends on. Returns the
    FakeScans instance so tests can assert call counts."""
    fake = FakeScans(scan_results)
    monkeypatch.setattr(run, "_rescan_form_fields", lambda d: {})
    monkeypatch.setattr(run, "_evaluate", lambda *a, **k: "body text")
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run.core_contact_url, "classify_page_form_state",
        fake.classify,
    )
    monkeypatch.setattr(
        run.core_contact_url, "detect_email_verification",
        lambda *a, **k: {"detected": False, "evidence": []},
    )
    monkeypatch.setattr(
        run.core_contact_url, "contact_link_candidates",
        lambda *a, **k: kwargs.get("link_cands", []),
    )
    monkeypatch.setattr(run, "_list_page_links", lambda: [])
    return fake


def _target(form_url: str = "https://example.co.jp/contact/") -> dict:
    return {"id": "t", "name": "テスト株式会社", "form_url": form_url,
            "draft": {"body": "はじめまして。"}}


def test_client_rendered_page_rescued_on_first_poll(monkeypatch):
    # SPA: empty on first scan, form_ok on the second (after ~1.5s wait).
    fake = _wire(monkeypatch, [_PAGE_EMPTY, _PAGE_OK])
    ok, state = run._assess_page_and_recover(_target(), [], None)
    assert ok is True
    assert state["state"] == "form_ok"
    # 2 calls: initial + first poll. The second poll (3s) is skipped because
    # we already succeeded.
    assert fake.calls == 2


def test_client_rendered_page_rescued_on_second_poll(monkeypatch):
    # Slower SPA: still empty after 1.5s, form_ok after the further 3s wait.
    fake = _wire(monkeypatch, [_PAGE_EMPTY, _PAGE_EMPTY, _PAGE_OK])
    ok, state = run._assess_page_and_recover(_target(), [], None)
    assert ok is True
    assert state["state"] == "form_ok"
    assert fake.calls == 3


def test_genuinely_empty_page_falls_through_to_recovery(monkeypatch):
    # All three polls return empty — we exhaust the poll budget. The
    # subsequent recovery hop / iframe takeover may then fire; here we
    # simulate no iframe and no recovery candidates, so the assessor reports
    # state="empty_render" (form_url_locked is also False so it tries
    # contact_link_candidates which we stub to []).
    fake = _wire(monkeypatch, [_PAGE_EMPTY, _PAGE_EMPTY, _PAGE_EMPTY])
    ok, state = run._assess_page_and_recover(_target(), [], None)
    assert ok is False
    # Three poll-pass scans were exhausted before the recovery hop.
    assert fake.calls >= 3


def test_iframe_takeover_invokes_navigation_and_emits_event(monkeypatch):
    # Direct unit test of _try_iframe_form_takeover. The fields include an
    # iframe whose host is a known hosted-form service; the takeover should
    # call oc_browser("open", src) and emit send.iframe_form_takeover.
    opens: list[tuple] = []
    events: list[dict] = []
    monkeypatch.setattr(run, "oc_browser",
                        lambda *a, **k: opens.append((a, k)) or "")
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        run, "_emit_event",
        lambda kind, **kw: events.append({"kind": kind, **kw}),
    )

    target = {"id": "legalon", "name": "LegalOn Technologies",
              "form_url": "https://legalontech.jp/contact/"}
    fields = {
        "iframes": [
            {"src": "https://lp.legalforce-cloud.com/index.php/form/XDFrame"},
        ],
    }
    src = run._try_iframe_form_takeover(target, fields, None, "legalon")
    assert src is not None
    assert "legalforce-cloud.com" in src
    # The takeover navigates and emits a structured event.
    assert any(a[0] == "open" for a, _ in opens)
    kinds = [e["kind"] for e in events]
    assert "send.iframe_form_takeover" in kinds
    # form_url is updated so downstream diagnostics reference the actual
    # submission URL rather than the parent page.
    assert target["form_url"] == src


def test_iframe_takeover_returns_none_when_no_iframe(monkeypatch):
    monkeypatch.setattr(run, "oc_browser", lambda *a, **k: "")
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    target = {"id": "x", "name": "X", "form_url": "https://x.example/"}
    assert run._try_iframe_form_takeover(target, {"iframes": []}, None, "x") is None


def test_iframe_takeover_returns_none_for_unrelated_iframe_host(monkeypatch):
    # An iframe pointing to youtube / cdn / tracking should NOT be taken as
    # the form host — only known form services or same-domain embeds win.
    monkeypatch.setattr(run, "oc_browser", lambda *a, **k: "")
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    target = {"id": "x", "name": "X", "form_url": "https://x.example/"}
    fields = {"iframes": [{"src": "https://www.youtube.com/embed/abc123"}]}
    assert run._try_iframe_form_takeover(target, fields, None, "x") is None


def test_iframe_takeover_returns_none_when_oc_browser_open_raises(monkeypatch):
    # Defensive: navigation failure must not crash the send loop.
    def _boom(*a, **k):
        raise RuntimeError("nav failed")

    monkeypatch.setattr(run, "oc_browser", _boom)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    monkeypatch.setattr(run, "_emit_event", lambda *a, **k: None)
    target = {"id": "y", "name": "Y", "form_url": "https://y.example/"}
    fields = {"iframes": [{"src": "https://share.hsforms.com/abc"}]}
    assert run._try_iframe_form_takeover(target, fields, None, "y") is None


# v30 §WS-B — policy table for classify_form_type. Each rule is locked in its
# own test so a regression points at the offending policy directly rather
# than at the umbrella function.


class TestClassifyFormPolicyTable:
    def test_password_field_wins_over_everything(self) -> None:
        from _outreach_core import contact_url as cu

        fields = {
            "inputs": [
                {"type": "password", "name": "password"},
                {"type": "text", "name": "name"},
            ],
            "textareas": [{"name": "message"}],
        }
        # A textarea would otherwise call this contact; the password rule
        # takes precedence.
        kind, reason = cu.classify_form_type(fields, "お問い合わせ ログイン")
        assert kind == "register"
        assert "password" in (reason or "")

    def test_birth_year_field_marks_register(self) -> None:
        from _outreach_core import contact_url as cu

        fields = {
            "inputs": [
                {"type": "text", "name": "name"},
                {"type": "text", "name": "birth_year"},
                {"type": "text", "name": "birth_month"},
            ],
            "textareas": [{"name": "message"}],
        }
        kind, reason = cu.classify_form_type(fields, "新規登録")
        assert kind == "register"
        assert "birth" in (reason or "")

    def test_recruit_heading_alone_not_enough_with_real_contact_textarea(self) -> None:
        from _outreach_core import contact_url as cu

        # A bare 採用 link in the navigation shouldn't poison /contact/.
        fields = {
            "inputs": [{"type": "text", "name": "name"}],
            "textareas": [{"name": "inquiry", "label": "お問い合わせ内容"}],
        }
        snap = "お問い合わせフォーム ナビ 採用情報"
        kind, _ = cu.classify_form_type(fields, snap)
        assert kind == "contact"

    def test_recruit_heading_plus_applicant_field_is_recruit(self) -> None:
        from _outreach_core import contact_url as cu

        fields = {
            "inputs": [
                {"type": "text", "name": "applicant_name"},
                {"type": "text", "name": "desired_position"},
            ],
            "textareas": [{"name": "self_pr"}],
        }
        snap = "中途採用エントリー"
        kind, _ = cu.classify_form_type(fields, snap)
        assert kind == "recruit"

    def test_b2b_hint_overrides_ir_heading(self) -> None:
        from _outreach_core import contact_url as cu

        # IR heading would normally route to ir; the B2B hint escape hatch
        # rescues the contact verdict.
        fields = {
            "inputs": [{"type": "text", "name": "company"}],
            "textareas": [{"name": "inquiry", "label": "お問い合わせ内容"}],
        }
        snap = "IR 投資家情報 法人のお問い合わせ"
        kind, _ = cu.classify_form_type(fields, snap)
        assert kind == "contact"

    def test_textarea_plus_submit_is_contact(self) -> None:
        from _outreach_core import contact_url as cu

        fields = {
            "inputs": [{"type": "text", "name": "email"}],
            "textareas": [{"name": "message", "label": "お問い合わせ内容"}],
            "submit_buttons": [{"text": "送信"}],
        }
        kind, reason = cu.classify_form_type(fields, "お問い合わせ")
        assert kind == "contact"
        assert reason in ("textarea_plus_submit", None)

    def test_no_textarea_falls_to_unknown(self) -> None:
        from _outreach_core import contact_url as cu

        fields = {
            "inputs": [{"type": "text", "name": "email"}],
            "textareas": [],
        }
        kind, _ = cu.classify_form_type(fields, "ニュースレター登録")
        assert kind == "unknown_no_textarea"

    def test_policies_run_in_declared_order(self) -> None:
        # The exported tuple matches the policy ordering — if a refactor
        # accidentally re-orders rules, the catch-all tests above might still
        # pass while the priority semantics shift. This locks the order
        # explicitly.
        from _outreach_core import contact_url as cu

        names = [p.__name__ for p in cu._CLASSIFY_POLICIES]
        assert names == [
            "_policy_password_field",
            "_policy_birthdate_field",
            "_policy_recruit_strong",
            "_policy_non_contact_headings",
            "_policy_pre_form_gate",
            "_policy_textarea_plus_submit",
            "_policy_no_textarea",
        ]
