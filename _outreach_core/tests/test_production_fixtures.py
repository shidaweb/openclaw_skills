"""v30 §WS-G — regression-lock production failure patterns via fixture files.

The actual ``resolve_snapshot_*.txt`` files in
``jp-form-outreach/data/briefs/*/`` are gitignored (they contain prospect
PII), so the regression tests here load synthesized-but-realistic fixtures
under ``_outreach_core/tests/fixtures/aria_snapshots/`` instead. Each
fixture is a minimum-viable repro of a specific production bug; the test
asserts the parser / classifier / wizard returns the expected verdict.

If a future refactor accidentally re-opens any of these holes, the matching
fixture test fails — pointing at both the bug class and the specific
target id that originally surfaced it.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from _outreach_core import contact_url as cu
from _outreach_core import form_validation as fv
from _outreach_core import wizard as wz


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "aria_snapshots"


def _load(name: str) -> str:
    """Read a fixture, stripping the leading `#`-prefixed header so the
    parser only sees the production-shaped payload."""
    text = (FIXTURE_DIR / name).read_text(encoding="utf-8")
    lines = [
        line for line in text.splitlines()
        if not line.startswith("#")
    ]
    return "\n".join(lines).lstrip()


class TestFujisoftValidationLeak(unittest.TestCase):
    """Privacy-policy body paragraph + row labels must not surface as fields."""

    def test_parser_finds_no_phantom_fields(self) -> None:
        text = _load("fujisoft_validation_leak.txt")
        errs = fv.parse_validation_errors(text)
        self.assertEqual(
            errs, [],
            f"aria-tree leak re-opened: {[(e['field'], e['kind']) for e in errs]}",
        )

    def test_text_actually_contains_the_bug_pattern(self) -> None:
        # Defensive: confirm the fixture really does include the verb that
        # used to trigger the false positive. If the fixture is silently
        # truncated, the test above would pass for the wrong reason.
        text = _load("fujisoft_validation_leak.txt")
        self.assertIn("入力した検索キーワード", text)
        self.assertIn("必ず入力してください", text)


class TestSuperStudioRadioLeak(unittest.TestCase):
    """unselected radio gate + repeated 「内容確認へ」 must be detected by the
    wizard's same-button gate, even when validation_error is never observed."""

    def test_parser_finds_no_phantom_fields(self) -> None:
        text = _load("super_studio_radio_leak.txt")
        self.assertEqual(fv.parse_validation_errors(text), [])

    def test_wizard_stops_after_three_clicks_of_same_button(self) -> None:
        # Simulate the production click sequence: 3x 「内容確認へ」 with no
        # observation_state transition (page stays "input"). The wizard
        # must fire REASON_SAME_BUTTON instead of looping forever.
        state = wz.WizardState()
        cfg = wz.WizardConfig()
        for _ in range(3):
            wz.bump_after_observation(
                state, observation_state="input", fingerprint=f"fp_{_}",
            )
            wz.record_click(state, "内容確認へ")
        reason = wz.compute_stuck_reason(state, cfg)
        self.assertIsNotNone(reason)
        assert reason is not None
        self.assertEqual(reason.code, wz.REASON_SAME_BUTTON)


class TestMilTextRequiredLeak(unittest.TestCase):
    """Short tree-node descriptors must never produce field entries."""

    def test_parser_drops_short_tree_node_descriptors(self) -> None:
        text = _load("mil_text_required_leak.txt")
        errs = fv.parse_validation_errors(text)
        self.assertEqual(
            errs, [],
            "short aria-tree descriptors leaked through the parser: "
            f"{[(e['field'], e['kind']) for e in errs]}",
        )


class TestLegalOnIframeTakeover(unittest.TestCase):
    """A hosted-form iframe on a different registrable domain must be
    detected so the send pipeline can take it over."""

    def test_iframe_form_src_detects_known_service_host(self) -> None:
        raw = _load("legalon_iframe_takeover.txt")
        data = json.loads(raw)
        src = cu.iframe_form_src(data["iframes"], data["base_url"])
        self.assertIsNotNone(src)
        assert src is not None
        self.assertIn("legalforce-cloud.com", src)


class TestProductionSnapshotsIfAvailable(unittest.TestCase):
    """Best-effort: if a developer has real production snapshots on disk
    (gitignored), run the parser against ALL of them to catch any pattern
    we haven't yet synthesized into a committed fixture.

    Skipped on CI where the data dirs do not exist.
    """

    def test_no_phantom_fields_across_local_snapshots(self) -> None:
        brief_dirs = list(
            (ROOT / "jp-form-outreach" / "data" / "briefs").glob("*/")
        )
        if not brief_dirs:
            self.skipTest("no local data/briefs/ — running on CI or fresh clone")
        snapshots: list[Path] = []
        for d in brief_dirs:
            snapshots.extend(d.glob("resolve_snapshot_*.txt"))
        if not snapshots:
            self.skipTest("no resolve_snapshot_*.txt files on disk")
        # We check that NONE of the local snapshots produce a "field" string
        # that starts with an aria-tree role token — that's the regression
        # the parser leak guard fixed. A new failure here means a novel
        # aria-tree shape we haven't accounted for yet.
        bad: list[tuple[Path, list[dict]]] = []
        for snap_path in snapshots[:50]:  # cap for fast CI runs
            try:
                text = snap_path.read_text(encoding="utf-8")
            except OSError:
                continue
            errs = fv.parse_validation_errors(text)
            leaks = [
                e for e in errs
                if any(
                    e["field"].startswith(tok) for tok in (
                        "text:", "row ", "cell ", "generic ", "paragraph ",
                        "listitem ", "link ", "/url:", "rowheader ",
                        "rowgroup ", "table ", "img ", "- ",
                    )
                )
            ]
            if leaks:
                bad.append((snap_path, leaks))
        # We tolerate any number of LEGITIMATE captured fields — but ZERO
        # tree-leak captures. The defensive guard should never fail.
        self.assertEqual(
            bad, [],
            "production snapshots produced aria-tree leak captures: "
            + ", ".join(f"{p.name}:{len(b)}" for p, b in bad[:5]),
        )


if __name__ == "__main__":
    unittest.main()
