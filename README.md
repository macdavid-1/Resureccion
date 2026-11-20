---
title: Resurrección
emoji: 📚
colorFrom: gray
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Resurrección

<img src="static/logo.png" alt="Resurrección logo" width="110">

Private, single-user, server-side autonomous KDP market-research agent.

Live long-running research driven by a real browser + AI agent, designed to run inside a Hugging Face Space with ~2 CPU cores / 16 GB RAM, persisting everything to the Space's persistent storage bucket so that research survives app restarts, Space restarts, browser crashes, and network/model failures. A session can run for hours unattended and be resumed afterward.

## Stack

- **Backend:** Python 3.11, FastAPI, Uvicorn
- **Storage:** SQLite (WAL mode) for structured state, filesystem for binary artifacts
- **Frontend:** Single-page vanilla HTML/JS dashboard (no build step)
- **Auth:** Single-owner login, server-side sessions, per-request token
- **Testing:** pytest

## Layout

```
app/
  config.py           # Env-driven configuration
  db.py               # SQLite connection, WAL, migrations, atomic helpers
  security.py         # Single-user auth, password hashing, session tokens
  sessions.py         # Research session lifecycle + isolation
  jobs.py             # Research job orchestration + crash recovery
  agent.py            # Agent state machine + agent state persistence
  research_data.py    # Observations, evidence, candidates, verification, opportunities
  events.py           # Research event/action log
  artifacts.py        # Artifact store (files, atomic writes)
  reports.py          # Research report storage
  exports.py          # Export job handling
  uploads.py          # Reference image uploads
  recovery.py         # Recovery / checkpoint state
  marketplace.py      # Marketplace abstraction (18 Amazon domains, explicit/auto plan)
  redact.py           # Secret hygiene: credential-shaped keys never leave the server
  browser_store.py    # Durable auth-state / extension / login-window / evidence stores
  firefox_addon.py    # Shared Firefox add-on lifecycle (install/validate/launch/state)
  kdspy.py            # KDSpy Pro Firefox add-on (thin subclass of firefox_addon)
  book_scout.py       # Scrivy Book Scout add-on: BSR badges on search results + parsing
  bsr_scan.py         # BSR without the add-on: in-page /dp/<asin> reads + rank parsing
vendor/scrivy-book-scout.xpi  # AMO-signed add-on shipped with the app (installed on first boot)
  browser_manager.py  # Persistent Camoufox (anti-detect Firefox) via Playwright (lifecycle, pages, crashes)
  amazon_auth.py      # Amazon auth state detection + one-shot login window + cookie import
  evidence_capture.py # Structured, redacted page/evidence extraction + screenshots
  final_dive.py       # Finalist deep dive: measured backend keywords + per-store category maps
  routes/
    auth.py
    sessions.py
    misc.py           # events/uploads/artifacts/reports/exports
    browser.py        # owner-only browser infra API
  main.py             # FastAPI app factory + wiring
static/
  index.html          # Dashboard
  app.js
  styles.css
scripts/
  set_password.py     # (app.scripts.set_password) owner hash generator
  boot_check.py
  browser_smoke.py    # live browser launch smoke test
  camoufox_smoke.py   # live check: Camoufox engine launches + spoofs identity
  ...
data/                 # Persistent storage root (created at runtime)
```

## Storage layout (persistent bucket)

```
$DATA_DIR/
  resurreccion.db        # SQLite, WAL mode
  artifacts/<session_id>/...
  uploads/<session_id>/...
  exports/<session_id>/...
  reports/<session_id>/...
  browser_profiles/kdspy/   # PERSISTENT Camoufox profile: cookies, localStorage, add-on state
  browser_downloads/
  extensions/kdspy-firefox/ # KDSpy Pro Firefox add-on (one-tap AMO install or XPI upload)
  extensions/scrivy-book-scout/ # Scrivy Book Scout add-on (BSR on search results)
```

Everything under `$DATA_DIR` must be mapped to Hugging Face Spaces persistent storage. `DATA_DIR=/data` is the expected value in the Space.

## Running

```bash
uvicorn app.main:app --host 0.0.0.0 --port 7860
```

## Deployment (Hugging Face Spaces)

The `Dockerfile` in the repo root is the production image — it is the complete
deployment: Python backend, Node 22 + PDFKit sidecar (report → 6×9in PDF),
Playwright Chromium with system libraries, and CJK/Noto fonts for non-Latin
marketplaces. It runs as a non-root user and binds `0.0.0.0:7860` as required
by Spaces.

Space setup checklist:

1. **SDK:** Docker (`sdk: docker`, `app_port: 7860` — already in this README's frontmatter).
2. **Persistent storage:** enable a persistent bucket in the Space settings; the image sets `DATA_DIR=/data`, which is where the bucket mounts. Everything durable (SQLite DB, artifacts, recordings, PDFs, uploads, browser profile with Amazon/KDSpy auth) lives under it.
3. **Secrets** (Space → Settings → Variables and secrets): `OWNER_PASSWORD` (the login password — the simple option) or `OWNER_PASSWORD_HASH` (generate with `python -m app.scripts.set_password`), plus `AUTH_SECRET`, `MODEL_API_KEY`, and recommended `BROWSER_LOGIN_SECRET`. Full list: `ENV_VARS.md`.
4. **KDSpy Pro:** one-tap install from Mozilla Add-ons — Settings → KDSpy Firefox add-on → Install from AMO (or upload the XPI). No shell access needed. The Camoufox **browser build must support add-on loading**: `run.sh` checks this at every boot and upgrades stale builds in place (`python scripts/camoufox_ensure.py`); if it can't, Settings records the add-on as *failed* with the fix command instead of claiming it loaded.

The browser runs without the Chromium sandbox flag entirely (Firefox/Camoufox
does not use it; containers run without user namespaces).

Environment variables (see `app/config.py` for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | Persistent storage root |
| `OWNER_USERNAME` | `owner` | Single authorized user |
| `OWNER_PASSWORD` | — | The login password itself; the scrypt hash is derived at boot (wins over `OWNER_PASSWORD_HASH`) |
| `OWNER_PASSWORD_HASH` | — | scrypt hash of the owner password (generated by CLI below) |
| `AUTH_SECRET` | — | Secret used to sign session tokens |
| `SESSION_TTL_HOURS` | `168` | Auth session lifetime |
| `MAX_SESSIONS` | `5` | Concurrent research session cap |

## First-run setup

Generate the owner credential hash and set it via environment:

```bash
python -m app.scripts.set_password
```

prints `OWNER_PASSWORD_HASH=...` — set it (and `AUTH_SECRET`) in the Space's environment settings.

Shortcut: set `OWNER_PASSWORD` to the password you want to sign in with
instead — the app derives the hash at boot, no CLI step needed.

## One-time account & extension setup (from your phone)

Everything the research browser needs is configured from Settings — no server
shell required, on the preview or on the deployed Space:

1. **Amazon sign-in.** Settings → Amazon authentication → **Sign in**. A
   full-screen remote browser opens (the server's real Camoufox, streaming
   live frames to your phone). Tap to click, use the text bar and key buttons
   to fill the login form, complete captcha/OTP, then Close. Cookies persist
   in the shared browser profile (`$DATA_DIR/browser_profiles/kdspy`), so
   every future research run is already signed in. The same flow is used
   again only if Amazon expires the session.
2. **KDSpy Pro install — one tap.** Settings → KDSpy Pro → **Install from
   Web Store**. Resurrección downloads the official KDSpy package directly
   from Google's Chrome Web Store CDN (the endpoint Chrome itself uses),
   verifies the CRX3 package structure, confirms the manifest declares the
   pinned KDSPY listing, installs atomically into
   `$DATA_DIR/extensions/kdspy`, and relaunches the browser. No ZIP is
   needed — Chrome no longer lets users export extension folders anyway.
   The setup browser then opens on kdspy.com for your account sign-in and
   license activation — same persistent profile, so Amazon stays signed in
   while KDSpy activates. (ZIP / folder-file upload remains available as a
   manual fallback.)
3. **Scrivy Book Scout (optional).** BSR capture already works without it: the
   scan reads each result's product page in the browser session and parses
   Amazon's printed rank. Installing it adds instant badges, estimated
   sales/revenue and the niche snapshot — Settings → Scrivy Book Scout →
   **Install add-on** installs the AMO-signed package that ships in the repo
   (`vendor/scrivy-book-scout.xpi`, also installed automatically on first boot —
   no account, no download, works offline; Mozilla Add-ons remains the update
   path), validates it and relaunches the browser. **Verify live**
   then opens an Amazon book search and reports which path answered and the
   measured result (e.g. "14/16 results carry a BSR · best #1,443: Books") — a
   green status chip alone is not treated as proof.
4. Amazon, KDSpy and Book Scout state are shown as status chips in Settings; the
   research agent pauses safely and waits if a marketplace wall is ever hit mid-run.
5. **Device relay — your phone's IP (optional but recommended).** Settings →
   Device Relay → **Enable**, copy the token, then run the one-file client on
   your phone (Termux: `pkg install python; pip install nothing — stdlib only`)
   or any always-on home device:

   ```bash
   python relay_client.py --server https://<your-space>.hf.space --token <token>
   termux-wake-lock   # keep the phone alive for long runs
   ```

   The device dials OUT to the server (works on carrier networks, no port
   forwarding) and shuttles the research browser's TCP bytes, so Amazon,
   KDSpy and CAPTCHA systems see your mobile/home IP instead of a datacenter
   address. TLS stays end-to-end; the relay carries only opaque encrypted
   bytes. Privacy is enforced server-side: tracker/ad hosts are blocked
   before any request touches your device's network, WebRTC cannot bypass
   the proxy, and DNT/GPC preferences are declared. If the device goes
   offline mid-run, egress falls back to `BROWSER_PROXY`, then direct —
   research never stalls — and switches back automatically when the device
   returns. Verify the live exit IP anytime with **Test egress**.

## Running on your own VPS

Clone, add your env values, and run one command:

```bash
git clone <repo> ~/resurreccion && cd ~/resurreccion
sudo sh scripts/vps_deploy.sh
```

It provisions the host (Python, Node for the PDF sidecar, browser libraries,
swap, pm2, the Camoufox binary), scaffolds `.env` with generated secrets,
creates the durable data directory the SQLite database lives in, starts the
app under pm2, registers it to survive a reboot, and waits until
`/api/health` answers. Re-running it is the idempotent update path. Full
walkthrough: `VPS.md`.

## Your own VPS as the egress proxy (recommended for offline hours)

A dual-stack (IPv4 **and** IPv6) VPS is the cheapest always-on egress that
reaches **all** hosts — including the IPv6-only Cloudflare challenge hosts
that some commercial proxy gateways fail (502 on
`brunhild.challenges.cloudflare.com`).

1. **Before buying**, pre-check any candidate IP:
   `python scripts/vps_ip_probe.py <candidate-ip>` — verifies IPv4+IPv6
   reachability, classifies the ASN (datacenter vs ISP), and checks
   blocklists. Buy only a `VIABLE` verdict.
2. **On the VPS** (Ubuntu, as root): `sh scripts/vps_proxy_setup.sh` —
   installs 3proxy, generates strong random credentials, locks UFW to
   SSH + the proxy port, enables IPv6, blocks private-range pivoting, and
   self-tests both address families. Prints a ready `BROWSER_PROXY` value.
3. **In Resurrección**: Settings → Environment → `BROWSER_PROXY` → paste →
   restart → **Test egress**. Egress order stays: phone relay → `BROWSER_PROXY`
   → direct.

Keep `scripts/vps_ip_probe.py <ip> --proxy "user:pass@host:port"` handy:
it probes *through* the finished proxy and pinpoints 407 (auth) vs 502
(routing/IPv6) instead of conflating them.

## Browser infrastructure (Stage 2)

- ONE persistent browser shared by all research (Playwright
  `launch_persistent_context`); profile at `$DATA_DIR/browser_profiles/kdspy`
  survives restarts, preserving Amazon authentication across runs.
- **The model drives it itself** (`app/model_browser.py`): `navigate`, `click`,
  `type`, `press`, `scroll` are real Playwright actions on a real page, paced
  like a person (pointer path, per-character typing, stepped scrolling) — so
  it can, for example, see Amazon's shipping-country notice in a screenshot and
  click "Dismiss" before reading the result total. Navigation stays
  deny-by-default (marketplaces + the external research allow-list); the DOM is
  the model's to drive.
- **Engine** (fixed): Camoufox — anti-detect Firefox. Fingerprint consistency
  is enforced by the browser binary itself (canvas/audio/fonts/WebGL/UA all
  coherent, spoofed at the C++ layer), so the tap-refusal class of Chromium
  headless tells disappears and cross-origin CAPTCHA iframes (Turnstile /
  reCAPTCHA) are clickable (`disable_coop`). The profile generates its
  fingerprint once and caches it under `$DATA_DIR` — the identity is stable
  across restarts, which is itself an anti-detection property.
- **BSR for a whole search page — with or without an add-on.** Amazon prints a
  book's rank only on its product page, so a search capture used to be
  rank-blind. Two paths close that gap, and the capability does not depend on
  any extension being installed:
  - *Native* (`app/bsr_scan.py`, always available): from the search page, fetch
    each result's `/dp/<asin>` inside the browser (same origin, the profile's
    own Amazon session — the add-on's own trick), and parse the printed rank +
    category chain with the add-on's patterns (US + localized). Two at a time
    with human pacing; the **first** wall is retried once, four walls in a row
    stop the scan (nothing is gained by hammering), and a failed fetch is
    recorded as a failure — never as a book without a rank. Stored as
    `bsr_scan`, an ordinary page observation.
  - *Add-on* (`app/book_scout.py`): if Scrivy Book Scout is loaded, its badges
    answer instantly for the whole page — BSR plus the add-on's estimated
    sales/revenue, an Indie pill, a niche snapshot and details table. That is
    recorded as `book_scout_scan`, a third-party **estimate** that can never
    move a filter verdict alone.
  One action covers both (`book_scout_scan`); `Verify live` in Settings opens a
  real Amazon book search and reports which path answered and how many results
  actually carry a BSR, so "installed" and "working" are never the same claim.
  The add-on is a convenience, not a requirement — Settings says so.
- KDSpy Pro ships as the **Firefox add-on (XPI)**, loaded *natively* by
  Camoufox via its `addons` launch option — KDSpy data is never faked:
  installed from Settings → KDSpy Firefox add-on (one tap from Mozilla
  Add-ons, or upload the `.xpi`/`.zip`), validated with the same zip-slip /
  manifest / version defenses, and durable state records the install.
- Amazon auth: owner-driven interactive sign-in over the persistent profile
  (live frame streaming + whitelisted tap/type/key/scroll/navigate controls),
  live auth-state detection (signed-in / login_required / captcha / OTP /
  signed-out), regional-redirect detection, and Amazon-only cookie import.
  The agent pauses safely when manual intervention is needed.
- Marketplaces: 18 Amazon domains abstracted; sessions obey explicit owner
  lists or auto-select ~4-5 by relevance (language hints + core markets).
- Evidence: structured extraction (rank/ASIN/title/price/rating/reviews/BSR,
  KDSpy panel reads) + screenshots as artifacts. Everything passes through
  `redact()`; credentials/cookies/tokens never reach API, logs, reports, or
  exports.

## Research agent (Stage 3)

- **Deterministic 9-phase methodology** (`app/methodology.py`) — discovery →
  aggressive niching → competitive landscape → consumer intelligence → demand
  validation → opportunity gap → cross-market validation → adversarial
  verification → opportunity synthesis. The model reasons *inside* each
  phase; it never invents its own process. Exit gates (min evidence, output
  contracts) are enforced by the orchestrator.
- **Phase switching + niche target** — the model may leave a phase at will
  with an optional `goto_phase` field (durable jump budget), and a target
  stated in the brief ("I need 10 niches", or `TARGET_NICHES`) re-opens the
  earlier phases after verification when too few niches survived — so the
  run works until the target is met instead of ending short. Switching never
  loses data: phase analyses are merged (never blanked), verified candidates
  are never re-verified, rejected niches stay rejected, and everything is
  checkpointed per pass.
- **Model**: GLM-5.3-Flash on DeepInfra by default (OpenAI-compatible API,
  native vision, prompt caching, 1M context) — set `MODEL_API_KEY` in the
  environment; see `ENV_VARS.md`.
- **Vision loop**: the newest browser screenshot is attached to every
  iteration, so the model reads rankings/prices/KDSpy panels/walls directly
  from the live viewport as visual ground truth.
- **Whitelisted actions**: the model can only request approved verbs
  (search, open category/product, reviews, related, autocomplete, bestsellers,
  new releases, KDSpy reads, capture, observe, wait); the backend validates
  everything against the phase contract before executing.
- **Depth over speed**: priority order is integrity > evidence > depth >
  opportunity quality > breadth > efficiency > speed. Sessions pause durably
  (never corrupt) on auth walls, model failures, or browser crashes, and are
  resumable from checkpoints.
- **Final report**: only synthesized, adversarially verified opportunities
  reach the owner-facing report; invalid candidates are rejected along the
  way with reasons recorded in the event log.

## Report writing (`app/report_writer.py`)

The report is **written by the model**, not assembled from a template. Once a
run finishes, the backend assembles the complete research dossier — the owner's
brief, every phase's analysis, the actions taken, the model's own reasoning,
the browser captures with their measured prices/BSRs/reviews/publication data,
the candidate ledger with rejection reasons, the verification verdicts, the
measured finalist deep dives and every captured screenshot — and the model
decides what matters, how to organise it and what to say. Each mode is framed
differently (answering a brief, addressing supplied keywords, or explaining an
autonomous discovery).

Guardrails stay deterministic:

- every figure in the delivered markdown must appear in the dossier — unknown
  figures are removed and a corrective retry is attempted (three attempts in
  all); the model's document is then the deliverable, whole;
- every `[ev:ID]` citation must name a real evidence record and resolves to
  `/api/sessions/{id}/evidence/{evidence_id}`, a page showing the capture;
- every `![caption](shot:EVIDENCE_ID)` must name a captured screenshot and
  resolves to the authenticated artifact URL, so images render inline in the
  report viewer and in the PDF export;
- no claim can cite a figure that exists only in the model's reasoning or its
  run notes.

If the model cannot produce a usable report after those attempts, the run is
reported as failed (`ReportWriteError`) rather than shipping a backend-written
stand-in: all research data stays saved, so the report can be written once the
model is available again. There is no generated fallback and no
`REPORT_AI` switch — the backend never authors a section of the report.

### Talking to the analyst (`app/report_chat.py`)

A delivered report opens a conversation, not a dead end: the owner asks
questions and keeps asking (
`POST /api/sessions/{id}/reports/{rid}/chat`, history at `GET …/chat`).
The analyst answers from the SAME record the report was written from — the
brief, every phase's analysis, every capture (search totals, competitor
prices/BSRs/review counts, category charts, the review text read), the
candidate ledger with rejection reasons, the verification checks, its own
notes and reasoning, the screenshots, the earlier runs in the chain and the
report itself — nothing sampled and nothing summarised. Every earlier question
and answer is re-sent on each turn, so "that one" and "the second option" keep
their meaning; the conversation is stored in `report_chat_messages` per report
and survives restarts. Answers are grounded exactly like the report: `[ev:ID]`
citations resolve to real evidence pages (unknown ids are dropped and reported,
never linked), and a question the research cannot answer is answered as a gap,
not a guess. In the dashboard the panel sits under the report — owner messages
on the right, the analyst's on the left. Every answer carries a collapsible
**Reasoning** row: tap it to read that call's chain of thought, fetched from
the same owner-only thought store the session workspace uses (`GET
/api/sessions/{id}/thoughts/{thought_id}`), and answers think at
`CHAT_REASONING_EFFORT` (default `low`).

### Mini sessions (`app/mini_session.py`)

Some questions cannot be answered from a finished record: a price that may
have moved, whether a book still exists, a phrase nobody searched, a niche this
run never touched. For those the analyst can run a **mini session** — a small,
focused piece of live work inside the same conversation.

- **The model decides, from the conversation.** When an answer needs a live
  measurement (or the owner says "go and check"), the analyst ends its reply
  with a `mini-session` block (`{"goal", "title"}`). The block is parsed out
  (never shown as text) and the session starts immediately — there is no yes/no
  card and no purpose prompt: the owner's message was the instruction. The
  answer says what is being checked; the session's findings arrive in the same
  thread.
- **It is not a research run.** No phases, order, output contracts or exit
gates: the methodology is supplied as reference the model may read and ignore.
  The model chooses its own actions from the same browser toolkit the research
  agent uses (`app/actions.py`), and stops as soon as the question is settled.
  Mini-session steps think at `MINI_SESSION_REASONING_EFFORT` (default
  `medium`) while chat answers stay at `low`.
- **It carries everything.** A mini session gets the same complete dossier a
  chat answer gets (every capture, candidate, verification, note, reasoning
  line, screenshot, phase analysis, earlier run in the chain and the report)
  plus the whole conversation and its own step results — nothing summarised.
- **The owner can steer it while it runs.** A message typed into the
  conversation is delivered to the running mini session (a durable owner-inbox
  step the model reads on its next step) and answered in the same thread when
  it finishes, as an analyst message tagged with its mini-session id.
- **It can be monitored like a full run.** The conversation shows a live
  monitor for the running (or last watched) session: status, the step log as it
  grows (each action with its measured summary or failure), the **Live browser**
  frame from the same browser the research session uses, and a **Reasoning**
  dropdown on every model step that opens that turn's chain of thought. The
  monitor polls while the session works and keeps its result afterwards.
- **No cap.** A conversation can run as many mini sessions as it needs; the
  only stops are the owner's cancel and the `MINI_SESSION_TIMEOUT_SECONDS`
  wall-clock budget. Every session keeps a durable step log
  (`mini_sessions`, `mini_session_steps`) so the owner can see exactly what it
  did, and a restart marks anything that was working as interrupted rather than
  leaving it "running" forever.

Routes: `POST/GET /api/sessions/{id}/reports/{rid}/mini-sessions`,
`GET /api/sessions/{id}/mini-sessions/{mini_id}` (detail + steps),
`POST …/mini-sessions/{mini_id}/cancel`.



## Research integrity layer (Stage 4)

Research output is only as good as its evidence. A deterministic integrity
layer sits between the AI agent and the final deliverable — the model can
reason, but it cannot fabricate its way into the report:

- **Claim chain of custody** (`app/claims.py`): observation → evidence →
  claim → interpretation → verification → conclusion. Any factual claim
carried into a report must be registered pointing at real evidence ids.
  Unsupported assertions are stamped `model_inference` and counted against
  the candidate — they may exist in the transcript but never become facts.
- **12 named candidate filters** (`app/filters.py`): duplicate, overly-broad,
  weak-demand, excessive-competition, single-source anomaly, stale evidence,
  contradictory and unverifiable claims, and more. Hard failures reject;
  soft failures downgrade to "needs more research". Every rejection carries
  a durable, human-readable reason.
- **Verification engine** (`app/verification.py`): deterministic audit of
  every candidate — evidence coverage, claim integrity, inference load,
  contradiction check, cross-source corroboration. The model's Phase-8
  verdict is one input; this engine is the system's own check.
- **KDP risk screening** (`app/kdp_risk.py`): named policy rules —
  trademarks, minor safety, medical/legal/financial advice positioning,
  guaranteed-outcome promises, public-domain repackaging. Blocking flags
  reject regardless of evidence quality.
- **7 session quality gates** (`app/quality_gates.py`): observation floor,
  evidence citation, candidate pipeline, verification coverage (every
  opportunity verified), claim support, inference load, opportunity
  diversity. Thresholds documented in `app/thresholds.py`, env-tunable
  (see `ENV_VARS.md`).
- **Adversarial report validation** (`app/report_validation.py`): the final
  report is validated against all of the above. A failing report ships as
  `PROVISIONAL` with its failures listed in the report — never a silently
  under-supported "final" deliverable.

Every filter, verification, gate, screening, and validation outcome is
persisted to `integrity_assessments` (DB schema v4) and queryable via
`GET /api/sessions/{id}/integrity` and `GET /api/sessions/{id}/claims` —
the owner can audit exactly WHY any candidate survived or died.

Synthesis cannot mint opportunities from nothing: a Phase-9 opportunity must
map onto an existing candidate the pipeline verified, and its
`verification_status` in the report comes from the engine, not the model.

## Deep discovery (Stage 5)

Discovery used to be whatever the model imagined. Five deterministic layers
now widen what a run can FIND, and every one of them feeds the ordinary
evidence → claims → filters → gates pipeline — none of them can override a
filter, a gate or a verification verdict:

- **Category census** (`app/census.py`) — before Phase 1 opines, the backend
  walks Amazon's bestseller and new-release charts plus the browse-node tree
  hanging off them, harvesting rank/price/rating/review counts per node. You
  cannot discover a category you never visited; this makes the system visit
  them, and hands the model a measured map instead of its imagination.
- **Keyword work belongs to the model** — the backend generates no keyword
  list before research starts. Phrases come from the model typing into
  Amazon's own search box and reading the suggestions live (or the
  `autocomplete` action calling the same completion stream), from KDSpy's
  keyword panel on a search page, and from its own searches' printed totals.
  `app/frontier.py` keeps only the completion call and the root/drift
  screening those phrases pass through.
- **Hard rules** (`app/product_rules.py`) — no journal/logbook/notebook/diary
  product, and no generic market ("self help", "books for women"). Enforced at
  candidate creation, in the deterministic filter, and in the prompt.
- **Whitespace score** (`app/whitespace.py`) — one deterministic
  `demand ÷ competition` number per candidate, computed only from captured
  evidence. It orders the candidate digest (highest headroom gets the next
  iteration first), ranks the report, and drives adjacency promotion. It
  never touches a verdict, and an unmeasured candidate is reported as
  *unmeasured*, never as a fake zero.
- **Adjacency expansion** (`app/adjacency.py`) — after competition and
  demand work, walk outward from every strong candidate (its autocomplete
  completions, census siblings), measure the most
  promising neighbours with real searches, and promote only what clears
  `ADJACENCY_MIN_WHITESPACE`. Discovery re-entry triggered by SUCCESS rather
  than by total failure, as a step inside the existing phases — P1→P9, the
  gates and the report contract are untouched.
- **Cross-session market memory** (`app/market_memory.py`) — one
  owner-scoped knowledge base of verified niches, measured keyword result
  counts, competitors, near-misses and census stats. Rows younger than
  `MEMORY_FRESH_DAYS` are reused as fact; older rows are carried as stale
  hints the model must cheaply re-verify. Every run stops paying twice for
  the same numbers, and autonomous runs get smarter over time.
- **Amazon pricing follows the marketplace, not the egress IP**
  (`app/amazon_location.py`) — Amazon takes both the currency and the
  delivery location from the visitor's IP, so a foreign exit makes
  amazon.com show NGN prices and "Deliver to Nigeria". The browser presents
  New York (`BROWSER_LOCALE`/`BROWSER_TIMEZONE`) and, once per launch, sets
  the marketplace's currency cookie and pins the delivery zip
  (`AMAZON_DELIVERY_ZIP`, default 10001) through Amazon's own "Deliver to"
  dialog — no extra service, no login. The header must confirm the zip; an
  unconfirmed pin is reported in the browser status instead of assumed.
- **Follow-up research continues the whole run** (`app/continuation.py`,
  `app/continuation_build.py`) — "Start follow-up research" no longer seeds a
  new session with a keyword list. It builds the previous run's complete
  record out of the durable stores (its report, verified and rejected
  candidates with reasons, measured backend keywords, both category maps,
  competitors, buyer quotes, its own notes and reasoning excerpts, its
  operational trace, its limitations and the adjacency it never funded) and
  makes that the new session's brief, then links the two sessions
  (`parent_session_id`). The owner can add a brief, attach reference images, or
  add nothing at all — an empty follow-up simply continues from the recorded
  open threads, and is told not to re-measure what already exists.
- **Finalist deep dive** (`app/final_dive.py`) — once the methodology has
  chosen the candidates that will reach the report, each one gets **screened,
  then verified** backend keywords plus its category map for **both** stores
  (the Kindle Store and the Books store keep separate category trees, so both
  are walked independently, up to `DIVE_MAX_CATEGORIES` each). Phrases are
  first screened deterministically — generic roots ("journal for women"),
  marketplace noise, dangling prefixes and phrases that drifted off the
  candidate are dropped, with the reason recorded. Survivors are then searched
  **one by one** in the Books department and refused if Amazon's printed total
  exceeds `DIVE_MAX_COMPETITION` (default 1,000) or is otherwise unwinnable;
  the walk keeps going until `DIVE_MAX_KEYWORDS` phrases are accepted, then a
  bounded second sweep mines deeper from the accepted ones. Every accept and
  every refusal lands in `browser_evidence` as an ordinary record, so the
  report can show the keywords that survived **and** why the rest did not;
  a phrase with no search is printed as "not searched in this run" rather
  than as a number. The initial autocomplete suggestions the model sees use
  the same screen and are presented as starting points, never as a boundary.

Supporting changes:

- **Research depth instead of an early finish** — runs were ending after an
  hour or two on an 8–12 hour budget. Two deterministic knobs fix that:
  while a session is under `RESEARCH_MIN_HOURS` (default 3) every phase owes
  `DEPTH_EVIDENCE_MULTIPLIER` times its declared new-evidence floor before it
  may close (the model sees the raised number and a time-remaining line in
  every prompt), and there is **no model-call ceiling and no per-phase
  iteration cap**: the model is never told it has run out of moves (0 = no
  limit in `MODEL_CALLS_<mode>` / `MODE_ITERATIONS_<mode>`; setting either
  restores a cap). Wall-clock time, an owner pause/cancel and the phase's own
  completion are the only stops. Depth never becomes a spin loop: when the declared floors are met and
  no NEW distinct source appears for `STALL_LIMIT` rounds, the phase commits
  and records `phase_committed_at_depth_ceiling` rather than re-capturing
  duplicates forever.
- **Accounting that matches the work** — a phase may no longer commit a list
  of blank rows (an empty verdict is not a finding), niches researched in
  Phases 3–7 are backfilled into the candidate ledger so counts, claims,
  rejections and verifications reflect what was actually analyzed, the
  executive summary states evidence-record and claim counts alongside
  candidate counts, blank demand verdicts are dropped, every researched
  niche appears in the consumer table (marked *not captured* rather than a
  dash), and a phase that produced nothing is reported as
  *produced no output* instead of *completed*.
- **Per-mode budget profiles** (`app/methodology.MODE_PROFILES`) — a detailed
  brief spends on competition/verification, no topic at all spends on
  discovery/niching. The profiles no longer cap a live phase (a phase runs
  until its contract is met), and the model-call ceiling is unlimited unless
  the owner sets one explicitly with `MODEL_CALLS_<mode>` or
  `MODE_ITERATIONS_<mode>`.
- **No more silently dropped niches** — candidates past the live cap land in
  a durable backlog and appear in the report's ledger.
- **Adjacent-opportunities ledger** — surviving-but-unfunded candidates,
  measured neighbours and backlog rows are published in every report with
  their headroom and evidence ids, with **Start follow-up research** in the
  dashboard seeding the next run through the existing keywords mode: each
  report funds a deeper one.
- **External sources, mid-methodology only** — `forum_search`,
  `google_suggest` and `goodreads_check` are allow-listed actions available
  in Phases 4/5/6 (where Amazon-side candidates already exist) and, for
  autonomous runs only, as a gated second wave in Phase 1 after Amazon-side
  anchors exist. They land as ordinary `external_demand` evidence, redacted,
  and flow through the same integrity pipeline.

## Design rules

1. Every research session is isolated: all session-scoped data lives under a session ID namespace in both DB rows and filesystem paths.
2. Nothing irreplaceable lives only in memory. Every state transition is committed to SQLite before it is acted on.
3. All file writes are atomic (temp file + fsync + `os.replace`).
4. On boot, any session in a transient state is moved to `interrupted` and marked for recovery by the job orchestrator.
5. The research agent state machine is a first-class, persisted entity — not an in-process object.
6. Failed research tasks are recorded with a recoverable error state, never silently swallowed.
