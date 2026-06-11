"""Browser adapter seam (v21 §3).

All of Doorman's browser I/O funnels through three primitives that mirror the
historical ``_outreach_core.infer`` functions:

    evaluate(js)        – run JS in the active page, return parsed result
    browser(*args)      – the old ``oc_browser`` verbs: open/snapshot/screenshot/focus/close
    browser_json(*args) – the old ``oc_browser_json`` verbs: open/tabs (parsed JSON)

Two implementations satisfy this protocol:

    OpenClawBrowserAdapter  – delegates 1:1 to the ``openclaw`` CLI (unchanged behavior)
    PlaywrightBrowserAdapter – drives Playwright in-process (no gateway in the hot path)

Selecting the backend is done in ``adapters/__init__.py``; the rest of the code
calls ``get_browser()`` and never imports a concrete adapter.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class BrowserAdapter(Protocol):
    backend: str

    def evaluate(self, js: str) -> Any: ...

    def browser(self, *args: str) -> str | None: ...

    def browser_json(self, *args: str) -> Any: ...

    def shutdown(self) -> None: ...
