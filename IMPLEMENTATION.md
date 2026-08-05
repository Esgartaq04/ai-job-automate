# AutoApply — Phase 1 implementation

`README.md` is the design doc. This file is what actually got built, where it
diverges, and how to run it.

Scope is **Phase 1** from README section 9: one user, Greenhouse only, manual
approval, minimal UI. Ingest → match → tailor → submit → confirm, end to end.

---

## Quick start

```bash
make install                      # package + dev extras + chromium
make db                           # postgres 16 + pgvector on :5432
cp .env.example .env              # set ANTHROPIC_API_KEY
autoapply init-db

autoapply load-profile docs/profile.example.json --email you@example.com
autoapply companies add "ExampleCorp" examplecorp     # board_token from the board URL

autoapply ingest                  # stage 1
autoapply match --email you@example.com               # stage 2
autoapply draft --email you@example.com --limit 3     # stage 3
autoapply serve                   # review at http://localhost:8000
autoapply submit --limit 3        # stage 4 — approved applications only
```

`make test` runs offline: no API key, no database, no network.

---

## Decisions taken during the build

Four questions came up that the design doc didn't settle. All four were answered
before writing code; recording them here so the reasoning survives.

| Question | Choice |
|---|---|
| Scope | Phase 1 vertical slice |
| LLM provider | Anthropic — Haiku 4.5 for match/field-mapping/classification, Opus 5 for tailoring and cover letters |
| Runtime | docker-compose for dev, Terraform for GCP |
| Submit mode | **Live**, gated on explicit human approval |

### Embeddings are a separate provider

Anthropic has no embeddings endpoint, so `AUTOAPPLY_EMBEDDING_PROVIDER` is
independent of the chat model:

- **`local`** (default) — deterministic hashed-lexical vectors, no key, no spend,
  works offline. Cosine over these is a *lexical* overlap signal, not a semantic
  one. It only has to keep the true match inside the top-k that the LLM precision
  pass then re-ranks, and for a few hundred postings it does.
- **`voyage`** — Voyage AI, for real semantic recall. Needs `VOYAGE_API_KEY`.

Switch to `voyage` once the job pool is large enough that lexical recall starts
dropping good matches. The stored dimension is `AUTOAPPLY_EMBEDDING_DIM` (1536,
matching the schema in the design doc); changing it means re-running migrations
and re-embedding.

### Live submit, and the guards around it

Submission is real. Everything that keeps that safe:

1. `submit` only ever reads applications in state `approved` (or `needs_input`
   after a human answered). There is no path from `drafted` to `submitting`.
2. Escalation before guessing: CAPTCHA, 2FA, account-creation walls, any required
   field mapped below `AUTOAPPLY_FIELD_CONFIDENCE_FLOOR`, and required free-text
   questions all raise and park the application in `needs_input`. Nothing is
   clicked.
3. Verification is mandatory. Clicked-but-unconfirmed lands in `failed`, not
   `submitted` — silent form failures are exactly what verification is for.
4. `AUTOAPPLY_DAILY_SUBMIT_CAP` (default 25) plus jittered pacing, per-domain
   token bucket, and a circuit breaker per source.
5. `AUTOAPPLY_DRY_RUN=true` fills and captures the form but skips the final
   click, leaving the application in `approved`. Useful against a real posting
   you don't want to actually apply to.

---

## Layout

```
src/autoapply/
  config.py         env-driven settings
  db.py             engine, session scope, migration runner
  models.py         ORM mirroring migrations/001_init.sql
  events.py         state machine + append-only run_events
  scrub.py          log scrubber (redacts on write)
  ratelimit.py      token bucket, backoff, circuit breaker
  storage.py        hash-addressed artifacts (local:// or gs://)
  llm/
    client.py       Anthropic wrapper: structured JSON, cost accounting, refusals
    embeddings.py   local / voyage providers, content-hash cache key
  ats/
    base.py         ATSAdapter protocol, Field, Escalation, SubmitResult
    fields.py       synonym dictionary + cached LLM fallback
    greenhouse.py   board API ingest + Playwright submit
  pipeline/
    ingest.py       stage 1
    match.py        stage 2 — hard filters, vector recall, LLM precision
    tailor.py       stage 3 — grounded generation loop
    validator.py    the deterministic verifier
    render.py       HTML → PDF through Chromium
    submit.py       stage 4
    outcome.py      stage 5
  api/main.py       REST + SSE
  api/static/       review UI
  cli.py            operator commands
```

---

## The parts worth reading

### Validator (`pipeline/validator.py`)

The load-bearing piece. A generated bullet is valid only if:

- it cites a `fact_id` that exists in the retrieved set,
- every number in it appears in that fact, and
- every proper noun, acronym or versioned token in it appears in that fact, its
  `org`/`role`, or its `skills` list.

Lowercase prose is the model's to rephrase freely; anything that looks like a
claim about the world is checked. Cover letters and the summary get the same
check against the union of retrieved facts, widened with the candidate's own name
and the employer's (a cover letter may name the company it's addressed to).

Fail → regenerate once with the violations fed back as a critique → fail again →
`pending_review` with the report attached, so a human sees exactly what was
rejected and why.

The classic failure this catches: the fact says "reduced latency", the model
writes "reduced latency by 40%". That's `NEW_NUMBER`, and it never ships.

### State machine (`events.py`)

`run_events` is append-only and authoritative; `applications.state` is a cache.
`replay_state()` recomputes from the log and is asserted in the integration test.
Transitions carry `application_id` in the event payload so the log stays
replayable even for runs that aren't scoped to one application (the outcome sweep
processes a whole mailbox in one run).

Illegal edges raise `IllegalTransition` rather than coercing — a bad edge means
caller logic is wrong and the run should fail loudly.

### Field mapping (`ats/fields.py`)

Heuristics first (label, `name`, `aria-label`, `placeholder`, enclosing `<label>`
against a synonym dictionary, longest match wins so "first name" beats "name"),
then the `field_map_cache` table, then one Haiku call whose answer is written
back. An unknown field costs one model call *ever* per `(ats, field_signature)`.

The model is asked what the field *is*, never what to answer. EEO/demographic
fields are recognized specifically so they can be left alone.

### Cost tracking

Every LLM call returns `cost_cents` computed from the model's published rates,
accumulated onto the `runs` row. `/api/stats` divides total spend by submitted
applications for a real cost-per-application number. System prompts are cached
(`cache_control: ephemeral`), so they bill at read rates after the first job in a
run.

---

## What is verified, and what is not

**Verified in this session** — 81 tests, plus a live Postgres 16 + pgvector 0.8:

- Migrations apply cleanly; all 12 tables created.
- `test_pipeline_integration.py` walks the whole slice against that database
  with the LLM and browser stubbed but every other code path real: vector recall
  query, state machine, artifact storage, adapter DOM handling, the
  `UNIQUE(user_id, job_id)` double-apply guard, and `replay_state`.
- A second integration test asserts an unresolvable required field ends in
  `needs_input` with nothing clicked.
- PDF rendering produces real PDFs through Chromium (45KB resume, 36KB cover).
- API endpoints return 200; auth returns 401 without a token, 404 for unknown IDs.
- 46 unit tests over the validator, state machine, hard filters, field mapping,
  dedup hashing, Greenhouse parsing, rate limiting, scrubbing, embeddings, render.

**Not verified** — needs your credentials or a real target:

- Any actual Anthropic call. Prompts, schemas and cost math are written but no
  live request was made.
- A real Greenhouse form. The adapter's selectors and confirmation markers are
  written from the public board structure and exercised against a faithful fake;
  the first live run should use `AUTOAPPLY_DRY_RUN=true` and you should read the
  captured screenshot before flipping it off.
- The Terraform. Written and internally consistent, never `plan`ned against a
  project. The VPC connector for private Cloud SQL IP in particular needs
  filling in.
- Voyage embeddings (no key available here).

---

## Deliberately not built (Phase 2+)

Per README section 9, left for later: Lever/Ashby/Workable adapters (the
`ATSAdapter` protocol and registry are in place — a new adapter is one file),
Gmail API watch (the classifier and matcher are done and take a `Message`; only
the transport is missing — Phase 1 feeds it from JSON), Temporal orchestration,
multi-user auth and per-user credential encryption (the `credentials` table and
KMS key exist, unused), and auto-submit for high-confidence matches.

The `credentials` table is created but nothing writes to it, and `require_token`
is a single shared token. Both are fine for one user and both need real work
before a second one.

---

## Operational notes

- **Log scrubbing runs on write.** `scrub()` is applied inside `log_event`, so
  emails, phone numbers, long digit runs and anything under a sensitive key name
  are redacted before they reach the database. Email domains survive because
  they're useful for debugging which ATS mailbox responded.
- **Artifacts are content-addressed.** Identical bytes reuse the row and the
  object. Screenshots and DOM snapshots are captured on both success and failure.
- **`AUTOAPPLY_CHROMIUM_PATH`** points at a specific Chromium binary when the
  ambient build doesn't match the pinned `playwright` wheel.
- **Ingest failures are per-board.** One dead board trips its own breaker and
  logs an error event; the sweep continues.
