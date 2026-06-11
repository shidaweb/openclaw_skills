"""v24 §S3 — closed-loop submission driver: page-state classifier tests."""

from __future__ import annotations

from _outreach_core import send_state


def _ev(**kw):
    base = {
        "url": "https://example.co.jp/contact/",
        "title": "お問い合わせ",
        "text": "",
        "visible_forms": 1,
        "visible_textareas": 1,
        "editable_visible": 8,
        "submit_controls": 1,
        "probe_text_hits": 0,
        "probe_field_hits": 0,
        "dialog_count": 0,
    }
    base.update(kw)
    return base


# --- input page ---------------------------------------------------------------

def test_filled_input_page_is_input():
    obs = send_state.classify_send_state(
        _ev(text="お名前 メールアドレス お問い合わせ内容", probe_field_hits=3)
    )
    assert obs["state"] == "input"


def test_empty_evidence_is_no_form():
    obs = send_state.classify_send_state({})
    assert obs["state"] == "no_form"


# --- validation bounce ----------------------------------------------------------

def test_validation_bounce_is_validation_error():
    obs = send_state.classify_send_state(
        _ev(
            text="入力内容に誤りがあります。フリガナは全角カタカナで入力してください。",
            probe_field_hits=3,
        )
    )
    assert obs["state"] == "validation_error"


def test_error_keyword_without_form_is_not_validation():
    # error text on a page with no editable fields → not a fixable bounce
    obs = send_state.classify_send_state(
        _ev(
            text="送信できませんでした",
            visible_forms=0, visible_textareas=0, editable_visible=0,
            submit_controls=0,
        )
    )
    assert obs["state"] == "no_form"


# --- confirm page ---------------------------------------------------------------

def test_confirm_page_via_value_echo():
    # our submitted values echoed as TEXT, no textarea, nothing editable
    obs = send_state.classify_send_state(
        _ev(
            text="入力内容の確認 志田典道 shida@torana.co.jp 株式会社トラーナ",
            visible_textareas=0, editable_visible=0,
            probe_text_hits=3, probe_field_hits=0,
        )
    )
    assert obs["state"] == "confirm"


def test_confirm_page_via_instruction_text():
    obs = send_state.classify_send_state(
        _ev(
            text="上記の内容でよろしければ送信ボタンをクリックしてください",
            visible_textareas=0, editable_visible=0,
            probe_text_hits=1,
        )
    )
    assert obs["state"] == "confirm"


def test_confirm_instruction_outranks_success_looking_keyword():
    # 「以下の内容でお問い合わせを受け付けます」 contains a success keyword but the
    # send-button instruction marks it as a confirm page, NOT done.
    obs = send_state.classify_send_state(
        _ev(
            text=(
                "以下の内容でお問い合わせを受け付けます。"
                "よろしければ送信ボタンを押してください。志田典道 株式会社トラーナ"
            ),
            visible_textareas=0, editable_visible=0,
            probe_text_hits=2,
        )
    )
    assert obs["state"] == "confirm"


def test_confirm_with_disabled_value_echo_fields_still_actable():
    # readonly/disabled inputs are excluded from editable_visible by the JS;
    # the classifier sees editable=0 → confirm.
    obs = send_state.classify_send_state(
        _ev(
            text="確認画面 志田典道 shida@torana.co.jp",
            visible_textareas=0, editable_visible=0,
            probe_text_hits=2,
        )
    )
    assert obs["state"] == "confirm"


# --- done page -------------------------------------------------------------------

def test_done_page_via_success_keyword():
    obs = send_state.classify_send_state(
        _ev(
            text="お問い合わせありがとうございました。送信完了しました。",
            visible_forms=0, visible_textareas=0, editable_visible=0,
            submit_controls=0,
        )
    )
    assert obs["state"] == "done"


def test_done_page_via_thanks_url():
    obs = send_state.classify_send_state(
        _ev(
            url="https://example.co.jp/contact/thanks.html",
            text="トップページへ戻る",
            visible_forms=0, visible_textareas=0, editable_visible=0,
            submit_controls=0,
        )
    )
    assert obs["state"] == "done"


def test_success_keyword_with_our_values_still_in_fields_is_not_done():
    # bounced page that mentions 完了 in boilerplate but the form is still live
    obs = send_state.classify_send_state(
        _ev(
            text="送信完了までいましばらくお待ちください",
            probe_field_hits=3,
        )
    )
    assert obs["state"] == "input"


# --- fingerprint / transition ------------------------------------------------------

def test_fingerprint_stable_and_sensitive():
    a = send_state.page_fingerprint(_ev(text="A"))
    a2 = send_state.page_fingerprint(_ev(text="A"))
    b = send_state.page_fingerprint(_ev(text="B"))
    c = send_state.page_fingerprint(_ev(text="A", url="https://example.co.jp/confirm"))
    assert a == a2
    assert a != b
    assert a != c


# --- probes ---------------------------------------------------------------------

def test_build_probes_filters_short_and_dedupes():
    probes = send_state.build_probes(
        {"email": "shida@torana.co.jp", "name": "志田典道", "company": "株式会社トラーナ",
         "phone": "090"},
        "はじめまして。\n  突然のご連絡で恐れ入ります。",
    )
    assert "shida@torana.co.jp" in probes
    assert "株式会社トラーナ" in probes
    assert "090" not in probes  # too short
    assert any(p.startswith("はじめまして。 突然の") for p in probes)  # ws-normalized head


def test_evidence_js_embeds_probes_safely():
    js = send_state.evidence_js(["shida@torana.co.jp", "株式会社トラーナ"])
    assert "shida@torana.co.jp" in js
    assert "__PROBES__" not in js
    assert js.lstrip().startswith("(() => {")


def test_dialog_js_shape():
    assert "window.confirm" in send_state.DIALOG_AUTOACCEPT_JS
    assert "return true" in send_state.DIALOG_AUTOACCEPT_JS
    assert "onbeforeunload" in send_state.DIALOG_AUTOACCEPT_JS
    assert send_state.READ_DIALOG_LOG_JS.startswith("() =>")
