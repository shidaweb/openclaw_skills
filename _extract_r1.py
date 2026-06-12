#!/usr/bin/env python3
"""Mechanical extraction of per-lead loop bodies (v15 §R1) — AST-guided."""
import ast
import sys
from pathlib import Path

RUN = Path("jp-form-outreach/run.py")


def find_func(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise SystemExit(f"function {name} not found")


def find_top_for(func):
    fors = [n for n in func.body if isinstance(n, ast.For)]
    assert len(fors) == 1, f"{func.name}: expected exactly 1 top-level for, got {len(fors)}"
    return fors[0]


def per_target_continue_lines(for_node):
    """Line numbers of `continue` statements belonging to THIS for (not nested loops)."""
    out = []

    def walk(stmts, loop_depth):
        for st in stmts:
            if isinstance(st, ast.Continue):
                if loop_depth == 0:
                    out.append(st.lineno)
                continue
            inner_depth = loop_depth + (1 if isinstance(st, (ast.For, ast.While)) else 0)
            for field in ("body", "orelse", "finalbody"):
                if hasattr(st, field) and getattr(st, field):
                    walk(getattr(st, field), inner_depth)
            if hasattr(st, "handlers"):
                for h in st.handlers:
                    walk(h.body, inner_depth)

    walk(for_node.body, 0)
    return out


def replace_continues(lines, continue_linenos, repl_stmt):
    for ln in continue_linenos:
        raw = lines[ln - 1]
        indent = raw[: len(raw) - len(raw.lstrip())]
        assert raw.strip() == "continue", f"line {ln} is not a bare continue: {raw!r}"
        lines[ln - 1] = f"{indent}{repl_stmt}"


def dedent4(seg_lines):
    out = []
    for ln in seg_lines:
        if ln.startswith("    "):
            out.append(ln[4:])
        elif ln.strip() == "":
            out.append("")
        else:
            raise SystemExit(f"cannot dedent line: {ln!r}")
    return out


text = RUN.read_text(encoding="utf-8")
lines = text.split("\n")
tree = ast.parse(text)

# ===================== stage_send (process LAST-in-file first) ==============
send = find_func(tree, "stage_send")
sfor = find_top_for(send)
assert isinstance(sfor.body[-1], ast.If), "send loop: last stmt should be the sleep-if"
tick_stmt = sfor.body[-2]
assert "hb.tick" in ast.get_source_segment(text, tick_stmt), "expected hb.tick stmt"

replace_continues(lines, per_target_continue_lines(sfor), 'return {"outcome": "skipped"}')

body_start = sfor.body[0].lineno - 1          # idx = sendable.index(d) + 1
body_end = tick_stmt.lineno - 1               # exclusive: hb.tick stays in loop
seg = lines[body_start:body_end]
assert seg[0].strip() == "idx = sendable.index(d) + 1"
seg = seg[1:]                                  # idx passed as param
send_body = "\n".join(dedent4(seg)).rstrip("\n")
assert "hb." not in send_body

send_func_src = f'''def _send_one_target(
    d: dict[str, Any],
    *,
    di: int,
    idx: int,
    mode: str,
    config: dict[str, Any],
    verify_strict: bool,
    iterative_fill: bool,
    autonomous: bool,
    score_on: bool,
    tab_isolation: bool,
    resolver_tab_ids: set[str],
    sent: list[dict[str, Any]],
    filled_only: list[dict[str, Any]],
) -> dict[str, Any]:
    """Send to a single target — extracted per-lead loop body (v15 §R1).

    Mutates ``sent`` / ``filled_only`` / ``resolver_tab_ids`` in place
    (mechanical extraction). Exceptions are isolated by the caller, so one
    company can no longer kill the whole batch.
    """
{send_body}
    return {{"outcome": "done"}}
'''

loop_repl = '''    try:
        for di, d in enumerate(targets):
            idx = sendable.index(d) + 1
            tid = str(d.get("id") or d.get("name", "?"))
            try:
                result = _send_one_target(
                    d, di=di, idx=idx, mode=mode, config=config,
                    verify_strict=verify_strict, iterative_fill=iterative_fill,
                    autonomous=autonomous, score_on=score_on,
                    tab_isolation=tab_isolation, resolver_tab_ids=resolver_tab_ids,
                    sent=sent, filled_only=filled_only,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 — per-lead isolation (v15 §R1)
                tb = traceback.format_exc()
                print(f"  [send] ✗ lead crashed: {exc} — continuing with next target",
                      file=sys.stderr)
                try:
                    _emit_event(
                        "send.lead_crashed", stage="send", target_id=tid,
                        payload={"error": str(exc)[:200], "tb_tail": tb[-800:]},
                    )
                    append_needs_attention(DATA_DIR, {
                        "target_id": d.get("id"), "name": d.get("name"),
                        "channel": "jp_form",
                        "reason": f"lead_crashed: {str(exc)[:160]}",
                    })
                except Exception:
                    pass
                _close_tab_safely(d.get("_send_tab_id"))
                result = {"outcome": "crashed"}
            d.pop("_send_tab_id", None)
            hb.tick(di + 1, f"{d.get('name', '?')} · {result.get('outcome', 'done')}")

            if di < len(targets) - 1:
                print(f"  [send] sleeping 30s before next...")
                time.sleep(30)
    finally:
        # §R1 acceptance: hb.end always runs; partial successes are persisted
        # even when the loop dies mid-batch.
        hb.end(f"send done · sent={{len(sent)}} · pending={{len(filled_only)}}")
        if sent:
            append_sent_history(sent)'''
# NOTE: the f-string braces above must stay literal in output:
loop_repl = loop_repl.replace(
    'hb.end(f"send done · sent={{len(sent)}} · pending={{len(filled_only)}}")',
    'hb.end(f"send done · sent={len(sent)} · pending={len(filled_only)}")',
)

# Old region to replace: for-line .. end of old trailer (hb.end + append block)
for_line_idx = sfor.lineno - 1
# Find old trailer end: the stmt after the for loop is hb.end, then `if sent:` block
after_stmts = send.body[send.body.index(sfor) + 1:]
hb_end_stmt = after_stmts[0]
assert "hb.end" in ast.get_source_segment(text, hb_end_stmt)
if_sent_stmt = after_stmts[1]
assert "append_sent_history" in ast.get_source_segment(text, if_sent_stmt)
old_region_end = if_sent_stmt.end_lineno  # inclusive

new_lines = (
    lines[:for_line_idx]
    + loop_repl.split("\n")
    + lines[old_region_end:]
)

# Insert _send_one_target before stage_send def line
send_def_idx = send.lineno - 1
# account: send_def_idx unchanged (insertions were after it? no - loop repl is inside stage_send, after def). Insert now.
new_lines = (
    new_lines[:send_def_idx]
    + send_func_src.split("\n")
    + ["", ""]
    + new_lines[send_def_idx:]
)

text = "\n".join(new_lines)
ast.parse(text)  # syntax check before continuing

# ===================== stage_enrich =========================================
lines = text.split("\n")
tree = ast.parse(text)
enr = find_func(tree, "stage_enrich")
efor = find_top_for(enr)

replace_continues(lines, per_target_continue_lines(efor), 'return {"outcome": "skipped"}')

ebody_start = efor.body[0].lineno - 1
ebody_end = efor.end_lineno  # inclusive of last stmt
eseg = lines[ebody_start:ebody_end]
ebody = "\n".join(dedent4(eseg)).rstrip("\n")
ebody = ebody.replace("len(targets)", "total")

enrich_func_src = f'''def _enrich_one_target(
    t: dict[str, Any],
    i: int,
    total: int,
    config: dict[str, Any] | None,
    enriched: list[dict[str, Any]],
) -> dict[str, Any]:
    """Enrich a single target — extracted per-lead loop body (v15 §R1).

    Appends exactly one row to ``enriched``. Exceptions are isolated by the
    caller so one bad site cannot kill the whole enrich batch.
    """
{ebody}
    return {{"outcome": "enriched"}}
'''

eloop_repl = '''    total = len(targets)
    for i, t in enumerate(targets, 1):
        try:
            _enrich_one_target(t, i, total, config, enriched)
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001 — per-lead isolation (v15 §R1)
            tb = traceback.format_exc()
            print(f"[enrich] ✗ ({i}/{total}) {t.get('name')}: crashed: {exc} — continuing",
                  file=sys.stderr)
            try:
                _emit_event(
                    "enrich.lead_crashed", stage="enrich",
                    target_id=str(t.get("id") or ""),
                    payload={"error": str(exc)[:200], "tb_tail": tb[-800:]},
                )
            except Exception:
                pass
            enriched.append({
                **t,
                "_enrich_skipped": "lead_crashed",
                "_enrich_skip_reason": str(exc)[:200],
            })'''

efor_line_idx = efor.lineno - 1
new_lines = (
    lines[:efor_line_idx]
    + eloop_repl.split("\n")
    + lines[efor.end_lineno:]
)

enr_def_idx = enr.lineno - 1
new_lines = (
    new_lines[:enr_def_idx]
    + enrich_func_src.split("\n")
    + ["", ""]
    + new_lines[enr_def_idx:]
)

text = "\n".join(new_lines)
ast.parse(text)
RUN.write_text(text, encoding="utf-8")
print("extraction done + syntax OK")
