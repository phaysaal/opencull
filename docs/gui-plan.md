# The Darkimiya GUI plan

One window, one shoot at a time, phases along the top. This document plans
where everything that exists and everything that is specified will live, so
that each piece lands in a place that was waiting for it rather than a place
that had room. The flow itself is in `flow.html`; this is about walls,
doors, and the order to build in.

## The interface language

These are the rules the built pages already follow. New work follows them
too; a proposal that needs to break one should say so out loud.

1. **The price is on the button.** A control that spends model calls names
   the count it will spend: "Assess 23 frames", "One per scene (5 calls)".
   Never "this may incur costs".
2. **Blocked says why, and how to unblock.** A dimmed phase carries the
   sentence that opens it. Dimming without a reason tells somebody they are
   wrong without telling them how to be right.
3. **An invitation shows the frames.** A phase that has not run shows what
   the run would read, as a contact sheet, before the button that pays.
4. **Beside, never over.** A person's decision sits next to the model's —
   "the assessment said EXCEPTIONAL · you set REJECT", "model asked
   +0.45 EV · you set +0.30" — because the evidence is immutable and the
   disagreement is the interesting part.
5. **Inherited answers say so.** A frame carrying another frame's treatment
   names it: "Treatments shared from A.JPG — same scene."
6. **Decoders are named before they run.** A proof from the camera's
   embedded JPEG is never presented as a development.
7. **Confirmations carry the choice, not a warning.** The dialog that asks
   also informs: the criteria dialog is also the cost confirmation; one
   decision, one dialog.
8. **The photograph gets the room.** Words live in side panels at reading
   width; the frame fills what remains.

## The window map

```
Launcher (the library)
├── folder cards · queue · open a folder
├── [Studio]                      ← planned; absorbs the chrome buttons
│     ├── personal profiles      (named, several, one in use, style drift)
│     ├── the taste ledger       (planned)
│     ├── providers
│     └── defaults               (stance, export folder)
└── ProjectShell (one shoot)
      ├── phase bar  1–7 built · 8 planned
      ├── 1 CULL         invitation+sheet │ review (clusters; scenes planned)
      ├── 2 ASSESSMENT   invitation+sheet │ criteria dialog │ rating page
      ├── 3 PROFILES     panel (moves under Studio when it exists)
      ├── 4 SUGGESTIONS  treatments · scene sharing · ask dialog
      ├── 5 DEVELOPMENT  stage · hold-to-compare · verify · export one
      ├── 6 FINE TUNING  17 bounded controls │ prompt box planned
      ├── 7 EXPORT       deliver the set │ sequencing planned │ proof sheet planned
      └── 8 DEBRIEF      planned
```

Single window stays. Two levels of "whose thing is this": the **shell** is
the shoot's, the **Studio** is the photographer's. Today the photographer's
things (Style, Providers) are chrome buttons; the Studio consolidates them
and gives the taste ledger and profile naming a home. Phase 3 stays on the
bar — the profile is an input to phase 4 — but its page becomes a view of
the same Studio panel.

## Per-phase plans

### 1 · Cull

- **Prefilter = the invitation sheet becomes selectable.** No new page:
  tiles on the contact sheet toggle, the button re-counts live ("Cull these
  19 of 23"). Deselection is recorded as a prefilter note so the report
  says the run never saw those frames. The same mechanism serves the
  assessment invitation. This is the cheapest large feature in the plan.
- **Scene headers in the review.** The cluster list gains scene groupings
  (machinery exists in `opencull_gui/scenes.py`): a scene header with its
  own accept-all. Photo-level review inside, scene-level acceptance above.
- **Approve the lot.** One button in the review bar that accepts every
  proposal; confirmation states the counts.
- **Trash the rejects.** Offered once the review is complete, as a banner
  on the review page. The backend exists and is careful (unselected only,
  platform trash, rollbackable batch); the dialog names the count and says
  the move is reversible. First control that moves files — the confirmation
  is load-bearing.

### 2 · Assessment

- Criteria dialog (built) gains the **axes chooser** (checkboxes over the
  ten axes) and the **releasable / needs editing verdict** — both
  provider-gated: kernel schema, normalizer and validator change together,
  and cannot be proven without a live model.
- The rating page is built (tiers, beside-not-over verdicts, by-hand mode).

### 3 · Profiles → Studio

- Profiles get **names** at build time ("What should this profile be
  called?") — smallest fix with the largest confusion-removal.
- **Style drift**: a compare view of two profiles, field by field.
- **The taste ledger** lives here: a compiled view of overrules across
  shoots, and (later) a memo that feeds the prompts.

### 4 · Suggestions

Built, including scene sharing. Remaining: none planned beyond what the
Studio ledger will feed into the prompts.

### 5 · Development

- **Decision archaeology**: a "Why this frame?" affordance on the frame
  header, opening a provenance drawer that walks the chain — kept
  (rationale) → assessed (tier, score) → marked → treatment (intent) →
  adjustments → certificate. Zero calls; the drawer is a shared component
  offered anywhere a frame is shown.

### 6 · Fine tuning

- **The prompt box, in two stages.** Stage one is free and provable now:
  the compiler that turns suggestion prose into bounded operations is
  local, so "shadows +10, vignette -8" typed above the sliders compiles on
  this machine with no call, and the sliders move to show what the words
  became. Stage two — free-form language interpreted by a model — is
  provider-gated. Build stage one first; it also teaches the recipe
  grammar by showing prose→numbers live.

### 7 · Export

- **Sequencing**: drag order on the delivery list; order recorded in the
  export record.
- **The client proof sheet**: an export option that writes a self-contained
  page per delivery — as-shot, treated, intent, certificate. Zero calls.

### 8 · Debrief (new phase, planned)

- Reads local aggregates (tier distributions, defect axes, scene scores,
  overrule counts) and shows them plainly; one call turns them into a
  memo. Blocked until an assessment exists, with the usual sentence.

## Shared components

| Component | Exists | Grows |
|---|---|---|
| `ContactSheet` | yes | selectable mode (prefilter), used by both invitations |
| `Invitation` | yes | unchanged |
| Provenance drawer | no | new; offered wherever a frame is shown |
| `Paragraph`, `PhotoLabel`, phase bar | yes | unchanged |
| Ask dialogs | yes | criteria dialog gains axes; scope dialog done |

## Build order

Cheap-and-shaping first; provider-gated last.

1. **Selectable contact sheet** — prefilter before cull and assessment
   (free, largest cost lever, reuses built parts) — **built**
2. **Approve the lot + trash the rejects** — completes the cull story
   (backend exists) — **built**
3. **Studio page** — profile naming, providers move, ledger placeholder
   — **built**
4. **Provenance drawer** — archaeology everywhere (free, evidence exists)
   — **built**
5. **Scene headers in cull review** — machinery exists — **built**
6. **Fine-tune prompt box, local stage** — free, uses the compiler —
   **built**
7. **Debrief phase** — aggregates first, memo call behind the usual gate
   — **built** (aggregates; the memo needs a kernel)
8. **Proof sheet + sequencing** on export — **built**
9. **Axes chooser + verdict** — provider-gated
10. **Taste ledger compilation, jury, borrow-a-look, budget governor** —
    after a live provider has proven the stages they depend on

## Open questions

- Does the Studio live as a page in the stack (like a shoot) or a dialog?
  Leaning page: the ledger and drift views deserve room.
- Scene thresholds (10 min / 40 frames) are documented defaults; do they
  need a control, or is editing the plan file enough until someone asks?
- The debrief's memo call should probably reuse the criteria dialog's
  pattern: the button names the price, cancel is default.
