"""v31 §WS1 — stage_bootstrap filter logic (previously uncovered).

Drives run.stage_bootstrap against a synthetic targets.yaml with the data
dir pointed at tmp_path, pinning: missing-id warning (not silent drop),
invalid_url transient re-attempt, registrable-domain dedup, form-service
non-dedup (different companies on forms.gle must not collide), and the
warn-only enum lint.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "jp-form-outreach"))

import run  # noqa: E402
from _outreach_core import history as core_history  # noqa: E402
from _outreach_core import target_lint  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(run, "DATA_DIR", d)
    monkeypatch.setattr(run, "SKIP_HISTORY_PATH", d / "skip_history.jsonl")
    monkeypatch.setattr(run, "SENT_HISTORY_PATH", d / "sent_history.jsonl")
    return d


def _write_targets(tmp_path: Path, companies_yaml: str) -> Path:
    p = tmp_path / "targets.yaml"
    p.write_text("companies:\n" + companies_yaml, encoding="utf-8")
    return p


def _bootstrap(tmp_path: Path, targets: Path, **kwargs) -> list[dict]:
    out = tmp_path / "leads.jsonl"
    run.stage_bootstrap(targets, out, **kwargs)
    if not out.exists():
        return []
    return [json.loads(x) for x in out.read_text().splitlines() if x.strip()]


def test_missing_id_row_warns_and_is_counted(tmp_path, data_dir, capsys):
    targets = _write_targets(tmp_path, """
  - name: ID無し株式会社
    form_url: https://noid.example.co.jp/contact
  - id: ok_co
    name: 正常株式会社
    form_url: https://ok.example.co.jp/contact
""")
    leads = _bootstrap(tmp_path, targets)
    out = capsys.readouterr().out
    assert [d["id"] for d in leads] == ["ok_co"]
    assert "missing_id" in out
    assert "ID無し株式会社" in out


def test_invalid_url_skip_is_transient(tmp_path, data_dir):
    # First run: malformed URL → skipped + recorded.
    targets_bad = _write_targets(tmp_path, """
  - id: fixable_co
    name: 直せる株式会社
    form_url: "not a url"
""")
    leads = _bootstrap(tmp_path, targets_bad)
    assert leads == []
    skip_rows = [
        json.loads(x)
        for x in (data_dir / "skip_history.jsonl").read_text().splitlines()
    ]
    assert skip_rows and skip_rows[-1]["reason"].startswith("invalid_url")

    # Second run: curator fixed the URL → the id must be eligible again.
    targets_fixed = _write_targets(tmp_path, """
  - id: fixable_co
    name: 直せる株式会社
    form_url: https://fixable.example.co.jp/contact
""")
    leads = _bootstrap(tmp_path, targets_fixed)
    assert [d["id"] for d in leads] == ["fixable_co"]


def test_non_transient_skip_still_excludes(tmp_path, data_dir):
    (data_dir / "skip_history.jsonl").write_text(
        json.dumps({"id": "b2c_co", "reason": "non_contact_form: B2C窓口のみ"})
        + "\n"
    )
    targets = _write_targets(tmp_path, """
  - id: b2c_co
    name: B2C株式会社
    form_url: https://b2c.example.co.jp/contact
""")
    assert _bootstrap(tmp_path, targets) == []


def test_domain_dedup_within_batch_keeps_first(tmp_path, data_dir):
    targets = _write_targets(tmp_path, """
  - id: unit_a
    name: 事業部A
    form_url: https://shared.co.jp/a/contact
  - id: unit_b
    name: 事業部B
    form_url: https://sub.shared.co.jp/b/contact
  - id: other_co
    name: 別会社
    form_url: https://other.co.jp/contact
""")
    leads = _bootstrap(tmp_path, targets)
    assert [d["id"] for d in leads] == ["unit_a", "other_co"]


def test_form_service_urls_do_not_collide(tmp_path, data_dir):
    # v31 §WS1a — two different companies on Google Forms used to collapse
    # to the same registrable-domain key and one silently vanished.
    targets = _write_targets(tmp_path, """
  - id: gf_one
    name: フォームズ株式会社
    form_url: https://forms.gle/abc123
  - id: gf_two
    name: グーグルフォーム商事
    form_url: https://forms.gle/xyz789
""")
    leads = _bootstrap(tmp_path, targets)
    assert [d["id"] for d in leads] == ["gf_one", "gf_two"]
    # …but the SAME service URL twice is still a duplicate.
    targets_dup = _write_targets(tmp_path, """
  - id: gf_one
    name: フォームズ株式会社
    form_url: https://forms.gle/abc123
  - id: gf_clone
    name: コピー株式会社
    form_url: https://forms.gle/abc123
""")
    leads = _bootstrap(tmp_path, targets_dup)
    assert [d["id"] for d in leads] == ["gf_one"]


def test_history_form_service_url_only_blocks_same_form(tmp_path, data_dir):
    (data_dir / "sent_history.jsonl").write_text(
        json.dumps({"id": "gf_sent", "form_url": "https://forms.gle/abc123"})
        + "\n"
    )
    targets = _write_targets(tmp_path, """
  - id: gf_new
    name: 新規フォーム株式会社
    form_url: https://forms.gle/other999
""")
    leads = _bootstrap(tmp_path, targets)
    assert [d["id"] for d in leads] == ["gf_new"]


def test_enum_lint_warns_but_never_filters(tmp_path, data_dir, capsys):
    targets = _write_targets(tmp_path, """
  - id: typo_co
    name: タイポ株式会社
    status: pendig
    flow: confrim
    form_url: https://typo.example.co.jp/contact
""")
    leads = _bootstrap(tmp_path, targets)
    out = capsys.readouterr().out
    assert [d["id"] for d in leads] == ["typo_co"]
    assert "pendig" in out
    assert "confrim" in out
    assert "lint warning" in out


class TestTargetLint:
    def test_clean_row_no_warnings(self):
        row = {"id": "x", "status": "pending", "category": "b2b_form",
               "flow": "confirm", "captcha": "none",
               "contact_url_candidates": ["https://x.co.jp/contact"],
               "field_map_overrides": {"phone_format": "hyphenated"}}
        assert target_lint.validate_target_row(row) == []

    def test_missing_fields_are_fine(self):
        assert target_lint.validate_target_row({"id": "x"}) == []

    def test_each_enum_field_flagged(self):
        row = {"status": "sentt", "category": "b2b", "flow": "both",
               "captcha": "recaptcha_v9"}
        warnings = target_lint.validate_target_row(row)
        assert len(warnings) == 4

    def test_wrong_container_types_flagged(self):
        warnings = target_lint.validate_target_row(
            {"contact_url_candidates": "https://x.co.jp/contact",
             "field_map_overrides": ["not", "a", "mapping"]}
        )
        assert len(warnings) == 2


class TestTransientSkipIds:
    def test_latest_reason_wins(self, tmp_path):
        rows = [
            {"id": "a", "reason": "invalid_url: bad"},
            {"id": "b", "reason": "invalid_url: bad"},
            {"id": "b", "reason": "non_contact_form"},   # later verdict wins
            {"id": "c", "reason": "captcha"},
        ]
        (tmp_path / "skip_history.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n"
        )
        assert core_history.load_transient_skip_ids(tmp_path) == {"a"}

    def test_missing_file_is_empty(self, tmp_path):
        assert core_history.load_transient_skip_ids(tmp_path) == set()
