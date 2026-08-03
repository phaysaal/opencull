# OpenCull Professional UX Roadmap

## Purpose

This document is the durable plan and implementation record for turning the
working OpenCull GUI into a professional macOS photo-culling application. It
records what each UX phase is meant to accomplish, the constraints that must
not be weakened, completed work, validation evidence, and the remaining
sequence.

This roadmap concerns the user experience. It is separate from the earlier
functional phases that introduced queueing, provider profiles, local face
indexing, exports, and native macOS packaging.

## Product position

OpenCull is a photographer's review environment built around a certified
Kimiya culling result. The AI recommendation is advice, not an irreversible
action. A professional experience must make four things continuously clear:

1. what the AI observed and recommended;
2. what the human reviewer has accepted or changed;
3. what will happen before any files are copied or moved;
4. which computation, model configuration, and checkpoint produced a result.

The intended experience should work for both professional sessions and large
family archives. It should prioritize pose, expression, readiness, interaction,
and irrecoverable surroundings while treating many RAW exposure and color
issues as recoverable.

## Non-negotiable constraints

UX work must preserve the following OpenCull properties:

- Original photographs remain read-only during culling and review.
- Human decisions remain separate from certified result JSON.
- File operations require a deterministic preflight and explicit confirmation.
- The culling queue remains sequential and starts at most one supervised
  Kimiya process.
- Resume and retry validate existing checkpoints rather than silently starting
  over.
- Provider credentials remain write-only and stored outside returned JSON.
- A queued job remains bound to the exact provider/program configuration it
  was created with.
- Face recognition remains local and private; embeddings are not returned by
  the GUI API.
- Remote image/model egress remains visible and attributable.
- Existing report, review-sidecar, queue, and provider formats are not changed
  merely for presentation.
- Keyboard-first review and large-folder behavior must remain bounded.

## Delivery policy

UX phases are implemented and tested in the source tree first. The installed
OpenCull application and distribution DMG will not be overwritten after every
phase. A fresh native build will be made after the planned UX work is complete
and has passed final integration testing.

Development builds may be produced in isolated output directories when
packaging needs validation. They must not replace the installed application.

## Phase map

| Phase | Area | Outcome | Status |
| --- | --- | --- | --- |
| 1 | Application foundation | Professional visual system and stable application shell | Complete |
| 2 | Review workspace | Fast, explicit cluster review and comparison | Complete |
| 3 | Culling queue | Guided folder intake and trustworthy job monitoring | Complete |
| 4 | Provider and privacy center | Clear model configuration, privacy, cost, and connection health | Complete |
| 5 | People workspace | Professional local face-group review and naming | Complete |
| 6 | Export and file operations | Understandable selection policies and exceptionally safe organization | Complete |
| 7 | First-run and recovery | Onboarding, empty states, missing-drive recovery, and error guidance | Complete |
| 8 | High-volume refinement | Performance perception, accessibility, and 1,500-photo usability | Complete |
| 9 | System polish | Consistent microcopy, motion, iconography, and complete state coverage | Complete |
| 10 | macOS release integration | Final native packaging, installation, signing, and release acceptance | Complete |
| 11 | Native macOS workspace | Replace the Tk launcher with a professional SwiftUI application shell | Acceptance candidate |

The scope of a future phase may be refined after inspecting the preceding
phase, but safety boundaries and completed decisions should not be silently
redefined.

## Phase 1 — Application foundation

### Goal

Create a professional, coherent application shell without changing working
review behavior or backend contracts.

### Implemented

- Introduced a navy, green, and blue design-token system for color, spacing,
  radius, elevation, and semantic states.
- Rebuilt the header around stable Review, Queue, and People navigation.
- Established a clear command hierarchy for result export, organization,
  report details, and undo.
- Refined the library summary, sidebar, filters, cluster list, dialogs, forms,
  and status presentation.
- Added responsive desktop and compact-window layouts.
- Added visible keyboard focus, a skip link, live status semantics, and
  reduced-motion handling.
- Preserved every existing JavaScript DOM identifier and workflow binding.
- Corrected the photo-card layout so Flag, Rating, and Label controls receive a
  full-width row instead of being compressed beside action buttons.

### Validation

- DOM identifiers were checked for uniqueness.
- Every JavaScript `#id` reference was checked against the HTML contract.
- Python and JavaScript syntax checks passed.
- The complete OpenCull test suite passed.
- Desktop and compact renders were inspected in Chrome.
- A signed arm64 application was built in the isolated
  `dist/ux-phase-a/` directory; it did not replace the installed application.

## Phase 2 — Review workspace

### Goal

Make daily cluster review fast, deliberate, and understandable for both small
groups and high-volume shoots.

### Implemented

- Added a progress indicator showing position through the complete shoot.
- Added an effective-selection summary that distinguishes an AI recommendation
  from a reviewed human selection.
- Added Comfortable and Compact photo-grid density modes.
- Added numbered photo badges corresponding to keeper keyboard shortcuts.
- Added an explicit comparison tray holding at most two frames.
- Changed comparison from an unexpected automatic modal to a deliberate
  command through the tray or the `C` shortcut.
- Added comparison clearing through the tray or `X`.
- Improved Keep, Remove, Compare, and Comparing states and accessible labels.
- Kept non-mutating navigation and comparison available when a review sidecar
  is stale or read-only.
- Added shortcut semantics and prevented browser-default behavior from
  conflicting with handled shortcuts.

### Keyboard review map

| Key | Action |
| --- | --- |
| `1`–`9` | Toggle the corresponding photograph as a keeper |
| `A` | Accept the AI recommendation |
| `N` | Keep none |
| `U` | Return the cluster to unreviewed |
| `Z` | Undo the most recent review change |
| `C` | Open marked comparison frames |
| `X` | Clear the comparison tray |
| `Left` / `Right` | Move between visible clusters |

### Validation

- Phase-specific DOM and behavior contracts were added.
- Comparison selection, Compact density, and selection feedback were exercised
  in a real browser.
- The synchronized side-by-side viewer was visually inspected.
- Desktop and compact renders were inspected.
- All 73 tests existing at the end of Phase 2 passed.

## Phase 3 — Culling queue

### Goal

Make the transition from a photo folder to a supervised Kimiya job clear and
trustworthy, and make failures recoverable without exposing users to raw
process details first.

### Implemented

- Reorganized job creation into two steps: Choose source and Set culling
  intent.
- Added clear original-photo safety and sequential-processing explanations.
- Added source, optional output, recursive-folder, profile, maximum-keeper, and
  provider configuration hierarchy.
- Disabled queue submission until local form requirements are ready.
- Added live states for initial, ready, validating, success, and error
  feedback.
- Added a queue overview for Active, Waiting, Completed, and Needs attention.
- Replaced metadata-first cards with operational states:
  Waiting, Culling, Pausing, Monitoring, Paused, Needs attention, Cancelled,
  and Ready to review.
- Added checkpoint percentage, completed-cluster counts, queue position,
  relevant job facts, and contextual recovery actions.
- Moved technical paths, hashes, privacy, timestamps, exit status, and bounded
  logs into explicit disclosures.
- Added a useful empty-queue state.
- Kept intake independently scrollable at compact window sizes so queue
  monitoring remains visible.

### Backend decisions preserved

No queue schema or state-transition change was required. Phase 3 continues to
use:

- one supervised process;
- persisted queue transitions;
- duplicate-folder and output collision prevention;
- validated checkpoint retry/resume;
- immutable provider/program hashes;
- explicit pause, cancel, retry, resume, and review actions.

### Validation

- Completed and failed/checkpointed jobs were rendered from realistic queue
  data.
- Wide and compact queue layouts were inspected in Chrome.
- Folder entry was verified to activate submission and show the correct
  readiness message.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 75 tests existing at the end of Phase 3 passed.

## Phase 4 — Provider and privacy center

### Goal

Turn model configuration into a comprehensible trust decision rather than a
technical form.

### Implemented

- Replaced the technical profile dialog with a provider trust center.
- Presented saved routes as identifiable cards with Local, Remote ZDR, or
  Remote provider-policy status.
- Added overview counts for local routes, ZDR routes, and profiles requiring
  credential attention.
- Added continuously visible routing explanations that distinguish photographs
  staying on the Mac from previews leaving the Mac.
- Added an agent data-flow map showing that agents A, B, and D receive observed
  image pixels while agent C receives text evidence.
- Organized configuration into Identity and route, Agent assignments, and
  Credential and policy sections.
- Added visible credential states for stored, missing, optional, and newly
  entered values without returning or displaying a stored token.
- Added explicit macOS Keychain assurance and runtime-only credential
  injection language.
- Added immutable queue-binding guidance: editing a profile does not change
  jobs already queued with generated Kimiya configuration hashes.
- Replaced raw connection-test JSON with structured endpoint, model-ID, and
  per-agent vision-capability results.
- Added actionable missing-model and connection/credential failure
  presentation.
- Preserved distinct setup behavior for OpenRouter, local Ollama, and
  OpenAI-compatible/Vast.ai endpoints.

### Security contracts preserved

- Public provider JSON exposes only `stored` or `missing`, never a secret.
- Credentials remain in macOS Keychain.
- Generated Kimiya files contain environment-variable references rather than
  credential values.
- Provider and queue schemas were not changed.
- Existing jobs retain the provider/program hashes captured when queued.

### Validation

- Local Ollama, OpenRouter ZDR, remote-policy, missing-credential, mixed
  model-discovery, and structured failure states were exercised.
- Desktop and compact-window renders were inspected in Chrome.
- Phase-specific DOM and no-secret-presentation contracts were added.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 77 tests existing at the end of Phase 4 passed.

## Phase 5 — People workspace

### Goal

Make local face grouping usable as a private photographic catalog tool.

### Implemented

- Rebuilt People as a private local catalog workspace with clear indexing,
  progress, coverage, and review summaries.
- Added group search and confirmed/unconfirmed filters for larger libraries.
- Added face thumbnails, keeper coverage, and high/medium/low match-confidence
  evidence without exposing raw embeddings.
- Made naming and identity confirmation separate decisions so an entered name
  cannot silently confirm an uncertain group.
- Added deliberate multi-group merge and selected-face split workflows with
  visible selection state.
- Added clear success and recovery feedback for rename, merge, split, and index
  operations.
- Kept forget-one and delete-all controls in an explicit danger area with typed
  confirmation and a reminder that original photographs remain untouched.
- Preserved safe pause/resume behavior and made a completed local index display
  as up to date after an application restart.

### Privacy contracts preserved

- Face detection and embedding generation remain local-only.
- Private names and embeddings remain in the local database.
- The GUI API returns face crops and review metadata, never embedding vectors.
- People actions do not modify original photographs.

### Validation

- Desktop and compact-window People renders were inspected with a populated
  local face index.
- Merge and split selection interactions were exercised in the rendered UI.
- Phase-specific privacy, interaction, and deliberate-mutation contracts were
  added.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 79 tests existing at the end of Phase 5 passed.

## Phase 6 — Export and file operations

### Goal

Make the difference between exporting decisions and changing files impossible
to misunderstand.

### Implemented

- Rebuilt delivery as a four-step workflow: choose selection, choose output,
  verify the exact plan, and complete safely.
- Created an unmistakable boundary between metadata-only exports and operations
  that copy or move photographs.
- Added plain-language explanations and risk guidance for human-only, effective,
  fully reviewed, AI-only, and human-modified selection policies.
- Added distinct verified-copy and journaled-move risk presentations.
- Made deterministic preflight explain that it changes nothing while checking
  every source, collision, destination, and available byte.
- Added human-review coverage warnings to preflight results.
- Kept typed plan-specific confirmation and hid it entirely when preflight is
  blocked.
- Added readable running, paused, completed, failed, and rolled-back states with
  progress percentage and verified-byte totals.
- Added a completion receipt showing operation identity, files verified, bytes,
  destination, and the exact recovery journal path.
- Preserved technical journal inspection, safe pause, move rollback, and
  validated resume controls.

### Safety contracts preserved

- Exporting JSON, CSV, text, or XMP never modifies photographs.
- Copy and move cannot execute without a fresh, error-free preflight and exact
  typed confirmation.
- Copy and cross-volume move destinations are SHA-256 verified.
- Move removes a source only after verification and retains rollback state.
- Any review revision after preflight invalidates the prepared plan.

### Validation

- Phase-specific separation, policy, confirmation, recovery, and receipt
  contracts were added.
- The operation API now returns its exact journal path for the completion
  receipt, covered by an operation regression.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 81 tests existing at the end of Phase 6 passed.

## Phase 7 — First-run and recovery

### Goal

Help a new user reach a trustworthy first result and help an existing user
recover from predictable interruptions.

### Implemented

- Added an optional native-launcher welcome path from choosing a photo folder,
  through provider setup, to human review before delivery.
- Persisted onboarding dismissal in the private application-support directory so
  established users are not forced through a tutorial on every launch.
- Added launcher-level recovery awareness for failed, paused, detached, and
  source-folder-unavailable queue jobs.
- Reworked the review empty state into a direct first-cluster action.
- Added a persistent Help & Recovery entry point and contextual recovery banner.
- Added diagnostic checks for report loading, source-volume availability, human
  review binding, and original-file protection.
- Added direct recovery routes to Queue, provider configuration, and safe review
  reload.
- Added specific guidance for disconnected volumes, interrupted jobs, stale
  review sidecars, and isolated preview decoding failures.
- Added structured failure handling when the report itself cannot load, while
  stating that no file operation was attempted.

### Recovery contracts preserved

- Reconnecting a volume or retrying a preview never rewrites the report or
  review sidecar.
- Stale human decisions remain read-only rather than being silently remapped.
- Interrupted culling resumes only through validated checkpoints.
- Provider guidance explains configuration without displaying stored secrets.
- First-run assistance is dismissible and expert navigation remains direct.

### Validation

- First-run, optional-dismissal, diagnostic, and recovery-scenario contracts
  were added.
- Existing disconnect/reconnect, checkpoint resume, dead-process recovery, and
  corrupt-preview-cache tests remain green.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 83 tests existing at the end of Phase 7 passed.

## Phase 8 — High-volume refinement

### Goal

Ensure the application remains understandable and responsive with 100 to 1,500
or more photographs.

### Implemented

- Bounded the cluster-list DOM to 200 entries while preserving navigation and
  filtering across the complete library.
- Added earlier/later list windows, visible range position, automatic recentering
  around the active cluster, and accessible busy state during list replacement.
- Added reviewed-versus-remaining awareness directly to the active cluster.
- Added independent Review, Culling, Previews, and People activity indicators so
  simultaneous background work does not collapse into one ambiguous status.
- Added keyboard traversal inside the rendered cluster window with Arrow Up,
  Arrow Down, Home, and End.
- Corrected Space activation on photo previews to prevent page scrolling before
  opening the detail viewer.
- Added accessible cluster position labels and retained direct full-library
  previous/next navigation.
- Improved wrapping and two-line containment for long filenames and assessment
  labels.
- Added coarse-pointer target sizing while preserving compact mouse/trackpad
  density.
- Preserved reduced-motion behavior and visible keyboard focus.

### High-volume contracts

- Filtering may inspect the complete report, but the cluster sidebar never
  mounts more than 200 cluster controls at once.
- Preview work remains bounded and focused on visible plus adjacent clusters.
- Face indexing and review history remain resumable and bounded at 1,500 photos.
- Background culling remains independent from the active manual review.

### Validation

- Added bounded-list, position, concurrent-activity, keyboard, and coarse-pointer
  contracts.
- Existing 1,500-photo report, 1,500-cluster review-history, 1,500-photo face
  index, bounded-preview-worker, and stale-prefetch tests passed.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 85 tests existing at the end of Phase 8 passed.

## Phase 9 — System polish

### Goal

Bring every remaining screen and state into one coherent product language.

### Implemented

- Introduced shared product status language for ready, in-progress, safely
  paused, needs-attention, completed, and verified states.
- Replaced mixed navigation glyphs with one CSS-rendered line-icon family.
- Normalized human-review labels to distinguish kept, not kept, awaiting review,
  changed, agreeing, and read-only states.
- Added a complete in-app keyboard reference covering navigation, decisions,
  inspection, dialogs, and safe scope.
- Added the `?` shortcut to open or close the keyboard reference when focus is
  outside an editable control.
- Added concise, accessible success and error toasts while retaining persistent
  status, evidence, journals, and recovery state as the sources of truth.
- Added a consistent dialog entrance transition that automatically collapses
  under the existing reduced-motion preference.
- Retained strong visual separation for warnings, failures, private state,
  non-mutating actions, and destructive confirmation.
- Completed responsive shortcut and feedback layouts.

### Consistency contracts

- Keyboard shortcuts cannot initiate copy, move, delete, identity, or credential
  operations.
- Temporary toasts supplement rather than replace persistent operational state.
- Status words retain the same meaning across Review, Queue, People, and
  Delivery.
- Icon marks are decorative and navigation retains explicit accessible text.
- Reduced-motion settings suppress the final dialog transition.

### Validation

- Added shared-language, icon-family, keyboard-reference, non-destructive scope,
  toast, and motion contracts.
- DOM identifier uniqueness and JavaScript-to-HTML bindings remain complete.
- Python compilation, JavaScript syntax, and patch-format checks passed.
- All 87 tests existing at the end of Phase 9 passed.

## Phase 10 — macOS release integration

### Goal

Deliver the finished UX as a dependable macOS application.

### Implemented

- Added a release-only smoke mode that runs without a GUI or network.
- Made the smoke test verify the bundled Kimiya program and audited Python
  extensions, static interface assets, pinned face models, OpenCV, NumPy,
  Pillow, private application paths, empty provider/queue stores, and
  single-instance locking.
- Added an independent release-verification script that requires arm64,
  deep-signature validity, the expected bundle identity, all required resources,
  and a passing frozen-runtime smoke receipt.
- Rebuilt the final application from the complete Phase 9 source state.
- Verified the bundled Kimiya program from inside the frozen application rather
  than from the source checkout.
- Verified native arm64 execution, ad-hoc code-signing integrity, macOS 13
  minimum version, private state permissions, and bundled model hashes.
- Produced a Finder-preserving ZIP release and a SHA-256 manifest.

### Final acceptance result

- All 88 automated tests passed.
- The source smoke test passed.
- The frozen arm64 packaged smoke test passed.
- The release verifier passed every bundle, dependency, signature, resource, and
  private-path check.
- Existing external-volume, queue restart, checkpoint resume, separate review
  server, and single-instance regression coverage passed.
- The final application is suitable for local installation on this Mac.

### Distribution limitations

- The application is ad-hoc signed, not Apple Developer ID signed or notarized.
  Wider distribution would require the developer’s Apple signing identity and
  notarization credentials.
- This restricted execution environment could not create a DMG because
  `hdiutil` returned `Device not configured`. The verified application and ZIP
  release are unaffected.
- Installation into `/Applications` remains subject to the host sandbox; when
  blocked, the verified bundle can be installed with the documented `ditto`
  command from the user’s Terminal.

## Acceptance discipline

Every phase should finish with validation proportional to its risk:

- Python compilation for changed Python modules;
- JavaScript syntax checking for GUI code;
- DOM contract tests for stable IDs and event bindings;
- targeted tests for new states and safety behavior;
- the complete automated suite;
- real browser inspection at desktop and compact window sizes;
- packaged-app validation only when packaging is in scope.

Source photographs and user reports used during development are not modified.
Temporary visual fixtures belong outside the repository. Existing uncommitted
user files must remain untouched.

## Phase 11 — Native macOS workspace

### Goal

Replace the engineering-oriented Tk launcher with a professional SwiftUI shell
while retaining the certified Kimiya program, persistent Python queue engine,
and mature browser review workspace.

### Implementation slice 1

- Added a native SwiftUI application shell with Overview, Culling Queue,
  Results, and Providers & Privacy workspaces.
- Added a native folder picker and guided culling-job sheet.
- Added live queue metrics, progress, recovery actions, Finder reveal, and
  completed-result review launch.
- Added an authenticated, ephemeral-token loopback bridge. It binds only to
  `127.0.0.1`; the SwiftUI shell owns the bridge process and the bridge owns the
  existing sequential `JobManager`.
- Kept the Tk application intact as the release launcher while the native shell
  is brought to parity.

### Implementation slice 2

- Replaced the native Providers placeholder with a complete provider and
  privacy workspace.
- Added create and edit flows for OpenRouter, local Ollama, and generic
  OpenAI-compatible endpoints, including all four agent model assignments.
- Preserved write-only credential handling: the native shell can replace a
  credential, but neither the bridge nor the interface can read it back.
- Added Keychain stored/missing state, local/remote/ZDR route summaries, and
  explicit image-egress language.
- Added live connection testing with endpoint reachability, model inventory,
  missing configured models, and vision-capability notices.
- Added explicit profile deletion with a separate choice to retain or remove
  its Keychain credential.
- Preserved optimistic revision checks and immutable provider bindings for
  already queued jobs.

### Implementation slice 3

- Added independent native review windows backed by `WKWebView`.
- Changed completed-result launch to create an isolated in-process local review
  server and return its ephemeral loopback address to the SwiftUI shell.
- Kept the existing professional Review, Queue, People, Delivery, evidence,
  keyboard, comparison, and sidecar behavior intact inside the native window.
- Restricted embedded navigation to `127.0.0.1` and `localhost`; explicit
  external links open through macOS instead of replacing the review workspace.
- Added native save-panel handling for report, XMP, and other attachment
  downloads initiated by the review interface.
- Made review-server lifetime follow the native desktop backend and retained
  independent windows so culling and manual review can continue concurrently.

### Implementation slice 4

- Added a bounded native recent-review library persisted under OpenCull
  Application Support.
- Added Open Existing Result with explicit result-file and corresponding
  photo-folder selection.
- Added macOS security-scoped bookmarks for chosen source folders, result
  files, and review photo folders, restored at the next application launch.
- Added unavailable-drive states for queued work and recent reviews.
- Added explicit source-folder relinking for stopped jobs. Relinking changes
  only the selected queue record, preserves its checkpoint and output identity,
  and never occurs from filename guessing.
- Kept recent-history removal separate from deleting reports, reviews, or
  photographs.
- Preserved concurrent culling and multiwindow review after restoring bookmarked
  external-volume access.

### Implementation slice 5

- Added a native Recovery & Diagnostics workspace with engine health, jobs
  requiring attention, missing-drive status, checkpoint-safe actions, and a
  credential-free support receipt.
- Added Finder access to the launcher log and result directory without exposing
  secrets through the diagnostics API.
- Added native workspace commands and `Command-1` through `Command-5`
  navigation, plus `Command-N` for a new job and `Command-O` for an existing
  result.
- Added macOS notifications for completed, failed, and paused jobs, emitted only
  for transitions observed after the initial queue load.
- Added guarded queue removal for failed, cancelled, paused, and completed jobs.
  The default removes only the queue record; a separate destructive choice can
  also remove its checkpoint and log. Source photographs and result JSON are
  never deleted by either operation.

### Implementation slice 6

- Added a hybrid native release pipeline in which the SwiftUI executable is the
  application entry point and the verified PyInstaller Python/Kimiya runtime is
  retained as the `OpenCullBackend` auxiliary executable.
- Added automatic bundled-helper discovery while preserving explicit and source
  development backend overrides.
- Added isolated version 0.11.0 assembly, ad-hoc signing, Finder-preserving ZIP
  creation, and SHA-256 output.
- Added a native release verifier requiring two arm64 executables, SwiftUI
  linkage, bundle identity/version, deep signature validity, and a passing
  frozen Python/Kimiya smoke receipt.

### Remaining acceptance work

- Exercise the isolated 0.11.0 application with a real queue, Keychain
  provider, external drive, concurrent embedded review, export, quit, and
  relaunch.
- Retire the Tk launcher only after those native acceptance checks pass.

### Development build requirement

Full Xcode 26.6 supplied a matching Swift 6.3.3 compiler and macOS SDK. The
SwiftUI frontend compiled successfully after replacing macOS 14-only empty-state
APIs with macOS 13-compatible views and adopting Swift 6.3 WebKit/concurrency
delegate signatures.

The isolated 0.11.0 application then passed arm64 frontend/backend inspection,
SwiftUI linkage verification, deep ad-hoc signature validation, and the frozen
Python/Kimiya release smoke test. A Finder-preserving ZIP and SHA-256 receipt
were produced without changing the installed application.
