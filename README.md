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
  kdspy.py            # KDSpy Pro extension validation + launch args (Chromium MV3 + Firefox XPI)
  browser_manager.py  # Persistent Chromium via Playwright (lifecycle, pages, crashes)
  amazon_auth.py      # Amazon auth state detection + one-shot login window + cookie import
  evidence_capture.py # Structured, redacted page/evidence extraction + screenshots
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
  browser_smoke.py    # live Chromium launch smoke test
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
  browser_profiles/kdspy/   # PERSISTENT Chromium profile: cookies, localStorage, KDSpy state
  browser_downloads/
  extensions/kdspy/         # owner-supplied unpacked KDSpy Pro extension
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
3. **Secrets** (Space → Settings → Variables and secrets): `OWNER_PASSWORD_HASH`, `AUTH_SECRET`, `MODEL_API_KEY`, and recommended `BROWSER_LOGIN_SECRET`. Generate the hash with `python -m app.scripts.set_password`. Full list: `ENV_VARS.md`.
4. **KDSpy Pro:** upload the unpacked extension directly from the app — Settings → KDSpy Pro → Install (ZIP or folder files). No shell access needed.

Chromium runs with `--no-sandbox` in the container (auto-detected; forced via
`BROWSER_NO_SANDBOX=true` in the image) because Spaces containers run without
user namespaces. Locally, sandboxing stays on.

Environment variables (see `app/config.py` for the full list):

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `./data` | Persistent storage root |
| `OWNER_USERNAME` | `owner` | Single authorized user |
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

## One-time account & extension setup (from your phone)

Everything the research browser needs is configured from Settings — no server
shell required, on the preview or on the deployed Space:

1. **Amazon sign-in.** Settings → Amazon authentication → **Sign in**. A
   full-screen remote browser opens (the server's real Chromium, streaming
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
3. Amazon and KDSpy state are shown as status chips in Settings; the research
   agent pauses safely and waits if a marketplace wall is ever hit mid-run.
4. **Device relay — your phone's IP (optional but recommended).** Settings →
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

## Your own VPS as the egress proxy (recommended for offline hours)

A dual-stack (IPv4 **and** IPv6) VPS is the cheapest always-on egress that
reaches **all** hosts — including the IPv6-only Cloudflare challenge hosts
that broke the Webshare gateway (502 on `brunhild.challenges.cloudflare.com`).

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
- **Engines** (`BROWSER_ENGINE`):
  - `camoufox` (default) — anti-detect Firefox. Fingerprint consistency is
    enforced by the browser binary itself (canvas/audio/fonts/WebGL/UA all
    coherent, spoofed at the C++ layer), so the tap-refusal class of Chromium
    headless tells disappears and cross-origin CAPTCHA iframes (Turnstile /
    reCAPTCHA) are clickable (`disable_coop`). Each profile generates its
    fingerprint once and caches it under `$DATA_DIR` — the identity is stable
    across restarts, which is itself an anti-detection property.
  - `chromium` — Playwright Chromium. Required for the KDSpy Pro **Chrome
    MV3** build (`--load-extension`).
- KDSpy Pro ships in two package formats, and the manager validates whichever
  the active engine can load — KDSpy data is never faked:
  - **Chrome MV3 build** (chromium engine): real unpacked extension via
    `--load-extension`; manifest/version validated durably, MV3 service
    worker verified after launch.
  - **Firefox add-on (XPI)** (camoufox engine): installed from Settings →
    KDSpy Firefox add-on → Upload XPI, validated with the same zip-slip /
    manifest / version defenses, and loaded *natively* by Camoufox via its
    `addons` launch option. This is the supported KDSpy path on the default
    camoufox engine — no Chromium switch needed.
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

## Design rules

1. Every research session is isolated: all session-scoped data lives under a session ID namespace in both DB rows and filesystem paths.
2. Nothing irreplaceable lives only in memory. Every state transition is committed to SQLite before it is acted on.
3. All file writes are atomic (temp file + fsync + `os.replace`).
4. On boot, any session in a transient state is moved to `interrupted` and marked for recovery by the job orchestrator.
5. The research agent state machine is a first-class, persisted entity — not an in-process object.
6. Failed research tasks are recorded with a recoverable error state, never silently swallowed.
