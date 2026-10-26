# Environment variables

Set these in the deployment platform's environment settings (never commit real values):

## Core

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATA_DIR` | yes | `./data` | Persistent storage root — must map to the durable bucket |
| `OWNER_USERNAME` | no | `owner` | Single authorized user |
| `OWNER_PASSWORD` | no | — | The login password as plain text — the simplest option: the app derives the scrypt hash at boot. Use this **or** `OWNER_PASSWORD_HASH`; if both are set, `OWNER_PASSWORD` wins |
| `OWNER_PASSWORD_HASH` | **yes**¹ | — | scrypt hash; generate with `python -m app.scripts.set_password` |
| `AUTH_SECRET` | **yes** | — | Long random string for auth session salting |
| `SESSION_TTL_HOURS` | no | `168` | Auth session lifetime |
| `COOKIE_SECURE` | no | `false` | Set `true` when served over HTTPS (custom domain behind TLS) — the session cookie then only travels over https |
| `MAX_SESSIONS` | no | `5` | Concurrent research session cap |
| `MAX_CONCURRENT_SESSIONS` | no | `1` | Research runs executing at once. Keep `1` on a 2-core box |
| `SESSION_SOFT_TIMEOUT_HOURS` | no | `8` | Soft research time budget |
| `SESSION_HARD_TIMEOUT_HOURS` | no | `12` | Hard kill switch for a run |
| `MAX_FOCUS_PIVOTS` | no | `1` | Strategic focus pivots per autonomous run (0 disables): after a full pass where deep research + niche-down + adversarial verification leave zero verified candidates, discovery re-enters on a new focus |
| `HEARTBEAT_TIMEOUT_SECONDS` | no | `180` | Consider agent dead after this |
| `CHECKPOINT_INTERVAL_SECONDS` | no | `120` | Periodic checkpoint cadence |
| `PORT` | no | `7860` | HTTP port |
| `LOG_LEVEL` | no | `info` | Uvicorn log level |

¹ Skip `OWNER_PASSWORD_HASH` when `OWNER_PASSWORD` is set. Quote either value
in `.env` if it contains spaces or shell special characters (`$`, backticks):
`OWNER_PASSWORD='my $trong password'`.

## Automatic backups

The live database is crash-safe (WAL + `synchronous=FULL`); snapshots cover
the other half — disk/volume loss and corruption. Every interval the database
is copied with SQLite `VACUUM INTO` (consistent online copy, no downtime),
verified with `PRAGMA quick_check` before it is published, and pruned to the
newest N. See `app/backup.py`; restore steps are in `VPS.md`.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BACKUP_ENABLED` | no | `true` | Set `false` to disable snapshots entirely |
| `BACKUP_INTERVAL_HOURS` | no | `24` | Hours between snapshots (min 1) |
| `BACKUP_KEEP` | no | `14` | Snapshots to retain (min 1); older ones are pruned automatically |
| `BACKUP_DIR` | no | `$DATA_DIR/backups` | Where snapshots are written — point this at another mount to keep backups off the data disk |

## Research model (Stage 3 — required for research runs)

The agent reasons, orchestrates, reads screenshots, and emits JSON actions
through an OpenAI-compatible vision LLM. Default: **MiMo-V2.6-Flash on
DeepInfra** (thinking/reasoning model, native vision, 1M context).

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MODEL_API_KEY` | **yes** (for research) | — | DeepInfra API token (or any OpenAI-compatible key) |
| `MODEL_BASE_URL` | no | `https://api.deepinfra.com/v1/openai` | OpenAI-compatible endpoint |
| `MODEL_NAME` | no | `XiaomiMiMo/MiMo-V2.6-Flash` | Model id at the endpoint |
| `MODEL_MAX_TOKENS` | no | `16384` | Max completion tokens per call. A reasoning model's thinking tokens are billed against this, so a thinking run at high effort needs headroom or the answer gets truncated |
| `MODEL_THINKING` | no | `true` | Ask the reasoning model to think before answering |
| `MODEL_REASONING_EFFORT` | no | `high` | `low` / `medium` / `high`. Any other value is a loud boot error. Providers ignore this on non-reasoning models |
| `MODEL_TEMPERATURE` | no | `0.4` | Sampling temperature |
| `MODEL_TIMEOUT_SECONDS` | no | `180` | HTTP timeout per model call |
| `MODEL_VISION_ENABLED` | no | `true` | Attach latest browser screenshot to each iteration |
| `MODEL_VISION_MAX_BYTES` | no | `400000` | Screenshot size guard for vision input |

The model sees the live browser viewport on every iteration (vision ground
truth) plus structured, redacted evidence digests — it never receives raw
HTML, cookies, or credentials.

## Container runtime (Docker / docker-compose)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SWAP_ENABLED` | no | `true` | Create + enable a swap file inside the container at boot (needs `--cap-add SYS_ADMIN`; skipped with a warning otherwise) |
| `SWAP_SIZE_MB` | no | `2048` | Swap file size in MB, created at `/swapfile` in the container's writable layer |

See `docker-compose.yml` for the full 2-core / 5 GB sizing (memory/CPU caps,
local-only port, in-container swap), `scripts/host_sleep.sh` /
`scripts/host_awake.sh` for the one-command sleep/awake lifecycle, and
`deploy/nginx-resureccion.conf` for the reverse-proxy block behind
`resurreccion.omnivex-academy.xyz`.

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
| `THRESH_MIN_CANDIDATE_EVIDENCE` | `4` | Per-candidate evidence floor: distinct evidence records an individual candidate needs before advancing (session totals don't count) |

The integrity pipeline (after Phase 8, before synthesis) runs 12 named
candidate filters, the verification engine, KDP risk screening, the 7 session
quality gates, and adversarial report validation. Every outcome is persisted
to `integrity_assessments` and visible via `GET /api/sessions/{id}/integrity`
— the audit trail of WHY each candidate survived or died. A failing final
report is delivered `PROVISIONAL` with the failures listed in the report
itself, never silently.

## Deep discovery (Stage 5)

Deterministic, model-free layers that run alongside the 9-phase methodology.
They only ORDER and ALLOCATE work (and gather measured inputs): none of them
can override a filter, a quality gate, a KDP risk screen or the verification
engine.

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `CENSUS_ENABLED` | no | `true` | Walk the bestseller/new-release charts and the browse-node tree BEFORE Phase 1, so discovery reasons over a measured map instead of the model's imagination |
| `CENSUS_MAX_CATEGORIES` | no | `12` | Browse nodes visited per marketplace (0 disables the census) |
| `CENSUS_ITEMS_PER_CATEGORY` | no | `20` | Items (rank/price/rating/reviews) captured per node |
| `CENSUS_MAX_MARKETPLACES` | no | `1` | Marketplaces censused — keep `1` on a 2-core box |
| `CENSUS_TIME_BUDGET_SECONDS` | no | `600` | Wall-clock cap for the whole census walk |
| `FRONTIER_ENABLED` | no | `true` | Autocomplete demand-language miner: seed → append a–z → deepen, against Amazon's real suggestion endpoint (one page load per marketplace, no login needed) |
| `FRONTIER_MAX_QUERIES` | no | `240` | Total suggestion queries per run |
| `FRONTIER_MAX_DEPTH` | no | `2` | Recursive deepening depth (1–4) |
| `FRONTIER_TIME_BUDGET_SECONDS` | no | `420` | Wall-clock cap for the frontier sweep |
| `ADJACENCY_ENABLED` | no | `true` | After competition/demand phases, walk outward from every strong candidate and promote high-headroom neighbours mid-run |
| `ADJACENCY_MAX_NEW` | no | `6` | Neighbours promotable to candidates per adjacency round |
| `ADJACENCY_MIN_WHITESPACE` | no | `0.55` | Measured headroom a neighbour must clear before it becomes a candidate |
| `MEMORY_ENABLED` | no | `true` | Cross-session market memory: verified niches, measured keyword result counts, competitors, near-misses and census stats shared between runs |
| `MEMORY_FRESH_DAYS` | no | `7` | Reuse measurements younger than this as fact; older ones are carried as stale hints the model must cheaply re-verify |
| `MEMORY_MAX_DIGEST` | no | `40` | Memory rows handed to a single run |
| `MODEL_CALLS_prompt` / `MODEL_CALLS_keywords` / `MODEL_CALLS_auto` | no | profile | Override the per-session model-call ceiling for a mode. Defaults come from the mode profile (120/140/160) and the raise is scaled down when `SESSION_HARD_TIMEOUT_HOURS` is under 12 |
| `MODE_ITERATIONS_prompt` / `_keywords` / `_auto` | no | profile | Nine colon-separated integers in phase order (`16:14:12:12:10:8:8:12:8`) overriding that mode's per-phase iteration caps |

External (non-Amazon) research actions — `forum_search`, `google_suggest`,
`goodreads_check` — are not env-configurable: the allow-listed hosts live in
`app/actions.py` (Reddit, Goodreads, Google suggestions) and the phases that
may fire them are fixed in `app/methodology.py`. In Phase 1 they are gated to
autonomous runs, from the second iteration onward, and only once Amazon-side
evidence exists.

## Browser infrastructure (Stage 2)

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `BROWSER_ENGINE` | no | `camoufox` | Research browser engine. Resurrección runs **Camoufox only** (anti-detect Firefox: fingerprint spoofing at the C++ layer — canvas/audio/fonts/WebGL/UA coherent, no headless tells, cross-origin CAPTCHA iframes clickable). Any other value — including the retired `chromium` — is a hard boot error, never a silent fallback |
| `CAMOUFOX_HUMANIZE` | no | `false` | Camoufox: humanized cursor movement on interactive pages (owner taps by hand, so usually off) |
| `CAMOUFOX_GEOIP` | no | `false` | Camoufox: derive timezone/locale/geo from the egress IP so the spoof matches the exit network (downloads a GeoIP DB at first launch; needs the `geoip` extra, included in the Dockerfile) |
| `BROWSER_HEADLESS` | no | `true` | Run the browser headless |
| `BROWSER_CHANNEL` | no | `chromium` | Playwright channel (chromium engine only); bundled chromium supports extensions headless |
| `BROWSER_NO_SANDBOX` | no | auto | Chromium engine: launch with `--no-sandbox`. Auto-detected: on when running as root or inside a container (HF Spaces/Docker), off locally. Set explicitly to `true`/`false` to override. |
| `BROWSER_EXTRA_ARGS` | no | — | Chromium engine: space/comma-separated extra launch args (deploy-specific tuning) |
| `BROWSER_PROXY` | no | — | Outbound proxy for the research browser: `user:pass@host:port`, or `socks5://user:pass@host:port` for a SOCKS5 exit (your Cloudflare WARP bridge on your own VPS — the supported shape). The scheme is preserved. Strongly recommended on cloud hosts: reCAPTCHA/Amazon silently refuse CAPTCHA challenges from datacenter IPs (including HF Spaces). Validated functionally at launch (real handshake with credentials); a broken proxy is never wired into the browser |
| `RELAY_ENABLED` | no | `false` | Start the owner-device egress relay at boot (Settings → Device Relay can also toggle it at runtime) |
| `RELAY_MODE` | no | `phone_first` | Egress order when the device is offline: `phone_first` (device → proxy → direct), `phone_only` (device only), `direct_only` (never use the device) |
| `RELAY_ALLOW_DIRECT` | no | `true` | When `false`, direct server egress is never used — browser traffic is strictly device-or-proxy only |
| `RELAY_TOKEN` | no | — | Explicit relay bearer token; when unset a strong token is generated once and stored under `DATA_DIR/relay_token` (owner-only permissions, never committed) |
| `BROWSER_IDLE_SHUTDOWN_SECONDS` | no | `300` | Auto-stop the browser after idle to free the 2 cores |
| `BROWSER_KEEPALIVE_SECONDS` | no | `45` | Cadence for the invisible same-origin request sent while the model is thinking, so the egress path is not reaped mid-call. Firefox reaps idle connections after ~115s and Cloudflare fronts after ~400s; 45s sits well inside both. It is real traffic through the real proxy — deliberately **not** cursor motion or scrolling, which produce no network traffic and cannot hold a connection open. `0` disables. A non-integer is a loud boot error, never a silent fallback. |
| `BROWSER_DEFAULT_TIMEOUT_SECONDS` | no | `45` | Playwright default action timeout |
| `BROWSER_NAV_TIMEOUT_SECONDS` | no | `60` | Navigation timeout |
| `BROWSER_MAX_OPEN_PAGES` | no | `4` | Page pool cap (2-core budget) |
| `BROWSER_CONTENT_PROCESSORS` | no | `2` | Firefox content processes (`dom.ipc.processCount`) — the main lever for fitting the engine to the host. Each costs a few hundred MB of RAM: keep `2` on a small box, `3`–`4` on a 4-core/8 GB VPS. Clamped to 1–8 so a typo can never starve or overcommit the browser; a non-integer is a loud boot error. The pm2 profile on a 4 vCPU/8 GB box sets `3` |
| `BROWSER_USER_AGENT` | no | — | Optional explicit UA override |
| `BROWSER_LOCALE` | no | `en-US` | Browser locale |
| `BROWSER_TIMEZONE` | no | `America/New_York` | Browser timezone |
| `KDSPY_FIREFOX_PATH` | no | `$DATA_DIR/extensions/kdspy-firefox` | Extracted KDSpy Firefox add-on lives here (installed via Settings — one-tap from Mozilla Add-ons or manual XPI upload; loaded natively by the camoufox engine) |
| `KDSPY_AMO_URL` | no | The verified KDSpy v5.13.56 download on addons.mozilla.org | One-tap Firefox add-on install source. Must be an `https://addons.mozilla.org/…​.xpi` URL (enforced in code); a version bump is just a new URL here |
| `KDSPY_MIN_VERSION` | no | — | Optional minimum version enforcement |
| `BROWSER_LOGIN_WINDOW_SECONDS` | no | `900` | Interactive browser session TTL (Amazon sign-in / KDSpy setup from the phone). Hard-capped at 2700s |
| `BROWSER_LOGIN_SECRET` | recommended | — | Shared secret required to open a manual login window (defense in depth on top of owner auth) |

**KDSpy Pro:** the engine loads the **KDSpy Firefox add-on** natively —
one tap in Settings installs it straight from Mozilla Add-ons (or upload the
XPI manually; `.xpi` or `.zip` both accepted), and the browser then opens for
the kdspy.com sign-in so the owner's subscription applies to the persistent
profile. Every research run gets the KDSpy panel without leaving the
anti-detect engine.

**Egress with cloudflare-warp (your own VPS as the exit — the supported
shape):** WARP needs root + a TUN device, so it cannot run inside an HF Space
container — run it on the VPS and point `BROWSER_PROXY` at it. Two supported
shapes:

1. `cloudflare-warp` on the VPS in proxy mode (`warp-cli mode proxy`, local
   SOCKS5 on :40000) behind an authenticated SOCKS5 front (3proxy from
   `scripts/vps_proxy_setup.sh`), or a small `wgcf`/`wireguard-go` userspace
   tunnel + SOCKS5 listener. Then set `BROWSER_PROXY=socks5://user:pass@vps:port`.
2. Plain CONNECT proxy on the VPS without WARP (the default
   `scripts/vps_proxy_setup.sh` path) — `BROWSER_PROXY=user:pass@vps:port`.

The launch gate now checks the proxy FUNCTIONALLY (a real CONNECT/SOCKS5
handshake, credentials included) — a proxy whose credentials are wrong or
whose egress is dead is never wired into the browser; the app demotes to
direct egress (or refuses to launch with `RELAY_ALLOW_DIRECT=false`) and
says exactly why in Settings → Browser egress.

What WARP does and does not buy: it hides the HF Space IP and gives you a
Cloudflare egress, and Cloudflare will not reject its own network —
WARP → Cloudflare-fronted sites is fine. But a WARP exit is still a
Cloudflare datacenter IP: it does not carry residential trust with Amazon
(not Cloudflare-fronted). Camoufox covers the fingerprint side; WARP covers
the IP-hiding side.

Both the browser binary and its dependencies are installed by the provided
`Dockerfile` — it is the reference deployment for Hugging Face Spaces (see
the Deployment section in `README.md`). Camoufox is fetched at image build
time (`camoufox fetch`, run as the runtime user).

## Amazon / KDSpy authentication model

- The owner opens a **one-shot login window** (owner session + `X-Login-Secret`
  header required). The persistent Camoufox profile
  (`$DATA_DIR/browser_profiles/kdspy`) keeps cookies/localStorage so future
  research runs reuse the authenticated session without re-asking.
- Raw Amazon cookies may alternatively be imported once via
  `POST /api/browser/auth/amazon/cookies` (Amazon domains only).
- Credentials/cookies are NEVER stored in the DB, logs, reports, or exports —
  only statuses (authenticated / login_required / captcha_required /
  otp_required / signed_out / error) and diagnostics.
