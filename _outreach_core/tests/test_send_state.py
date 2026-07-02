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


def test_progress_done_label_with_echo_and_submit_button_is_confirm():
    # Step indicators often include 「送信完了」 while the page is still a
    # confirmation screen. Echoed values + a pending submit control must win.
    obs = send_state.classify_send_state(
        _ev(
            text="入力画面 確認画面 送信完了 志田典道 shida@torana.co.jp 株式会社トラーナ",
            visible_textareas=0,
            editable_visible=0,
            submit_controls=2,
            final_submit_controls=1,
            probe_text_hits=3,
            probe_field_hits=0,
        )
    )
    assert obs["state"] == "confirm"
    assert obs["send_verdict"] == "uncertain"


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


def test_success_keyword_without_pending_submit_is_sent_evidence():
    result = send_state.assess_submission_result(
        _ev(
            text="お問い合わせありがとうございました。内容を確認次第ご連絡いたします。",
            visible_forms=1,
            visible_textareas=0,
            editable_visible=1,
            submit_controls=1,
            final_submit_controls=0,
            probe_field_hits=0,
        )
    )
    assert result["verdict"] == "sent_ok"
    assert "success_without_pending_submit" in result["signals"]


def test_success_keyword_with_our_values_still_in_fields_is_not_done():
    # bounced page that mentions 完了 in boilerplate but the form is still live
    obs = send_state.classify_send_state(
        _ev(
            text="送信完了までいましばらくお待ちください",
            probe_field_hits=3,
        )
    )
    assert obs["state"] == "input"


def test_cf7_sent_with_visible_form_is_done():
    # Contact Form 7 often leaves the form/textarea visible after Ajax success.
    obs = send_state.classify_send_state(
        _ev(
            text="お問い合わせフォーム",
            cf7_sent=True,
            cf7_statuses=["sent wpcf7-form sent"],
            cf7_response_text="ありがとうございます。メッセージは送信されました。",
            visible_forms=1,
            visible_textareas=1,
            editable_visible=8,
            probe_field_hits=0,
        )
    )
    assert obs["state"] == "done"


def test_cf7_invalid_with_visible_form_is_validation_error():
    obs = send_state.classify_send_state(
        _ev(
            text="入力内容に問題があります。確認してもう一度お試しください。",
            cf7_invalid=True,
            cf7_statuses=["invalid wpcf7-form invalid"],
            visible_forms=1,
            visible_textareas=1,
            editable_visible=8,
            probe_field_hits=3,
        )
    )
    assert obs["state"] == "validation_error"


def test_generic_sent_status_with_visible_form_is_done():
    obs = send_state.classify_send_state(
        _ev(
            text="お問い合わせフォーム",
            submission_sent=True,
            submission_statuses=["success form-success submitted"],
            submission_status_text="お問い合わせを受け付けました。",
            visible_forms=1,
            visible_textareas=1,
            editable_visible=8,
            probe_field_hits=0,
        )
    )
    assert obs["state"] == "done"
    assert obs["send_verdict"] == "sent_ok"


def test_generic_invalid_status_with_visible_form_is_validation_error():
    obs = send_state.classify_send_state(
        _ev(
            text="お問い合わせフォーム",
            submission_invalid=True,
            submission_statuses=["error form-error invalid"],
            submission_status_text="入力内容に問題があります。",
            visible_forms=1,
            visible_textareas=1,
            editable_visible=8,
            probe_field_hits=3,
        )
    )
    assert obs["state"] == "validation_error"
    assert obs["send_verdict"] == "failed"


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


# --- v31 §WS7a: URL success tokens ------------------------------------------

def test_url_success_token_matches():
    ok = send_state.url_looks_like_success
    assert ok("https://example.co.jp/contact/thanks/")
    assert ok("https://example.co.jp/contact/thank-you")
    assert ok("https://example.co.jp/inquiry/complete.html")
    assert ok("https://example.co.jp/form/kanryo")
    assert ok("https://example.co.jp/contact?mode=complete")
    assert ok("https://example.co.jp/contact/?sent=1")
    assert ok("https://example.co.jp/contact/done")


def test_url_success_rejects_substring_false_positives():
    ok = send_state.url_looks_like_success
    # the old substring matcher scored all of these +2
    assert not ok("https://example.co.jp/london/contact/")          # "done" in london
    assert not ok("https://example.co.jp/contact/?completed=0")     # explicit NOT completed
    assert not ok("https://example.co.jp/successstory/")            # no token boundary
    assert not ok("https://thanks.example.co.jp/contact/")          # hostname doesn't count
    assert not ok("https://example.co.jp/contact/")
    assert not ok("")


# --- v31 §WS7b: pre-submit baseline demotion --------------------------------

def _thanks_page_ev():
    return {
        "url": "https://example.co.jp/contact/thanks-guide/",
        "text": "お問い合わせを受け付けました。ありがとうございました。",
        "visible_forms": 0,
        "visible_textareas": 0,
        "editable_visible": 0,
        "submit_controls": 0,
        "final_submit_controls": 0,
        "probe_text_hits": 0,
        "probe_field_hits": 0,
    }


def test_baseline_demotes_pre_existing_success_markers():
    ev = _thanks_page_ev()
    # WITHOUT baseline the markers score (url +2, keyword +2, ...) → sent_ok
    no_base = send_state.assess_submission_result(ev)
    assert no_base["verdict"] == "sent_ok"
    # WITH a pre-submit baseline showing the SAME markers, they demote to 0.
    baseline = {
        "url": "https://example.co.jp/contact/thanks-guide/",
        "text": "お問い合わせを受け付けました。ありがとうございました。",
    }
    with_base = send_state.assess_submission_result(ev, baseline=baseline)
    assert with_base["verdict"] != "sent_ok"
    assert "success_url_present_pre_submit" in with_base["signals"]
    assert "success_keyword_present_pre_submit" in with_base["signals"]
    # demoted entries carry 0 points in the breakdown
    zeroed = {
        b["signal"] for b in with_base["score_breakdown"] if b["points"] == 0
    }
    assert "success_url_present_pre_submit" in zeroed


def test_baseline_does_not_demote_new_markers():
    # baseline was a normal input page → post-submit markers score normally
    ev = _thanks_page_ev()
    baseline = {
        "url": "https://example.co.jp/contact/",
        "text": "お名前 メールアドレス お問い合わせ内容",
    }
    result = send_state.assess_submission_result(ev, baseline=baseline)
    assert result["verdict"] == "sent_ok"


def test_baseline_demotes_pre_stamped_explicit_sent():
    # a framework that stamps "complete" on the INPUT page's container
    ev = _ev(
        submission_statuses=["form complete"],
        submission_sent=True,
        visible_textareas=0,
        editable_visible=0,
        submit_controls=0,
    )
    baseline = {"submission_statuses": ["form complete"], "submission_sent": True}
    result = send_state.assess_submission_result(ev, baseline=baseline)
    assert "explicit_sent_status_present_pre_submit" in result["signals"]
    assert result["verdict"] != "sent_ok"


# --- v31 §WS7: FINAL_SUBMIT_RE_JS consolidation ------------------------------

def test_final_submit_re_single_source():
    # the tightened regex: no bare 問い合わせ/完了, word-bounded send
    re_js = send_state.FINAL_SUBMIT_RE_JS
    assert "問い合わせ" not in re_js
    assert "完了" not in re_js
    assert r"\bsend\b" in re_js
    assert "送信" in re_js
    # evidence_js interpolates it (no leftover placeholder)
    js = send_state.evidence_js(["probe"])
    assert "__FINAL_SUBMIT_RE__" not in js
    assert re_js in js


def test_verify_js_uses_shared_final_submit_re():
    from _outreach_core import verify
    assert "__FINAL_SUBMIT_RE__" not in verify.PAGE_EVIDENCE_JS
    assert "__FINAL_SUBMIT_RE__" not in verify.FORM_VISIBILITY_JS
    assert send_state.FINAL_SUBMIT_RE_JS in verify.PAGE_EVIDENCE_JS
    assert send_state.FINAL_SUBMIT_RE_JS in verify.FORM_VISIBILITY_JS
