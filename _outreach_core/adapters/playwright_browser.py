"""Playwright browser adapter (v21 §3–5) — drives a real Chromium in-process.

No OpenClaw gateway in the hot path: every evaluate/open/click talks directly to
a persistent Chromium context, so the dominant "gateway hung → everything stalls"
failure mode disappears. State (cookies, cf_clearance) persists in a Doorman-only
profile dir, independent of OpenClaw's profile (sharing a user-data-dir would
deadlock Chromium).

Playwright is imported lazily so this module is importable (and the pure tab
registry is testable) even where playwright isn't installed.

Backend selection lives in adapters/__init__.py; this class is never imported by
the rest of the codebase directly.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

# Unified snapshot (v21 §4 case A): the historical a11y-tree text is only ever
# scanned for keywords / control counts, so innerText + a compact control summary
# is an equivalent, cheaper signal that verify.py / contact_url.py already accept.
_SNAPSHOT_JS = r"""
() => {
  const txt = (document.body && document.body.innerText) ? document.body.innerText : '';
  return txt.slice(0, 16000);
}
"""


class TabRegistry:
    """Pure targetId↔page bookkeeping (no Playwright types needed → unit-testable).

    Emits the exact payload shapes _outreach_core.tab_utils expects:
      open  → {"targetId", "url", "type": "page"}
      tabs  → {"tabs": [{"targetId", "type": "page", "url"}, ...]}  (insertion order)
    """

    def __init__(self) -> None:
        self._pages: dict[str, Any] = {}  # insertion order == open order
        self.current: Any = None

    def add(self, page: Any) -> str:
        tid = uuid.uuid4().hex
        self._pages[tid] = page
        self.current = page
        return tid

    def get(self, target_id: str) -> Any:
        return self._pages.get(target_id)

    def id_of(self, page: Any) -> str | None:
        for tid, p in self._pages.items():
            if p is page:
                return tid
        return None

    def remove(self, target_id: str) -> Any:
        page = self._pages.pop(target_id, None)
        if page is not None and page is self.current:
            self.current = next(reversed(self._pages.values()), None) if self._pages else None
        return page

    def open_payload(self, target_id: str, url: str) -> dict[str, Any]:
        return {"targetId": target_id, "url": url, "type": "page"}

    def tabs_payload(self) -> dict[str, Any]:
        out = []
        for tid, page in self._pages.items():
            url = ""
            try:
                url = page.url  # playwright Page.url; duck-typed in tests
            except Exception:  # noqa: BLE001
                url = ""
            out.append({"targetId": tid, "type": "page", "url": url})
        return {"tabs": out}


def _truthy_env(name: str) -> bool | None:
    v = os.environ.get(name, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return None


class PlaywrightBrowserAdapter:
    backend = "playwright"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self._config = config or {}
        self._reg = TabRegistry()
        self._pw = None
        self._context = None
        self._default_timeout_ms = int(
            os.environ.get("DOORMAN_PW_TIMEOUT_MS", "30000") or 30000
        )

    # --- lifecycle ----------------------------------------------------------
    def _profile_dir(self) -> Path:
        root = Path(__file__).resolve().parents[2]  # repo root
        d = root / "data" / "pw_profile"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _headless(self) -> bool:
        env = _truthy_env("DOORMAN_BROWSER_HEADLESS")
        if env is not None:
            return env
        br = (self._config.get("browser") or {})
        if "headless" in br:
            return bool(br.get("headless"))
        return False  # headful default — friendlier to Cloudflare/Turnstile (§5)

    def _ensure_context(self):
        if self._context is not None:
            return self._context
        from playwright.sync_api import sync_playwright  # lazy

        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self._profile_dir()),
            headless=self._headless(),
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context.set_default_timeout(self._default_timeout_ms)
        return self._context

    def _ensure_current_page(self):
        ctx = self._ensure_context()
        if self._reg.current is None:
            # reuse an existing blank page if the context opened one, else create.
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            self._hook_dialogs(page)
            if self._reg.id_of(page) is None:
                self._reg.add(page)
            self._reg.current = page
        return self._reg.current

    # --- BrowserAdapter primitives -----------------------------------------
    def evaluate(self, js: str) -> Any:
        page = self._ensure_current_page()
        try:
            return page.evaluate(js)
        except Exception as exc:  # noqa: BLE001 — mirror oc_evaluate's None-on-error
            import sys
            print(f"[pw evaluate err] {str(exc)[:200]}", file=sys.stderr)
            return None

    def browser(self, *args: str) -> str | None:
        if not args:
            return None
        verb = args[0]
        rest = args[1:]
        if verb == "open":
            url = rest[0] if rest else "about:blank"
            self._open(url)
            return ""
        if verb == "snapshot":
            page = self._ensure_current_page()
            try:
                return page.evaluate(_SNAPSHOT_JS)
            except Exception:  # noqa: BLE001
                return ""
        if verb == "screenshot":
            return self._screenshot()
        if verb == "focus":
            return "ok" if self._focus(rest[0]) else None
        if verb == "close":
            self._close(rest[0] if rest else None)
            return "ok"
        return None

    def browser_json(self, *args: str) -> Any:
        if not args:
            return None
        verb = args[0]
        if verb == "open":
            url = args[1] if len(args) > 1 else "about:blank"
            tid = self._open(url)
            return self._reg.open_payload(tid, url)
        if verb == "tabs":
            return self._reg.tabs_payload()
        return None

    # --- internals ----------------------------------------------------------
    @staticmethod
    def _hook_dialogs(page) -> None:
        """Auto-accept native JS dialogs (v22 §SM). Playwright's default is to
        DISMISS an unhandled dialog, so onsubmit="return confirm(...)" forms
        were silently cancelled — the click 'succeeded' and nothing happened."""
        try:
            page.on("dialog", lambda d: d.accept())
        except Exception:  # noqa: BLE001 — never let a hook break navigation
            pass

    def _open(self, url: str) -> str:
        ctx = self._ensure_context()
        page = ctx.new_page()
        self._hook_dialogs(page)
        tid = self._reg.add(page)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self._default_timeout_ms)
        except Exception as exc:  # noqa: BLE001 — keep the tab; caller assesses page
            import sys
            print(f"[pw open warn] {url}: {str(exc)[:160]}", file=sys.stderr)
        return tid

    def _focus(self, target_id: str) -> bool:
        page = self._reg.get(target_id)
        if page is None:
            return False
        try:
            page.bring_to_front()
            self._reg.current = page
            return True
        except Exception:  # noqa: BLE001
            return False

    def _close(self, target_id: str | None) -> None:
        if not target_id:
            return
        page = self._reg.remove(target_id)
        if page is not None:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def _screenshot(self) -> str | None:
        page = self._ensure_current_page()
        root = Path(__file__).resolve().parents[2]
        out = root / "data" / f"pw_shot_{uuid.uuid4().hex}.png"
        try:
            page.screenshot(path=str(out), full_page=True)
            return str(out)
        except Exception:  # noqa: BLE001
            return None

    def shutdown(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            pass
        self._context = None
        self._pw = None
