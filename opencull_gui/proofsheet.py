"""The proof sheet: what was done to the photographs, shown to the client.

A delivery is not just files; it is the craft the client paid for, and the
evidence chain already holds everything needed to show it -- the frame as
shot, the frame as delivered, the treatment's intent in words, and the
certificate that checked the promise. This writes that as one
self-contained page per delivery: no external references, images embedded,
openable from a memory stick in a decade.

Zero model calls. Everything on the page is already on record.
"""

from __future__ import annotations

import base64
import html
import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from colour_profile import srgb_profile

# Bounded copies are embedded, not the delivered bytes: a proof sheet is
# for looking, and a page that is a gigabyte is a page nobody opens.
EDGE = 1200
QUALITY = 86

_PALETTE = {
    "ink": "#131211", "surface": "#1B1918", "edge": "#302C29",
    "paper": "#EDE7DE", "muted": "#98908A", "amber": "#FF9B47",
    "verdigris": "#5CB39E", "alarm": "#E8735A",
}


def _embedded(path: Path) -> str:
    """One photograph as a bounded data URI, honouring its orientation."""
    with Image.open(path) as image:
        image = ImageOps.exif_transpose(image) or image
        image = image.convert("RGB")
        image.thumbnail((EDGE, EDGE), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=QUALITY, icc_profile=srgb_profile())
    return "data:image/jpeg;base64," + base64.b64encode(
        buffer.getvalue()).decode("ascii")


def _style_of(variant: str) -> str:
    return str(variant).split("-", 1)[0]


def write_proof_sheet(
    workspace: Any, records: list[dict[str, Any]], destination: Path,
    shoot: str,
) -> Path:
    """Write the delivery's story as one self-contained page.

    ``records`` are export records -- source render path, source_photo,
    variant -- in delivery order. The sheet never overwrites: a taken name
    yields the next free one, like every other write in this application.
    """
    payload = workspace.payload()
    photos_root = Path(str(payload.get("source_folder") or "."))
    sections: list[str] = []
    for position, record in enumerate(records, start=1):
        photo = str(record.get("source_photo") or "")
        variant = str(record.get("variant") or "render")
        render_path = Path(str(record.get("path") or ""))
        original = photos_root / photo
        try:
            treated = _embedded(render_path)
        except Exception:                            # noqa: BLE001 - said below
            treated = ""
        try:
            as_shot = _embedded(original)
        except Exception:                            # noqa: BLE001 - said below
            as_shot = ""
        intent = ""
        try:
            intent = workspace.suggestion_for(photo, _style_of(variant))
        except Exception:                            # noqa: BLE001 - optional
            intent = ""
        certificate = None
        try:
            certificate = workspace.verification_for(str(render_path))
        except Exception:                            # noqa: BLE001 - optional
            certificate = None
        verdict = ""
        if certificate is not None:
            satisfied = bool(
                (certificate.get("verdict") or {}).get("satisfied"))
            verdict = (
                '<p class="verdict ok">Checked against its treatment: '
                "satisfied.</p>" if satisfied else
                '<p class="verdict alarm">Checked against its treatment: '
                "not satisfied.</p>")
        intent_html = (
            f'<p class="intent">{html.escape(intent.splitlines()[0])}</p>'
            if intent.strip() else "")
        sections.append(f"""
  <section>
    <h2>{position:02d} · {html.escape(photo)}
      <span class="variant">{html.escape(variant)}</span></h2>
    <div class="pair">
      <figure>
        {'<img src="' + treated + '" alt="as delivered">' if treated
         else '<p class="missing">The delivered file could not be read '
              'back.</p>'}
        <figcaption>As delivered</figcaption>
      </figure>
      <figure class="small">
        {'<img src="' + as_shot + '" alt="as shot">' if as_shot
         else '<p class="missing">The original could not be read '
              'back.</p>'}
        <figcaption>As shot</figcaption>
      </figure>
    </div>
    {intent_html}
    {verdict}
  </section>""")

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(shoot)} — proof sheet</title>
<style>
  body {{ background: {_PALETTE['ink']}; color: {_PALETTE['paper']};
         font-family: system-ui, sans-serif; margin: 0;
         padding: 2rem 1.4rem 4rem; }}
  main {{ max-width: 1080px; margin: 0 auto; display: flex;
          flex-direction: column; gap: 2.4rem; }}
  header p {{ color: {_PALETTE['muted']}; margin: 0.4rem 0 0; }}
  h1 {{ margin: 0; font-size: 1.8rem; letter-spacing: 0.02em; }}
  section {{ background: {_PALETTE['surface']};
             border: 1px solid {_PALETTE['edge']};
             border-radius: 12px; padding: 1.2rem 1.3rem; }}
  h2 {{ margin: 0 0 0.9rem; font-size: 1.05rem; }}
  .variant {{ color: {_PALETTE['amber']}; font-size: 0.75rem;
              margin-left: 0.7rem; letter-spacing: 0.08em;
              text-transform: uppercase; }}
  .pair {{ display: flex; gap: 1rem; align-items: flex-start;
           flex-wrap: wrap; }}
  figure {{ margin: 0; flex: 3 1 380px; }}
  figure.small {{ flex: 1 1 180px; }}
  img {{ width: 100%; height: auto; border-radius: 6px; display: block; }}
  figcaption {{ color: {_PALETTE['muted']}; font-size: 0.75rem;
                margin-top: 0.35rem; letter-spacing: 0.06em;
                text-transform: uppercase; }}
  .intent {{ color: {_PALETTE['paper']}; margin: 0.9rem 0 0;
             font-size: 0.92rem; }}
  .verdict {{ font-size: 0.8rem; margin: 0.5rem 0 0; }}
  .ok {{ color: {_PALETTE['verdigris']}; }}
  .alarm {{ color: {_PALETTE['alarm']}; }}
  .missing {{ color: {_PALETTE['muted']}; font-size: 0.85rem; }}
  footer {{ color: {_PALETTE['muted']}; font-size: 0.75rem; }}
</style></head><body><main>
  <header>
    <h1>{html.escape(shoot)}</h1>
    <p>{len(records)} photograph{'' if len(records) == 1 else 's'} ·
       delivered {stamp} · developed in Darkimiya</p>
  </header>
  {''.join(sections)}
  <footer>Each frame is shown as delivered beside the frame as shot, with
  the treatment it was developed under. This page is self-contained; the
  full-resolution files are the delivery itself.</footer>
</main></body></html>
"""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f"{shoot} — proof sheet.html"
    revision = 2
    while target.exists():
        target = destination / f"{shoot} — proof sheet-{revision}.html"
        revision += 1
    target.write_text(page, encoding="utf-8")
    return target
