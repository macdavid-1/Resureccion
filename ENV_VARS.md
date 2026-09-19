# Environment variables

Set these in the deployment platform's environment settings (never commit real values):

## Core

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATA_DIR` | yes | `./data` | Persistent storage root — must map to the durable bucket |
| `OWNER_USERNAME` | no | `owner` | Single authorized user |
| `OWNER_PASSWORD_HASH` | **yes** | — | scrypt hash; generate with `python -m app.scripts.set_password` |
| `AUTH_SECRET` | **yes** | — | Long random string for auth session salting |
| `SESSION_TTL_HOURS` | no | `168` | Auth session lifetime |
| `MAX_SESSIONS` | no | `5` | Concurrent research session cap |
| `SESSION_SOFT_TIMEOUT_HOURS` | no | `8` | Soft research time budget |
| `SESSION_HARD_TIMEOUT_HOURS` | no | `12` | Hard kill switch for a run |
| `HEARTBEAT_TIMEOUT_SECONDS` | no | `180` | Consider agent dead after this |
| `CHECKPOINT_INTERVAL_SECONDS` | no | `120` | Periodic checkpoint cadence |
| `PORT` | no | `7860` | HTTP port |
| `LOG_LEVEL` | no | `info` | Uvicorn log level |

## Research model (Stage 3 — required for research runs)

The agent reasons, orchestrates, reads screenshots, and emits JSON actions
through an OpenAI-compatible vision LLM. Default: **GLM-5.3-Flash on
DeepInfra** (native vision + strong reasoning + prompt caching, 1M context).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MODEL_API_KEY` | **yes** (for research) | — | DeepInfra API token (or any OpenAI-compatible key) |
| `MODEL_BASE_URL` | no | `https://api.deepinfra.com/v1/openai` | OpenAI-compatible endpoint |
| `MODEL_NAME` | no | `zai-org/GLM-5.3-Flash` | Model id at the endpoint |
| `MODEL_MAX_TOKENS` | no | `8192` | Max completion tokens per call |
| `MODEL_TEMPERATURE` | no | `0.4` | Sampling temperature |
| `MODEL_TIMEOUT_SECONDS` | no | `180` | HTTP timeout per model call |
| `MODEL_VISION_ENABLED` | no | `true` | Attach latest browser screenshot to each iteration |
| `MODEL_VISION_MAX_BYTES` | no | `400000` | Screenshot size guard for vision input |

The model sees the live browser viewport on every iteration (vision ground
truth) plus structured, redacted evidence digests — it never receives raw
HTML, cookies, or credentials.

## Research integrity thresholds (Stage 4)

Every threshold is a deliberate quality gate; each documents its rationale in
`app/thresholds.py`. They can only be RAISED via env — the integrity layer
never silently lowers them. If evidence is thin, the correct outcome is
rejection or downgrade, never a lowered bar.

| Variable | Default | Purpose |
|---|---|---|
| `THRESH_MIN_COMPETITORS` | `6` | Distinct competitor ASINs required before the landscape may be judged |
| `THRESH_MIN_REVIEWS` | `8` | Reviews required before a complaint pattern counts as market-wide |
| `THRESH_MIN_REVIEW_PRODUCTS` | `3` | Distinct products reviews must span |
| `THRESH_MIN_SOURCES` | `2` | Independent sources a claim needs before supporting an opportunity |
| `THRESH_MIN_OBSERVATIONS` | `15` | Session-level floor of raw page contact |
| `THRESH_MIN_VERIFICATION_COVERAGE` | `1.0` | Fraction of opportunities requiring a verification record (1.0 = none unverified) |
| `THRESH_MIN_SUPPORTED_RATIO` | `0.6` | Min share of important claims that must cite evidence ids |
| `THRESH_MAX_INFERENCE_RATIO` | `0.5` | Max share of claims that may be pure model inference |
| `THRESH_MIN_SATURATION_SAMPLE` | `4` | Competitors with observable data before a saturation judgment is allowed |

The integrity pipeline (after Phase 8, before synthesis) runs 12 named
candidate filters, the verification engine, KDP risk screening, the 7 session
quality gates, and adversarial report validation. Every outcome is persisted
to `integrity_assessments` and visible via `GET /api/sessions/{id}/integrity`
— the audit trail of WHY each candidate survived or died. A failing final
report is delivered `PROVISIONAL` with the failures listed in the report
itself, never silently.

## Browser infrastructure (Stage 2)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BROWSER_HEADLESS` | no | `true` | Run Chromium headless (extensions work via bundled chromium channel) |
| `BROWSER_CHANNEL` | no | `chromium` | Playwright channel; bundled chromium supports extensions headless |
| `BROWSER_NO_SANDBOX` | no | auto | Launch Chromium with `--no-sandbox`. Auto-detected: on when running as root or inside a container (HF Spaces/Docker), off locally. Set explicitly to `true`/`false` to override. |
| `BROWSER_EXTRA_ARGS` | no | — | Space/comma-separated extra Chromium launch args (deploy-specific tuning) |
| `BROWSER_IDLE_SHUTDOWN_SECONDS` | no | `300` | Auto-stop Chromium after idle to free the 2 cores |
| `BROWSER_DEFAULT_TIMEOUT_SECONDS` | no | `45` | Playwright default action timeout |
| `BROWSER_NAV_TIMEOUT_SECONDS` | no | `60` | Navigation timeout |
| `BROWSER_MAX_OPEN_PAGES` | no | `4` | Page pool cap (2-core budget) |
| `BROWSER_USER_AGENT` | no | — | Optional explicit UA override |
| `BROWSER_LOCALE` | no | `en-US` | Browser locale |
| `BROWSER_TIMEZONE` | no | `America/New_York` | Browser timezone |
| `KDSPY_EXTENSION_PATH` | no | `$DATA_DIR/extensions/kdspy` | Owner uploads the **unpacked KDSpy Pro** extension here |
| `KDSPY_MIN_VERSION` | no | — | Optional minimum version enforcement |
| `KDSPY_EXTENSION_ID` | no | — | Optional known extension id |
| `BROWSER_LOGIN_WINDOW_SECONDS` | no | `900` | One-shot manual login window TTL |
| `BROWSER_LOGIN_SECRET` | recommended | — | Shared secret required to open a manual login window (defense in depth on top of owner auth) |

Playwright + Chromium must be installed on the server:
`pip install playwright && playwright install chromium`

The provided `Dockerfile` does all of this already — it is the reference
deployment for Hugging Face Spaces (see the Deployment section in `README.md`).

## Amazon / KDSpy authentication model

- The owner opens a **one-shot login window** (owner session + `X-Login-Secret`
  header required). The persistent Chromium profile
  (`$DATA_DIR/browser_profiles/kdspy`) keeps cookies/localStorage so future
  research runs reuse the authenticated session without re-asking.
- Raw Amazon cookies may alternatively be imported once via
  `POST /api/browser/auth/amazon/cookies` (Amazon domains only).
- Credentials/cookies are NEVER stored in the DB, logs, reports, or exports —
  only statuses (authenticated / login_required / captcha_required /
  otp_required / signed_out / error) and diagnostics.
