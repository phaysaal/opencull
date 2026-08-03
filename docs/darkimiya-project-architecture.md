# Darkimiya project architecture

Darkimiya treats one photograph folder as one project. The application is the
project workspace; OpenCull remains its Kimiya-powered culling engine.

## Storage boundary

New projects use the following inspectable layout:

```text
Photograph Folder/
├── original photographs and user folders
└── Darkimiya/
    ├── project.json
    ├── Reports/
    ├── Reviews/
    ├── Recipes/
    ├── Developments/
    ├── Verification/
    ├── Operations/
    ├── Previews/
    ├── RAW Reserve/
    └── Rejected/
```

Every recursive photograph scanner excludes the managed `Darkimiya`
directory. Derived images, retained RAWs, rejected files, and previews must
never re-enter culling as source photographs.

Provider credentials, private face embeddings, caches, queue supervision, and
security-scoped bookmarks are device-private and remain in macOS Application
Support. The project manifest may reference them by identity or evidence hash,
but never contains a credential value.

## Compatibility

`darkimiya-project-v1` is the folder-local format. An existing
`opencull-project-v1` manifest remains readable in place. Opening a legacy
project does not silently create folders, move photographs, or rewrite hashes;
migration is an explicit, journaled operation.

OpenCull JSON reports, Kimiya source programs, CSRF header names, and evidence
format identifiers retain their established technical names. Product branding
does not invalidate historical evidence.

## File-state vocabulary

- **Active**: a source photograph still participating in the project.
- **Rejected**: recoverable project quarantine, not the macOS Trash.
- **RAW Reserve**: a RAW retained because a selected or professionally useful
  photograph may need future development.
- **System Trash**: a later, explicit Finder-compatible removal operation.

No AI decision directly invokes System Trash. Human review and a journaled
preflight are mandatory before physical organization. Final cleanup offers to
preserve useful RAWs, keep every RAW, remove JPEGs only, inspect individual
files, or cancel.

## One-window ownership

The native application owns one primary window and one active project session.
Its root is the persistent **Projects** catalog, never the activity queue.
**Add Project** creates or opens folder-local state without starting culling or
contacting a model. Opening a project replaces the catalog content area with
the project dashboard; it does not create a second SwiftUI `WindowGroup`.

From the dashboard, the user may start optional AI culling or continue without
culling. The latter creates a deterministic, local, all-included selection
report, groups JPEG/RAW companions by folder and stem, and makes no network or
model call. While culling is queued or running, selection and development for
that project are locked because their candidate set is not stable. Its project
card shows live progress; all other projects remain available. The separate
**Activity** panel exposes the shared sequential queue. Returning to Projects
closes only the active project view; supervised jobs continue in the
background.

The Application Support `projects.json` file is only a small discovery index;
the folder-local manifest remains authoritative. Existing queue jobs and
previously opened review results are imported into the catalog without being
removed from Activity or overwriting newer project evidence.

## Artifact ownership

Adding a folder creates or opens its project immediately but creates no queue
entry. Starting culling from that project later creates an activity record
carrying both the immutable project ID and the manifest path.
If the user does not choose an override path, supervised stages write to these
project-owned locations:

| Stage | Durable location | Manifest artifact |
| --- | --- | --- |
| OpenCull culling | `Reports/` | `culling_report` |
| Human culling review | `Reviews/` | `culling_review` |
| Professional shortlist | `Reports/` | `shortlist` |
| Human shortlist review | `Reviews/` | `shortlist_review` |
| Edit directions | `Recipes/` | `edit_directions` |
| Guided renders and renderer comparisons | `Developments/` | `renders`, `renderer_comparisons` |
| Semantic checks | `Verification/` | `verifications` |

Registration happens only after a supervised process exits successfully and
its expected output exists. Each file record includes its resolved path,
SHA-256 identity, size, creation time, and producing job ID where applicable.
Reopening a project also discovers and registers review sidecars without
duplicating artifact entries. Explicit user-selected output paths remain
supported and are linked into the manifest by identity rather than copied.

Global Application Support remains the queue supervisor and private settings
home. It is no longer the default owner of a new project's report or developed
photographs. Old queue records without a project ID remain readable and
resumable. A legacy project is migrated only from **Guidance and Recovery**,
after showing its source and destination paths, evidence counts, unavailable
references, legacy SHA-256, and a four-digit confirmation code. The operation
creates `PHOTO_FOLDER/Darkimiya/project.json`, preserves recognized and unknown
artifact categories, retains the project identity and history, and writes a
completed `Operations/project-migration-*.json` journal. The old manifest is
byte-for-byte unchanged and no photograph is moved. A failed confirmation
creates neither a new manifest nor a migration journal; once a folder-local
manifest exists it is preferred and repeat migration is refused.

## Recoverable project filtering

After every cluster has a human review, **Organize reviewed project** creates a
deterministic mixed-operation plan:

- unselected photographs move to `Rejected/`, preserving relative paths;
- RAW companions of keepers move to `RAW Reserve/` when they are already
  inside the project source folder;
- matching RAWs from a separately configured source are copied into
  `RAW Reserve/External/`, leaving that external archive unchanged;
- keeper JPEGs remain in their original locations.

The preflight refuses partial human review, missing sources, destination
collisions, or insufficient temporary free space. It lists every source,
destination, category, byte count, and whether that item will be copied or
moved before showing the four-digit authorization code.

Execution copies each item to a temporary file, verifies SHA-256, atomically
places it, and only then removes a source scheduled to move. Its journal lives
in `Operations/`; the project manifest records rejected items, RAW-reserve
items, and the journal. The RAW source map is automatically rebound to the
project reserve so later development can locate the retained files.

Rollback restores moved rejected/RAW files and deletes only the verified
external RAW copies created by that operation. Restored artifact records remain
in project history with `status: restored`. Quarantined photographs remain
viewable in review because the photo resolver can safely locate their preserved
relative path under `Rejected/` or `RAW Reserve/`. Nothing in this phase invokes
macOS System Trash.

## Explicit final cleanup

**Final cleanup to macOS Trash** is available only as a separate action after
project filtering. It inventories the files physically present in `Rejected/`
and never treats the active source tree or `RAW Reserve/` as cleanup input.
Every cleanup still requires complete human review, deterministic preflight,
inspection of the exact paths and byte counts, and the generated four-digit
authorization code.

The user chooses one of four policies:

- **Preserve useful RAWs** moves everything in `Rejected/` to System Trash and
  leaves the existing `RAW Reserve/` untouched.
- **Keep every RAW** moves rejected RAW files into
  `RAW Reserve/Retained from Rejected/`, while other rejected files go to
  System Trash.
- **Move rejected JPEGs only** moves only `.jpg` and `.jpeg` files to Trash;
  rejected RAWs and other formats remain quarantined.
- **Inspect and choose individual files** exposes the live quarantine
  inventory with per-file checkboxes; only checked files enter the signed
  plan.

Trash routing is volume-aware: files on the startup volume use the user's
macOS Trash, while files on an external volume use that volume's per-user
`.Trashes` area. A uniquely named Darkimiya batch prevents unrelated Trash
items from being mixed with the operation. As with project filtering, every
item is copied, SHA-256 verified, and only then removed from quarantine.

The cleanup journal remains under `Operations/` and is automatically offered
for resume after interruption. Rollback restores Trash items to `Rejected/`
and moves any “keep every RAW” items back from reserve. It succeeds only while
the verified destination still exists and its hash has not changed. Emptying
macOS Trash is deliberately outside Darkimiya's authority.
