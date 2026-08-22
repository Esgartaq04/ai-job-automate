# AutoApply — implementation notes

`README.md` is the design doc. This file is what actually got built, where it
diverges, and how to run it.

**Phase 1** (README §9): one user, Greenhouse only, manual approval, minimal UI.
Ingest → match → tailor → submit → confirm, end to end.

**Phase 2** (in progress): Lever and Ashby adapters, and the Gmail transport for
outcome tracking. The review queue, SSE log viewer, state machine, artifacts and
screenshots that §9 also lists under Phase 2 all landed in Phase 1.

---

## Quick start

```bash
make install                      # package + dev extras + chromium
make db                           # postgres 16 + pgvector on :5432
cp .env.example .env              # set ANTHROPIC_API_KEY
autoapply init-db

autoapply load-profile docs/profile.example.json --email you@example.com
autoapply companies import docs/companies.example.json   # one board per ATS
# or one at a time — board_token is the slug in the board URL:
autoapply companies add "ExampleCorp" examplecorp --ats-type lever

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
  models.py         ORM mirroring migrations/*.sql
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
    outcome.py      stage 5 — classify + match, idempotent
    gmail.py        stage 5 transport — OAuth, fetch, MIME decode
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

**Verified** — 128 tests, plus a live Postgres 16 + pgvector 0.8 and live board APIs:

- Migrations apply cleanly; all 13 tables created.
- `test_pipeline_integration.py` walks the whole slice against that database
  with the LLM and browser stubbed but every other code path real: vector recall
  query, state machine, artifact storage, adapter DOM handling, the
  `UNIQUE(user_id, job_id)` double-apply guard, and `replay_state`.
- A second integration test asserts an unresolvable required field ends in
  `needs_input` with nothing clicked.
- PDF rendering produces real PDFs through Chromium (45KB resume, 36KB cover).
- API endpoints return 200; auth returns 401 without a token, 404 for unknown IDs.
- Unit tests over the validator, state machine, hard filters, field mapping,
  dedup hashing, rate limiting, scrubbing, embeddings, render, the shared form
  driver, and Gmail MIME decoding.
- The Lever and Ashby tests parse **real captured API responses** (trimmed), not
  hand-written shapes.
- Live ingest through all three adapters: **501 real postings** from
  `greenhouse/anthropic`, `lever/ro` and `ashby/ashby` into a real database in
  6.5s, every one embedded, and a re-run correctly dedupes to 0 new / 501 dup.
- The hard filters, run over those 501 real descriptions, block **54%** before
  any model call — the README's "kills 60% for free" claim, roughly borne out on
  live data. Years-required parsed cleanly from 266 of them.
- Migration 002 applies as an upgrade to an existing 001 database without data
  loss, and `init-db` stays idempotent.

**Not verified** — needs your credentials or a real target:

- Any actual Anthropic call. Prompts, schemas and cost math are written but no
  live request was made.
- Any real application form, on any of the three ATSes. Ingest is verified
  against live APIs; **submission is not**. Selectors and confirmation markers
  are written from the public form structure and exercised against faithful
  fakes. The first live run on each ATS should use `AUTOAPPLY_DRY_RUN=true`, and
  you should read the captured screenshot before flipping it off.
- The Gmail transport end to end. The MIME decoding is well covered, but the
  OAuth flow and the live `users.messages` calls need your credentials.
- The Terraform. Written and internally consistent, never `plan`ned against a
  project. The VPC connector for private Cloud SQL IP in particular needs
  filling in.
- Voyage embeddings (no key available here).

---

## Phase 2 — what landed

### Three ATSes, one form driver

`ats/form.py` holds the Playwright half that every board shares: guard, read the
controls with their labels, refuse to guess, fill, click, verify. An adapter now
supplies only its ingest call and its selectors/confirmation wording. Greenhouse
was refactored onto it; Lever and Ashby are ~90 lines each.

Their ingest shapes differ more than the design doc implies:

- **Greenhouse** returns the whole description as one HTML-escaped blob.
- **Lever** splits a posting across `descriptionPlain`, an ordered set of `lists`
  sections (*What You'll Do*, *What You'll Bring*), and `additionalPlain`.
  Matching on the intro alone would score against company boilerplate with every
  actual requirement missing, so `parse_job` reassembles the posting. Lever also
  reports `createdAt` in epoch **milliseconds** and carries an authoritative
  `workplaceType` — a posting whose location string reads "New York, NY or
  Remote" is `workplaceType: remote`, and trusting the string would get it wrong.
- **Ashby** is the cleanest: real `isRemote` and `secondaryLocations` fields, so
  geography doesn't have to be inferred from free text. Unlisted postings
  (`isListed: false`) are drafts and are skipped.

Submission caveat: the Ashby application form is a client-rendered SPA whose
file upload and custom questions are React-controlled. Label-driven mapping
handles the standard name/email/resume fields, but expect a higher escalation
rate there than on Greenhouse. That is the designed behaviour, not a failure.

### Gmail transport

`pipeline/gmail.py` is transport only — the classifier and matcher were already
done and take plain `Message` objects. Scope is **`gmail.readonly`**: it never
sends, deletes, or modifies mail.

`parse_message` is a pure function over a Gmail API message resource, so nested
multiparts, unpadded base64url, and HTML-only mail are all tested without
credentials or network. HTML-only is the common path, not an edge case — most
ATS notifications have no text/plain part.

Idempotency is a new `processed_messages` table (migration 002) rather than
mutating the mailbox. The Gmail query is time-windowed, so consecutive runs
re-fetch the same mail; without the guard a second pass would re-bill the
classifier and try to re-transition an application that has already moved on.
Messages are skipped **before** the model is called. `--reprocess` forces them
through, and the state machine still refuses the illegal re-transition.

Google's libraries are an optional extra (`pip install -e ".[gmail]"`) and are
imported lazily, so everything else installs and tests without them.

---

## Still not built (Phase 3+)

Per README §9: Temporal orchestration, multi-user auth with per-user credential
encryption (the `credentials` table and KMS key exist, unused), auto-submit for
high-confidence matches, and the A/B analytics on resume phrasing.

Workable is the one §2 ATS without an adapter. The protocol and registry make it
one file when you want it.

`require_token` is still a single shared token. Fine for one user; real work
before a second.

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
