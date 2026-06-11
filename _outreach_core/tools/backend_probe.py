"""Browser-backend parity probe (v21 Phase 2).

Run the SAME page through both browser adapters and compare what each one sees,
so the Playwright backend can be validated against the historical OpenClaw one
before any real sends. The comparison targets the *signals the pipeline actually
acts on* — page-form-state classification (v17) and live-captcha kind (v18) —
not pixel-level identity.

Usage (on a machine where BOTH backends work):

    python3 -m _outreach_core.tools.backend_probe https://example.com/contact
    python3 -m _outreach_core.tools.backend_probe <url> --backends playwright,openclaw

Exit code is 0 when the signal set matches, 1 when it diverges — so it can gate a
migration step in a script.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _outreach_core import adapters, captcha, contact_url

# Self-contained DOM probe (independent of run.py) — enough to drive the same
# classifiers the send path uses.
_PROBE_JS = r"""
() => {
  const q = (s) => Array.from(document.querySelectorAll(s));
  const isText = (e) => {
    const t = (e.type || '').toLowerCase();
    return !['hidden','submit','button','image','radio','checkbox'].includes(t);
  };
  const inputs = q('input').filter(isText);
  const tas = q('textarea');
  const btns = q('button, input[type="submit"], input[type="button"]');
  const radios = {};
  q('input[type="radio"]').forEach((r) => { if (r.name) radios[r.name] = 1; });
  const checks = q('input[type="checkbox"]');
  const body = (document.body && document.body.innerText) ? document.body.innerText : '';
  return {
    url: location.href,
    title: document.title || '',
    inputs: inputs.length,
    textareas: tas.length,
    submit_buttons: btns.length,
    radio_groups: Object.keys(radios).length,
    checkboxes: checks.length,
    body_len: body.length,
    body_head: body.slice(0, 4000),
  };
}
"""

# The signal keys whose agreement actually matters for migration safety.
SIGNAL_KEYS = ("page_state", "captcha_kind", "captcha_blocking",
               "textareas", "submit_buttons", "radio_groups")


def _fields_from_probe(p: dict[str, Any]) -> dict[str, Any]:
    """Synthesize the fields dict shape classify_page_form_state expects."""
    return {
        "inputs": [{"type": "text"}] * int(p.get("inputs") or 0),
        "textareas": [{}] * int(p.get("textareas") or 0),
        "submit_buttons": [{}] * int(p.get("submit_buttons") or 0),
        "radios": {f"r{i}": [] for i in range(int(p.get("radio_groups") or 0))},
        "checkboxes": [{}] * int(p.get("checkboxes") or 0),
    }


def run_probe(adapter: Any, url: str) -> dict[str, Any]:
    """Open url in the adapter, classify page-state + captcha, return signals."""
    out: dict[str, Any] = {"backend": getattr(adapter, "backend", "?"), "url": url}
    try:
        adapter.browser("open", url)
        probe = adapter.evaluate(_PROBE_JS) or {}
        if not isinstance(probe, dict):
            probe = {}
        cap_raw = adapter.evaluate(captcha.LIVE_CAPTCHA_JS)
        cap = captcha.classify_live_state(cap_raw)
        state = contact_url.classify_page_form_state(
            _fields_from_probe(probe), str(probe.get("body_head") or "")
        )
        out.update({
            "ok": True,
            "title": probe.get("title"),
            "final_url": probe.get("url"),
            "inputs": probe.get("inputs"),
            "textareas": probe.get("textareas"),
            "submit_buttons": probe.get("submit_buttons"),
            "radio_groups": probe.get("radio_groups"),
            "checkboxes": probe.get("checkboxes"),
            "body_len": probe.get("body_len"),
            "page_state": state.get("state"),
            "captcha_kind": cap.get("kind"),
            "captcha_blocking": cap.get("blocking"),
            "cloudflare": cap.get("cloudflare"),
        })
    except Exception as exc:  # noqa: BLE001
        out.update({"ok": False, "error": str(exc)[:300]})
    return out


def compare_probes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Pure diff over the SIGNAL_KEYS. Returns {match, diffs:{key:[a,b]}}."""
    diffs: dict[str, list[Any]] = {}
    for k in SIGNAL_KEYS:
        va, vb = a.get(k), b.get(k)
        if va != vb:
            diffs[k] = [va, vb]
    return {
        "match": not diffs and bool(a.get("ok")) and bool(b.get("ok")),
        "diffs": diffs,
        "a_ok": bool(a.get("ok")),
        "b_ok": bool(b.get("ok")),
    }


def format_comparison(url: str, results: dict[str, dict[str, Any]],
                      cmp: dict[str, Any]) -> str:
    lines = [f"# backend parity probe: {url}", ""]
    for name, r in results.items():
        if r.get("ok"):
            lines.append(
                f"[{name}] state={r.get('page_state')} captcha={r.get('captcha_kind')}"
                f"/blocking={r.get('captcha_blocking')} "
                f"ta={r.get('textareas')} btn={r.get('submit_buttons')} "
                f"radio={r.get('radio_groups')} title={(r.get('title') or '')[:40]!r}"
            )
        else:
            lines.append(f"[{name}] ERROR: {r.get('error')}")
    lines.append("")
    if cmp["match"]:
        lines.append("✓ MATCH — both backends see the same signals.")
    else:
        lines.append("✗ DIVERGENCE:")
        for k, (va, vb) in cmp["diffs"].items():
            lines.append(f"   {k}: {va!r}  vs  {vb!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare browser backends on a URL.")
    ap.add_argument("url")
    ap.add_argument("--backends", default="openclaw,playwright",
                    help="comma list, default openclaw,playwright")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    results: dict[str, dict[str, Any]] = {}
    for backend in backends:
        adapter = None
        try:
            adapter = adapters.make_browser(backend)
            results[backend] = run_probe(adapter, args.url)
        except Exception as exc:  # noqa: BLE001
            results[backend] = {"backend": backend, "ok": False, "error": str(exc)[:300]}
        finally:
            if adapter is not None:
                try:
                    adapter.shutdown()
                except Exception:  # noqa: BLE001
                    pass

    names = list(results.keys())
    cmp = (compare_probes(results[names[0]], results[names[1]])
           if len(names) >= 2 else {"match": False, "diffs": {}, "note": "need 2 backends"})

    if args.json:
        print(json.dumps({"results": results, "comparison": cmp},
                         ensure_ascii=False, indent=2))
    else:
        print(format_comparison(args.url, results, cmp))
    return 0 if cmp.get("match") else 1


if __name__ == "__main__":
    raise SystemExit(main())
