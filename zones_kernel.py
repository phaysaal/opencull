"""Where the sliders' advice bands sit, for one photograph, measured first.

The fine-tune page paints every slider in three bands -- safe, artistic,
damage -- and until a model has looked at the frame, those bands are
professional defaults: right on average, wrong in particular. A night
frame's safe exposure range is not a beach frame's; an infrared frame
with no natural palette can take saturation moves that would ruin a
wedding. This kernel serves the program that asks one model, once, to
place the bands for one photograph, and writes the answer beside the
recipes where the page already looks.

The answer is advice, not walls: the page lets the photographer travel
the compiler's whole range regardless. So the validation here is about
honesty, not authority -- every stated band is clamped inside the
compiler's executable range, safe is forced inside artistic, and a
control the model says nothing about keeps the default rather than
inheriting an invention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opencull_gui.zones import DEFAULTS, FORMAT, zones_for
from recipe_compiler import RANGES
from treatment_kernel import as_record, data, measure_frame, starting_frame

# What the bands are judged against: at least this many controls placed
# before the file is worth writing at all. Fewer reads as a model that
# answered the format and not the photograph.
ENOUGH = 8

_LABELS = {
    "tone.exposure": "exposure (EV)",
    "tone.brightness": "brightness", "tone.contrast": "contrast",
    "tone.highlight": "highlights", "tone.shadow": "shadows",
    "tone.white": "whites", "tone.black": "blacks",
    "levels.black_input": "black point (0-255)",
    "levels.white_input": "white point (0-255)",
    "levels.midpoint": "midpoint gamma",
    "color.temperature": "temperature (kelvin, delta)",
    "color.tint": "tint", "color.saturation": "saturation",
    "detail.clarity": "clarity", "detail.structure": "structure",
    "detail.dehaze": "dehaze", "finish.vignette": "vignette",
    "color.neutralize": "infrared neutralize",
}


def now_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def zones_proof(photos: str, photo: str, edge: float = 1100) -> str:
    """The frame with nothing done to it, through the real renderer.

    The bands advise adjustments applied on top of this rendering, so
    this rendering -- not the camera's JPEG of a different processing --
    is what the model must look at. Kept under Previews beside the other
    proofs, because it is one.
    """
    where = (Path(str(photos)).expanduser().resolve() / ".darkimiya"
             / "Previews" / "ControlZones" / Path(str(photo)).stem)
    where.mkdir(parents=True, exist_ok=True)
    return starting_frame(photos, photo, str(where), edge)


def zones_evidence(photo: str, proof: str, spectrum: str = "visible",
                   cutoff_nm: float = 0, about: str = "") -> str:
    """Everything known locally about the frame the bands are for."""
    return json.dumps({
        "photo": str(photo),
        "spectrum": str(spectrum),
        "cutoff_nm": float(cutoff_nm) if spectrum == "infrared" else 0.0,
        "about": " ".join(str(about).split())[:600],
        "measured": measure_frame(proof),
    }, sort_keys=True)


def controls_catalogue() -> str:
    """Every control the page offers: range, and the default bands.

    The defaults are given as the starting point to disagree with,
    because "move the bands where this frame needs them" is a better
    question than "invent seventeen ranges from nothing".
    """
    catalogue = {}
    for op, (low, high, unit) in RANGES.items():
        default = DEFAULTS.get(op, {})
        catalogue[op] = {
            "what": _LABELS.get(op, op),
            "unit": unit,
            "executable": [low, high],
            "default_safe": list(default.get("safe", (low, high))),
            "default_artistic": list(default.get("artistic", (low, high))),
        }
    return json.dumps(catalogue, indent=1, sort_keys=True)


def zones_prompt(evidence_text: str) -> str:
    evidence = data(evidence_text) or {}
    lines = [
        "You are a professional retoucher setting the advisory ranges on "
        "an editing panel, for ONE photograph you are looking at.",
        "For each control: SAFE is the range a professional would move "
        "within without comment on this frame; ARTISTIC is the wider "
        "range where the move is a deliberate statement that had better "
        "be meant. Beyond artistic, the photograph is being damaged for "
        "nothing. The bands are advice, not walls -- the photographer "
        "can go anywhere; your job is that the advice fit THIS frame, "
        "not photographs in general.",
        "THE FRAME, MEASURED:\n"
        + json.dumps(evidence.get("measured") or {}, indent=1),
        "THE CONTROLS, with executable ranges and the generic defaults "
        "you are correcting for this frame:\n" + controls_catalogue(),
        "Answer in 'zones' as a JSON object: control name to "
        '{"safe": [low, high], "artistic": [low, high]}. Place every '
        "control you have an opinion on; omit the ones where the "
        "default already fits -- an omitted control keeps its default. "
        "Numbers in the control's own unit. Safe inside artistic. "
        "In 'rationale', say in two or three sentences what about this "
        "frame moved which bands and why.",
    ]
    if evidence.get("about"):
        lines.insert(3, "WHAT THE PHOTOGRAPHER SAYS THIS SHOOT IS: "
                     f"“{evidence['about']}”")
    if evidence.get("spectrum") == "infrared":
        cutoff = float(evidence.get("cutoff_nm") or 0)
        lines.insert(3, (
            "THIS IS AN INFRARED PHOTOGRAPH"
            + (f" ({cutoff:.0f}nm cut-off)" if cutoff else "") + ". "
            "There is no natural palette to protect, so colour moves "
            "that would be damage on a visible-light frame may be "
            "ordinary here -- and tonal structure is usually all the "
            "picture has, so protect it accordingly."))
    return "\n\n".join(lines)


def zones_file(photo: str, answer: Any) -> str:
    """The model's bands as the file the page reads, or "" if unusable.

    Clamped through the same zones_for the page uses, so what is written
    is exactly what will be painted -- a band the file exaggerates past
    the executable range is narrowed here, not at read time by surprise.
    """
    record = as_record(answer)
    stated = data(record.get("zones"))
    if not isinstance(stated, dict):
        return ""
    kept = {}
    for op, bands in stated.items():
        if op not in RANGES or not isinstance(bands, dict):
            continue
        settled = zones_for(op, bands)
        if not settled:
            continue
        told = data(json.dumps(bands)) or {}
        # Only keep what the model actually placed; zones_for fills the
        # rest with defaults, which the reader would do anyway.
        entry = {}
        if isinstance(told.get("safe"), list):
            entry["safe"] = list(settled["safe"])
        if isinstance(told.get("artistic"), list):
            entry["artistic"] = list(settled["artistic"])
        if entry:
            kept[op] = entry
    if len(kept) < ENOUGH:
        return ""
    return json.dumps({
        "format": FORMAT,
        "photo": str(photo),
        "created_at": datetime.now(UTC).isoformat(),
        "rationale": " ".join(str(record.get("rationale") or "").split()),
        "zones": kept,
    }, indent=2, sort_keys=True)


def zones_valid(file_text: str) -> bool:
    """What must be true before the file is worth an act."""
    value = data(file_text)
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        return False
    stated = value.get("zones")
    if not isinstance(stated, dict) or len(stated) < ENOUGH:
        return False
    for op, bands in stated.items():
        if op not in RANGES:
            return False
        low, high = RANGES[op][0], RANGES[op][1]
        for name in ("safe", "artistic"):
            pair = bands.get(name)
            if pair is not None and not (
                    isinstance(pair, list) and len(pair) == 2
                    and all(isinstance(v, (int, float)) for v in pair)
                    and low <= pair[0] <= pair[1] <= high):
                return False
    return True


def zones_warrant(evidence_text: str, file_text: str) -> str:
    """What the panel reads: the frame's numbers beside the bands."""
    evidence = data(evidence_text) or {}
    value = data(file_text) or {}
    return json.dumps({
        "measured": evidence.get("measured"),
        "spectrum": evidence.get("spectrum"),
        "bands": value.get("zones"),
        "rationale": value.get("rationale"),
    }, separators=(",", ":"), sort_keys=True)


def zones_policy() -> str:
    return (
        "advisory adjustment ranges for one photograph that a working "
        "retoucher could sign: each band inside the executable range, "
        "safe inside artistic, and the departures from the generic "
        "defaults consistent with the frame's measured state and stated "
        "spectrum -- not a copy of the defaults, and not invention "
        "contradicting the numbers.")
