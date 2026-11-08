"""Structured handling of approvals.snapshot payloads.

A turn's subscription delivers ``approvals.snapshot`` events whose
payload carries ``pending_approvals``: the approvals Muse is
waiting on. This module parses that snapshot into display-ready
records. Deciding an approval is not possible from this client:
every candidate decision route is refused by the credential's
scopes (measured 2026-11-08, see docs/PROBE-RESULTS.md), so the
client presents pending approvals accurately and leaves the
decision to the Muse app.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PendingApproval:
    approval_id: str | None
    title: str
    detail: str = ""
    raw: dict = field(default_factory=dict)


def _first_text(record: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def parse_snapshot(snapshot) -> list[PendingApproval]:
    """Pending approvals from an approvals.snapshot payload dict."""
    if not isinstance(snapshot, dict):
        return []
    pending = snapshot.get("pending_approvals")
    if not isinstance(pending, list):
        return []
    approvals: list[PendingApproval] = []
    for entry in pending:
        if not isinstance(entry, dict):
            continue
        title = _first_text(entry, ("title", "summary", "description",
                                    "action", "tool_name", "name")) \
            or "Approval requested"
        detail = _first_text(entry, ("detail", "reason", "command",
                                     "args_summary"))
        approval_id = entry.get("id") or entry.get("approval_id")
        approvals.append(PendingApproval(
            approval_id=approval_id if isinstance(approval_id, str)
            else None,
            title=title, detail=detail, raw=entry))
    return approvals


def format_pending(approvals: list[PendingApproval]) -> str:
    if not approvals:
        return ""
    lines = [f"{len(approvals)} approval(s) waiting in the Muse app:"]
    for approval in approvals:
        line = f"- {approval.title}"
        if approval.detail:
            line += f" ({approval.detail[:120]})"
        lines.append(line)
    return "\n".join(lines)
