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

## Current limitations

Version 0.1 gives the declared generator the observed pixels, but the final
cross-family panel currently warrants the textual report and scanner evidence,
not the full image group. Extending `shows` to compare an image collection is
the next certification milestone.

The `composition_proxy` only measures whether edge energy is concentrated near
rule-of-thirds intersections; it is not an artistic-quality score. Visual
agents should treat it as one weak signal rather than ground truth.
