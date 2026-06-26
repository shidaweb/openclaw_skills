#!/usr/bin/env python3
"""Single entry point for campaign/persona/channel routing."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from _outreach_core import channel_state
from _outreach_core.helpers import run_job
from _outreach_core.persona import list_persona_ids
from _outreach_core.routing import resolve_route


def _route_from_args(args: argparse.Namespace):
    return resolve_route(
        brief_id=getattr(args, "brief", None),
        persona_id=getattr(args, "persona", None),
        channel=getattr(args, "channel", None),
        slack_channel_id=getattr(args, "slack_channel_id", None),
        slack_thread_ts=getattr(args, "slack_thread_ts", None),
    )


def cmd_resolve(args: argparse.Namespace) -> int:
    print(json.dumps(asdict(_route_from_args(args)), ensure_ascii=False, indent=2))
    return 0


def cmd_bind(args: argparse.Namespace) -> int:
    route = _route_from_args(args)
    if not args.slack_channel_id or not args.slack_thread_ts:
        raise ValueError("--slack-channel-id and --slack-thread-ts are required")
    path = channel_state.bind_thread(
        args.slack_channel_id,
        args.slack_thread_ts,
        brief_id=route.brief_id,
        persona_id=route.persona_id,
        channel=route.channel,
        operator_user_id=args.operator_user_id or "",
    )
    print(
        f"bound thread {args.slack_channel_id}/{args.slack_thread_ts} -> "
        f"campaign={route.brief_id} persona={route.persona_id} channel={route.channel}"
    )
    print(path)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    route = _route_from_args(args)
    run_args = list(args.run_args or [])
    if run_args and run_args[0] == "--":
        run_args = run_args[1:]
    if not run_args:
        raise ValueError("pass the channel command after --, e.g. -- campaign --limit 5")
    command = run_args[0]
    tail = run_args[1:]
    routed_args = [command, "--brief", route.brief_id]
    if route.persona_id:
        routed_args.extend(["--persona", route.persona_id])
    routed_args.extend(tail)

    if args.slack_channel_id:
        os.environ["DOORMAN_SLACK_CHANNEL_ID"] = args.slack_channel_id
    if args.slack_thread_ts:
        os.environ["DOORMAN_SLACK_THREAD_TS"] = args.slack_thread_ts
    if route.persona_id:
        os.environ["DOORMAN_PERSONA_ID"] = route.persona_id
    os.environ["DOORMAN_OUTREACH_CHANNEL"] = route.channel

    info = run_job.start(
        route.skill,
        routed_args,
        slack_channel_id=args.slack_channel_id or None,
        slack_thread_ts=args.slack_thread_ts or None,
    )
    print(json.dumps({"route": asdict(route), "job": info}, ensure_ascii=False, indent=2))
    return 0


def _add_route_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--brief", default=None, help="Campaign brief id")
    parser.add_argument("--persona", default=None, help="Speaker persona id")
    parser.add_argument("--channel", choices=["jp_form", "linkedin"], default=None)
    parser.add_argument("--slack-channel-id", default=os.environ.get("DOORMAN_SLACK_CHANNEL_ID", ""))
    parser.add_argument("--slack-thread-ts", default=os.environ.get("DOORMAN_SLACK_THREAD_TS", ""))


def main() -> None:
    ap = argparse.ArgumentParser(prog="outreach", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("resolve", help="Resolve campaign + persona + channel")
    _add_route_args(p)

    p = sub.add_parser("bind", help="Bind one Slack thread to an outreach context")
    _add_route_args(p)
    p.add_argument("--operator-user-id", default="")

    p = sub.add_parser("start", help="Resolve route and start the correct channel skill")
    _add_route_args(p)
    p.add_argument("run_args", nargs=argparse.REMAINDER)

    sub.add_parser("personas", help="List available personas")

    args = ap.parse_args()
    try:
        if args.cmd == "resolve":
            code = cmd_resolve(args)
        elif args.cmd == "bind":
            code = cmd_bind(args)
        elif args.cmd == "start":
            code = cmd_start(args)
        else:
            print("\n".join(list_persona_ids()))
            code = 0
    except Exception as exc:  # noqa: BLE001 - concise CLI boundary
        print(str(exc), file=sys.stderr)
        code = 2
    raise SystemExit(code)


if __name__ == "__main__":
    main()
