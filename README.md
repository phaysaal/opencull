# OpenCull

OpenCull is an open-source, local-first photo culling experiment built as a
[Kimiya](../kimiya-lang/) program. It groups near-duplicate RAW/JPEG photos,
measures explainable technical signals, and asks user-declared LLM agents to
recommend the best one to three photographs from each group.

OpenCull is deliberately conservative:

- source photographs are opened read-only;
- image pixels enter agents only through Kimiya's audited image observation;
- remote model egress is declared by Kimiya before a run;
- model responses cannot introduce unknown filenames;
- output is a recommendation report, never automatic deletion;
- every completed run carries a Kimiya certificate and Python-extension SHA.

## Architecture

`scan.py` is an audited Python kernel extension loaded directly by Kimiya. Its
`scan_directory` function reads embedded RAW previews (or a half-size decode
when necessary), calculates technical measurements and a clearly labelled
rule-of-thirds proxy, groups bursts using capture time and perceptual dHash,
and returns the manifest to the Kimiya program as a value.

`opencull.kim` orchestrates scanning and decisions in one invocation. It calls
the scanner kernel, validates its manifest, explicitly observes candidates in
multi-photo clusters, asks agent A for a multimodal recommendation,
mechanically validates filenames and selection counts, asks independent agent
families B and C to warrant the complete report, and only then writes
`opencull-results.json`.

`opencull_kernel.py` is the deterministic bridge. Kimiya announces it as a
Python extension and records its SHA in the certificate.

## Set up

Python 3.11 or newer is required.

```bash
cd /Users/faisal/code/opencull
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

On macOS, Fujifilm RAF previews use the system `sips` decoder. To use LibRaw
through Python instead (or on a system without `sips`):

```bash
python -m pip install -r requirements-raw.txt
```

Kimiya itself currently runs from the neighboring source checkout:

```bash
export PYTHONPATH=/Users/faisal/code/kimiya-lang
```

## Choose agents

Edit `agents.kim`. The checked-in default sends each observed image to
OpenRouter's `google/gemini-2.5-flash` with per-request Zero Data Retention
enforcement. Its two report reviewers also use fast, cross-family OpenRouter
models with ZDR enforced. Only the first agent receives image pixels. The API credential is
loaded at runtime from `../apis/opencull.api`; its contents never enter Kimiya
source, traces, or certificates. Credential files can contain a raw token,
`OPENROUTER_API_KEY=...`, or a JSON object with an `OPENROUTER_API_KEY`,
`api_key`, `key`, or `token` field.

The output report has two primary collections: `clusters` lists every detected
cluster and all its photos; `keep` lists zero to `keep_per_group` selected
photos, rationale, confidence, and any quality warning for each cluster.
`warnings` also collects clusters where the vision model judged that no
candidate was good enough to keep. `keep_per_group` is a maximum, not a quota.

Large folders are handled as a sequential divide-and-conquer job. Each detected
cluster is an independent work unit; OpenCull checkpoints its decision before
starting the next cluster and merges the validated decisions into the final
report. Per-frame and per-cluster model readings use Kimiya's exact-input memo
cache, keyed by the prompt, selected model, and observed image preview SHA.
After an interruption, rerunning the same command resumes from the first
unfinished cluster without paying for already memoized readings.

Kimiya permits at most eight images in one multimodal generation. OpenCull
therefore curates clusters larger than eight as a hierarchical tournament:
stable batches of at most eight advance up to four distinct finalists, and
those finalists are compared in further rounds until one final comparison can
apply the original cluster's `keep_per_group` ceiling. The original cluster
remains one cluster in the report and is checkpointed only after the whole
tournament completes.

Multi-photo culling uses two visual layers. Gemini first describes every frame
without selecting: pose/body, eyes/gaze, mouth/expression, readiness/timing,
interaction, occlusion/surroundings, hard-to-repair defects, RAW-recoverable
issues, meaningful variation, family value, and uncertainty. GPT-4.1 Mini then
reinspects the photographs and acts as the final curator. Human timing and
hard-to-repair defects outweigh moderate exposure or white-balance errors.
Natural open-mouth smiles and laughs are positive; accidental mid-speech mouth
positions, blinks, and visibly unready poses are negative. Distinct good family
moments may justify multiple keepers. See
`docs/photographic-decision-policy.md`.

The default `profile=family` favors documentary and relationship value. It can
be overridden at runtime, for example with `profile=professional`.

Singleton clusters are retained deterministically. Since there is no comparison
to make, their pixels are not sent to a vision model.

Adaptive clustering is enabled by default. A separate Qwen vision agent reviews
a bounded number of ambiguous existing clusters and near-time boundaries. It
may propose one conservative `stricter` or `relaxed` step for hash distance,
time window, or both. OpenCull rescans once and applies the proposal only when
it changes cluster count in the requested direction, preserves every candidate
identity, and creates no cluster larger than eight. Pose, gaze, blinking, body
angle, and meaningfully different smiles are explicitly treated as alternatives
that normally belong in the same culling cluster. The report records all valid
assessments, the proposed thresholds, and whether the rescan was applied.

## Run OpenCull

Start with a copied test folder, not your only copy of a shoot:

```bash
python -m kimiya run opencull.kim \
  photos=/path/to/test-shoot \
  output=opencull-results.json \
  keep_per_group=2
```

For nested folders and custom clustering:

```bash
python -m kimiya run opencull.kim \
  photos=/path/to/test-shoot \
  output=opencull-results.json \
  keep_per_group=2 \
  recursive=true \
  time_window=8 \
  hash_distance=14 \
  comparison_window=16
```

Disable adaptive review or limit its model-call budget:

```bash
python -m kimiya run opencull.kim \
  photos=/path/to/test-shoot \
  output=opencull-results.json \
  adaptive=false

python -m kimiya run opencull.kim \
  photos=/path/to/test-shoot \
  output=opencull-results.json \
  tuning_budget=4
```

Resumption is enabled by default. Its checkpoint is written beside the output
as `OUTPUT.checkpoint.json`. Choose a different path or intentionally start
without prior cluster decisions using:

```bash
python -m kimiya run opencull.kim \
  photos=/path/to/shoot \
  output=opencull-results.json \
  checkpoint=/path/to/opencull-progress.json

python -m kimiya run opencull.kim \
  photos=/path/to/shoot \
  output=opencull-results.json \
  resume=false
```

A checkpoint is reused only when its manifest hash, ordered cluster identities,
maximum keeper count, and profile all match the current run. A changed photo,
changed grouping, or changed policy starts a fresh decision sequence safely.

Grouping is intentionally inspectable rather than magical. Two photographs
are joined when they are close in capture time, have the same orientation and
aspect ratio, and their 64-bit difference hashes are sufficiently close.

Check it without calling models:

```bash
python -m kimiya check opencull.kim
```

The standalone scanner command remains available when you want to inspect or
archive an intermediate manifest:

```bash
python scan.py /path/to/test-shoot -o manifest.json
```

Kimiya prints an egress warning before contacting remote agents. On success it
writes a certificate under `.kimiya/` and a report under the chosen output
path. OpenCull never moves or deletes photographs.

## Move selected photographs

Moving files is deliberately separate from culling. First preview the exact
moves using the result report and the original photo folder:

```bash
python move_selected.py opencull-results.json /path/to/test-shoot
```

The default destination is `/path/to/test-shoot/opensull`. After reviewing the
dry-run output, perform the moves explicitly:

```bash
python move_selected.py opencull-results.json /path/to/test-shoot --apply
```

Use `--subfolder keepers` to choose another subfolder. For an OpenCull run made
with `recursive=true`, also pass `--recursive`. The script refuses missing or
ambiguous filenames and existing destination files; it never overwrites a
photograph.

## Review results in the GUI

The local review interface preserves the certified result JSON and source
photographs as immutable evidence while storing human decisions separately:

```bash
python gui.py \
  600_Fuji-results.json \
  "/Volumes/NVMeF1/RawPhotos/Luvre/600_FUJI"
```

The server binds only to `127.0.0.1` and opens the browser automatically. It
shows the library summary, cluster filters, lazy thumbnails, AI keepers,
warnings, fallbacks, tournament clusters, side-by-side comparison, curator
rationale, every stored frame assessment, adaptive-clustering evidence, and
the raw decision JSON.

Human review adds keeper/reject controls, AI-versus-human difference states,
reviewer notes, reviewed/unreviewed progress, keyboard culling, last-position
resume, restore-AI controls, and a provenance-labelled JSON export. Decisions
are saved atomically beside the report as `REPORT.review.json`; the previous
version is copied to `REPORT.review.json.bak`. The sidecar is cryptographically
bound to the source report SHA and becomes read-only with a visible warning if
the report changes. Concurrent browser writes use optimistic revisions rather
than silently overwriting one another.

Viewing and reviewing never modifies original photographs or the certified
report. Generated JPEG previews are cached under `.opencull-cache/thumbnails`;
JPEG and RAW files use the same audited preview decoder as the scanner. Review
writes require a per-launch CSRF token, the server rejects non-local Host
headers, and exports label each cluster with its decision provenance.

Use `--no-browser` for a manual launch, `--port 9000` to choose another local
port, or `--review /path/to/decisions.review.json` to choose a sidecar.

Keyboard shortcuts outside text fields:

- `1` through `9`: toggle that numbered photograph;
- `A`: accept the AI recommendation;
- `N`: keep none;
- `U`: return the cluster to unreviewed;
- left/right arrows: navigate filtered clusters.

### Large-library preview performance

Preview decoding runs through a bounded background priority queue rather than
inside HTTP request threads. The visible cluster is scheduled first, adjacent
clusters are prefetched at lower priority, and stale prefetch work is cancelled
when navigation changes. The default is two workers to bound memory and RAW
decoder pressure; choose between one and eight with:

```bash
python gui.py REPORT.json /path/to/photos --preview-workers 3
```

The GUI reports queued, decoding, ready, and failed previews plus disk-cache
size. Decoder or disconnected-drive failures remain retryable. Cached JPEGs
are integrity-checked before reuse, stale source files naturally receive new
cache identities, and generated previews can be cleared from Report details
without touching originals.

The certified result does not contain scanner measurements. Supply the
standalone scanner manifest when technical evidence should appear on photo
cards:

```bash
python scan.py /path/to/photos -o shoot-manifest.json

python gui.py REPORT.json /path/to/photos \
  --manifest shoot-manifest.json
```

The manifest is accepted only when its complete photo identity set matches the
report. When present, the GUI exposes capture time, dimensions, technical
score, sharpness, exposure, contrast, clipping, composition proxy, and source
hash prefix without changing the report.

### Export and organize selected photographs

The **Export & organize** dialog turns either the human-reviewed decisions or
the original AI recommendations into JSON, text, or CSV. Its selection
policies are explicit: human decisions only, effective decisions (human where
reviewed and AI otherwise), require every cluster to be reviewed, AI only, or
only clusters where the human changed the AI decision.

Copy and move are separate, deliberate actions. Before enabling execution, the
GUI produces a deterministic preflight plan listing every source,
destination, byte count, missing file, collision, and free-space check. It can
put keepers in one `opensull` directory, place them under
`selected-by-cluster/cluster-N`, or copy every cluster into an inspection
layout. Optional companion discovery includes a same-stem RAW or JPEG file.
No destination is overwritten.

Execution requires typing the exact confirmation shown by the GUI (`COPY
<plan-id>` or `MOVE <plan-id>`). Each file is copied first, SHA-256 verified,
and recorded in an atomic operation journal. A move removes its source only
after that verification. Operations can be paused, resumed from their journal
after restarting the GUI, and monitored in the browser. Verified move
operations can be rolled back in reverse order; rollback refuses to overwrite
an existing source or restore a destination whose content changed. Copy
operations deliberately have no automatic destructive rollback.

Contact-sheet export runs in the background and creates labelled JPEG pages
from the same policy selection. The GUI reports page progress and supports
cancellation. Operation journals and generated contact sheets are normal
files at destinations chosen in the dialog; keep them when audit history is
important.

The original `move_selected.py` command remains available as a small,
independent dry-run-first tool. The GUI workflow is preferable when provenance,
preflight inspection, resumability, verification, or rollback is required.

## Current limitations

Version 0.1 gives the declared generator the observed pixels, but the final
cross-family panel currently warrants the textual report and scanner evidence,
not the full image group. Extending `shows` to compare an image collection is
the next certification milestone.

The `composition_proxy` only measures whether edge energy is concentrated near
rule-of-thirds intersections; it is not an artistic-quality score. Visual
agents should treat it as one weak signal rather than ground truth.
