"""The phases of a shoot, and which of them a folder can enter right now.

Darkimiya is a pipeline: a cull narrows a folder to a selection, an
assessment judges that selection, suggestions are written against what was
judged, a development executes them, and an export delivers the result.
Until now that order lived only in the photographer's head and in which
button happened to be on which page.

This module states it once, in one place, with no interface attached. It
answers two questions about every phase of one folder: has it happened, and
can it be entered now. A phase that cannot be entered carries the reason,
because "greyed out with no explanation" is the interface telling somebody
they are wrong without saying how.

Two rules shape the gates:

Nothing is blocked to protect the photographer from a bad photograph. The
gates exist where a phase genuinely has no input -- there is no assessment
to review before one has been run -- and nowhere else. Development in
particular is never gated on the AI: a calibrated baseline can be rendered
from any frame, and a photographer who wants no suggestions should not have
to buy them.

The personal style is never blocked at all. It belongs to the photographer
rather than to the shoot, it can be built before any folder is opened, and
it appears in the order only because it is what the AI editing phase reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from opencull_gui.project import load_project

CULL = "cull"
ASSESSMENT = "assessment"
PROFILE = "profile"
SUGGESTIONS = "suggestions"
DEVELOPMENT = "development"
FINE_TUNING = "fine_tuning"
EXPORT = "export"
DEBRIEF = "debrief"

ORDER = (CULL, ASSESSMENT, PROFILE, SUGGESTIONS, DEVELOPMENT, FINE_TUNING,
         EXPORT, DEBRIEF)

TITLES = {
    CULL: "Cull",
    ASSESSMENT: "Assessment",
    PROFILE: "Personal style",
    SUGGESTIONS: "AI editing",
    DEVELOPMENT: "Development",
    FINE_TUNING: "Advanced fine tuning",
    EXPORT: "Export",
    DEBRIEF: "Debrief",
}

# What each phase is for, in one sentence, shown wherever there is room.
PURPOSE = {
    CULL: "Narrow the folder to the frames worth keeping.",
    ASSESSMENT: "Have the keepers judged, then decide which are worth developing.",
    PROFILE: "Read your own photographs to learn how you edit.",
    SUGGESTIONS: "Have a model propose how to edit the frames you choose.",
    DEVELOPMENT: "Render a treatment and compare it against the frame as shot.",
    FINE_TUNING: "Move the numbers a treatment compiled into, within their bounds.",
    EXPORT: "Write the finished rendering where it is going.",
    DEBRIEF: "Read the shoot back: what each filter passed, and where "
             "you disagreed.",
}

ACTIVE = {"queued", "running"}


def _count(manifest: dict[str, Any], key: str) -> int:
    items = manifest.get("artifacts", {}).get(key)
    return len(items) if isinstance(items, list) else 0


def manifest_for(project: dict) -> dict[str, Any]:
    """The project manifest, or an empty one when it cannot be read.

    A folder whose manifest has gone missing is not an error to raise here:
    the phases simply report as unstarted, which is what the photographer
    sees on disk too.
    """
    path = Path(str(project.get("project", ""))).expanduser()
    if not path.is_file():
        return {}
    try:
        return load_project(path)
    except (ValueError, OSError):
        return {}


def _job_state(job: Any) -> str:
    return str((job or {}).get("status") or "")


def plan(
    project: dict,
    *,
    profile_selected: bool = False,
    marked: int | None = None,
    culled: bool | None = None,
    suggested: bool | None = None,
    suggesting: bool = False,
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Describe every phase of one folder, in order.

    ``marked`` is how many assessed photographs have been flagged as worth
    developing. ``None`` means nobody has looked yet, which is different
    from nobody having marked anything: an unread assessment does not block
    the suggestion phase, it just has nothing to ask about.

    ``suggested`` overrides the manifest's count of direction rounds. The
    directions belonging to a shortlist are found on disk by convention, and
    a caller holding that index knows the answer more exactly than the
    manifest does.

    ``culled`` overrides what the catalog says. Development on a folder
    that was never culled writes a deterministic everything-included
    selection and registers it as the culling report, so the catalog counts
    that folder as culled. It has a selection; it has not been culled, and
    the phase bar must not claim a run happened that never did.
    """
    if manifest is None:
        manifest = manifest_for(project)

    available = bool(project.get("available"))
    selected = bool(project.get("report_available"))
    culled = selected if culled is None else bool(culled)
    assessed = bool(project.get("shortlist_available"))
    culling = _job_state(project.get("culling"))
    assessing = _job_state(project.get("assessment"))
    rounds = _count(manifest, "edit_directions")
    directions = bool(rounds) if suggested is None else bool(suggested)
    renders = _count(manifest, "renders")
    exports = _count(manifest, "exports")
    tuned = sum(
        1 for item in manifest.get("artifacts", {}).get("renders", []) or []
        if isinstance(item, dict) and item.get("adjustments"))

    offline = "" if available else "The photographs folder is not on disk."

    def entry(
        key: str, state: str, reason: str = "", detail: str = "",
    ) -> dict[str, Any]:
        # The profile is the photographer's, not the shoot's, so a folder
        # that has gone offline has no bearing on it.
        if offline and state != "done" and key != PROFILE:
            state, reason = "blocked", offline
        return {
            "id": key,
            "number": ORDER.index(key) + 1,
            "title": TITLES[key],
            "purpose": PURPOSE[key],
            "state": state,
            "reason": reason,
            "detail": detail,
        }

    phases = []

    if culling in ACTIVE:
        phases.append(entry(CULL, "running", detail="Culling now."))
    elif culled:
        phases.append(entry(CULL, "done", detail="Culled."))
    else:
        phases.append(entry(CULL, "ready"))

    if assessing in ACTIVE:
        phases.append(entry(ASSESSMENT, "running", detail="Assessing now."))
    elif assessed:
        marked_note = (
            f"{marked} marked to develop." if marked
            else "Assessed. Nothing marked to develop yet.")
        phases.append(entry(ASSESSMENT, "done", detail=marked_note))
    elif selected:
        # An assessment reads a selection, and a cull is not the only way to
        # have one: a folder opened without culling gets the deterministic
        # everything-included selection. So this is not blocked -- but the
        # difference is what it costs, and that is worth saying out loud
        # rather than discovering on the invoice.
        phases.append(entry(
            ASSESSMENT, "ready",
            detail="Assesses the frames the cull kept." if culled else
            "No cull yet, so this reads every frame in the folder rather "
            "than the keepers. That is a model call each."))
    else:
        phases.append(entry(
            ASSESSMENT, "blocked",
            "An assessment reads a selection, and this folder has none yet. "
            "Cull it, or open it once to select everything."))

    phases.append(entry(
        PROFILE, "done" if profile_selected else "ready",
        detail="A profile is in use." if profile_selected else
        "Optional. Without one, suggestions have no personal treatment."))

    if suggesting:
        phases.append(entry(
            SUGGESTIONS, "running", detail="Writing directions now."))
    elif directions:
        phases.append(entry(
            SUGGESTIONS, "done",
            detail=f"{rounds} round{'s' if rounds > 1 else ''} asked for."
            if rounds else "Asked for."))
    elif not assessed:
        phases.append(entry(
            SUGGESTIONS, "blocked",
            "Editing suggestions are written against an assessment. "
            "Assess first."))
    else:
        # Nothing marked is not a locked door. The assessment ranked these
        # frames, so the phase has its input; which of them to pay for is a
        # choice made on the way in, and the page itself is where it is
        # made.
        phases.append(entry(
            SUGGESTIONS, "ready",
            detail=f"{marked} frames marked to develop." if marked else
            "Nothing marked yet. Choose the frames to treat on the page."))

    # Never gated on the AI: the calibrated baseline is available for any
    # frame, and asking for it should not require paying for a cull first.
    phases.append(entry(
        DEVELOPMENT, "done" if renders else "ready",
        detail=f"{renders} rendered." if renders else
        "The calibrated baseline is available with or without suggestions."))

    if tuned:
        phases.append(entry(
            FINE_TUNING, "done",
            detail=f"{tuned} adjusted version{'s' if tuned > 1 else ''} kept."))
    elif directions:
        phases.append(entry(
            FINE_TUNING, "ready",
            detail="Every control keeps the sentence that produced it."))
    else:
        phases.append(entry(
            FINE_TUNING, "blocked",
            "Fine tuning moves the numbers a treatment compiled into, so "
            "there has to be a treatment. Ask for AI editing first."))

    if exports:
        phases.append(entry(
            EXPORT, "done", detail=f"{exports} delivered."))
    elif renders:
        phases.append(entry(EXPORT, "ready"))
    else:
        phases.append(entry(
            EXPORT, "blocked",
            "There is nothing rendered to deliver yet. Develop a frame first."))

    # The debrief reads judgements, so it needs some to read. It is never
    # "done": a shoot can be read back as often as it is added to.
    if assessed:
        phases.append(entry(
            DEBRIEF, "ready",
            detail="Counts over this shoot's own records; nothing is spent."))
    else:
        phases.append(entry(
            DEBRIEF, "blocked",
            "A debrief reads the assessment's judgements, and there are "
            "none yet. Assess first."))

    return phases


def openable(phases: list[dict[str, Any]]) -> set[str]:
    """The phases a photographer can enter now.

    A running phase stays open: watching a cull is the whole point of
    having started one.
    """
    return {
        item["id"] for item in phases if item["state"] != "blocked"}
