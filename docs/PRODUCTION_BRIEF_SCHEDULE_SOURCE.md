# Production Brief Schedule Source (Phase 6 discovery)

Findings from probing `http://20.62.194.32:8094` (the Eagle production brief,
`Eagle-Woodworking/eagle-production-brief`) for a machine-readable source of
"drawers scheduled to finish today," before writing any Phase 6 code.

## TL;DR

**No JSON endpoint exists for this number.** The production brief is not FastAPI —
it's a stdlib `http.server` app (`production_brief/server.py`,
`SimpleHTTPRequestHandler` subclass). Its only JSON routes are `/api/health`,
`/api/quality-issues` (GET), and `/api/ignore` / `/api/unignore` (POST). Every other
path — including `/openapi.json` and `/docs`, both confirmed 404 — falls through to
plain static file serving of the pre-rendered daily board HTML.

The number has to come from scraping the rendered `drawers.html` board.
**Past dates are retrievable** — the brief keeps a dated archive
(`/archive/<YYYY-MM-DD>/drawers.html`) going back to whenever this feature and the
archive both existed — so historical backfill is possible, back to **2026-07-20**
(see "How far back" below), not just forward from today.

## What was probed

1. `GET /api/quality-issues` → 200, real JSON (`{"ok": true, "count": 54, ...}`).
   Confirms the box is reachable from this machine, as expected.
2. `/api/drawers`, `/api/daily-brief`, `/api/production`, `/api/schedule`,
   `/api/drawers/schedule`, `/api/schedule/drawers`, `/api/doors`, `/api/shipping`,
   `/api/board`, `/drawers`, `/docs` → all 404, and — tellingly — the 404 body is
   Python's `http.server` default error page (`<title>Error response</title>`,
   `Error code: 404`), not a FastAPI JSON 404 (`{"detail":"Not Found"}`). That was
   the tell that this isn't a FastAPI app at all.
3. `GET /openapi.json` → 404, same stdlib error page. Confirmed via source
   (`production_brief/server.py`) that there is no ASGI/WSGI framework here, no
   route table, no OpenAPI — `do_GET`/`do_POST` are hand-written `if` branches over
   three hardcoded paths, and everything else is `super().do_GET()` (static files).
4. `GET /drawers.html` → 200, HTML. This is the board. Confirmed via source
   (`production_brief/brief.py`, `render.py`, `kpi.py`) that "drawers scheduled to
   finish today" is `kpi.plan_snapshot()`'s `drawers_plan_scheduled` series,
   persisted **first-write-wins** (`state.upsert_daily_first`, see `brief.py` line
   ~89) at the daily ~06:15 ET board generation and rendered as one `Fact` in the
   "Today's plan" section (`render.py` `_facts()`/`_section()`).
5. `GET /archive/<date>/drawers.html` → 200 for dates the archive holds (confirmed
   for `2026-08-20`, `2026-08-17`), 404 for `2026-01-01` (too far back — see below).
   Confirmed via `production_brief/server.py` `translate_path()`: `/archive/...`
   maps straight into `config.output.archive_dir`, and via `brief.py` (`archive_dir
   = Path(config.output.archive_dir) / today.isoformat()`) that a full dated copy
   of every board is written on every run, kept forever ("dated copies, kept
   forever (small files)" per `OutputConfig.archive_dir`'s docstring).

## The exact scrape target

- **URL (today):** `GET {PRODUCTION_BRIEF_URL}/drawers.html`
- **URL (a specific past date):**
  `GET {PRODUCTION_BRIEF_URL}/archive/<YYYY-MM-DD>/drawers.html`
- **Selector:** find the `<section>` whose `<h2>` text starts with `"Today's plan"`;
  within it, find the `<div class="fact ...">` whose `<div class="fact-label">` text
  is exactly `"drawers scheduled to finish today"`; take the sibling
  `<div class="fact-value">` text, strip thousands-separator commas, parse as int.
  If the section or that exact fact-label isn't present, treat as "no schedule
  known for that date" (not zero) — see `render.py`'s `_facts()`: the whole
  `<div class="facts">` block is only rendered "if facts" i.e. it's absent entirely
  on any day the brief had no plan snapshot for (e.g. before the feature existed,
  or a day with no state row for some other reason).

### Real sample response (`GET /drawers.html`, captured 2026-08-21)

Raw markup, `<section>` for "Today's plan":

```html
<section><h2>Today&#x27;s plan — Friday Aug 21</h2><div class="facts">
  <div class="fact fact-none"><div class="fact-value">406</div><div class="fact-label">drawers scheduled to finish today</div></div>
  <div class="fact fact-none"><div class="fact-value">0</div><div class="fact-label">already done</div></div>
  <div class="fact fact-none"><div class="fact-value">0</div><div class="fact-label">in progress</div></div>
  <div class="fact fact-none"><div class="fact-value">406</div><div class="fact-label">not started</div></div>
</div></section>
```

Header stamp on that same page: `generated 2026-08-21 06:15 EDT · 5b9d41c main
2026-08-07T15:31:03Z` — confirming the board (and this fact) is generated once, at
06:15 ET, not continuously.

Archived date (`GET /archive/2026-08-20/drawers.html`), same shape, different day:

```html
<section><h2>Today&#x27;s plan — Thursday Aug 20</h2><div class="facts">
  <div class="fact fact-none"><div class="fact-value">431</div><div class="fact-label">drawers scheduled to finish today</div></div>
  ...
</div></section>
```

## Can the endpoint return past dates?

**Yes, but only via the dated archive path, not a query parameter.** There is no
`?date=` or `?since=` on `/drawers.html` itself — it always renders "today" (i.e.
whatever the box's local clock said at its last 06:15 ET generation). To get a past
date's number, fetch `/archive/<YYYY-MM-DD>/drawers.html` instead and scrape the same
`.fact`/`.fact-label` structure out of that dated copy.

### How far back

- Confirmed working: `2026-08-20`, `2026-08-17` (both 200, correct shape).
- Confirmed *not* available: `2026-01-01` (404).
- Via `git log -S"plan_scheduled" --oneline` in the production-brief repo: the
  "Today's plan" section (and the underlying `plan_snapshot()`/first-write-wins
  persistence) shipped in commit `89248fc`, dated **2026-07-20**. Archived boards
  from before that date won't have this fact at all (older `drawers.html` archives
  render fine — they're just missing the "Today's plan" section entirely, which the
  scraper must treat as "unknown," not "zero," per the rule above).
- Net: **historical backfill is possible back to on/around 2026-07-20** (whenever
  that commit was actually deployed to the box — the two spot-checked archive dates
  are both after it and both work). Anything requested before that will 404 or come
  back with no fact block; the relay should log that as "no data for this date" and
  leave `daily_schedules` blank for it, not write a 0.

## Why this is scraping, not a real API — and who owns fixing that

This is exactly the "if and only if no JSON endpoint exists" case the task called
out. The scraping is isolated to one function (see
`scripts/relay_customer_issues.py`, `_scrape_drawers_scheduled()` — clearly marked,
does nothing else, and can be swapped for a real API call with no other code
changes) so it's a single, contained point of technical debt.

**Blake owns `Eagle-Woodworking/eagle-production-brief`.** The clean fix is a small
JSON endpoint next to the existing `/api/quality-issues` handler in
`production_brief/server.py` — e.g. `GET /api/schedule?date=YYYY-MM-DD` returning
`{"date": "...", "drawers_scheduled": <int or null>}` sourced directly from
`state.daily_series("drawers_plan_scheduled")`, with the same "missing = null, not
0" semantics `render.py` already uses. That would let the relay (and any other
future consumer) drop HTML scraping entirely. Flagging this in
[`BACKLOG.md`](../../eagle-production-brief/BACKLOG.md) of that repo is Blake's call,
not made here — this doc is the handoff.

## Consequences for this phase's implementation

- `scripts/relay_customer_issues.py` gets a second, independent fetch+forward in the
  same run: scrape today's `/drawers.html` plus a trailing 7-day window of
  `/archive/<date>/drawers.html` (self-healing for a missed hourly run), forward the
  raw `{date: count}` map to a new ingest endpoint, same `X-Relay-Key` auth pattern
  as the customer-issues relay. See `docs/PROJECT_SPEC.md` Phase 6 addendum for the
  wire shape.
- Because the brief's own number is a first-write-wins morning snapshot, re-scraping
  it hourly on Render's behalf is harmless (it'll be the same number every time
  after ~06:15 ET) and matches the spec's "overwrite today's value on every hourly
  run" requirement without needing any dedup logic on our side — the brief already
  did the dedup.
- `source_thread_id`-style dedup doesn't apply here; `daily_schedules` is keyed by
  `production_date` directly, one row per date, so the sync endpoint just
  upserts by date (manual-wins rule per `docs/PROJECT_SPEC.md` Phase 6 addendum).
