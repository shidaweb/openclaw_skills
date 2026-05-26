"""v3 model policy: Sonnet default in infer; verify must not call LLM."""

from __future__ import annotations

import re
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import infer


class TestModelPolicy(unittest.TestCase):
    def test_oc_infer_default_is_sonnet(self) -> None:
        import inspect

        sig = inspect.signature(infer.oc_infer)
        default = sig.parameters["model"].default
        self.assertEqual(default, "claude-cli/claude-sonnet-4-6")
        self.assertIn("sonnet", default.lower())
        self.assertNotIn("opus", default.lower())

    def test_verify_has_no_llm_calls(self) -> None:
        src = (Path(__file__).resolve().parent.parent / "verify.py").read_text()
        # Strip module docstring so "no oc_infer" in docs does not false-positive.
        if '"""' in src:
            parts = src.split('"""', 2)
            body = parts[2] if len(parts) > 2 else src
        else:
            body = src
        self.assertNotRegex(body, r"\boc_infer\s*\(")
        self.assertNotRegex(body, r"openclaw\s+infer")
        self.assertNotRegex(body, r"subprocess\.(run|call|Popen).*infer")


if __name__ == "__main__":
    unittest.main()
