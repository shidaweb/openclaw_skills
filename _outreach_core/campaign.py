"""Shared List -> Enrich -> Draft -> Send campaign runner.

Channel implementations supply four operations. The runner owns ordering,
stop semantics, result reconciliation, and the machine-readable summary.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

PHASES = ("list", "enrich", "draft", "send")


@dataclass(frozen=True)
class CampaignContext:
    brief_id: str
    persona_id: str | None
    channel: str
    skill: str
    data_dir: Path
    slack_channel_id: str | None = None
    slack_thread_ts: str | None = None
    run_id: str | None = None

    def public_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["data_dir"] = str(self.data_dir)
        return row


@dataclass
class PhaseResult:
    phase: str
    total: int = 0
    ready: int = 0
    skipped: int = 0
    pending: int = 0
    sent: int = 0
    failed: int = 0
    status: str = "ok"
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CampaignResult:
    context: CampaignContext
    phases: list[PhaseResult]
    status: str
    stopped_after: str
    reconciled: bool

    def phase(self, name: str) -> PhaseResult | None:
        return next((row for row in self.phases if row.phase == name), None)


# v32 FX6 — an empty LIST phase is not a crash. Bootstrap legitimately
# writes 0 targets when the brief is exhausted (everything sent/skipped)
# or when a stale --ids filter matches nothing; raising RuntimeError here
# used to burn 3 futile supervisor restarts per occurrence.

def is_empty_list_result(result: "CampaignResult") -> bool:
    """True when the campaign failed ONLY because LIST produced 0 targets."""
    if result.status != "failed" or result.stopped_after != "list":
        return False
    list_phase = result.phase("list")
    if list_phase is None:
        return False
    return str(list_phase.detail.get("reason") or "") == "no targets"


def empty_list_exit_code(only_ids: list | None) -> int:
    """Exit code for the empty-list terminal.

    - ``--ids`` was given (2): the operator named ids that matched nothing —
      a deterministic input error; the supervisor never retries exit 2.
    - no filter (0): the brief is simply exhausted — a normal, successful
      no-op terminal.
    """
    return 2 if only_ids else 0


def empty_list_message(brief_id: str | None, only_ids: list | None) -> str:
    brief = brief_id or "?"
    if only_ids:
        ids = ",".join(str(x) for x in list(only_ids)[:10])
        return (
            f"リスト0件のため送信対象がありません（brief={brief}）。"
            f"--ids 指定（{ids}）が古い/存在しない可能性があります。"
            "targets.yaml の id を確認してください。"
        )
    return (
        f"リスト0件のため送信対象がありません（brief={brief}）。"
        "全件が送信済み/スキップ済みの可能性があります（正常終了扱い）。"
    )


class OutreachChannelAdapter(Protocol):
    """Channel boundary. List/enrich/send differ; draft may reuse core draft."""

    channel: str

    def list(self, context: CampaignContext) -> PhaseResult: ...

    def enrich(self, context: CampaignContext) -> PhaseResult: ...

    def draft(self, context: CampaignContext) -> PhaseResult: ...

    def send(self, context: CampaignContext) -> PhaseResult: ...


@dataclass
class FunctionChannelAdapter:
    channel: str
    list_fn: Callable[[CampaignContext], PhaseResult]
    enrich_fn: Callable[[CampaignContext], PhaseResult]
    draft_fn: Callable[[CampaignContext], PhaseResult]
    send_fn: Callable[[CampaignContext], PhaseResult]

    def list(self, context: CampaignContext) -> PhaseResult:
        return self.list_fn(context)

    def enrich(self, context: CampaignContext) -> PhaseResult:
        return self.enrich_fn(context)

    def draft(self, context: CampaignContext) -> PhaseResult:
        return self.draft_fn(context)

    def send(self, context: CampaignContext) -> PhaseResult:
        return self.send_fn(context)


class CampaignRunner:
    def __init__(self, context: CampaignContext):
        self.context = context

    def run(
        self,
        adapter: OutreachChannelAdapter,
        *,
        stop_after: str = "send",
        replace_context: bool = False,
    ) -> CampaignResult:
        if adapter.channel != self.context.channel:
            raise ValueError(
                f"adapter channel {adapter.channel!r} != context channel {self.context.channel!r}"
            )
        if stop_after not in PHASES:
            raise ValueError(f"invalid stop_after {stop_after!r}; expected one of {PHASES}")

        self._prepare_workspace(replace_context=replace_context)

        results: list[PhaseResult] = []
        for phase in PHASES:
            result = getattr(adapter, phase)(self.context)
            if result.phase != phase:
                raise ValueError(f"adapter returned phase {result.phase!r} while running {phase!r}")
            results.append(result)
            if result.status not in {"ok", "skipped"}:
                reconciled = phase == "send" and self._send_reconciled(result)
                campaign = CampaignResult(self.context, results, "failed", phase, reconciled)
                self._write_summary(campaign)
                return campaign
            if phase == stop_after:
                status = "complete" if phase == "send" else "stopped"
                reconciled = phase == "send" and self._send_reconciled(result)
                if phase == "send" and not reconciled:
                    result.status = "failed"
                    result.detail["reason"] = "send counts do not reconcile"
                    status = "failed"
                campaign = CampaignResult(self.context, results, status, phase, reconciled)
                self._write_summary(campaign)
                return campaign

        send = results[-1]
        reconciled = self._send_reconciled(send)
        campaign = CampaignResult(
            self.context,
            results,
            "complete" if reconciled else "failed",
            "send",
            reconciled,
        )
        self._write_summary(campaign)
        return campaign

    @staticmethod
    def _send_reconciled(result: PhaseResult) -> bool:
        return result.total == result.sent + result.pending + result.skipped + result.failed

    def _prepare_workspace(self, *, replace_context: bool) -> None:
        """Prevent resumable artifacts from crossing persona/channel contexts."""
        path = self.context.data_dir / "campaign_context.json"
        current = {
            "brief_id": self.context.brief_id,
            "persona_id": self.context.persona_id,
            "channel": self.context.channel,
            "skill": self.context.skill,
        }
        previous: dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                previous = loaded if isinstance(loaded, dict) else {}
            except (OSError, json.JSONDecodeError):
                previous = {}
        identity_changed = previous and any(previous.get(k) != v for k, v in current.items())
        if identity_changed and not replace_context:
            raise RuntimeError(
                "campaign/persona/channel context changed; rerun with --clean to "
                "avoid reusing drafts from another persona"
            )
        self.context.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {**current, "updated_at": datetime.utcnow().isoformat() + "Z"},
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def _write_summary(self, result: CampaignResult) -> None:
        payload = {
            "context": result.context.public_dict(),
            "status": result.status,
            "stopped_after": result.stopped_after,
            "reconciled": result.reconciled,
            "phases": [asdict(row) for row in result.phases],
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        result.context.data_dir.mkdir(parents=True, exist_ok=True)
        (result.context.data_dir / "campaign_summary.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
