"""The shoot, read back to the photographer who shot it.

Every phase of the pipeline filtered this shoot and wrote down what it did.
The debrief aggregates those records into the shape a photographer can
learn from: how many frames entered, what each filter passed, where they
agreed with the models and where they overruled them. It is the filter
ladder from the chart, drawn with this shoot's own numbers.

Everything here is a count over local records. No model is asked; the memo
that would turn these numbers into advice needs a kernel that does not
exist yet, and this module does not pretend otherwise.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

TIER_ORDER = ("exceptional", "strong", "promising", "ordinary", "reject")


def aggregate(bench: Any) -> dict[str, Any]:
    """Every number the records can honestly give, in pipeline order."""
    report = bench.report
    total = len(report.photo_names)
    culled = bool(bench.culled())

    # --- the cull and its review ------------------------------------------
    try:
        review = bench.reviews.public_state()
    except Exception:                                # noqa: BLE001 - absent
        review = {}
    clusters = review.get("clusters") or {}
    reviewed = sum(
        1 for item in clusters.values() if item.get("reviewed") is True)
    overruled = 0
    keep_none = 0
    for cluster_id, human in clusters.items():
        if human.get("reviewed") is not True:
            continue
        proposed = set(
            report.decision_by_id.get(cluster_id, {}).get("photos") or [])
        kept = set(human.get("keepers") or [])
        if not kept:
            keep_none += 1
        if kept != proposed:
            overruled += 1
    selection = len(bench.selection())

    # --- the assessment and the ratings -----------------------------------
    assessed = 0
    by_hand = False
    tiers: Counter[str] = Counter()
    your_tiers: Counter[str] = Counter()
    rating_overrules = 0
    marked = 0
    if bench.shortlist_path.is_file():
        try:
            entries = list(bench.shortlist.entries)
            marks = (bench.shortlist_reviews.public_state()
                     .get("entries") or {})
            by_hand = str(
                bench.shortlist.data.get("rated_by") or "") == "photographer"
        except Exception:                            # noqa: BLE001 - absent
            entries, marks = [], {}
        assessed = len(entries)
        for entry in entries:
            photo = str(entry.get("photo", ""))
            proposed_tier = str(entry.get("tier", ""))
            if not by_hand:
                tiers[proposed_tier] += 1
            mark = marks.get(photo) or {}
            yours = str(mark.get("tier") or "")
            if yours:
                your_tiers[yours] += 1
                if not by_hand and yours != proposed_tier:
                    rating_overrules += 1
            if mark.get("interesting"):
                marked += 1

    # --- what the marks became --------------------------------------------
    suggested = 0
    developed = 0
    delivered = 0
    try:
        payload = bench.directions.payload()
        suggested = int(payload.get("processed_count") or 0)
    except Exception:                                # noqa: BLE001 - absent
        pass
    try:
        workspace = bench.workspace
        developed = len({
            str(item.get("source_photo"))
            for item in workspace.payload().get("variants", []) or []
            if isinstance(item, dict) and item.get("source_photo")})
        delivered = len(workspace.export_payload().get("exports") or [])
    except Exception:                                # noqa: BLE001 - absent
        pass

    return {
        "total": total,
        "culled": culled,
        "groups": len(report.cluster_by_id),
        "reviewed_groups": reviewed,
        "overruled_groups": overruled,
        "keep_none_groups": keep_none,
        "selection": selection,
        "assessed": assessed,
        "rated_by_hand": by_hand,
        "tiers": dict(tiers),
        "your_tiers": dict(your_tiers),
        "rating_overrules": rating_overrules,
        "marked": marked,
        "suggested": suggested,
        "developed": developed,
        "delivered": delivered,
    }


def ladder(numbers: dict[str, Any]) -> list[tuple[str, int, str]]:
    """The shoot's own filter ladder: stage, count, and who decided it."""
    steps: list[tuple[str, int, str]] = [
        ("In the folder", int(numbers["total"]), "the camera")]
    if numbers["culled"]:
        steps.append(
            ("Kept by the cull", int(numbers["selection"]),
             "the models, then you"))
    else:
        steps.append(
            ("In the selection", int(numbers["selection"]),
             "everything, uncalled"))
    if numbers["assessed"]:
        steps.append(("Assessed", int(numbers["assessed"]),
                      "you alone" if numbers["rated_by_hand"]
                      else "the models"))
        steps.append(("Marked worth developing", int(numbers["marked"]),
                      "you"))
    if numbers["suggested"]:
        steps.append(("Given treatments", int(numbers["suggested"]),
                      "the models, or a scene's sharing"))
    if numbers["developed"]:
        steps.append(("Developed", int(numbers["developed"]), "you"))
    if numbers["delivered"]:
        steps.append(("Delivered", int(numbers["delivered"]), "you"))
    return steps


def disagreements(numbers: dict[str, Any]) -> list[str]:
    """Where you and the models parted ways, as sentences.

    These are the seeds of the taste ledger: each one is already recorded
    beside the evidence, and here they are counted.
    """
    lines: list[str] = []
    if numbers["overruled_groups"]:
        lines.append(
            f"You overruled the cull's proposal in "
            f"{numbers['overruled_groups']} of "
            f"{numbers['reviewed_groups']} reviewed groups.")
    if numbers["keep_none_groups"]:
        lines.append(
            f"You rejected {numbers['keep_none_groups']} whole "
            f"group{'' if numbers['keep_none_groups'] == 1 else 's'} the "
            "cull had kept something from.")
    if numbers["rating_overrules"]:
        lines.append(
            f"You re-rated {numbers['rating_overrules']} frame"
            f"{'' if numbers['rating_overrules'] == 1 else 's'} away from "
            "the assessment's tier.")
    if not lines:
        lines.append(
            "No recorded disagreements: everywhere you spoke, you agreed "
            "with what was proposed — or nothing has been proposed yet.")
    return lines
