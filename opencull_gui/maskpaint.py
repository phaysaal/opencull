"""Show where a mask actually lands, before anything is paid to find out.

The engine's own weights, tinted over the photograph: full weight is a
solid wash, half weight is a half wash, nothing is nothing. Computed at
proof scale from exactly the value dict the renderer will read -- the
same function, so the picture cannot lie about the render. The gap
between "a bottom gradient over the rooftop" and a ramp at half
strength over the sky lived unmeasured for nine treatment runs; this
is the same honesty, for the hand.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from development_engine import mask_weights

# One tint. Amber-ish, readable on both a violet sky and a black roof.
TINT = (255, 130, 40)


def overlay_png(image_path: str | Path, shape: str,
                value: dict[str, Any], edge: int = 1100,
                strength: float = 0.6) -> bytes:
    """A transparent PNG the size of the proof: the mask as a wash."""
    with Image.open(str(image_path)) as opened:
        picture = opened.convert("RGB")
        picture.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    rgb = np.asarray(picture, dtype=np.float32) / 255.0
    weights = np.clip(
        mask_weights(rgb, str(shape), dict(value)), 0.0, 1.0)
    opacity = float(value.get("opacity", 1.0) or 1.0)
    height, width = weights.shape
    sheet = np.zeros((height, width, 4), dtype=np.uint8)
    sheet[..., 0], sheet[..., 1], sheet[..., 2] = TINT
    sheet[..., 3] = (weights * opacity * strength * 255).astype(np.uint8)
    out = io.BytesIO()
    Image.fromarray(sheet, "RGBA").save(out, "PNG")
    return out.getvalue()
