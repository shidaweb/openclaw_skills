"""Browser backend selection (v21 §3).

Call ``get_browser(config)`` everywhere; never import a concrete adapter. The
backend is chosen once per process:

    env DOORMAN_BROWSER_BACKEND  (wins)  →  config.browser.backend  →  "openclaw"

Default is ``openclaw`` so existing deployments are byte-for-byte unchanged until
someone opts in to ``playwright``.
"""

from __future__ import annotations

import os
from typing import Any

from .base import BrowserAdapter  # noqa: F401 (re-exported)

_singleton: Any = None


def select_backend(config: dict[str, Any] | None = None) -> str:
    env = os.environ.get("DOORMAN_BROWSER_BACKEND", "").strip().lower()
    if env:
        return env
    cfg = ((config or {}).get("browser") or {}).get("backend")
    if cfg:
        return str(cfg).strip().lower()
    return "openclaw"


def _build(config: dict[str, Any] | None):
    backend = select_backend(config)
    if backend == "playwright":
        from .playwright_browser import PlaywrightBrowserAdapter
        return PlaywrightBrowserAdapter(config=config)
    from .openclaw_browser import OpenClawBrowserAdapter
    return OpenClawBrowserAdapter()


def get_browser(config: dict[str, Any] | None = None):
    """Process-wide singleton adapter (lazy)."""
    global _singleton
    if _singleton is None:
        _singleton = _build(config)
    return _singleton


def reset() -> None:
    """Tear down and forget the singleton (tests / backend switch)."""
    global _singleton
    if _singleton is not None:
        try:
            _singleton.shutdown()
        except Exception:  # noqa: BLE001
            pass
    _singleton = None
