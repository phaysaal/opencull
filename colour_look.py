"""Camera looks: colour fitted from a chart, or authored as a preference.

The Capture One study came down to one sentence: their colour is a
smooth warp of colour space, built per camera by hand, co-designed with
the tone curve, and relaxed until it cannot band. This module is that
sentence as a tool. It produces looks -- a 3x3 matrix plus a coarse
delta lattice for the engine's color.warp -- and saves them as ordinary
presets, so a fitted camera look sits in the same strip as "Warm
daylight" and travels the same way.

Two ways to make one:

  fit       -- photograph a ColorChecker 24, export the as-shot
               baseline render, and point this at it. White balance
               comes from the grey row, a matrix carries the reach,
               and only the residue lands in the lattice -- relaxed,
               per the doctrine: smoothness before accuracy.
  standard  -- no chart at all: the preferred-rendering deltas the
               literature has measured for sixty years. Skin walks a
               little red, skies a little cyan, everything gains a
               breath of chroma, neutrals never move.

Usage:
  python colour_look.py fit CHART.jpg --corners x,y,x,y,x,y,x,y \
      --name "X-T5 daylight"     # corners TL,TR,BR,BL of the patch
                                 # area, as fractions of the image
  python colour_look.py standard --strength 100
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
from typing import Any

import numpy as np

from development_engine import _ENCODE_GAMMA

# The classic X-Rite ColorChecker 24, sRGB 8-bit, row by row from the
# dark-skin corner: the values every profiling text keys on.
CC24_SRGB: tuple[tuple[int, int, int], ...] = (
    (115, 82, 68), (194, 150, 130), (98, 122, 157), (87, 108, 67),
    (133, 128, 177), (103, 189, 170),
    (214, 126, 44), (80, 91, 166), (193, 90, 99), (94, 60, 108),
    (157, 188, 64), (224, 163, 46),
    (56, 61, 150), (70, 148, 73), (175, 54, 60), (231, 199, 31),
    (187, 86, 149), (8, 133, 161),
    (243, 243, 242), (200, 200, 200), (160, 160, 160), (122, 122, 121),
    (85, 85, 85), (52, 52, 52),
)
NEUTRAL_ROW = tuple(range(18, 24))

LOOK_SIZE = 9          # nodes per axis: coarse on purpose -- no cliffs
LOOK_SPREAD = 1.4      # RBF reach, in node spacings
LOOK_PULL = 0.12       # how hard far-from-data nodes are held at zero


class LookError(ValueError):
    """A look cannot be fitted from what was given."""


# --- reading the chart ----------------------------------------------------

def patch_means(image: np.ndarray,
                corners: list[float]) -> np.ndarray:
    """The 24 patch colours, sampled from a bilinear 6x4 grid.

    corners: TL, TR, BR, BL of the patch area as (x, y) fractions of
    the image, eight numbers. Each patch is read from the central 40%
    of its cell, so a slightly loose corner does not smear neighbours
    into each other.
    """
    if len(corners) != 8:
        raise LookError("corners must be eight numbers: TL,TR,BR,BL x,y")
    height, width = image.shape[:2]
    points = np.array(corners, dtype=np.float64).reshape(4, 2)
    points *= (width, height)
    tl, tr, br, bl = points
    means = []
    for row in range(4):
        for col in range(6):
            u0, u1 = (col + 0.3) / 6.0, (col + 0.7) / 6.0
            v0, v1 = (row + 0.3) / 4.0, (row + 0.7) / 4.0
            samples = []
            for v in np.linspace(v0, v1, 5):
                left = tl + (bl - tl) * v
                right = tr + (br - tr) * v
                for u in np.linspace(u0, u1, 5):
                    x, y = left + (right - left) * u
                    xi = int(np.clip(x, 0, width - 1))
                    yi = int(np.clip(y, 0, height - 1))
                    samples.append(image[yi, xi])
            means.append(np.mean(samples, axis=0))
    return np.asarray(means, dtype=np.float32)


# --- the fit --------------------------------------------------------------

def fit_matrix(measured_display: np.ndarray) -> tuple[np.ndarray, dict]:
    """White balance and a 3x3, fitted in linear light.

    The matrix bounds the lattice's reach (Torger): it does the heavy
    colorimetric lifting so the LUT only carries a residue small enough
    to relax. Grey balance comes from the neutral row alone.
    """
    measured = np.clip(measured_display, 1e-4, 1.0) ** _ENCODE_GAMMA
    target = (np.asarray(CC24_SRGB, np.float32) / 255.0) ** _ENCODE_GAMMA
    greys_m = measured[list(NEUTRAL_ROW)]
    greys_t = target[list(NEUTRAL_ROW)]
    gains = (greys_t.mean(axis=0) /
             np.maximum(greys_m.mean(axis=0), 1e-6))
    balanced = measured * gains
    solved, *_ = np.linalg.lstsq(balanced, target, rcond=None)
    matrix = (solved.T * gains).astype(np.float64)   # folds WB in
    report = {
        "gains": [round(float(g), 4) for g in gains],
        "residual_before": round(float(
            np.abs(measured - target).mean()), 5),
        "residual_after": round(float(
            np.abs(measured @ matrix.T - target).mean()), 5),
    }
    return matrix, report


def fit_lattice(rendered_display: np.ndarray,
                target_display: np.ndarray,
                size: int = LOOK_SIZE,
                spread: float = LOOK_SPREAD,
                pull: float = LOOK_PULL) -> np.ndarray:
    """The residue as a coarse lattice, relaxed by construction.

    Each node takes the RBF-weighted average of the nearby patches'
    display-domain errors, pulled toward zero where no patch speaks --
    so the warp says nothing about colours nobody measured, and what
    it does say cannot step, only slope.
    """
    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    nodes = np.stack(np.meshgrid(axis, axis, axis, indexing="ij"),
                     axis=-1).reshape(-1, 3)
    errors = (target_display - rendered_display).astype(np.float32)
    sigma = spread / (size - 1)
    away = nodes[:, None, :] - rendered_display[None, :, :]
    weight = np.exp(-(away ** 2).sum(axis=2) / (2.0 * sigma * sigma))
    weighed = (weight @ errors) / (
        weight.sum(axis=1, keepdims=True) + pull)
    return weighed.reshape(size, size, size, 3)


def relax(lattice: np.ndarray, passes: int = 1) -> np.ndarray:
    """Box-smooth the deltas: the doctrine's last word on every look."""
    out = lattice.astype(np.float32)
    for _round in range(max(0, int(passes))):
        padded = np.pad(out, ((1, 1), (1, 1), (1, 1), (0, 0)),
                        mode="edge")
        total = np.zeros_like(out)
        for dr in (0, 1, 2):
            for dg in (0, 1, 2):
                for db in (0, 1, 2):
                    total += padded[dr:dr + out.shape[0],
                                    dg:dg + out.shape[1],
                                    db:db + out.shape[2]]
        out = total / 27.0
    return out


def warp_value(lattice: np.ndarray) -> dict[str, Any]:
    """The engine's word for a lattice: size + float16 base64."""
    flat = np.ascontiguousarray(lattice.astype(np.float16))
    return {"size": int(lattice.shape[0]),
            "lattice": base64.b64encode(flat.tobytes()).decode()}


def fit_look(image: np.ndarray, corners: list[float],
             relax_passes: int = 1) -> tuple[list[dict[str, Any]], dict]:
    """From a photographed chart to the operations of a look."""
    measured = patch_means(image, corners)
    matrix, report = fit_matrix(measured)
    linear = np.clip(measured, 1e-4, 1.0) ** _ENCODE_GAMMA
    rendered = np.clip(linear @ matrix.T, 1e-6, None) \
        ** (1.0 / _ENCODE_GAMMA)
    target = np.asarray(CC24_SRGB, np.float32) / 255.0
    lattice = relax(fit_lattice(
        np.clip(rendered, 0, 1).astype(np.float32), target), relax_passes)
    operations = [
        {"op": "color.channel_mixer", "unit": "matrix", "mode": "absolute",
         "value": [[round(float(v), 6) for v in row] for row in matrix],
         "source_instruction": "fitted from a ColorChecker",
         "enabled": True},
        {"op": "color.warp", "unit": "warp", "mode": "absolute",
         "value": warp_value(lattice),
         "source_instruction": "chart residue, relaxed",
         "enabled": True},
    ]
    report["lattice_peak"] = round(float(np.abs(lattice).max()), 4)
    return operations, report


# --- the authored look ----------------------------------------------------

def standard_look(strength: float = 100.0,
                  size: int = LOOK_SIZE) -> list[dict[str, Any]]:
    """The preferred-rendering deltas, written as a lattice.

    Sixty years of memory-colour research in three gentle moves: skin
    hues walk a few degrees toward red, sky blues a few degrees toward
    cyan, and everything with colour in it gains a breath of chroma
    that fades before saturation could clip. Neutrals cannot move --
    a grey node has no chroma to scale and no hue to turn.
    """
    k = min(max(float(strength), 0.0), 200.0) / 100.0
    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    r, g, b = np.meshgrid(axis, axis, axis, indexing="ij")
    mx = np.maximum.reduce([r, g, b])
    mn = np.minimum.reduce([r, g, b])
    delta = mx - mn
    sat = np.where(mx > 1e-6, delta / np.maximum(mx, 1e-6), 0.0)
    hue = np.zeros_like(mx)
    lively = delta > 1e-6
    is_r = (mx == r) & lively
    is_g = (mx == g) & lively & ~is_r
    is_b = lively & ~is_r & ~is_g
    hue[is_r] = (((g - b) / np.maximum(delta, 1e-6))[is_r]) % 6
    hue[is_g] = ((b - r) / np.maximum(delta, 1e-6))[is_g] + 2
    hue[is_b] = ((r - g) / np.maximum(delta, 1e-6))[is_b] + 4
    hue *= 60.0

    def band(centre: float, width: float) -> np.ndarray:
        away = np.abs(((hue - centre) + 180.0) % 360.0 - 180.0)
        edge = np.clip(1.0 - away / width, 0.0, 1.0)
        return edge * edge * (3.0 - 2.0 * edge)

    # Hue walks, in degrees; each gated by its band and by having
    # honest colour to walk with.
    turn = (-5.0 * band(35.0, 40.0)      # skin: toward red
            - 8.0 * band(225.0, 45.0)    # sky: toward cyan
            ) * k * np.clip(sat / 0.15, 0.0, 1.0)
    walked_hue = (hue + turn) % 360.0
    # A breath of chroma, fading out above sat 0.7 so nothing clips.
    gain = 1.0 + 0.05 * k * np.clip(1.0 - (sat - 0.7) / 0.3, 0.0, 1.0)
    walked_sat = np.clip(sat * gain, 0.0, 1.0)
    # Rebuild the nodes from walked hue and saturation, same value.
    chroma = walked_sat * mx
    sixth = (walked_hue / 60.0) % 6.0
    x = chroma * (1.0 - np.abs(sixth % 2.0 - 1.0))
    zeros = np.zeros_like(chroma)
    bandix = (np.floor(sixth).astype(np.int32) % 6)[..., None]
    highs = np.select(
        [bandix == 0, bandix == 1, bandix == 2, bandix == 3, bandix == 4],
        [np.stack([chroma, x, zeros], -1), np.stack([x, chroma, zeros], -1),
         np.stack([zeros, chroma, x], -1), np.stack([zeros, x, chroma], -1),
         np.stack([x, zeros, chroma], -1)],
        np.stack([chroma, zeros, x], -1))
    walked = highs + (mx - chroma)[..., None]
    lattice = (walked - np.stack([r, g, b], axis=-1)).astype(np.float32)
    lattice[~lively] = 0.0                     # neutrals stay put
    lattice = relax(lattice, 1)
    # Relaxation smears a whisper of colour onto the grey axis; a
    # smooth gate on saturation takes it back off. Neutrals never
    # move is a promise, not a tendency.
    gate = np.clip(sat / 0.15, 0.0, 1.0)
    lattice *= (gate * gate * (3.0 - 2.0 * gate))[..., None]
    return [{
        "op": "color.warp", "unit": "warp", "mode": "absolute",
        "value": warp_value(lattice),
        "source_instruction": "the standard look: skin warmer, "
                              "skies cleaner, a breath of chroma",
        "enabled": True,
    }]


# --- keeping a look -------------------------------------------------------

def save_look(name: str, operations: list[dict[str, Any]],
              intent: str = "", root: Path | None = None) -> dict[str, Any]:
    """A look is a preset: same store, same strip, same portability."""
    from opencull_gui import presets

    return presets.save(name, operations, intent=intent, root=root)


def dress_camera(model: str, operations: list[dict[str, Any]],
                 note: str = "") -> dict[str, Any]:
    """Assign a look as one camera's default: worn under every render."""
    from opencull_gui import cameralooks

    return cameralooks.assign(model, operations, note=note)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    fit = sub.add_parser("fit", help="fit a look from a chart photo")
    fit.add_argument("chart", help="as-shot render of the ColorChecker")
    fit.add_argument("--corners", required=True,
                     help="TL,TR,BR,BL of the patch area, eight "
                          "fractions 0..1, comma separated")
    fit.add_argument("--name", required=True)
    fit.add_argument("--relax", type=int, default=1)
    fit.add_argument("--camera", default="",
                     help="also assign as this camera model's default "
                          "look, worn under every render of its frames")
    std = sub.add_parser("standard", help="author the standard look")
    std.add_argument("--strength", type=float, default=100.0)
    std.add_argument("--name", default="Darkimiya Standard")
    std.add_argument("--camera", default="",
                     help="also assign as this camera model's default")
    listing = sub.add_parser("cameras", help="which cameras wear a look")
    args = parser.parse_args(argv)
    if args.command == "cameras":
        from opencull_gui import cameralooks

        print(json.dumps(cameralooks.cameras(), indent=2))
        return 0
    if args.command == "fit":
        from PIL import Image

        with Image.open(args.chart) as opened:
            image = np.asarray(
                opened.convert("RGB"), dtype=np.float32) / 255.0
        corners = [float(v) for v in args.corners.split(",")]
        operations, report = fit_look(image, corners, args.relax)
        kept = save_look(
            args.name, operations,
            intent="Fitted from a ColorChecker 24: white balance and a "
                   "matrix carry the reach, a relaxed lattice the rest.")
        told = {"kept": kept["name"], "report": report}
        if args.camera:
            dressed = dress_camera(
                args.camera, operations,
                note=f"fitted from {Path(args.chart).name}")
            told["camera"] = dressed["camera"]
        print(json.dumps(told, indent=2))
    else:
        operations = standard_look(args.strength)
        kept = save_look(
            args.name, operations,
            intent="The preferred rendering, gently: skin a little "
                   "warmer, skies a little cleaner, a breath of chroma, "
                   "neutrals untouched.")
        told = {"kept": kept["name"]}
        if args.camera:
            dressed = dress_camera(args.camera, operations,
                                   note="the standard look")
            told["camera"] = dressed["camera"]
        print(json.dumps(told, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
