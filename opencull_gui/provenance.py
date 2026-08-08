"""Why a frame is where it is: the chain, read back as a story.

Every decision this application makes is recorded beside the evidence --
the cull's rationale, the review that agreed or did not, the assessment's
tier, the marks, the treatments, the adjustments, the certificate, the
delivery. Assembled in order, those records answer the question a
photographer actually asks months later, and the question a client asks
sooner: why is this frame in the delivery, and why is that one not?

Nothing here computes anything new. The story is exactly what is on
record, in pipeline order, with what has not happened stated plainly at
the end rather than implied by absence. Zero model calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _chapter(label: str, lines: list[str], tone: str = "") -> dict[str, Any]:
    return {"label": label, "lines": [line for line in lines if line],
            "tone": tone}


def frame_story(bench: Any, photo: str) -> list[dict[str, Any]]:
    """The recorded life of one frame, oldest decision first.

    ``bench`` is the shoot's Bench: every store is read through the same
    objects the pages read through, so the story can never disagree with
    the pages telling it.
    """
    chapters: list[dict[str, Any]] = []

    # --- the cull ---------------------------------------------------------
    report = bench.report
    cluster_id = next(
        (key for key, cluster in report.cluster_by_id.items()
         if photo in cluster.get("photos", [])), None)
    if cluster_id is None:
        return [_chapter(
            "NOT IN THIS SHOOT",
            [f"{photo} does not appear in the culling report."], "alarm")]

    decision = report.decision_by_id.get(cluster_id, {})
    proposed = photo in (decision.get("photos") or [])
    manual = bench.culled() is False
    if manual:
        chapters.append(_chapter(
            "SELECTED", ["No cull was run; every frame entered the "
                         "selection as shot."]))
    else:
        lines = [
            f"Group {cluster_id}, "
            f"{len(report.cluster_by_id[cluster_id].get('photos', []))} "
            "frame(s).",
            ("Proposed as a keeper" if proposed else "Not proposed")
            + (f" — {decision.get('rationale')}"
               if decision.get("rationale") else "."),
        ]
        confidence = decision.get("confidence")
        if isinstance(confidence, (int, float)):
            lines.append(f"Curator confidence {float(confidence):.0%}.")
        chapters.append(_chapter("CULLED", lines))

    # --- your cull review -------------------------------------------------
    try:
        review = bench.reviews.public_state()
    except Exception:                                # noqa: BLE001 - absent
        review = {}
    human = (review.get("clusters") or {}).get(cluster_id) or {}
    if human.get("reviewed") is True:
        kept = photo in (human.get("keepers") or [])
        overruled = kept != proposed and not manual
        lines = ["You kept it." if kept else "You did not keep it."]
        if overruled:
            lines.append("That overruled the proposal.")
        if str(human.get("note") or "").strip():
            lines.append(f"Your note: {human['note']}")
        chapters.append(_chapter(
            "YOUR CULL REVIEW", lines, "ok" if kept else "alarm"))

    # --- the assessment ---------------------------------------------------
    entry: dict[str, Any] = {}
    mark: dict[str, Any] = {}
    if bench.shortlist_path.is_file():
        try:
            entry = dict(bench.shortlist.entry_by_photo.get(photo) or {})
            mark = dict((bench.shortlist_reviews.public_state()
                         .get("entries") or {}).get(photo) or {})
        except Exception:                            # noqa: BLE001 - absent
            entry, mark = {}, {}
    if entry:
        by_hand = str(bench.shortlist.data.get("rated_by") or "") == (
            "photographer")
        if by_hand:
            chapters.append(_chapter(
                "LAID OUT FOR RATING",
                ["No model was asked; the ratings below are yours alone."]))
        else:
            lines = [
                f"{str(entry.get('tier', '')).upper()} · "
                f"{float(entry.get('score', 0)):.0f}/100 · "
                f"{float(entry.get('confidence', 0)):.0%} confident."]
            if str(entry.get("rationale") or "").strip():
                lines.append(str(entry["rationale"]))
            chapters.append(_chapter("ASSESSED", lines))
        if mark:
            yours = str(mark.get("tier") or "")
            lines = []
            if yours and yours != str(entry.get("tier") or ""):
                lines.append(
                    f"You set {yours.upper()} where the assessment said "
                    f"{str(entry.get('tier', '')).upper()}.")
            elif yours:
                lines.append(f"You agreed: {yours.upper()}.")
            lines.append(
                "Marked worth developing." if mark.get("interesting")
                else "Not marked for development.")
            if str(mark.get("note") or "").strip():
                lines.append(f"Your note: {mark['note']}")
            chapters.append(_chapter(
                "YOUR RATING", lines,
                "ok" if mark.get("interesting") else ""))

    # --- treatments -------------------------------------------------------
    try:
        directions = bench.directions.payload()
    except Exception:                                # noqa: BLE001 - absent
        directions = {}
    answer = next(
        (item for item in (directions.get("directions") or {})
         .get("entries", []) or []
         if isinstance(item, dict) and item.get("photo") == photo), None)
    if answer:
        titles = [
            str(answer.get(f"{style}_title") or "")
            for style in ("standard", "signature", "creative", "personal")
            if str(answer.get(f"{style}_recipe") or "").strip()]
        lines = ["Treatments: " + " · ".join(title for title in titles
                                             if title)]
        if str(answer.get("derived_from") or ""):
            lines.append(
                f"Shared from {answer['derived_from']} — same scene, "
                "developed as one edit.")
        chapters.append(_chapter("SUGGESTED", lines))

    # --- renders, adjustments, certificates, deliveries -------------------
    workspace = None
    try:
        workspace = bench.workspace
        payload = workspace.payload()
    except Exception:                                # noqa: BLE001 - absent
        payload = {}
    renders = [
        item for item in payload.get("variants", []) or []
        if isinstance(item, dict) and item.get("source_photo") == photo]
    if renders and workspace is not None:
        lines = []
        for item in renders:
            variant = str(item.get("variant", "render"))
            note = " — with your adjustments" if item.get(
                "adjustments") else ""
            lines.append(f"{variant}{note}")
        chapters.append(_chapter("DEVELOPED", lines))
        certified = []
        for item in renders:
            certificate = None
            try:
                certificate = workspace.verification_for(
                    str(item.get("path", "")))
            except Exception:                        # noqa: BLE001 - absent
                certificate = None
            if certificate is not None:
                verdict = certificate.get("verdict", {})
                satisfied = bool(verdict.get("satisfied"))
                certified.append((str(item.get("variant", "")), satisfied))
        for variant, satisfied in certified:
            chapters.append(_chapter(
                "VERIFIED",
                [f"{variant}: "
                 + ("did what its treatment promised."
                    if satisfied else "did not satisfy its treatment.")],
                "ok" if satisfied else "alarm"))
        exports = [
            item for item in
            (workspace.export_payload().get("exports") or [])
            if isinstance(item, dict)
            and Path(str(item.get("source", ""))).name in {
                Path(str(render.get("path", ""))).name
                for render in renders}]
        for item in exports:
            chapters.append(_chapter(
                "DELIVERED",
                [f"To {item.get('destination', '')}"], "ok"))

    # --- what has not happened, said rather than implied ------------------
    if not entry:
        chapters.append(_chapter(
            "NOT ASSESSED", ["No assessment has read this frame."]))
    elif not renders:
        chapters.append(_chapter(
            "NOT DEVELOPED", ["Nothing has been rendered from it yet."]))
    return chapters
