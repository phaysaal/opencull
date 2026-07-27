# First Kimiya Program Retrospective

**Date:** 2026-07-26  
**Project:** OpenCull  
**Context:** Reflection after writing the first OpenCull program in Kimiya

## Question

> wait, first tell me how was experience in writing the first kimiya program?
> This language is my invention wtih help from claude fable5.

## Answer

Honestly, it was a surprisingly coherent first experience. Kimiya feels less
like an “agent framework” and more like a proof-carrying workflow language.
That distinction became apparent while writing OpenCull.

The strongest parts:

- Agent declarations are clear. Switching among Ollama, OpenRouter and an
  OpenAI-compatible vast.ai endpoint requires no change to the decision logic.
- `use "agents.kim"` cleanly separates deployment choices from the program.
- `observe`, `check`, `judge`, `act`, `settle`, and `commit` form a readable
  chain from evidence to effect.
- The rule that an irreversible action must follow a verified gate influenced
  the design positively. OpenCull produces a report only after validation and
  cross-family judgment.
- Python extensions being announced and recorded by SHA is excellent. It
  admits that Python escapes the language’s guarantees instead of pretending
  otherwise.
- `retry ... until check` was particularly natural for rejecting hallucinated
  filenames.
- The checker passed the first complete program without special workarounds.
  That says the examples and grammar are internally consistent.
- Certificates and explicit abstention give the program a meaningful outcome
  model. “Nothing happened” cannot masquerade as success.

The language also shaped the architecture. I initially considered letting a
Python extension scan the photo directory directly from Kimiya. But that would
quietly bring world observations through a supposedly kernel-grade extension.
Instead, I separated scanning from judgment: the scanner creates a manifest,
and Kimiya explicitly observes that manifest. That is evidence the language’s
philosophy is actually affecting implementation decisions.

The main friction points I encountered:

### 1. The observation surface is currently too narrow

OpenCull needs to observe directories, images and image collections. Kimiya
supports files and screens, but a photo collection is neither naturally
represented as one text file nor as a GUI screenshot.

Useful future surfaces might look like:

```kimiya
photos := observe directory(input, recursive=true)
image := observe image(photo.path)
dataset := observe manifest("manifest.json")
```

An `image` observation would also let vision-capable agents receive an
explicitly audited image rather than forcing that capability through the
screen surface.

### 2. Python extensions create a boundary ambiguity

They are described as deterministic kernel instruments, but technically they
can read files, access the network or mutate the world. The SHA disclosure is
honest, but the checker cannot distinguish:

```python
def mean(values): ...
```

from:

```python
def scan_private_directory(path): ...
```

It may eventually be useful to declare extension capabilities:

```kimiya
use python "metrics.py" capabilities [pure]
use python "scanner.py" capabilities [read_directory, read_image]
```

Then the runtime could treat the latter as an observation instrument rather
than kernel certainty.

### 3. Vision is too closely tied to GUI automation

The existing vision path is built around screenshots and locating controls.
OpenCull needs semantic image assessment without GUI coordinates: faces,
expressions, framing, subject isolation and aesthetic comparison.

A more general form could be:

```kimiya
assessment := gen<Assessment>(
    "Compare these photographs",
    images=[a, b, c]
) by A
```

Kimiya could then report whether those pixels stayed local or left the
machine.

### 4. The mock oracle cannot satisfy constrained structured generation

The offline mock produced placeholder schema fields, so OpenCull correctly
exhausted its retry budget because those placeholders weren’t valid filenames.
Static checking still works, but deterministic end-to-end tests need a
programmable mock response mechanism.

Something like this would help:

```bash
KIMIYA_MOCK_FIXTURE=tests/cull-responses.json python -m kimiya run ...
```

That would allow tests of successful effects and certificates without real
models.

### 5. Python-returned values lose static shape information

`parse_manifest()` returns groups with fields such as `id` and `candidates`,
but the Kimiya type checker effectively has to treat Python results
dynamically. Declaring a Python function’s return schema would improve typo
detection:

```kimiya
pyfn parse_manifest = "opencull_kernel.parse_manifest"
    returns list<Group>
```

### 6. Agent configuration is readable but static

Editing `agents.kim` is appropriate for an auditable program, but deployments
may want to choose a predeclared profile without rewriting source:

```kimiya
use agents from "agents.kim" profile "local"
```

The certificate should then record the selected profile and resolved
declarations.

My overall impression: Kimiya already has a real identity. Its best idea is not
the syntax—it is the insistence that model judgment, deterministic
computation, world observation and world effects are epistemically different
things.

That is rare. Many agent systems flatten everything into arbitrary tool calls
and logs. Kimiya makes the programmer state what kind of claim each operation
is making.

The next challenge is expanding the data and perception surfaces without
weakening that distinction. OpenCull is actually a very good pressure test for
it because it combines deterministic measurements, subjective judgment,
privacy-sensitive media, grouping, human review and potentially destructive
decisions.
