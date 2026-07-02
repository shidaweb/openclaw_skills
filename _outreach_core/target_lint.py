"""v31 §WS1d — targets.yaml row linting (pure, warn-only).

``stage_bootstrap`` loads a hand-curated YAML; a typo in an enum-ish field
(``status: pendig``, ``flow: confrim``) silently changes behavior downstream
(an unknown status is treated as pending, an unknown flow falls back to the
LLM plan). These helpers surface such typos at load time. They only WARN —
bootstrap must never drop a row because of a lint hit, since new legitimate
values (e.g. a new captcha vendor) may precede the lint list being updated.
"""

from __future__ import annotations

from typing import Any

# Legend documented in jp-form-outreach/targets.example.yaml.
KNOWN_STATUS = ("sent", "pending", "blocked", "manual", "linkedin", "dropped")
KNOWN_CATEGORY = ("b2b_form", "b2c_only", "iframe", "site_closed")
KNOWN_FLOW = ("single", "confirm")
# Observed values plus the vendors captcha.py can classify. "unknown" is a
# legitimate curator's shrug, not a typo.
KNOWN_CAPTCHA = (
    "none",
    "unknown",
    "recaptcha_v2_visible",
    "recaptcha_v2",
    "recaptcha_v3_invisible",
    "recaptcha_v3",
    "hcaptcha",
    "turnstile",
)

_ENUM_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("status", KNOWN_STATUS),
    ("category", KNOWN_CATEGORY),
    ("flow", KNOWN_FLOW),
    ("captcha", KNOWN_CAPTCHA),
)


def validate_target_row(c: dict[str, Any] | None) -> list[str]:
    """Return human-readable warnings for one targets.yaml company row.

    Empty list means clean. Missing fields are fine (they all have
    defaults); only PRESENT values that don't match the known vocabulary
    are flagged.
    """
    if not isinstance(c, dict):
        return ["row is not a mapping"]
    warnings: list[str] = []
    for field, known in _ENUM_FIELDS:
        raw = c.get(field)
        if raw in (None, ""):
            continue
        value = str(raw).strip().lower()
        if value not in known:
            warnings.append(
                f"{field}={raw!r} is not a known value "
                f"(expected one of: {', '.join(known)})"
            )
    cands = c.get("contact_url_candidates")
    if cands is not None and not isinstance(cands, list):
        warnings.append("contact_url_candidates must be a list of URLs")
    overrides = c.get("field_map_overrides")
    if overrides is not None and not isinstance(overrides, dict):
        warnings.append("field_map_overrides must be a mapping")
    return warnings
