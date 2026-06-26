#!/usr/bin/env python3
"""Multi-brief management CLI (v4 §14-F)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import channel_state
from _outreach_core.config import (
    ACTIVE_BRIEF_FILE,
    BRIEFS_DIR,
    BRIEF_TEMPLATE,
    SKILLS_ROOT,
    list_brief_ids,
    load_brief,
    resolve_brief_id,
)
from _outreach_core.active_run import is_lock_alive, read_lock, remove_lock
from _outreach_core.helpers.reconstruct import build_status_report
from _outreach_core.paths import brief_data_dir

try:
    import yaml
except ImportError:
    yaml = None

SKILL_DIRS = [
    ("jp-form-outreach", "jp_form"),
    ("linkedin-outreach", "linkedin"),
]

DATA_GLOBS = (
    "*.jsonl",
    "sample_*.txt",
    "verify_snapshot_*.txt",
    "events.jsonl",
)


def cmd_list(_args: argparse.Namespace) -> int:
    active = ""
    if ACTIVE_BRIEF_FILE.is_file():
        active = ACTIVE_BRIEF_FILE.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    print("# briefs\n")
    for bid in list_brief_ids():
        mark = " (active)" if bid == active else ""
        try:
            b = load_brief(bid)
            name = (b.get("brief") or {}).get("display_name") or bid
        except Exception:
            name = bid
        chs = channel_state.channels_for_brief(bid)
        ch_note = f" · {len(chs)} channel(s)" if chs else ""
        print(f"- {bid}: {name}{mark}{ch_note}")
    if not list_brief_ids():
        print("_No briefs yet. Run: brief new <id> or brief migrate ..._")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    bid = resolve_brief_id(args.brief_id)
    path = BRIEFS_DIR / f"{bid}.yaml"
    print(f"# {bid}\n")
    print(path.read_text(encoding="utf-8"))
    return 0


def cmd_set_active(args: argparse.Namespace) -> int:
    bid = args.brief_id.strip()
    path = BRIEFS_DIR / f"{bid}.yaml"
    if not path.is_file():
        print(f"brief not found: {path}", file=sys.stderr)
        return 1
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_BRIEF_FILE.write_text(bid + "\n", encoding="utf-8")
    print(f"active brief → {bid}")
    return 0


def cmd_new(args: argparse.Namespace) -> int:
    if yaml is None:
        print("pyyaml required", file=sys.stderr)
        return 1
    bid = args.brief_id.strip()
    dest = BRIEFS_DIR / f"{bid}.yaml"
    if dest.exists() and not args.force:
        print(f"exists: {dest} (use --force)", file=sys.stderr)
        return 1
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    if not BRIEF_TEMPLATE.is_file():
        print(f"missing template: {BRIEF_TEMPLATE}", file=sys.stderr)
        return 1
    data = yaml.safe_load(BRIEF_TEMPLATE.read_text(encoding="utf-8")) or {}
    data.setdefault("brief", {})["id"] = bid
    if args.display_name:
        data["brief"]["display_name"] = args.display_name
    dest.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"created {dest}")
    return 0


def _deep_merge(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def cmd_migrate(args: argparse.Namespace) -> int:
    if yaml is None:
        print("pyyaml required", file=sys.stderr)
        return 1
    bid = args.to.strip()
    dest = BRIEFS_DIR / f"{bid}.yaml"
    merged: dict[str, Any] = {}
    for legacy_path in args.from_legacy or []:
        p = Path(legacy_path)
        if p.is_file():
            merged = _deep_merge(merged, yaml.safe_load(p.read_text(encoding="utf-8")) or {})
    for cfg_path in args.from_config or []:
        p = Path(cfg_path)
        if p.is_file():
            merged = _deep_merge(merged, yaml.safe_load(p.read_text()) or {})
    merged.setdefault("brief", {})
    merged["brief"]["id"] = bid
    if args.display_name:
        merged["brief"]["display_name"] = args.display_name
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    if dest.exists() and not args.force:
        print(f"exists: {dest} (use --force to overwrite)", file=sys.stderr)
        return 1
    dest.write_text(
        yaml.safe_dump(merged, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"wrote {dest}")
    if not ACTIVE_BRIEF_FILE.is_file():
        ACTIVE_BRIEF_FILE.write_text(bid + "\n", encoding="utf-8")
        print(f"set active → {bid}")
    return 0


def cmd_migrate_data(args: argparse.Namespace) -> int:
    bid = resolve_brief_id(args.brief)
    moved = 0
    for skill_name, _ch in SKILL_DIRS:
        skill_dir = SKILLS_ROOT / skill_name
        legacy = skill_dir / "data"
        dest = brief_data_dir(skill_dir, bid)
        dest.mkdir(parents=True, exist_ok=True)
        if not legacy.is_dir():
            continue
        for pattern in DATA_GLOBS:
            for src in legacy.glob(pattern):
                if not src.is_file():
                    continue
                target = dest / src.name
                if target.exists():
                    continue
                shutil.move(str(src), str(target))
                moved += 1
                print(f"  moved {src.relative_to(SKILLS_ROOT)} → {target.relative_to(SKILLS_ROOT)}")
        traces = legacy / "traces"
        if traces.is_dir() and not (dest / "traces").exists():
            shutil.move(str(traces), str(dest / "traces"))
            print(f"  moved traces → {dest / 'traces'}")
        events = legacy / "events.jsonl"
        if events.is_file() and not (dest / "events.jsonl").exists():
            shutil.move(str(events), str(dest / "events.jsonl"))
            print(f"  moved events.jsonl")
    targets_dir = SKILLS_ROOT / "jp-form-outreach" / "targets"
    targets_dir.mkdir(parents=True, exist_ok=True)
    legacy_yaml = SKILLS_ROOT / "jp-form-outreach" / "targets.yaml"
    dest_yaml = targets_dir / f"{bid}.yaml"
    if legacy_yaml.is_file() and not dest_yaml.exists():
        shutil.copy2(legacy_yaml, dest_yaml)
        print(f"  copied targets.yaml → {dest_yaml}")
    legacy_csv = SKILLS_ROOT / "linkedin-outreach" / "targets.csv"
    dest_csv = SKILLS_ROOT / "linkedin-outreach" / "targets" / f"{bid}.csv"
    dest_csv.parent.mkdir(parents=True, exist_ok=True)
    if legacy_csv.is_file() and not dest_csv.exists():
        shutil.copy2(legacy_csv, dest_csv)
        print(f"  copied targets.csv → {dest_csv}")
    print(f"migrate-data done ({moved} files moved)")
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    channels = [c.strip() for c in args.default_channels.split(",") if c.strip()]
    path = channel_state.bind(
        args.channel_id,
        args.brief,
        default_channels=channels or None,
        channel_name=args.channel_name or "",
        operator_user_id=args.operator_user_id or "",
    )
    print(f"bound {args.channel_id} → {args.brief} ({path})")
    return 0


def cmd_unbind(args: argparse.Namespace) -> int:
    if channel_state.unbind(args.channel_id):
        print(f"unbound {args.channel_id}")
        return 0
    print(f"no binding for {args.channel_id}", file=sys.stderr)
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    ch = args.channel_id or channel_state.slack_channel_id_from_env() or None
    thread_ts = args.thread_ts or channel_state.slack_thread_ts_from_env() or None
    print(
        build_status_report(
            args.brief,
            channel_id=ch,
            thread_ts=thread_ts,
            skill=args.skill,
        )
    )
    return 0


def cmd_stop_run(args: argparse.Namespace) -> int:
    bid = resolve_brief_id(args.brief)
    skill_dir = SKILLS_ROOT / args.skill
    data = brief_data_dir(skill_dir, bid)
    lock = read_lock(data)
    if not lock:
        print(f"[stop-run] no active_run.lock under {data}", file=sys.stderr)
        return 1
    pid = lock.get("pid")
    if isinstance(pid, int) and is_lock_alive(lock):
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[stop-run] sent SIGTERM to pid {pid} (run_id={lock.get('run_id')})")
        except OSError as exc:
            print(f"[stop-run] kill failed: {exc}", file=sys.stderr)
            return 1
    else:
        print(f"[stop-run] stale lock (pid={pid}), removing")
    remove_lock(data)
    return 0


def cmd_write_from_json(args: argparse.Namespace) -> int:
    """Write briefs/<id>.yaml from onboarding answers JSON (§14-N)."""
    if yaml is None:
        print("pyyaml required", file=sys.stderr)
        return 1
    bid = args.brief_id.strip()
    answers_path = Path(args.answers)
    if not answers_path.is_file():
        print(f"not found: {answers_path}", file=sys.stderr)
        return 1
    answers = json.loads(answers_path.read_text(encoding="utf-8"))
    if not isinstance(answers, dict):
        print("answers JSON must be an object", file=sys.stderr)
        return 1
    data = yaml.safe_load(BRIEF_TEMPLATE.read_text(encoding="utf-8")) if BRIEF_TEMPLATE.is_file() else {}
    data = _deep_merge(data, answers)
    data.setdefault("brief", {})["id"] = bid
    if args.display_name:
        data["brief"]["display_name"] = args.display_name
    dest = BRIEFS_DIR / f"{bid}.yaml"
    BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"wrote {dest}")
    if args.bind_channel:
        channel_state.bind(
            args.bind_channel,
            bid,
            default_channels=[c.strip() for c in (args.default_channels or "jp_form,linkedin").split(",") if c.strip()],
            channel_name=args.channel_name or "",
        )
        print(f"bound channel {args.bind_channel} → {bid}")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    bid = args.brief_id.strip()
    src = BRIEFS_DIR / f"{bid}.yaml"
    if not src.is_file():
        print(f"not found: {src}", file=sys.stderr)
        return 1
    archived_dir = BRIEFS_DIR / "archived"
    archived_dir.mkdir(parents=True, exist_ok=True)
    dest = archived_dir / f"{bid}.yaml"
    src.rename(dest)
    print(f"archived → {dest}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Doorman multi-brief CLI (v4 §14)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List brief ids")

    p = sub.add_parser("show")
    p.add_argument("brief_id")

    p = sub.add_parser("set-active")
    p.add_argument("brief_id")

    p = sub.add_parser("new")
    p.add_argument("brief_id")
    p.add_argument("--display-name", default="")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("migrate", help="Build brief YAML from legacy files")
    p.add_argument(
        "--from-legacy",
        action="append",
        default=[],
        help="Legacy root YAML to merge (e.g. former shared brief file)",
    )
    p.add_argument("--from-config", action="append", default=[])
    p.add_argument("--to", required=True)
    p.add_argument("--display-name", default="")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("migrate-data", help="Move data/* to data/briefs/<id>/")
    p.add_argument("--brief", default=None)

    p = sub.add_parser("bind", help="Bind Slack channel to brief")
    p.add_argument("--channel-id", required=True)
    p.add_argument("--brief", required=True)
    p.add_argument(
        "--default-channels",
        default="jp_form,linkedin",
        help="Comma-separated: jp_form, linkedin",
    )
    p.add_argument("--channel-name", default="")
    p.add_argument("--operator-user-id", default="")

    p = sub.add_parser("unbind", help="Remove Slack channel binding")
    p.add_argument("--channel-id", required=True)

    p = sub.add_parser("archive")
    p.add_argument("brief_id")

    p = sub.add_parser("status", help="Reconstruct progress from file state (§14-G)")
    p.add_argument("--brief", default=None)
    p.add_argument("--channel-id", default=None)
    p.add_argument("--thread-ts", default=None)
    p.add_argument("--skill", default=None, choices=[s[0] for s in SKILL_DIRS])

    p = sub.add_parser("stop-run", help="SIGTERM lock pid and remove active_run.lock")
    p.add_argument("--brief", default=None)
    p.add_argument("--skill", default="jp-form-outreach", choices=[s[0] for s in SKILL_DIRS])

    p = sub.add_parser("write-from-json", help="Create brief YAML from onboarding answers JSON")
    p.add_argument("brief_id")
    p.add_argument("--answers", required=True, help="Path to answers.json")
    p.add_argument("--display-name", default="")
    p.add_argument("--bind-channel", default=None, help="Also bind Slack channel id")
    p.add_argument("--channel-name", default="")
    p.add_argument("--default-channels", default="jp_form,linkedin")

    args = ap.parse_args()
    if args.cmd == "list":
        sys.exit(cmd_list(args))
    if args.cmd == "show":
        sys.exit(cmd_show(args))
    if args.cmd == "set-active":
        sys.exit(cmd_set_active(args))
    if args.cmd == "new":
        sys.exit(cmd_new(args))
    if args.cmd == "migrate":
        sys.exit(cmd_migrate(args))
    if args.cmd == "migrate-data":
        sys.exit(cmd_migrate_data(args))
    if args.cmd == "bind":
        sys.exit(cmd_bind(args))
    if args.cmd == "unbind":
        sys.exit(cmd_unbind(args))
    if args.cmd == "archive":
        sys.exit(cmd_archive(args))
    if args.cmd == "status":
        sys.exit(cmd_status(args))
    if args.cmd == "stop-run":
        sys.exit(cmd_stop_run(args))
    if args.cmd == "write-from-json":
        sys.exit(cmd_write_from_json(args))


if __name__ == "__main__":
    main()
