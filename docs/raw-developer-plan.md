# OpenCull RAW Developer Plan

## Evidence and scope

The first recipe corpus is
`607_FUJI-results.professional-shortlist.edit-directions-r15.json.checkpoint.json`.
It contains 13 photographs and 39 treatments: standard, signature, and
creative for every photograph. Each treatment contains ten ordered sections:

1. base and lens
2. composition
3. global exposure
4. HDR, levels, and curves
5. white balance and global color
6. selective color editor
7. layers and masks
8. detail and noise
9. finishing and output
10. evaluation order

The matched RAW inspected for the initial implementation is a Fujifilm X-S10
RAF from a Sigma 16mm F1.4 DC DN lens. LibRaw reports a 6×6 X-Trans filter
pattern, a 6240×4155 active RAW inset, per-CFA black levels, as-shot and
reference white-balance coefficients, and camera color matrices.

RAF is a proprietary, camera-dependent container. OpenCull will not make a
home-grown RAF parser or decompressor its foundation. It will use a replaceable
decoder adapter and keep all OpenCull-owned image development stages independent
of the container decoder.

## Compiled recipe vocabulary

The 39 recipes currently use these operation families:

- Decode and calibration: black/white normalization, bad-pixel handling,
  lens profile, distortion, chromatic aberration, diffraction, and falloff.
- Geometry: orientation, crop/aspect ratio, rotation, horizon, perspective,
  and keystone.
- Scene-linear tone: exposure, highlight reconstruction, shadow recovery,
  white/black points, contrast, brightness, levels, RGB/luma curves, and
  local contrast.
- Color: camera WB, Kelvin or temperature delta, tint, camera-to-working-space
  transform, saturation, color balance, and hue/saturation/lightness ranges.
- Local work: subject/background, brush, linear/radial gradient, luma-range,
  color-range, heal, and clone masks with opacity and feathering.
- Detail: demosaic choice, denoise, moire suppression, sharpening, clarity,
  structure, dehaze, and grain.
- Output: soft proof, gamut mapping, resize, output sharpening, metadata, and
  sRGB/Display-P3/Adobe-RGB/TIFF/JPEG export.
- Verification: clipping, skin/color integrity, haloing, noise, gradients,
  subject visibility, geometry, mask seams, and scene-specific guardrails.

Observed suggested starting ranges include exposure -1.0 to +0.8 EV,
contrast -20 to +40, brightness -20 to +10, highlight controls roughly
-60 to +40, shadow controls -30 to +30, clarity -15 to +40, structure -10
to +40, dehaze +5 to +15, tint -5 to +10, and vignette -30 to -5.
These are evidence ranges, not yet a stable public slider scale.

The current natural-language recipes sometimes mix absolute values and deltas
(for example, `Kelvin 5200` versus `Kelvin +400`) and tool-specific scales.
Execution must therefore begin only after conversion into the typed recipe IR.

## Typed recipe IR

The executor consumes versioned JSON, never raw LLM prose:

```json
{
  "format": "opencull-development-recipe-v1",
  "source_sha256": "...",
  "decoder_profile": "fujifilm-x-s10",
  "working_space": "scene-linear-rec2020-d65",
  "operations": [
    {
      "id": "exposure",
      "op": "tone.exposure",
      "value": 0.3,
      "unit": "EV",
      "mode": "delta",
      "enabled": true
    }
  ],
  "guardrails": [],
  "output": {}
}
```

Every operation has a declared unit, valid range, absolute/delta mode,
coordinate space, mask reference, blend mode, and deterministic ordering.
Unknown prose is retained as a note but cannot execute. Recipe compilation
produces validation errors rather than guessing.

## Processing architecture

```text
RAF/JPEG + immutable hash
  -> metadata and camera/lens identification
  -> decoder adapter (LibRaw first)
  -> calibrated scene-linear RGB + metadata
  -> deterministic recipe IR executor
  -> local masks and geometry
  -> display transform and proof preview
  -> objective guardrail measurements
  -> Kimiya perceptual review on before/after previews
  -> accepted render + recipe + certificate + audit trace
```

### Decoder boundary

The initial adapter will call LibRaw through a narrow native boundary. A Python
prototype may use `rawpy`, but the production pipeline should own a stable
C/C++ or subprocess adapter so Python packaging does not define the image
engine. The adapter returns:

- unpacked mosaic and CFA description
- active area and orientation
- black/white levels
- as-shot WB and camera matrices
- exposure and lens metadata
- embedded preview
- decoder/version/camera-support evidence

RawSpeed can be evaluated as a second decoder adapter. For production-quality
development, darktable is now the preferred external processing-engine
candidate. OpenCull should invoke an installed `darktable-cli` executable
through a narrow subprocess adapter, generate a versioned XMP history/style
from the typed recipe, and import only the exported artifact and provenance.
This preserves a clean boundary: OpenCull owns intent, validation, job state,
and semantic verification; darktable owns RAW decoding and pixel processing.

The adapter must use an isolated OpenCull configuration and library directory,
pin and record the darktable version, avoid touching the user's darktable
catalog, never place sidecars beside source photographs unless explicitly
requested, and capture the command, generated XMP hash, output hash, standard
error, ICC configuration, and exit status. Unsupported recipe operations must
be reported or safely reduced rather than silently ignored. The existing
LibRaw/OpenCull renderer remains a deterministic fallback and a comparison
oracle while the adapter reaches feature parity.

darktable is GPL-3.0 software. Calling a separately installed, unmodified
executable is the initial integration boundary; bundling or modifying darktable
requires a separate distribution and license-compliance review. RawTherapee
remains a useful independent test oracle.

Research status: `darktable_engine.py` now finds the official macOS application
bundle, verifies the CLI version response, and renders through disposable
configuration/cache/library state without source-side sidecars.
`renderer_comparison.py` preserves the existing OpenCull result, a native
darktable control, and an explicitly labelled darktable-decode/OpenCull-recipe
hybrid. It creates a contact sheet and a JSON technical report. This establishes
the comparison framework before native darktable recipe translation begins;
it does not replace either engine or declare a winner.

X-Trans comparison renders expose Markesteijn 1-pass, Markesteijn 3-pass, and
Markesteijn 3-pass + VNG dual as explicit settings. For darktable 5.6 their
history method identifiers are 1025, 1026, and 3074 respectively. The adapter
does not trust the request alone: it extracts the completed JPEG's embedded
darktable XMP, reads the demosaic module parameter block, and rejects the render
unless the applied method matches. Each mode receives distinct output,
provenance, contact-sheet, and report filenames so experiments remain
comparable rather than overwriting one another.

### Development stages

1. Decode without modifying the source.
2. Normalize black/white levels and repair defective pixels.
3. Apply lens shading/CA/distortion calibration where trustworthy.
4. Apply WB in mosaic or scene-linear space.
5. Demosaic X-Trans with a quality and a preview mode.
6. Transform camera RGB into a wide-gamut scene-linear working space.
7. Reconstruct highlights and apply exposure/tone operations.
8. Execute global and selective color operations.
9. Build and apply local masks.
10. Apply capture sharpening, denoise, moire handling, and creative detail.
11. Apply display/output transform, resize, and output sharpening.
12. Export without overwriting the original.

JPEG enters after the camera-development stages. Its recipe compiler rejects or
reduces operations that assume recoverable RAW highlights, editable camera WB,
or scene-linear sensor data.

## Kimiya trust workflow

Kimiya should supervise decisions, not perform pixel arithmetic.

- Observe the immutable source and baseline preview.
- Check that the compiled recipe is typed, bounded, and compatible with
  RAW versus JPEG evidence.
- Act by rendering into a new versioned artifact.
- Measure deterministic guardrails: clipping deltas, luminance distribution,
  color/gamut excursions, noise, sharpness/halo indicators, face/skin-region
  changes where applicable, and mask boundaries.
- Observe before, after, and difference previews.
- Ask a diverse vision panel whether the requested artistic intent is present
  and scene guardrails remain intact.
- If rejected, permit a bounded tuning loop over named parameters. Never let an
  agent rewrite arbitrary pixels or silently expand allowed ranges.
- Commit only the recipe, output hash, measurements, model/provider evidence,
  and Kimiya certificate that passed.

The original remains immutable. Every result is reproducible from source hash,
decoder version, recipe IR, masks, color profiles, and engine version.

## Implementation phases

### Phase 0 — corpus compiler

- Parse the existing 39 Capture One-oriented recipes.
- Normalize synonyms and distinguish absolute values from deltas.
- Emit typed IR plus a report of ambiguous or unsupported instructions.
- Build unit tests from all 39 recipes.

Implementation status: the first compiler is available in
`recipe_compiler.py`. On the initial 13-photo/39-treatment corpus it reads
928 instructions and emits 510 bounded executable operations, 111 guardrail
checks, and 309 explicit unsupported diagnostics. No unsupported instruction
is executable. Current typed coverage includes lens/CA toggles, crop/rotation,
global tone, HDR, Levels points/midpoint, WB temperature/tint, HSL ranges,
clarity/structure/dehaze, sharpening amount, luminance/color denoise,
vignette, and output color space.

Run it with:

```bash
python recipe_compiler.py \
  "/path/to/edit-directions.checkpoint.json" \
  --output "/path/to/development-corpus.json" \
  --source-kind raw
```

Add `--strict` when a pipeline must refuse any recipe containing an
unsupported instruction.

### Phase 1 — faithful baseline

- Implement the LibRaw X-S10 adapter.
- Render a neutral 16-bit TIFF and preview JPEG.
- Compare dimensions, WB, clipping, and color against LibRaw/dcraw and one
  established RAW developer.
- Add golden files from a small, explicitly selected RAF test set.

Implementation status: `raw_developer.py` now provides the first safe LibRaw
adapter. It hashes and identifies the immutable source, refuses to write beside
the RAF, renders through explicit temporary paths, atomically publishes a
16-bit scene-linear TIFF and 2048-pixel sRGB proof JPEG, and records allowlisted
camera/decode/output provenance. The X-S10 acceptance RAF renders at 6246×4170.
LibRaw embeds a Rec.2020 gamma-1 (linear) ICC profile in this TIFF. The
provenance record names that profile explicitly; consumers must not trust
generic “sRGB” labels from tools that classify the TIFF by channel layout.

Run it with:

```bash
python raw_developer.py "/path/to/photo.RAF" \
  --output-dir "/safe/output/directory"
```

### Phase 2 — global deterministic edits

- Geometry, exposure, WB, HDR, levels/curves, saturation, HSL ranges, basic
  sharpening/denoise, vignette, and color-managed export.
- Apply the standard treatment for one X-S10 RAF end to end.

Implementation status: development_engine.py applies the typed global subset
to the verified linear baseline, converts Rec.2020 linear RGB to sRGB, applies
exposure/contrast/brightness/HDR/Levels/WB/tint/saturation/detail controls,
performs crop/rotation, and writes a new JPEG with provenance. It refuses
incomplete recipes by default. --allow-incomplete creates an explicitly
labelled preview while unsupported diagnostics remain unexecuted.

When an original camera JPEG is available, `--reference-jpeg` performs a
per-channel sRGB quantile calibration of the LibRaw baseline before recipe
operations. This keeps RAW development in the same visual domain used by the
LLM edit assessment; the reference hash and calibration LUT are recorded in
render provenance.

The first Standard preview for DSCF7646.RAF is in
build/developments/DSCF7646/. It is a development preview, not a final
photographic treatment.

### Phase 3 — masks and local edits

- Linear/radial/luma/color masks first.
- Subject/background segmentation behind an optional model adapter.
- Brush/heal/clone require stored mask or source geometry and remain
  non-destructive.

Implementation status: the compiler and engine now represent and execute
bounded linear, radial, luma, color-range, and vignette masks. Numeric local
effects are blended non-destructively at their declared opacity; concise
directions such as “darken foreground” receive a conservative, auditable
exposure default. Brush/heal/clone and subject segmentation remain explicit
diagnostics until their geometry or model adapter is available.

### Phase 4 — Kimiya verification

- Objective measurement schema and thresholds.
- Before/after/difference evidence boards.
- Per-photo vision judgment and bounded retuning.
- Audit certificate exposed in the GUI.

Implementation status: `semantic_verifier.py` provides the semantic triplet
judge core, and `semantic_verification.kim` exposes the same evidence contract
to Kimiya. They send the original JPEG, optional thumbnail, developed image,
and edit suggestion as one multimodal request; require strict JSON judgments;
validate bounded scores; and record evidence hashes in an auditable
certificate. Provider bundling recognizes the new Kimiya program. GUI/job
wiring, multi-model consensus, and the Kimiya certificate panel remain the
next integration step.

### Phase 5 — batch and GUI

- Queue 39 treatment renders with checkpoint/resume.
- Compare Original / Standard / Signature / Creative.
- Let the user accept a treatment, modify sliders, or export its recipe.
- Preserve independent RAW and JPEG capability declarations.

Personal-style workflow: `style_profile.kim` and `style_profile_kernel.py`
create a versioned semantic profile from a folder of finished photographs.
Profiles support `replace` and `update` modes, retain example hashes and
revision history, and are consumed by the fourth edit-direction option:
“Your personal style — professionally refined.” The GUI exposes profile
extraction and lets the user attach an existing profile when generating edit
directions.

The GUI development workspace now provides variant tabs for Original,
Calibrated, Standard, Signature, Creative, and Personal treatments. It only
displays renders explicitly linked in the project manifest and shows a clear
missing-artifact state otherwise; linked images are served through an
allowlisted project endpoint.

Verification adjustment drafts are consumable by `development_engine.py` via
`--adjustments`. Each named control maps to bounded recipe operations and
increments a recipe revision; the original recipe and render remain intact.

## First acceptance target

For `DSCF7646.RAF`, render the standard “Documentary Clarity” treatment into a
16-bit TIFF and an sRGB JPEG while:

- preserving the RAF byte-for-byte
- reproducing crop, exposure, WB, HDR, selective green, two local masks,
  sharpening, and noise reduction through typed operations
- avoiding channel clipping beyond the declared guardrail
- retaining believable skin and tent/interior detail
- producing a complete provenance record and Kimiya certificate

## Technical references

- [Fujifilm Camera Control SDK announcement](https://www.fujifilm-x.com/en-sg/news/fujifilm-releases-x-gfx-series-camera-control-sdk/)
  explicitly states that RAF conversion information is not supplied.
- [ExifTool Fujifilm RAF tags](https://exiftool.org/TagNames/FujiFilm.html)
  documents observed RAF header, crop, compression, WB, black-level, lens
  correction, and X-Trans metadata.
- [LibRaw documentation](https://www.libraw.org/docs) describes RAF support,
  embedding intent, and LGPL 2.1/CDDL dual licensing.
- [LibRaw C API](https://www.libraw.org/docs/API-C.html) exposes unpacked data,
  camera multipliers/matrices, sizes, lens metadata, and processing calls.
