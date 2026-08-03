# OpenCull in Kimiya versus standalone Python

Date: 2026-07-27

## Question

If a similar program were written without Kimiya, how large would it be, and
what would the advantages and disadvantages be?

## Commentary

A fair estimate is that a standalone Python version of OpenCull would require
roughly 600–1,000 lines of orchestration code, in addition to the existing
scanner and deterministic helper code.

At the time of this comparison, OpenCull contained:

| Component | Lines |
| --- | ---: |
| `opencull.kim` | 141 |
| `agents.kim` | 49 |
| Kimiya orchestration total | **190** |
| `scan.py` | 363 |
| `opencull_kernel.py` | 868 |
| `move_selected.py` | 155 |

The 136-line generated Python artifact is not the whole standalone
application. It imports Kimiya's runtime, which supplies model execution,
image observation, memoization, certification, checking, actions, and agent
configuration.

A conventional Python replacement would need to implement or assemble:

- OpenRouter HTTP requests;
- structured-output parsing;
- vision-image encoding;
- agent and credential configuration;
- Zero Data Retention routing;
- response validation and retry handling;
- persistent response caching;
- image-hash cache keys;
- cluster iteration and checkpointing;
- resume validation;
- cross-model voting;
- file-action safeguards;
- execution tracing;
- cost and disclosure accounting;
- final certification or audit reporting.

Using libraries could keep this around 600–1,000 lines. A careful
implementation of all Kimiya guarantees could easily exceed 1,500 lines.

## Advantages of Kimiya

The main advantage is not merely fewer lines. Kimiya makes the program's
epistemic structure visible:

```kimiya
photo := observe image(path)
reading := memo gen<FrameReading>(prompt, images=[photo]) by A
check valid_group_reading(reading, [candidate])
```

This directly expresses:

1. what was observed;
2. which agent made the judgment;
3. what output structure is required;
4. whether the reading may be reused;
5. which deterministic condition must hold afterward.

Other benefits include:

- agent declarations are separated from application logic;
- network and image egress are announced automatically;
- model outputs cannot silently bypass validation;
- `check`, `judge`, `act`, `settle`, `commit`, and `abstain` have explicit
  meanings;
- the compiler detects unguarded irreversible actions;
- exact-input memoization is built into the language;
- runs produce traces and certificates;
- the source resembles the reasoning architecture more closely than HTTP
  plumbing;
- models and providers can be changed primarily through `agents.kim`.

## Disadvantages of Kimiya

- It introduces a custom language, compiler, and runtime dependency.
- Contributors must learn unfamiliar syntax and semantics.
- Python IDEs, debuggers, profilers, and type checkers cannot directly
  understand the complete program.
- Some ordinary syntax is currently restrictive; for example, function calls
  must remain on one line.
- Much deterministic work still lives in Python extensions: 1,231 lines across
  `scan.py` and `opencull_kernel.py` at the time of this comparison.
- Errors can cross multiple layers: Kimiya source, generated Python, runtime,
  Python extension, or model provider.
- The ecosystem is presently much smaller than Python's.
- Packaging and deployment require shipping or installing Kimiya.
- A malicious or poorly audited Python extension could undermine guarantees
  suggested by the Kimiya layer.

## Assessment

For a basic photo sorter, standalone Python would be simpler.

For OpenCull's intended design—multiple nondeterministic agents, explicit
observations, privacy declarations, validated judgments, safe actions,
checkpointing, and auditable outcomes—Kimiya has a genuine conceptual
advantage. Its strongest value is that it turns the AI workflow's trust
boundaries into source-language constructs.

The important long-term test is whether more of the deterministic bridge can
become concise, safe Kimiya code without making the language complicated. If
that happens, OpenCull will become a particularly strong demonstration of why
Kimiya exists.
