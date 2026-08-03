# Darkimiya

Darkimiya is an open-source, local-first computational darkroom built around
[Kimiya](../kimiya-lang/). Its Kimiya-powered culling engine, **OpenCull**,
groups near-duplicate RAW/JPEG photographs, measures explainable technical
signals, and asks user-declared LLM agents to recommend the strongest frames.
The wider application carries a folder project through human review, RAW
development, semantic verification, and delivery.

New projects store portable state under `PHOTO_FOLDER/Darkimiya/`. Credentials,
private face embeddings, caches, and security bookmarks remain device-private.
Established OpenCull reports and legacy project manifests remain readable so
historical evidence is not rewritten merely because the application acquired
a broader name.

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

## Position among existing RAW applications

As of August 2026, no documented mainstream application combines OpenCull's
entire development loop. Several products overlap with important parts of it:

| Application | Overlap | Important difference |
| --- | --- | --- |
| [Aftershoot Develop](https://aftershoot.com/edit/) | RAW development, masks, personalized AI profiles, batch editing, and export | The closest commercial workflow, but its learned profiles are based on a large prior editing corpus rather than OpenCull's explicit semantic directions and before/after verification. |
| [Imagen AI](https://support.imagen-ai.com/hc/en-us/articles/6069711141009-What-is-a-Personal-AI-Profile?v=0i) | Learns a photographer's established style and applies it across galleries | Primarily an automated Lightroom workflow; its documented personal profile requires at least 2,000 edited examples with the corresponding RAW/XMP evidence. |
| [Capture One](https://www.captureone.com/en/explore-features/assisted-editing) | Mature RAW conversion, layers, masks, Match Look, and adaptive exposure/white balance | It transfers or adapts a reference look, but does not implement OpenCull's full LLM-directed, guarded, semantically verified loop. |
| [Adobe Lightroom](https://helpx.adobe.com/lightroom/web/edit-photos/apply-effects/use-adaptive-profiles.html) | Mature RAW conversion, image-adaptive profiles, AI masks, denoise, and local editing | Its adaptive tools do not propose multiple reasoned artistic treatments and verify a rendered result against the selected intent. |
| [darktable](https://www.darktable.org/about/) | Open-source, non-destructive RAW development, image management, styles, XMP history, and command-line export | It provides the mature pixel engine OpenCull currently lacks, but not OpenCull's personal semantic profile, Kimiya policy, or perceptual acceptance loop. |

OpenCull's distinctive target is the composition of these capabilities:

1. photographic and semantic assessment of the scene;
2. standard, signature, creative, and learned-personal treatment proposals;
3. compilation of prose intent into a typed, deterministic recipe;
4. calibration against the camera JPEG when one is available;
5. auditable Kimiya policy checks and abstention;
6. comparison of the original evidence, selected direction, and rendered image;
7. rejection, revision, or regeneration when the result misses the intent.

The established applications have much more mature demosaicing, camera and
lens profiles, denoise, masking, color management, and acceleration. OpenCull's
potential advantage is therefore not a new low-level pixel pipeline. It is the
explainable, personalized, self-checking control system above that pipeline.
The preferred production architecture is to retain OpenCull's recipe,
provenance, Kimiya guardrails, and semantic verification while allowing a
mature external RAW engine—initially darktable—to execute supported image
operations through a narrow, versioned adapter.

## Set up

Python 3.11 or newer is required.

```bash
cd /path/to/opencull
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

RAW previews are decoded by `rawpy`, which is a base requirement and works
on Linux, macOS and Windows. It reads the preview already embedded in the
RAW file. On macOS the system `sips` tool remains as a fallback if `rawpy`
is unavailable, but it decodes the whole frame in a separate process per
photograph and is much slower; the interface reports when it is in use.

Kimiya runs from a checkout beside this repository. Either clone it there
or symlink it:

```bash
ln -s /path/to/kimiya-lang ../kimiya-lang
export PYTHONPATH=$PWD/../kimiya-lang
```

The launcher is a native Qt window. The review workspace is still the local
web interface, which the launcher opens in your browser; that part is being
moved to Qt next.

### Linux

`scripts/install_linux.sh` installs into a private virtual environment under
`~/.local`, puts `darkimiya` and `darkimiya-review` on `PATH`, and registers a
desktop entry so the application appears in the menu. It writes nothing
outside your own directories, so it needs no privileges and uninstalling is a
deletion of `~/.local/share/darkimiya`.

```bash
./scripts/install_linux.sh
```

Set `PREFIX` to install elsewhere. `darktable` is reported if absent; it is
needed only for guided RAW development. File and folder choosers come from Qt
itself, so no `zenity` or `kdialog` is required.

Application state follows the XDG base directories:

| Purpose | Location |
| --- | --- |
| Queue, providers, results | `$XDG_DATA_HOME/Darkimiya` (`~/.local/share/Darkimiya`) |
| Generated previews | `$XDG_CACHE_HOME/Darkimiya` (`~/.cache/Darkimiya`) |
| Logs | `$XDG_STATE_HOME/Darkimiya` (`~/.local/state/Darkimiya`) |
| Trashed photographs | the XDG trash for the volume holding them |
| Provider API keys | the system keyring, or an owner-only file |

Photographs removed by a filter or cleanup go to the XDG trash, so the
desktop's own Restore works on them. A photograph on a separate volume goes to
that volume's `.Trash-$UID`, because a move must stay on one device.

Provider API keys saved from the interface go to the system keyring through
`secret-tool` when libsecret is installed and a Secret Service is running.
Otherwise they go to an owner-only file (`0600`) under the application support
directory. The interface reports which of the two is in use, because a key
protected only by file permissions is readable by anything running as you.
Install `libsecret-tools` for keyring storage:

```bash
sudo apt install libsecret-tools     # Debian and Ubuntu
sudo dnf install libsecret           # Fedora
```

### Windows

Windows is not yet supported. The test suite runs against it in CI and is
reported but not enforced, so the remaining gaps are visible. Application
state resolves to `%LOCALAPPDATA%\Darkimiya` and the choosers fall back to Tk,
but there is no installer and no Recycle Bin integration: files a filter
removes go to a `Darkimiya Trash` folder in the user profile rather than the
bin.

## Choose agents

Edit `agents.kim`. The checked-in default sends each observed image to
OpenRouter's `openai/gpt-5.6-luna-pro` for vision roles and
`openai/gpt-4.1-mini` for text judging, with per-request Zero Data Retention
enforcement. Only the first agent receives image pixels. The API credential is
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

The professional interface roadmap and the implementation record for completed
UX phases are maintained in
[`docs/professional-ux-roadmap.md`](docs/professional-ux-roadmap.md).

The local review interface preserves the certified result JSON and source
photographs as immutable evidence while storing human decisions separately:

```bash
python gui.py \
  opencull-results.json \
  /path/to/shoot
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

### Phase 5: high-volume human review

Each photograph can carry three independent human judgments: a keep, maybe,
reject, or unrated flag; a zero-to-five-star rating; and an optional Lightroom
color label. A keep flag also selects the photograph, while a reject flag
removes it from the keepers. These judgments live in the report-bound review
sidecar and are included in provenance exports. Existing Phase 2–4 sidecars
load with empty Phase 5 fields; no manual migration is needed.

Every cluster mutation appends a timestamped before/after entry to a bounded
history. **Undo review** restores the last saved cluster state through the same
revision-conflict protection as ordinary saves. The summary shows event count
and a time-based remaining-work estimate once enough review activity exists.
Filters find clusters containing maybe or rejected frames and curator decisions
below 65% confidence.

The detail viewer provides a cluster filmstrip, synchronized zoom and pan, and
whole-frame, upper-body, or central-detail review crops. The upper-body crop is
only a consistent visual aid—it does not claim to detect a face. Mark up to two
frames with **Compare**, press `C`, and drag or zoom either frame to inspect the
same region. `+` and `-` adjust zoom, `Z` undoes the last saved judgment, and
normal culling shortcuts are suppressed while typing in notes or controls.

Preview scheduling follows the active filtered order and queues the next two
likely clusters after the visible one, while retaining the bounded workers and
stale-prefetch cancellation from Phase 3.

The Export dialog can download a Lightroom-oriented XMP ZIP without changing
photographs. Standard `xmp:Rating` and `xmp:Label` fields carry stars and color;
OpenCull's pick state and source identity remain in an explicit OpenCull
namespace because Lightroom applications vary in how they map pick/reject
flags. Extract the sidecars beside copies of the corresponding photographs and
verify the mapping in the intended catalog before applying it to a production
library.

### Phase 6: culling queue and concurrent review

Open **Culling queue** from any running review GUI to choose a macOS folder or
enter its path, set the maximum keeper count, profile, recursion, and optional
output path, and add it to a persistent queue. The default output is named from
the source folder and placed in the OpenCull project directory. Existing
outputs are never overwritten, duplicate active folders are refused, and all
paths are resolved and validated before queuing.

The queue lives in `.opencull-jobs.json` by default and runs exactly one Kimiya
process at a time. Later folders advance automatically when the current run
completes, fails, pauses, or is cancelled. The browser displays live state,
bounded subprocess logs, output and checkpoint paths, and the count of
independently checkpointed clusters. OpenRouter credentials remain in
`../apis/opencull.api`; neither the API key nor file contents are sent to the
browser or written into queue state.

**Pause safely** terminates the supervised Kimiya process and retains its
checkpoint. **Resume checkpoint** starts the same declared program with
`resume=true`; Kimiya validates the manifest and ordered cluster identities
before accepting prior decisions. Cancelled and failed jobs can likewise be
resumed or retried explicitly. If the GUI closes while its child is still
running, the next GUI instance records that process as detached and will not
launch a competing queue job. It monitors the recorded PID until the process
ends, then marks the result complete or makes the checkpoint resumable. For
safety, a restarted GUI does not signal a detached PID because operating
systems can reuse process numbers.

Culling executes in a process separate from the threaded local review server.
Preview generation, review saves, comparisons, and exports therefore remain
available in the current tab or another browser tab while OpenRouter culling
continues. A completed job offers **Open review in new tab**, which starts an
additional review-only GUI on an automatically assigned local port. That
review-only process disables its queue worker so there is still exactly one
queue owner. It also offers a copyable terminal command as a transparent
fallback.

Choose a different queue state location when desired:

```bash
python gui.py REPORT.json /path/to/photos \
  --jobs /path/to/private/opencull-jobs.json
```

The queue controls process execution, not remote inference cancellation.
Pausing during an in-flight OpenRouter request may still allow that already
submitted provider request to finish or be billed. The last completely
validated cluster checkpoint remains the recovery boundary.

### Phase 7: secure model-provider profiles

Open **Culling queue → Provider settings** to create reusable profiles for:

- OpenRouter, with optional mandatory zero-data-retention routing;
- local Ollama using its native `/api/generate` interface;
- Vast.ai, vLLM, LM Studio, or another OpenAI-compatible `/v1` endpoint.

Each profile assigns separate models to agents A through D. A performs
per-frame vision, B performs visual curation, C judges textual evidence, and D
performs visual clustering review and final panel judgment. The endpoint test
checks reachability and exact model IDs. For OpenRouter it also reads advertised
input modalities and reports whether image support is confirmed. Generic
OpenAI-compatible servers and Ollama do not expose one consistent capability
schema, so their vision status remains explicitly unverified until a live
multimodal request succeeds.

Each culling job also has an **Advanced judgment policy**. It may select any
non-empty subset of the configured A–D roles, choose 1–15 total votes, and set
the required approval count. The default remains C + D with four approvals out
of five votes. Votes are distributed across the selected roles; the vote count
must therefore be at least the number of panel members. Darkimiya validates the
policy, writes it into the immutable generated `opencull.kim` and provider
manifest, and runs `kimiya check` before admitting the job to the queue.
Changing a reusable provider or the form after queueing cannot alter that
job's recorded panel or threshold.

Provider JSON contains names, endpoint URLs, model IDs, ZDR choice, and an
optional cost note—but no token. Tokens are write-only in the browser and are
stored as generic passwords in macOS Keychain under a profile-specific service
name. Existing tokens are never placed back into an HTML field or returned by
an API. The Keychain command receives new password bytes through a private
stdin channel rather than putting the token in its process argument list.

When a provider profile is selected for a queue job, OpenCull generates an
immutable job bundle under `.opencull-generated/providers/JOB-ID/`:

- `agents.kim` contains backend, model, URL, vision, ZDR, and `key_env`
  declarations, never a credential value;
- `opencull.kim` is the original program with absolute, auditable module paths;
- `manifest.json` records the non-secret profile snapshot and SHA-256 hashes of
  both generated sources.

The generated program must pass `kimiya check` before the folder enters the
queue. Queue state records the profile identity, privacy classification,
agent-configuration hash, program hash, and exact generated path. Editing a
profile later cannot silently change an already queued job. At process launch,
the supervisor reads the profile token from Keychain into the declared child
environment variable; it does not modify the GUI environment, log the value,
or persist it. Removing a Keychain credential can therefore make a queued
remote job fail safely at launch without exposing the missing value.

Privacy labels mean:

- `local`: an Ollama endpoint on `127.0.0.1` or `localhost`;
- `remote-zdr`: OpenRouter with `zdr=true` in every generated agent;
- `remote-provider-policy`: a remote endpoint whose storage and retention
  depend on that operator;
- `declared-in-agents.kim`: the legacy configuration selected by default.

The legacy `agents.kim` route remains available so existing terminal and queue
workflows keep working. Selecting a Phase 7 profile is explicit and recorded
per job. Endpoint tests contact the configured provider but do not perform a
paid generation. Pricing is not inferred: enter a dated cost note from the
provider, because remote prices and billing units change independently of
OpenCull.

Use a different non-secret profile file when desired:

```bash
python gui.py REPORT.json /path/to/photos \
  --providers /path/to/private/opencull-providers.json
```

### Phase 8: local private person grouping

Open **People** to build an optional face index for the current report. Face
detection, alignment, embeddings, anonymous clustering, crops, names, and
coverage analysis all run on the Mac. The face module contains no network or
subprocess API and is not imported by the Kimiya program, prompt builder,
provider layer, or culling queue. Remote agents therefore receive neither
private names nor persistent face embeddings.

Phase 8 uses OpenCV 4.11 with two OpenCV Zoo ONNX models:

- YuNet face detection, MIT licensed, SHA-256
  `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`;
- SFace recognition embeddings, Apache-2.0 licensed, SHA-256
  `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`.

Install the headless runtime and then use the audited installer:

```bash
python -m pip install opencv-python-headless==4.11.0.86
python install_face_models.py
```

The installer downloads the declared files from OpenCV's repository into
`.opencull-models`, verifies the complete hashes before replacing a model, and
removes partial or mismatched downloads. Face indexing refuses missing or
hash-mismatched models.

The default private database is `REPORT.faces.sqlite3`. It is bound to the
exact report SHA, resolved photo root, and model hashes and is created with
owner-only `0600` permissions. It contains source identities, face boxes,
landmarks, normalized SFace vectors, anonymous person assignments, and
optional private names. The HTTP API deliberately omits embeddings; it exposes
only local JPEG crops, boxes, confidence, anonymous IDs, coverage counts, and
names entered for this local GUI.

Indexing is a single bounded background worker. It records source size and
modification time after each photo, skips unchanged completed photos on
restart, and can be paused between photographs. RAW files use the same local
preview decoder as OpenCull. Detection is performed on a bounded-resolution
copy while coordinates are mapped back to the source preview.

Automatic grouping is intentionally conservative. It requires cosine
similarity of at least `0.55`, a minimum pairwise guard, and never automatically
puts two faces from one photograph into one identity. This favors extra
anonymous groups over a harmful false merge. Review every crop before naming:

- **Merge checked groups** combines groups only after an explicit human action;
- **Split checked faces** creates another anonymous group;
- a private name and **Identity confirmed** flag are optional;
- **Forget person and embeddings** deletes that group's detections, vectors,
  and name, then compacts the database;
- **Delete all private face data** clears every detection, vector, group, name,
  and indexed-photo record after an exact typed confirmation.

The SQLite database is permission-restricted but not encrypted at rest.
Full-disk FileVault protects it when the Mac is powered off. For stronger
separation, place `--faces` on an encrypted volume. Deleting rows and running
SQLite `VACUUM` is explicit removal at the application layer, but flash-storage
wear levelling can retain physical remnants; destroy the encrypted volume or
its key when cryptographic erasure is required.

The sidebar's private-person filter shows clusters containing an anonymous or
named group. Coverage compares all photos containing that group with the
current effective human/AI keeper set, making systematic omission visible
during manual culling. Names and embeddings are not added to certified reports,
XMP exports, generated provider files, or Kimiya prompts. Person-aware evidence
in Phase 8 is therefore a local human-review instrument, not a remote identity
signal.

OpenCV documents YuNet/SFace thresholds from public face benchmarks, but those
benchmarks do not guarantee correct grouping for children, relatives, ageing,
profile views, occlusion, or a particular family. OpenCull presents similarity
as uncertain evidence and requires human merge/split confirmation; it must not
be used for security, access control, surveillance, or consequential identity
decisions.

### Phase 10: native macOS application

Phase 10 provides a Finder-launchable Apple Silicon application. Its native
launcher exists before any culling report: choose a photo folder, set the
maximum keep count and photographic profile, select or configure a provider,
and add the shoot to the persistent sequential queue. Double-click a completed
job to open its full evidence and human-review interface.

The packaged application contains Python, Kimiya, OpenCull, OpenCV, YuNet,
SFace, and the audited Kimiya extension source files. Culling runs in a separate
internal worker process, so a long job does not block the launcher. Only one
launcher owns the queue, while completed reports open in independent review
processes. The packaged Kimiya program is compiler-checked during provider
materialization just as it is in source mode.

macOS state follows platform conventions:

- settings, queue, generated provider programs, and results:
  `~/Library/Application Support/Darkimiya/`;
- per-job Kimiya trace, certificate, memo, locate cache, and datasheets:
  `~/Library/Application Support/Darkimiya/Kimiya/JOB_ID/`;
- generated previews: `~/Library/Caches/Darkimiya/`;
- launcher diagnostics: `~/Library/Logs/Darkimiya/Darkimiya.log`;
- API credentials: macOS Keychain;
- review and face sidecars: beside their immutable result report.

Application-support, result, cache, and log directories are created with
owner-only permissions. Source photographs remain in their original folders
and are still opened read-only. Generated result reports default to
`Application Support/Darkimiya/Results`. On first launch, Darkimiya copies the
small queue, provider, onboarding, and native-library indexes from the legacy
OpenCull application-support folder when a Darkimiya counterpart does not yet
exist. The legacy files are not moved or rewritten.

#### Build the Apple Silicon application

The reproducible recipe requires Homebrew's native Python and Tk. The build
environment is local to the checkout and is not inherited from Anaconda:

```bash
brew install python@3.12 python-tk@3.12
python3.12 -m venv .macos-build-venv-arm64
.macos-build-venv-arm64/bin/python -m pip install \
  pyinstaller -r requirements.txt \
  "opencv-python-headless==4.11.0.86" certifi
scripts/build_macos.sh
```

The native build also checks the reusable environment against
`requirements.txt` and installs any newly required packaged-runtime library
before invoking PyInstaller.

This produces:

```text
dist/OpenCull.app
dist/OpenCull-0.10.0-arm64.dmg
```

The local build is ad-hoc signed and verified, but not Apple-notarized. On the
first launch, macOS may require Control-clicking **OpenCull.app**, choosing
**Open**, and confirming once. Do not bypass Gatekeeper globally. A public
release should replace ad-hoc signing with a Developer ID Application
certificate, submit the archive for notarization, staple the ticket, and
verify it with `spctl`.

To rebuild the application while intentionally skipping disk-image creation:

```bash
SKIP_DMG=1 scripts/build_macos.sh
```

Run the final verification against the frozen application:

```bash
scripts/verify_macos_release.sh \
  dist/OpenCull.app \
  dist/release-receipts
```

The verifier rejects the release unless the executable is native `arm64`, the
deep code signature is valid, the bundle identity and required assets match,
and the Kimiya/static/model/private-path/single-instance smoke test passes from
inside the frozen application. Its JSON and text receipts are suitable for
release audit.

If `hdiutil` is unavailable, preserve Finder metadata in a ZIP instead:

```bash
ditto -c -k --sequesterRsrc --keepParent \
  dist/OpenCull.app dist/OpenCull-0.10.0-arm64.zip
```

The `.app` executable and bundled OpenCV library are verified as native
`arm64`. The frozen Kimiya worker is also checked from a directory outside the
source tree, proving that it uses the bundled program and audited extensions.

### Phase 11 native SwiftUI development shell

The professional native launcher is being developed alongside the existing
release application. It already uses the real persistent queue and provider
state; it does not use demonstration data. With a matching Swift compiler and
macOS SDK, run it from the source tree with:

```bash
cd native-macos
swift run OpenCullNative
```

For source development, the shell starts `../opencull_desktop.py
--native-server` through the active `python3`. Set `OPENCULL_SOURCE_ROOT` to the
OpenCull checkout when launching from another working directory. A packaged
build will instead set `OPENCULL_BACKEND` to its bundled Python/Kimiya helper.

The bridge binds only to `127.0.0.1`, requires a new random bearer token for
every launch, and owns the same single-instance lock as the Tk launcher.
Provider profiles can be created, edited, connection-tested, and deleted from
the native privacy workspace; credentials remain write-only in macOS Keychain.
Completed results open in independent native `WKWebView` windows whose local
review servers remain isolated from the active sequential culling queue.
Chosen folders and reports receive persistent macOS security-scoped bookmarks;
recent reviews can be reopened after relaunch, and disconnected sources are
relinked only after an explicit folder choice. The Tk application remains the
supported packaged launcher until the native acceptance build passes.

Stopped and finished queue entries provide **Remove from Queue…**. Removing the
entry alone preserves its checkpoint and log. The separate cleanup choice
removes those two resumable/diagnostic files as well; neither choice deletes
source photographs or a completed result JSON.

The isolated native release pipeline is:

```bash
scripts/build_native_macos.sh
```

It produces `dist/native/Darkimiya.app` and
`dist/native/Darkimiya-0.13.0-arm64.zip`, then verifies the SwiftUI frontend and
frozen Python/Kimiya backend independently. It never replaces the installed
application. A matching compiler and SDK are mandatory. The accepted local
build uses Xcode 26.6 with Swift 6.3.3. If Command Line Tools are selected
instead, select full Xcode before building:

```bash
sudo xcode-select --switch /Applications/Xcode.app/Contents/Developer
scripts/build_native_macos.sh
```

### Paired RAW-engine research

OpenCull keeps its deterministic renderer and can compare it with an installed
darktable engine without changing the project default. The Develop workspace
offers **Compare OpenCull and darktable** after an OpenCull treatment exists.
The resulting **Engines** view contains a three-panel contact sheet:

1. the completed OpenCull treatment;
2. an isolated, native darktable control render;
3. darktable initial development followed by the identical compiled OpenCull
   creative operations.

The third panel is deliberately labelled as a hybrid. It does not claim that
darktable natively executed the OpenCull recipe. The comparison report records
output hashes, dimensions, luminance distribution, clipping, saturation,
detail variance, pairwise RGB error, PSNR, and luminance correlation. Visual
and numeric measurements are bounded to a 2048-pixel research preview so a
26-megapixel comparison does not exhaust application memory.

The Develop inspector makes X-Trans demosaicing explicit for every darktable
comparison:

- **Markesteijn 1-pass** (`1025`) is the faster darktable default;
- **Markesteijn 3-pass** (`1026`) favors maximum fine-detail reconstruction;
- **Markesteijn 3-pass + VNG dual** (`3074`) blends the high-frequency method
  with VNG in flatter or artifact-prone regions.

OpenCull rewrites the versioned darktable demosaic history for the selected
mode and then verifies the applied method identifier from the exported JPEG's
embedded XMP. Requested and applied identifiers are stored in provenance.

The standalone research command accepts a completed OpenCull render:

```bash
python renderer_comparison.py \
  /path/to/photo.RAF \
  /path/to/reference.JPG \
  /path/to/compiled.recipe.json \
  --opencull-render /path/to/completed.opencull.jpg \
  --demosaic markesteijn-3-pass \
  --output-dir /path/to/comparison
```

darktable runs with a disposable configuration, cache, and library, with
custom presets disabled and the source exposed through a temporary path. It
does not read or write a sidecar beside the source photograph. Native mapping
of each typed OpenCull operation into versioned darktable history modules is a
later research stage and must report unsupported operations rather than
silently approximate them.

After proofing a treatment, **Render full-size darktable files** in the Develop
inspector runs a separate delivery job using the selected X-Trans demosaic
method. It preserves the RAW's complete output dimensions and registers two
exportable project renders: the native darktable control and the
darktable-development/OpenCull-recipe result. Both then appear under
**Export → Linked render** and can be copied to a destination selected with the
native macOS folder chooser. The bounded 2048-pixel contact sheet remains a
preview-only research artifact; it is never promoted as a full-size export.

## Current limitations

Version 0.1 gives the declared generator the observed pixels, but the final
cross-family panel currently warrants the textual report and scanner evidence,
not the full image group. Extending `shows` to compare an image collection is
the next certification milestone.

The `composition_proxy` only measures whether edge energy is concentrated near
rule-of-thirds intersections; it is not an artistic-quality score. Visual
agents should treat it as one weak signal rather than ground truth.
