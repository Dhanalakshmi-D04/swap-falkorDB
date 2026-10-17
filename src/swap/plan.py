"""Format a drafted plan as the coordinator packet: a plain Markdown document.

Pure formatting over the packet `SwapTools.get_plan` returns (everything in it was read back out of
FalkorDB); nothing here talks to the graph. The packet always carries the synthetic-data notice and
the approval record, so a draft can never be mistaken for a decision.
"""

from __future__ import annotations

from typing import Any


def render_packet_markdown(packet: dict[str, Any]) -> str:
    """The packet as Markdown: status and approval record, the swaps with every step, and notes."""
    plan = packet["plan"]
    summary = packet["summary"]
    status = str(plan["status"]).upper()
    lines = [
        f"# Swap plan {plan['id']} ({status})",
        "",
        f"> {packet['notice']}",
        "",
        f"- Prepared: {plan['created_at']} (UTC), swaps of up to {plan['max_len']} pairs",
        (
            f"- Covers {summary['matched']} of {summary['pairs_in_pool']} patients in "
            f"{plan['swaps']} swaps; {summary['not_in_plan']} pairs are not in this plan"
        ),
        f"- Hard-to-match patients covered: {summary['hard_matched']} of {summary['hard_total']}",
    ]
    if plan["priority"] != 1.0:
        lines.append(f"- Priority weight for hard-to-match patients: {plan['priority']:g}")
    if summary.get("excluded_committed"):
        lines.append(
            f"- Skipped {summary['excluded_committed']} pairs already committed by an approved plan"
        )
    lines.append(_approval_line(plan))
    lines += ["", "## Swaps", ""]
    for swap in packet["swaps"]:
        lines.append(f"### Swap {swap['index']} ({swap['size']}-way)")
        for step in swap["steps"]:
            lines.append(
                f"- Pair {step['donor_pair']}'s donor (type {step['donor_abo']}) gives to "
                f"pair {step['patient_pair']}'s patient (type {step['patient_abo']})"
            )
        lines.append("")
    lines += [
        "## Evidence",
        "",
        "Every step above is a `CAN_GIVE` link stored in the graph, and the swaps share no pair.",
        "The plan itself is recorded in the graph as a `Plan` node that `PROPOSES` each pair.",
        "",
    ]
    return "\n".join(lines)


def _approval_line(plan: dict[str, Any]) -> str:
    if plan["status"] == "draft":
        return "- **Not approved.** A person must approve this plan before anyone acts on it."
    verb = "Approved" if plan["status"] == "approved" else "Rejected"
    return f"- **{verb}** by {plan['decided_by']} on {plan['decided_at']} (UTC)"
