"""OpenClaw browser adapter (v21 §3) — behavior-identical wrapper.

Delegates 1:1 to the existing ``_outreach_core.infer`` subprocess helpers, so
selecting this backend reproduces the historical behavior byte-for-byte. This is
the default backend and the instant rollback target for the Playwright migration.
"""

from __future__ import annotations

from typing import Any


class OpenClawBrowserAdapter:
    backend = "openclaw"

    def __init__(self, profile: str | None = None) -> None:
        from _outreach_core import infer  # lazy: avoid import cost/cycles
        self._infer = infer
        self._profile = profile or infer.BROWSER_PROFILE

    def evaluate(self, js: str) -> Any:
        return self._infer.oc_evaluate(js, profile=self._profile)

    def browser(self, *args: str) -> str | None:
        return self._infer.oc_browser(*args, profile=self._profile)

    def browser_json(self, *args: str) -> Any:
        return self._infer.oc_browser_json(*args, profile=self._profile)

    def shutdown(self) -> None:  # nothing to tear down — subprocess per call
        return None
