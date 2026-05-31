from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core.prompt import extract_first_json


class TestPromptExtraction(unittest.TestCase):
    def test_extract_json_fence_object(self) -> None:
        text = '```json\n{"subject":"x","body":"y"}\n```'
        out = extract_first_json(text)
        self.assertIsNotNone(out)
        self.assertEqual(out, {"subject": "x", "body": "y"})

    def test_extract_with_prologue_and_epilogue(self) -> None:
        text = (
            "以下が結果です。\n"
            "```json\n"
            '{"subject":"件名","body":"本文"}\n'
            "```\n"
            "以上です。"
        )
        out = extract_first_json(text)
        self.assertEqual(out, {"subject": "件名", "body": "本文"})

    def test_extract_from_json_array_returns_first_object(self) -> None:
        text = 'prefix [{"subject":"a","body":"b"}, {"subject":"c","body":"d"}] suffix'
        out = extract_first_json(text)
        self.assertEqual(out, {"subject": "a", "body": "b"})

    def test_unparseable_returns_none(self) -> None:
        self.assertIsNone(extract_first_json("```json\nnot-json\n```"))


if __name__ == "__main__":
    unittest.main()
