# hermes-extras

Three small, self-contained ideas built on top of **Hermes Agent** — each one
solves a problem that shows up once an agent session gets long. They are shared
verbatim (de-personalised) so you can read the design or point an agent at this
repo and have it install/adjust them.

| Component | Problem it solves | Shape |
|---|---|---|
| [`context-advisor/`](context-advisor/) | The token/context footer lies: it measures against the raw window while compression actually fires at 50 %. This makes the footer truthful. | Python core + Hermes plugin |
| [`tool-output-cap/`](tool-output-cap/) | Tool results dominate a long prompt, so the context grows fast. This caps oversized tool results (head + marker + tail) before they enter context. | Single-file Hermes plugin + tests |
| [`noemem/`](noemem/) | An agent has no long-term memory. This is a full archival pipeline: session → structured archive → consolidated topic files → retrieval. | Python scripts (+ design doc) |

Each component is independent; you can take one and ignore the rest.

## What is Hermes?

These are add-ons for [Hermes Agent](https://hermes-agent.nousresearch.com/), an
agent harness whose plugin system exposes hooks such as `pre_tool_call`,
`transform_tool_result`, `post_api_request` and `transform_llm_output`. Two of the
three components are plugins; the third (`noemem`) is a set of standalone Python
scripts that run on a schedule and talk to Hermes' session database.

## How to use this repo (human)

Read a component's `README.md` + `design.md`/`DESIGN.md`. Start with
`context-advisor/design.md` — it is the most complete write-up and the reasoning
transfers.

## How to use this repo (agent)

Point your agent at the directory it needs and tell it to read `AGENTS.md`:

```
Read noemem/AGENTS.md and set up the pipeline for me.
```

Every component ships an `AGENTS.md` written *for an agent*: what it is, the
dependencies, the install steps, the exact verification command, and the known
failure modes. The agent does not need the surrounding conversation.

## Layout

```
hermes-extras/
├── context-advisor/   # truthful context-pressure footer
├── tool-output-cap/   # cap oversized tool results before they enter context
└── noemem/            # session → archive → memory pipeline
```

## Notes

- Paths are given as `~/hermes-extras/...`. Adjust to wherever you clone.
- The `noemem/` scripts use `HERMES_NOEMEM_HOME` (default `~/hermes-noemem`) to
  locate their data tree; see `noemem/AGENTS.md`.
- No secrets, no personal data. The `context-advisor`/`tool-output-cap` code is
  original; `noemem` is original except where a file says otherwise.
- License: MIT (see `LICENSE`).
