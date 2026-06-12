"""
v25 regression tests — the 2026-06-12 kakuyasu / baycrews / petline failures.

Three distinct root causes, all reproduced verbatim from production:

1. baycrews — confirm page said 「ご確認のうえ、「この内容で送信する」ボタンを
   押してください」 but detect_confirm_instruction missed it (intervening する +
   closing quote between the verb and ボタン) → classified no_form → lost_form.
2. kakuyasu — required radio group 「現在お取り引きしている酒屋はありますか？」
   carried only a VISUAL 必須 mark (no DOM required attribute, unknown label),
   so pick_radio_gate_actions skipped it → validation_error loop, fixed=0.
3. petline — phone filled as 090-1650-1629 on a ハイフンなし form;
   「電話番号を正しく入力してください。」 was parsed as a junk REQUIRED error and
   no fixer addressed the format.
"""

from __future__ import annotations

import unittest

from _outreach_core import form_validation as fv
from _outreach_core import submit_progress as sp

BAYCREWS_CONFIRM_TEXT = (
    "以上の内容で送信します。ご確認のうえ、「この内容で送信する」ボタンを押してください。\n"
    "修正する場合は、ブラウザの戻るボタンで戻ってください。"
)

KAKUYASU_RADIO_GROUP = {
    "name": "torihiki_sakaya",
    "label": "現在お取り引きしている酒屋はありますか？",
    "required": False,  # visual 必須 mark only — no DOM attribute
    "selected": False,
    "options": [
        {"label": "ある", "value": "1", "checked": False},
        {"label": "ない", "value": "2", "checked": False},
        {"label": "カクヤスと取り引き中", "value": "3", "checked": False},
    ],
}


class TestConfirmInstructionQuotedButton(unittest.TestCase):
    def test_baycrews_instruction_detected(self) -> None:
        self.assertTrue(sp.detect_confirm_instruction(BAYCREWS_CONFIRM_TEXT))

    def test_classic_forms_still_detected(self) -> None:
        self.assertTrue(sp.detect_confirm_instruction(
            "上記の内容でよろしければ送信ボタンをクリックしてください"))
        self.assertTrue(sp.detect_confirm_instruction("送信ボタンを押してください"))

    def test_done_page_not_confused(self) -> None:
        self.assertFalse(sp.detect_confirm_instruction(
            "送信が完了しました。お問い合わせありがとうございました。"))

    def test_empty(self) -> None:
        self.assertFalse(sp.detect_confirm_instruction(""))
        self.assertFalse(sp.detect_confirm_instruction(None))


class TestValidationRadioRescue(unittest.TestCase):
    def test_gate_picker_skips_kakuyasu_group(self) -> None:
        # Documents WHY the aggressive pass exists: the normal gate picker
        # ignores a non-required unknown-label group.
        self.assertEqual(sp.pick_radio_gate_actions([KAKUYASU_RADIO_GROUP]), [])

    def test_validation_picker_answers_kakuyasu_group(self) -> None:
        actions = sp.pick_validation_radio_actions([KAKUYASU_RADIO_GROUP])
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["name"], "torihiki_sakaya")
        self.assertEqual(actions[0]["value"], "ない")  # neutral preferred

    def test_validation_picker_falls_back_to_first_option(self) -> None:
        g = dict(KAKUYASU_RADIO_GROUP)
        g["options"] = [
            {"label": "電話で連絡", "value": "tel", "checked": False},
            {"label": "メールで連絡", "value": "mail", "checked": False},
        ]
        g["label"] = "謎の選択肢"
        actions = sp.pick_validation_radio_actions([g])
        self.assertEqual(len(actions), 1)

    def test_validation_picker_skips_selected_groups(self) -> None:
        g = dict(KAKUYASU_RADIO_GROUP)
        g["selected"] = True
        self.assertEqual(sp.pick_validation_radio_actions([g]), [])


class TestPhoneFormatError(unittest.TestCase):
    def test_petline_error_parsed_as_phone_format(self) -> None:
        errors = fv.parse_validation_errors("電話番号を正しく入力してください。")
        self.assertTrue(any(
            e["kind"] == "format" and fv.is_phone_field_label(e["field"])
            for e in errors
        ))

    def test_classic_format_error_still_parsed(self) -> None:
        errors = fv.parse_validation_errors("「メールアドレス」の形式が正しくありません")
        self.assertEqual(errors[0]["kind"], "format")

    def test_toggle_strips_hyphens(self) -> None:
        self.assertEqual(fv.toggle_phone_hyphens("090-1650-1629"), "09016501629")

    def test_toggle_adds_hyphens_11_digits(self) -> None:
        self.assertEqual(fv.toggle_phone_hyphens("09016501629"), "090-1650-1629")

    def test_toggle_adds_hyphens_10_digits(self) -> None:
        self.assertEqual(fv.toggle_phone_hyphens("0312345678"), "03-1234-5678")

    def test_non_phone_unchanged(self) -> None:
        self.assertEqual(fv.toggle_phone_hyphens("abc"), "abc")
        self.assertEqual(fv.toggle_phone_hyphens(""), "")
        self.assertEqual(fv.toggle_phone_hyphens("123"), "123")


class TestClassifyBaycrewsConfirmPage(unittest.TestCase):
    def test_confirm_state_with_instruction_and_no_echo(self) -> None:
        # baycrews confirm03: instruction present, buttons visible, but our
        # values NOT echoed (text_hits=0). Before the regex fix this fell all
        # the way to no_form → lost_form.
        from _outreach_core.send_state import classify_send_state

        obs = classify_send_state({
            "url": "https://baycrews.co.jp/contact/confirm03",
            "text": BAYCREWS_CONFIRM_TEXT,
            "visible_forms": 1,
            "visible_textareas": 0,
            "editable_visible": 0,
            "submit_controls": 2,
            "probe_text_hits": 0,
            "probe_field_hits": 0,
        })
        self.assertEqual(obs["state"], "confirm")


if __name__ == "__main__":
    unittest.main()
