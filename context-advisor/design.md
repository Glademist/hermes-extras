# Context-Advisor — design

**Status:** prototype built and verified 2026-10-07 (see §9). Not enabled in the gateway.
**Author:** Noemi (Hermes agent).
**Scope:** problem 2 of 2 (the context-advisory + pre-compress safety net). Split out of the
footer-measurement thread.

---

## 1. The symptom

The Discord footer an agent appends to each reply reads:

```
Ctx: 315K / 1M
```

`/1M` is the raw model context window (`deepseek/deepseek-v4.1-flash` @ nous = 1,048,576).
It reads as "315K of 1M — plenty of room." It is misleading, for two independent reasons:

1. **The denominator is wrong.** Nothing compresses at 1M. The agent's own compressor fires at
   **50%** (`compression.threshold`), i.e. 524,288 — and a between-turn gateway safety net fires
   at **85%**. The *effective* ceiling is ~524K, so 315K is not "32% safe," it is **60% of the
   real budget**.
2. **The numerator is a different measurement than `/compress` shows.** Run `/compress` and the
   numbers disagree with the footer, because they are three different figures (§4). the user
   (rightly) distrusts `/compress` when the footer implies safety and the command says otherwise.

The goal is a footer that tells the truth about *pressure* — measured against the effective
budget, not the raw window — and, near a boundary, a recommendation plus a safety net.

---

## 2. Ground truth — the three measurement layers

Hermes does not have "a" context measurement. It has three layers, each comparing a different
figure against a different denominator. All citations are to the live tree at
`~/.hermes/hermes-agent/` on 2026-10-07.

### 2.1 Agent compressor — fires at 50%

- `agent/context_compressor.py::threshold_tokens` = `context_length * threshold_percent`.
- `compression.threshold: 0.5` (`~/.hermes/config.yaml:150`) → **524,288** for the 1M model.
- The figure it compares is **usage-anchored**: `agent/turn_preflight.py:274-289` prefers
  `anchored_context_tokens(messages, agent._usage_anchor)` first, then `last_prompt_tokens`,
  then a rough estimate. `_compressor.should_compress(_real_tokens)` at `:299`.

### 2.2 Gateway session hygiene — fires at 85%

- `gateway/run_turn.py:609` hard-codes `threshold_pct=0.85`, with the explicit comment
  (`:604`) that it is "deliberately HIGHER than the agent's compressor (0.50): a safety net for
  sessions that grew between turns."
- `:671` `_compress_token_threshold = int(_hyg_context_length * 0.85)` → **891,289**.
- Its figure (`:683-694`): `session_entry.last_prompt_tokens` if > 0, else
  `persisted_anchor_tokens(...)` from the session row, else `estimate_messages_tokens_rough`.

### 2.3 Message valve — fires at `hygiene_hard_message_limit`

- `:698` `_needs_compress = _approx_tokens >= _compress_token_threshold or _msg_count >= hs.hard_msg_limit`.
- `compression.hygiene_hard_message_limit: 400` (`~/.hermes/config.yaml:154`). The code default is
  5000, but the config pins **400**.
- This axis is a **hard safety valve**: it bypasses the token check entirely, so a session can be
  forced into compression on message count alone while tokens look comfortable.

### 2.4 The effective budget

```
window            = 1,048,576            (context_length_cache.yaml)
compress budget   = window * 0.50 = 524,288   ← earliest event = THE budget
hygiene ceiling   = window * 0.85 = 891,289   (last resort)
message valve     = 400 messages
```

**Effective token budget = the compressor threshold (524K)**, because that is the *earliest*
thing that fires. The hygiene ceiling only matters if the agent loop never hit 50% mid-turn.

---

## 3. Worked numbers (measured live, 2026-10-07)

`tools/probe.py` runs the real pipeline against the session DB. Selected rows:

| session (title) | msgs | anchored | new footer |
|---|---|---|---|
| `Předělat a sjednotit web pro děti` | 214 | 479K | `Ctx: 91% · Msg: 54% · ⚠ handoff + /compress recommended` |
| `Vybrat další předmět pro webový deploy` | 323 | 315K | `Ctx: 60% · Msg: 81% · ⏳` |
| `morning-briefing` (cron) | 29 | 39K | `Ctx: 7% · Msg: 7%` |

The second row is the whole argument: a live session sitting at **91% of the real auto-compress
budget** read as `479K / 1M` — "less than half." It was one short turn from an unplanned
compression, and the footer said everything was fine.

---

## 4. Why the footer, `/compress` and `/context` disagree

They use three different figures. None is "wrong"; they answer different questions.

| surface | figure | code | denominator | what it means |
|---|---|---|---|---|
| old footer | `usage.prompt_tokens` of the **last** API call | `plugins/context-footer/__init__.py` via `post_api_request` | raw window (1M) | exact size of the last request *on the wire*; on reasoning models inflated by replayed thinking |
| `/context`, auto-compress | **usage-anchored** (turn-base anchor + delta) | `agent/context_breakdown.py:152-156`, `agent/turn_preflight.py:274-289` | raw window | provider-exact + what was appended since = "context going into the next turn" |
| `/compress` (before) | `estimate_request_tokens_rough` | `agent/conversation_compression_manual.py:58` | — | pure local `chars/4` estimate of system prompt + messages + tools; **runs 30–50% high** (stated in `gateway/run_turn.py:677`) |

Two consequences:

- **Footer vs `/context`:** the footer shows the *last call's* raw count; `/context` shows the
  *turn-base anchored* count. For a reasoning model the last call re-includes thinking that
  evaporates at the turn boundary, so the footer can read **higher** than `/context`.
- **Footer vs `/compress`:** `/compress`'s "before" number is a *local estimate* that runs high,
  while the footer is *provider-exact*. So `/compress` can look bigger than the footer — that is
  not a bug, it is a different yardstick.

The fix is not to make them equal (they can't be, by construction) but to make the **footer show
the anchored figure the compressor actually triggers on**, and to label its provenance so no
number over-claims precision.

---

## 5. The truthful pressure model

Two axes, both real, combined into one reading:

```
token_ratio = anchored_tokens / (window * compress_ratio)
msg_ratio   = message_count  / hygiene_hard_message_limit
driver      = "messages" if msg_ratio > token_ratio else "tokens"
ratio       = max(token_ratio, msg_ratio)
```

The driver is *whichever axis is nearer its ceiling*, because the message valve fires
independently of tokens.

Levels (cut points chosen so the footer stays quiet until a decision is worth making):

```
ok            ratio <  0.60
approaching   0.60 <= ratio < 0.85      informational marker only
near          0.85 <= ratio < 1.00      carries the recommendation
imminent      ratio >= 1.00             compression is happening now
```

Rendered line — both axes as full-percent figures against their real ceiling:

```
Ctx: <ctx%> · Msg: <msg%>[ · <mark>][ handoff + /compress recommended]
```

`<ctx%>` is token pressure against the **effective** budget (`window * compress_ratio`), not
the raw window; `<msg%>` is the message-hard-limit axis. `<mark>` is `⏳` (approaching), `⚠`
(near) or `🔴` (imminent); the recommendation rides the line only at `near`/`imminent`. A
leading `~` on `<ctx%>` marks a **local estimate**. The figures are the true integer percents —
never bucketed — so the footer and the verification log print the same numbers.

### 5.1 Figure selection, and the fingerprint caveat

A plugin's `post_api_request` gives `session_id`, `usage`, and `message_count` — but **not the
agent object**, so it cannot read `agent._usage_anchor` directly. The anchored figure is instead
read from the session row the same way gateway hygiene does it
(`gateway/run_turn.py:684-688` → `agent/usage_anchor.py::persisted_anchor_tokens`), by loading the
transcript with `SessionDB.get_messages_as_conversation` and letting the anchor's fingerprint
decide whether it is still valid.

**This is best-effort by design.** `persisted_anchor_tokens` returns `None` when the DB-loaded
transcript no longer matches the anchor's `base_last_fp` (compaction / splice / rewind, or a
content round-trip difference). Every path in `advisor/session_db.py` therefore **fails open** to
the raw `prompt_tokens` (a safe upper bound on pressure), then to nothing.

**Sharper diagnosis (2026-10-07, `probe_anchor.py`).** The miss is not random: it correlates with
**active** sessions. For idle/closed sessions the anchor matches exactly (`base_last_role=tool`,
fingerprint equal). For sessions with an in-flight turn (this one, and the footer thread) the
anchor's `base_count-1` slot still reads `role=tool` from capture but the DB-reloaded transcript
shows `role=assistant` at that index — a **role/index shift** from how the reload reconstructs
tool-call/tool-result rows. So the failure is a *positional* mismatch in the reloaded transcript,
not corruption. The `provider` fallback keeps the footer correct in denominator and message axis
regardless; only the numerator's precision degrades.

**Decision (2026-10-07, `probe_loaders` in `tools/loader_probe.py`).** Tried every transcript
loader (`get_messages_as_conversation`, `repair_alternation`, `include_compacted`, `get_messages`)
— **none** recovers a match for the active sessions, and `include_compacted` breaks even the idle
ones (it adds superseded generations, shifting the base index). So the mismatch is not a loader
choice; it is a genuine live-vs-persisted transcript divergence during a turn. Consequence: the
anchored figure is only valid for **idle** sessions — the ones that need no warning — while the
sessions that *do* need a footer get the fallback anyway. **The plugin therefore uses the hook's
raw provider `prompt_tokens` directly and does not read the session DB at all** (no per-turn
query, no fingerprint risk). `advisor/session_db.py` remains for offline probes; with output
rounded to the nearest 5, the raw figure's reasoning-inflation is immaterial.

---

## 6. Pre-compress safety net (design)

The complaint behind this: the user is "rightly scared to run `/compress` without knowing whether the
summary will drop key info." Three parts.

### 6.1 The invariant that constrains everything

Per `agent/AGENTS.md`: **prompt caching is sacred, and `/compress` is the ONE sanctioned
cache-break.** Any design that re-injects context mid-conversation creates a *second* break and is
out of bounds.

### 6.2 The elegant consequence

A handoff file does **not** have to cost an extra cache break — *if it is folded into the
compression event itself*. `/compress` already replaces the prefix and pays the cache-write once.
If the handoff content rides the same event (prepended to the compacted `after_messages`, or
written into the new session's first user message *as part of the compression commit*), it is
covered by the one permitted break. Cost of the handoff then reduces to **input tokens for the
handoff text for the turns until it is cached** — i.e. near-zero marginal cache cost. The
compressor already does something structurally identical: it protects `protect_last_n: 20` and
injects a structured summary (`compression.target_ratio: 0.2`).

**Caveat — this part is not a plugin's to do.** A plugin cannot install `after_messages`; that is
the caller of `compress_now` (`agent/conversation_compression_manual.py:73`), i.e. core/gateway
code. So handoff-injection-within-the-break is a small **core** change, not a plugin feature. This
is the single most important architectural finding here, and it should be decided explicitly
before building the safety net.

### 6.3 What the plugin *can* do without touching core

1. **Advise** (this prototype): surface pressure + recommendation so `/compress` is a deliberate
   act at a known boundary, not a leap.
2. **Write the handoff file before `/compress`.** A `pre_tool_call`/slash-adjacent path can dump a
   compact "state of play" (open threads, decisions, file paths, next steps) to
   `~/hermes-noemem/...` *before* compression runs, so nothing is lost even if the auto-summary
   drops it. Writes are non-destructive. The file is then available to the user and to a future
   session via normal file tools — no re-injection, no cache cost.
3. **Judgment (optional, model-delegated).** Ask a cheap model "would this summary lose key info?"
   by diffing the pre-compress handoff against the post-compress summary. This is a *review*
   step, not a context mutation, so it is cache-safe. It should run off the turn (background) and
   report to chat.

### 6.4 Cache-cost quantification

For the two designs:

- **Handoff folded into the compression commit (preferred):** zero extra cache events. The new
  prefix (summary + handoff) is written once — the same write `/compress` already pays — then read
  from cache for every later turn until `prompt_caching.cache_ttl` (`5m`) / `long_lived_ttl`
  (`1h`) expires. Marginal cost ≈ `len(handoff) * input_price` for one turn.
- **Handoff re-injected as a standalone mid-conversation user message (avoid):** the cached prefix
  is invalidated → full re-write of the whole prefix + handoff, billed at input rate instead of
  cache-read rate for that turn, plus a cache-write surcharge. For a 100–500K prefix this is the
  expensive option by an order of magnitude, and it violates the "one cache break" invariant.

Exact per-token prices (input / cache-read / cache-write) for the nous route are **not** in the
tree; the cost delta must be computed against the provider's published table once confirmed. The
*shape* of the trade-off is settled: fold the handoff into the compression event.

---

## 7. Architecture & layout

House pattern: project code lives OUTSIDE `~/.hermes` (`~/AI/...`); Hermes gets a thin layer.

```
~/AI/context-advisor/                ← the project (logic, tests, tooling)
├── advisor/
│   ├── core.py         pure: Budgets, Snapshot, Pressure, build_pressure, level_for
│   ├── render.py       footer/advisory line (percentages) + config-backed budgets
│   ├── session_db.py   anchored-figure reader (offline probes only; not in the hot path)
│   └── __init__.py
├── tests/
│   ├── test_model.py   pure core + render (10 tests, real fixtures)
│   └── test_plugin.py  plugin handlers: replace-vs-delta, gating, idempotency (6 tests)
├── tools/
│   ├── probe.py          live pipeline against the real session DB
│   ├── anchor_probe.py   why the anchor misses (active vs idle sessions)
│   └── loader_probe.py   does any loader fix the anchor match? (no)
└── design.md           this file

~/.hermes/plugins/context-advisor/   ← the thin shim (enabled 2026-10-07)
├── plugin.yaml
└── __init__.py         imports ~/AI/context-advisor/advisor; wires 3 hooks
```

The shim adds `~/AI/context-advisor` to `sys.path` and does nothing else of its own. All
budget math, formatting and provenance live in the project, so they are unit-testable and
reviewable without a gateway.

### 7.1 Hooks

- `post_api_request` — capture `usage.prompt_tokens`, `message_count`, `model`, `base_url`,
  `turn_id`. Window from `agent.model_metadata.get_cached_context_length`.
- `transform_llm_output` — gate `platform == "discord"`, suppress live voice chats, build the
  pressure reading, append. Runs in `turn_finalizer.py` **after** `_persist_step()`, so nothing
  lands in history and the cache is untouched. Discord is not a `draft_stream_is_message` adapter
  (config: `display.platforms.discord.streaming: false`), so no sealed native stream is reopened.
- `on_session_end` — evict the ledger and caches.

---

## 8. Enabling — DONE 2026-10-07

Enabling required **all** of: the manifest, the name in `plugins.enabled`, and a gateway restart.
Executed:

```bash
cp ~/.hermes/config.yaml ~/.hermes/backups/config/config.yaml.pre-context-advisor.<ts>
hermes config set plugins.enabled '[...]'   # dropped context-footer, added context-advisor
# gateway restart is queued; see note below
```

The swap removed `context-footer` and added `context-advisor` atomically (both append a footer,
so both enabled = two footers). The gateway restart is **deferred, not forced**: the daemon
`we are running inside` finishes the in-flight turn before restarting
(`gateway.run: Restart deferred: waiting on 1 active work unit(s)`), so the new plugin loads on
the next gateway process. The daemon runs as `UserName: <your-user>`, so `~/AI/...` resolves correctly.

---

## 9. What is built and verified (2026-10-07)

- `advisor/` core + render + session_db — importable, dependency-free (stdlib + optional `yaml`).
- `tests/test_model.py` — **10/10 pass** (standalone and pytest).
- `tests/test_plugin.py` — **6/6 pass** — proves: the footer is appended (replace, not delta),
  re-entrant calls do not double it, non-Discord platforms are gated out, live voice is
  suppressed, and missing/malformed usage fails open.
- `tools/probe.py` / `anchor_probe.py` / `loader_probe.py` — exercise the *real* session DB.
- Config swap performed; `plugins.enabled` now holds `context-advisor` and no longer
  `context-footer`. Gateway restart queued (deferred behind the running turn).

Sample real render (Discord session, 214 msgs, anchored 479K of the 524K budget):

```
Ctx: 90% · Msg: 55% · ⚠ handoff + /compress recommended
```

### Not yet done (needs a decision)

- The pre-compress handoff writer + lossiness judgment (§6.3) — designed, not built; the user
  prefers to do handoffs by hand, via a dedicated skill (`session-handoff`).
- The handoff-folded-into-compression core change (§6.2) — the correct fix, but it is core and
  is deliberately out of scope for now.

---

## 10. Open questions / risks

1. **Anchor coverage — RESOLVED as "don't use it".** Idle sessions match; in-flight sessions
   shift, and no loader fixes it (`tools/loader_probe.py`). The plugin uses the raw provider
   figure directly; `session_db.py` is offline-only. See §5.1.
2. **Reasoning inflation.** The raw provider `prompt_tokens` can over-report on a reasoning
   model. Over-reporting is the *safe* direction (fires the recommendation early), and output is
   rounded to the nearest 5, so the residual is immaterial.
3. **Per-turn cost — none.** No session-DB read in the hot path (removed with #1).
4. **Where the handoff lives.** To be decided with the `session-handoff` skill; it affects the
   H1 memory-write hook (writes to `memory/` are guarded in this deployment).
5. **Multiple footers.** Resolved — the swap removed `context-footer` in the same edit.

---

## 11. Alternatives considered

**A. Patch the existing `context-footer` in place.** Smallest diff — it already captures
`prompt_tokens` and the window. Rejected: it has no budget model and no provenance, so the fix
would be "add a denominator" sprawled across the handler. Keeping the logic in a separate,
unit-testable project (and a thin shim) is the house pattern and makes the pressure model
reviewable on its own.

**B. Have the footer import `/context`'s exact payload (`compute_session_context_breakdown`).**
That function needs the **agent object** (`agent.context_compressor`, `agent.tools`,
`agent._turn_base_usage_anchor`). A plugin's `post_api_request` never receives it. Rejected for the
plugin path; the anchored figure is instead reconstructed from the session row (§5.1), which is
the same source gateway hygiene uses.

**C. Change the footer inside core.** Rejected on footprint grounds: the footer is a per-deployment
nicety, and the core is a narrow waist. Per the Footprint Ladder, this is a rung-4 **plugin**, not
a core change. (The one exception is the pre-compress handoff injection, §6.2, which genuinely
*is* core — it has to install `after_messages`.)

**D. MCP server.** Rejected: there is no external service. This is a render hook firing in the
agent loop, which is exactly what plugins exist for.

**E. Provider figure only (no session-DB read).** Chosen. The loader probe showed the anchored
figure is unavailable for exactly the active sessions a footer is for, so a per-turn DB read buys
nothing; the raw provider `prompt_tokens` is always available and, at nearest-5 rounding, its
residual inflation is immaterial.

---

## Appendix A — raw verification log (2026-10-07)

Core + renderer:

```
PASS  test_budgets_effective_is_compressor_threshold
PASS  test_budgets_rejects_incoherent_knobs
PASS  test_fmt_tokens
PASS  test_level_boundaries
PASS  test_live_session_is_quiet
PASS  test_message_valve_can_be_the_binding_axis
PASS  test_message_valve_fires_at_limit
PASS  test_provenance_is_surfaced
PASS  test_web_session_is_near_the_real_budget
9/9 passed
```

Plugin handlers:

```
PASS  test_bad_usage_is_ignored
PASS  test_fail_open_without_usage
PASS  test_footer_appended_and_replaces_text
PASS  test_idempotent
PASS  test_platform_gating
PASS  test_voice_suppressed
6/6 passed
```

Live pipeline against the real session DB (`tools/probe.py`):

```
window=1M  compress_budget=524K  hygiene_ceiling=891K  msg_valve=400

Předělat a sjednotit web pro děti      n= 214
    new: Ctx: 91% · Msg: 54% · ⚠ handoff + /compress recommended
TeleOCR vs Gemma A/B comparison · Oct  n=  56
    new: Ctx: 9% · Msg: 14%
morning-briefing · Oct 07 05:46        n=  29
    new: Ctx: 7% · Msg: 7%
Redesign AI/Teacher web presentation c n= 222
    new: Ctx: 20% · Msg: 56%
Vybrat další předmět pro webový deploy n= 323
    new: Ctx: 60% · Msg: 81% · ⏳
```

Config after the swap:

```
plugins.enabled = ['approval-announce', 'context-advisor', 'memory-write-guard',
                   'prompt-guard', 'qmd-retrieval-telemetry', 'rtk-rewrite', 'voice-auto-join']
```

Backup: `~/.hermes/backups/config/config.yaml.pre-context-advisor.20261007-193013`

