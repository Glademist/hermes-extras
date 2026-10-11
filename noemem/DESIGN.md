# noemem — design

Long-term memory for a Hermes agent. The agent's chat sessions are the raw signal;
`noemem` turns them into **durable, structured memory** on a nightly schedule, and
makes that memory retrievable during future sessions.

It is opinionated, and most of the value is in *why* each rule exists. Treat this
document as the reasoning; the code is the implementation.

---

## 1. The problem

A Hermes session is long-lived: there is no auto-timeout, so a channel can be a
single conversation running for months. Three consequences:

1. **Context is finite.** Old turns are eventually compressed or dropped, so facts
   agreed weeks ago silently leave the window.
2. **Recall is unreliable.** Ask "what did we decide about X?" three weeks later and
   the answer lives nowhere — unless it was *written down and indexed*.
3. **A flat archive is not memory.** Dumping every session into one growing file
   makes retrieval worse over time, not better.

`noemem` answers with a pipeline: **session → structured archive → consolidated
topic memory → retrieval**, run every night, with verification at each step.

---

## 2. The four layers

| Layer | What it holds | Written by | Lifetime |
|---|---|---|---|
| Mission | where *we* want to go (user-owned) | **user only**, never the agent | months–years |
| **Memory** (`memory/*.md`) | current state of a topic | consolidation / explicit writes | weeks |
| **Verdicts** (`memory/verdicts.md`) | one line per trial→outcome ("why we left it") | consolidation | years |
| **Archive** (`archive/*.md`) | episodic history — one record per session | the Dreamer | permanent |

The single most important rule: **`memory/*.md` is CURRENT STATE ONLY, and every
state claim carries a date** (e.g. `verified 2026-10-01`). History — past
experiments, dead ports, old migrations — belongs in `archive/`, never in a topic
file. A topic file that accumulates history becomes unreadable and, worse, cannot be
superseded, so stale facts win retrieval forever.

A hand-maintained `memory/_index.md` is the topic→file map (with aliases) — one
place to look.

---

## 3. The Dreamer — session → archive

`runtime/scripts/dreamer_v2.py`. Input: Hermes' `sessions.json` plus per-session
content snapshots. For each new/changed session it asks an LLM to produce a
structured record and writes `archive/{date}_{project}_{topic}.md`.

### 3.1 The editor framing (why the archive stopped being an inventory)

A historian prompt produces *factually correct noise*: pip/venv trivia, approval-gate
failures, test-run timings — inventory, not memory. Adding more ignore-rules did not
fix it. The fix was the **persona plus one question**:

> You are the EDITOR of a long-term memory archive. Every sentence must pass "will
> this still be true and useful *next month*?"
> `vysledek` (outcome) = END STATE only — decisions, state changes, durable handles
> (paths, ports, crons, numbers, deadlines). A failure is memory *only* if the failed
> approach is itself a durable lesson. `ponauceni` (lesson learned) may honestly be
> "none".

### 3.2 Three prompt paths, not one

A session travels through one of three prompt paths depending on size:

- short sessions → `prompt_variants.py::tiered_anchor_fidelity`
- normal sessions → `dreamer_v2.py::build_prompt` + `DREAMER_SYSTEM`
- XL sessions (≥ ~10 K chars) → `multipass_dreamer.py` (extract pass, then write pass)

**A prompt change must patch all three, or part of the traffic silently keeps the
old text.** This is the single easiest way to make a change look like it "did not
work" — you edited one path, and read the output of another.

### 3.3 Skip gates (two of them, both required)

Re-archiving the same session every night is the classic failure. Two gates:

- **Content clock.** Compare the stored `processed[key]` against a *content*
  timestamp, **not** the channel's `updated_at` (which advances on any activity in
  the channel and would re-archive a frozen session forever).
- **Digest.** `processed[key:digest]` = sha256 of the last **user** message. A bot or
  cron reply appends an assistant message and moves the clock without any human
  input; the digest catches "nothing actually changed". Hash the last *user* role
  message, not the last message.

State is saved after **every** session, so a timeout loses no work.

### 3.4 The project guard (deterministic, after the LLM)

The LLM's project field is unreliable. `projekt_utils.resolve_project()` runs at the
single choke point before saving, in this order:

1. exact catalog slug → keep;
2. `NEW: <slug>` → keep as a reviewable proposal (never auto-approved);
3. empty / off-catalog / a copied example → map by the catalog's own keywords;
4. nothing fits → `unclassified`, **visible**, never a silent fallback.

Two rules that cost real time: test the **order** (the placeholder check must run
before the "looks like a slug" check, or a copied example is accepted as a genuine
proposal), and keep keywords *inside* the catalog definitions — a second mapping
table drifts out of sync with the slugs it describes.

### 3.5 Anchors force facts verbatim — and that cuts both ways

To keep records accurate, the prompt injects "mandatory details" (paths, ports) that
must appear verbatim in the outcome. This fails in two directions:

- If the *anchor extractor* accepts transient tooling tokens (`.venv`, `pip`,
  `python3.x`, `/tmp/`), it mechanically reintroduces the install trivia the editor
  prompt excluded.
- If the extractor **normalises** a string (e.g. lowercases a path) before injecting
  it, the model copies the normalised form, and a wrong-case path lands in the
  record. Normalise for *comparison* only; never in the value you inject.

Symptom class worth remembering: **the same corruption across different models is a
shared-input bug, not a model tendency.** Check the pre-LLM stages first.

---

## 4. Verification and quarantine

`archive_lint.py` is a pure-code linter (no LLM) run on each new archive: truncated
backticks, literal `\n` in code spans, empty outcome/lesson fields, wrong-case paths,
project-vs-catalog. `archive_verify.py` turns a lint failure into a
`retrieval_tier: quarantine` banner — and quarantined records are excluded from both
consolidation and retrieval.

Two lessons from the lint's own bugs, both about *detectors*:

- **An unfireable check and a clean corpus produce identical logs.** A field-parsing
  regex used `\s*` for the separator; under `re.MULTILINE`, `\s*` ate the newline, so
  an *empty* field captured the next line and passed the "non-empty" test. A validator
  that has never fired is suspect before the corpus is congratulated.
- **A detector for defect class X must be able to extract X.** A wrong-case-path check
  written so that it lowercases both sides can never fire.

Quarantine is not "delete": release is a human/agent pass that verifies each cited
anchor (path, line) against the real file, then drops the banner. Regenerate only
when the *content* is wrong, not just the anchors.

---

## 5. Consolidation — archive → memory

`memory_consolidate.py` (nightly) routes the day's valid archives into facts and
rewrites the touched `memory/*.md` files. This is where state accumulates.

Hard-won rules:

- **`memory/*.md` must never be overwritten with the model's own reasoning text.**
  A merge once replaced a topic file with "Let's plan: …". Guard the write: reject
  reasoning markers, require a `# ` title and a `## ` section. (Use `"\nhmm"`-style
  markers, never a bare `"hmm"` — real content can contain it.)
- **Budgets must have one home.** A file-size cap defined in two modules will
  disagree (`6000` here, `14000` there), so a file can be simultaneously "fine",
  "refused growth", and "an alert". One constant, imported everywhere.
- **A guard and the prompt it guards can be mutually unsatisfiable.** A merge floor
  ("reject output shorter than 50 % of the current file") contradicts an instruction
  to *compress* a file over budget: for a 25 KB file the compliant answer is ~5 KB
  but the floor demands ~12.5 KB, so the file freezes forever while the self-check
  reports "over budget". A floor derived from a quantity the prompt asks the model to
  reduce must be clamped to a small absolute value.
- **The write path is sanctioned, not free.** `memory_write.py` (append one bullet,
  auto-stamps the date) and `memory_rewrite.py` (whole-file repair, dated backup)
  are the only writers; a hook blocks everything else. The hook is **fail-open by
  design**, so a daily self-check is the actual safety net, not the hook.

`verdicts.md` records big, durable trial→outcome lines only — never a one-off event,
a tool error, or an approval denial.

---

## 6. Retrieval — QMD

Memory and archive are indexed by [QMD](https://github.com/…) as separate
collections (vector + BM25). Two design points:

- **Query cost is dominated by `candidateLimit`, not model loading.** Measured on a
  warm daemon, identical hits: default `candidateLimit: 40` with rerank ≈ 3.3 s vs
  `candidateLimit: 10` ≈ 0.08 s. Pass a small candidate limit on every agent-side
  query before blaming the index.
- **Superseded content poisons vector retrieval.** "X is dead" and "X is live" are
  nearly identical vectors, so a live/dead pair in one index can surface either. The
  direction taken here: keep a *current* index (live files) for automatic injection
  and a separate full-text history index for explicit lookups.

Auto-injection (a "prefetch" that runs before each turn) is a trade: it costs tokens
on every turn, and an injected topic file labelled "current" can be days stale. When
it is off, the awareness line must say so and tell the agent to search explicitly —
otherwise the archive is invisible.

Telemetry (`logs/qmd-retrieval-*.jsonl`) records both prefetch and explicit tool
calls. **Before concluding "retrieval found nothing", check the sink is still being
written** (`stat`/`lsof`). A dead logger and a dead feature look identical.

---

## 7. What to take from this

If you build your own, the transferable ideas are:

1. Separate **episodic archive** from **current-state memory**; put a date on every
   state claim and keep history out of state files.
2. Frame the LLM as an **editor with one question**, not a historian with a template.
3. Make **project/classification decisions deterministic after the LLM**, at one
   choke point, and make "unknown" a visible value rather than a silent fallback.
4. **Verify with pure code** (a linter) and quarantine rather than delete.
5. Treat your own checkers as suspect: **a check that never fires may be unable to
   fire.**
6. **Measure** the retrieval stack (candidate limits, sink liveness) before blaming it.
