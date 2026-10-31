"""Env-driven configuration for Resurrección.

All configuration comes from environment variables so the app can be
configured entirely through the Hugging Face Space settings UI. Nothing is
hardcoded and no secrets live in the repo.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


# Research-model defaults. Defined HERE (not in model_client) so there is one
# source of truth: model_client imports these, and config uses them. Keeping
# them duplicated is how a model switch ends up half-applied.
DEFAULT_BASE_URL = "https://api.deepinfra.com/v1/openai"
# MiMo-V2.6-Flash (Xiaomi): natively multimodal (text/image/video/audio), 1M
# context, and a thinking/reasoning model — which is why reasoning_effort is
# sent below. Supersedes GLM-5.3-Flash.
DEFAULT_MODEL = "XiaomiMiMo/MiMo-V2.6-Flash"

# DeepInfra documents these reasoning strengths for reasoning-capable models.
_REASONING_EFFORTS = ("none", "low", "medium", "high")


class Config:
    """Central runtime configuration. Instantiated once per process."""

    def __init__(self) -> None:
        self.data_dir = Path(os.environ.get("DATA_DIR", "./data")).resolve()
        self.db_path = self.data_dir / "resurreccion.db"
        self.artifacts_dir = self.data_dir / "artifacts"
        self.uploads_dir = self.data_dir / "uploads"
        self.exports_dir = self.data_dir / "exports"
        self.reports_dir = self.data_dir / "reports"

        # --- single-user auth -------------------------------------------------
        self.owner_username = os.environ.get("OWNER_USERNAME", "owner").strip()
        # OWNER_PASSWORD is the human-facing knob: set it to the literal
        # password typed at the sign-in screen and the scrypt hash is derived
        # below at boot (the users row is re-synced from env on every start,
        # so .env wins after a restart). It takes precedence over
        # OWNER_PASSWORD_HASH when both are set — a stale hash silently
        # locking the owner out of their own single-user app is exactly the
        # failure this prevents: the owner edits a password line, not a hash.
        self.owner_password = os.environ.get("OWNER_PASSWORD", "").strip()
        self.owner_password_hash = os.environ.get("OWNER_PASSWORD_HASH", "").strip()
        if self.owner_password:
            self.owner_password_hash = self._hash_owner_password(self.owner_password)
        self.auth_secret = os.environ.get("AUTH_SECRET", "").strip()
        self.auth_session_ttl_hours = _int_env("SESSION_TTL_HOURS", 24 * 7)
        self.auth_cookie_name = "resurreccion_session"
        # Set the auth cookie's Secure flag when the app is served over HTTPS
        # (custom domain behind a TLS reverse proxy). Browsers will then only
        # send the cookie over https://, so it cannot be sniffed in transit.
        # Off by default so plain-http LAN previews keep working.
        self.cookie_secure = os.environ.get("COOKIE_SECURE", "").strip().lower() in (
            "1", "true", "yes", "on"
        )

        # --- research ----------------------------------------------------------
        self.max_sessions = _int_env("MAX_SESSIONS", 5)
        self.max_concurrent_sessions = _int_env("MAX_CONCURRENT_SESSIONS", 1)
        self.session_soft_timeout_hours = _int_env("SESSION_SOFT_TIMEOUT_HOURS", 8)
        self.session_hard_timeout_hours = _int_env("SESSION_HARD_TIMEOUT_HOURS", 12)
        # Strategic focus pivots: after a full research pass in which ZERO
        # candidates survive adversarial verification + the integrity
        # pipeline (deep research and niche-down both failed), an autonomous
        # session may spend another pass re-entering discovery on a fresh
        # focus. 0 disables the behavior entirely.
        self.max_focus_pivots = max(0, min(3, _int_env("MAX_FOCUS_PIVOTS", 1)))
        # Niche TARGET: the number of verified niches the owner asked for.
        # The brief itself wins when it states a target ("I need 10 niches",
        # "niche target of 12", or an owner message saying the same); this
        # env var is the default for briefs that don't state one. 0 = no
        # target (the old behavior: whatever survives verification stands).
        # When verified candidates fall short of the target after Phase 8,
        # the runner re-opens the earlier phases to rediscover more niches —
        # bounded by MAX_TARGET_REFILLS and the session's time budget.
        self.target_niches = max(0, _int_env("TARGET_NICHES", 0))
        self.max_target_refills = max(0, _int_env("MAX_TARGET_REFILLS", 2))
        self.heartbeat_timeout_seconds = _int_env("HEARTBEAT_TIMEOUT_SECONDS", 180)
        self.checkpoint_interval_seconds = _int_env("CHECKPOINT_INTERVAL_SECONDS", 120)
        # Per-mode budget profiles (deterministic; see app.methodology
        # MODE_PROFILES). Each mode shifts iteration caps between phases and
        # the per-session model-call ceiling: a detailed brief (mode
        # 'prompt') spends on competition/verification, an empty brief (mode
        # 'auto') spends on discovery/niching. 0/empty = use the profile.
        self.mode_model_calls = {
            mode: max(0, _int_env(f"MODEL_CALLS_{mode}", 0))
            for mode in ("prompt", "keywords", "auto")
        }
        # Compact per-phase override: MODE_ITERATIONS_auto=16:14:12:12:10:8:8:12:8
        # (nine colon-separated integers in PHASE_ORDER). Empty = profile.
        self.mode_iterations = {
            mode: os.environ.get(f"MODE_ITERATIONS_{mode}", "").strip()
            for mode in ("prompt", "keywords", "auto")
        }
        # --- research depth (how much of the time budget research may spend) --
        # A run used to finish in 1-2 hours on an 8-hour budget: every phase
        # satisfied its floors on the first pass and the session simply ran
        # out of things to do while hours remained. Two deterministic knobs
        # stop that, and BOTH are disabled by setting them to 0:
        #   - research_min_hours: while the session is under this wall-clock
        #     mark, a phase owes DEPTH_EVIDENCE_MULTIPLIER times its normal
        #     new-evidence floor before it may close (the model sees the
        #     raised number in its exit gate and keeps researching).
        #   - model_calls_per_hour: the session's model-call ceiling is
        #     derived from the time budget instead of being a fixed cap, so
        #     an unfinished run is never stopped by a call budget. 45/hour
        #     against the 12-hour hard budget = 540 calls per session; a full
        #     two-pass methodology uses ~300, so the ceiling only ever binds
        #     on a runaway loop, never on honest work (it never approaches a
        #     multi-million-token bill: calls are capped before they run).
        self.research_min_hours = max(
            0.0, float(os.environ.get("RESEARCH_MIN_HOURS", "3").strip() or "3")
        )
        self.depth_evidence_multiplier = max(
            1, _int_env("DEPTH_EVIDENCE_MULTIPLIER", 2)
        )
        self.model_calls_per_hour = max(1, _int_env("MODEL_CALLS_PER_HOUR", 45))

        # --- deep-discovery layers (deterministic, model-free) ----------------
        # Autocomplete frontier: seed -> append letters -> recursive
        # completion mining on Amazon's real suggestion endpoint. The demand
        # LANGUAGE buyers actually type, gathered as ordinary evidence.
        self.frontier_enabled = _flag("FRONTIER_ENABLED", True)
        self.frontier_max_queries = max(0, _int_env("FRONTIER_MAX_QUERIES", 240))
        self.frontier_max_depth = max(1, min(_int_env("FRONTIER_MAX_DEPTH", 2), 4))
        self.frontier_time_budget_seconds = max(
            30, _int_env("FRONTIER_TIME_BUDGET_SECONDS", 420)
        )
        # Category census: a deterministic walk of the browse-node tree and
        # the bestseller/new-release charts BEFORE the model opines, so
        # discovery reasons over a measured census instead of guessing where
        # to look. Cannot discover a category that was never visited.
        self.census_enabled = _flag("CENSUS_ENABLED", True)
        self.census_max_categories = max(0, _int_env("CENSUS_MAX_CATEGORIES", 12))
        self.census_items_per_category = max(0, _int_env("CENSUS_ITEMS_PER_CATEGORY", 20))
        self.census_max_marketplaces = max(1, _int_env("CENSUS_MAX_MARKETPLACES", 1))
        self.census_time_budget_seconds = max(
            60, _int_env("CENSUS_TIME_BUDGET_SECONDS", 600)
        )
        # Adjacency expansion: walk outward from every strong candidate
        # (related carousels, category siblings, autocomplete of its keyword)
        # and promote high-headroom neighbors to candidates mid-run.
        self.adjacency_enabled = _flag("ADJACENCY_ENABLED", True)
        self.adjacency_max_new = max(0, _int_env("ADJACENCY_MAX_NEW", 6))
        # Neighbors below this headroom score are recorded but not promoted.
        self.adjacency_min_whitespace = float(
            os.environ.get("ADJACENCY_MIN_WHITESPACE", "0.55").strip() or "0.55"
        )
        # Final-candidate deep dive (deterministic, runs once the report's
        # candidates are chosen, before the report is written): measured
        # backend keywords per surviving niche plus SEPARATE category maps
        # for the Kindle Store and the Books store (different category
        # systems). Every figure lands in browser_evidence as evidence.
        self.dive_enabled = _flag("DIVE_ENABLED", True)
        # Default covers synthesis' max_opportunities (12): every niche the
        # report SHOWS must carry its measured keywords and category maps.
        self.dive_max_candidates = max(1, _int_env("DIVE_MAX_CANDIDATES", 12))
        self.dive_max_keywords = max(0, _int_env("DIVE_MAX_KEYWORDS", 20))
        self.dive_max_categories = max(0, _int_env("DIVE_MAX_CATEGORIES", 10))
        self.dive_products_per_store = max(1, _int_env("DIVE_PRODUCTS_PER_STORE", 6))
        self.dive_autocomplete_queries = max(
            0, _int_env("DIVE_AUTOCOMPLETE_QUERIES", 24)
        )
        self.dive_time_budget_seconds = max(
            60, _int_env("DIVE_TIME_BUDGET_SECONDS", 300)
        )
        # Cross-session market memory: verified niches, measured keyword
        # result counts, competitors and near-misses, reused by later runs.
        # Evidence fresher than memory_fresh_days is reused as fact; older is
        # carried as a stale hint the model must cheaply re-verify.
        self.memory_enabled = _flag("MEMORY_ENABLED", True)
        self.memory_fresh_days = max(1, _int_env("MEMORY_FRESH_DAYS", 7))
        self.memory_max_digest = max(0, _int_env("MEMORY_MAX_DIGEST", 40))

        # --- research model (OpenAI-compatible; DeepInfra MiMo-V2.6-Flash default)
        # MiMo-V2.6-Flash: a thinking model with native vision and 1M context.
        # It reasons, orchestrates, reads screenshots, and emits JSON actions.
        self.model_api_key = os.environ.get("MODEL_API_KEY", "").strip()
        self.model_base_url = os.environ.get("MODEL_BASE_URL", DEFAULT_BASE_URL).strip()
        self.model_name = os.environ.get("MODEL_NAME", DEFAULT_MODEL).strip()
        # Thinking: MiMo-V2.6-Flash is a reasoning model. DeepInfra's
        # OpenAI-compatible API takes `reasoning_effort` for reasoning models
        # and silently ignores it on models that don't support it, so sending
        # it is safe either way. Turn off with MODEL_THINKING=false.
        self.model_thinking_enabled = os.environ.get("MODEL_THINKING", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        effort = os.environ.get("MODEL_REASONING_EFFORT", "high").strip().lower()
        if effort not in _REASONING_EFFORTS:
            raise RuntimeError(
                f"MODEL_REASONING_EFFORT must be one of {', '.join(_REASONING_EFFORTS)}, "
                f"got {effort!r}"
            )
        self.model_reasoning_effort = effort
        # Completion budget. On an OpenAI-compatible API a reasoning model's
        # thinking tokens are billed against max_tokens, so at high effort it
        # can spend the whole allowance thinking and leave nothing for the
        # answer, hence a generous default. Providers clamp above a model's own
        # ceiling rather than erroring.
        self.model_max_tokens = _int_env("MODEL_MAX_TOKENS", 16384)
        self.model_temperature = float(os.environ.get("MODEL_TEMPERATURE", "0.4").strip() or "0.4")
        self.model_timeout_seconds = _int_env("MODEL_TIMEOUT_SECONDS", 180)
        # Vision: when enabled, the runner attaches the latest browser
        # screenshot to each iteration so the model sees what the browser sees.
        self.model_vision_enabled = os.environ.get("MODEL_VISION_ENABLED", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.model_vision_max_bytes = _int_env("MODEL_VISION_MAX_BYTES", 400_000)
        # Live chain of thought: stream the model response so the owner can
        # watch the reasoning as it is produced. The stream is an enhancement
        # only — any endpoint that will not stream falls back to the plain
        # request automatically, so research never depends on it. Set
        # MODEL_STREAM=false to force the plain request.
        self.model_stream_enabled = _flag("MODEL_STREAM", True)
        # Optional headers for OpenRouter attribution.
        self.app_title = os.environ.get("APP_TITLE", "Resurreccion").strip()

        # --- browser infrastructure -------------------------------------------
        # Persistent browser profiles live under DATA_DIR so they survive
        # restarts. There is exactly ONE engine: Camoufox (anti-detect
        # Firefox). BROWSER_ENGINE exists for deployment clarity; asking for
        # anything else is a hard boot error, never a silent fallback.
        self.browser_profiles_dir = self.data_dir / "browser_profiles"
        engine_env = os.environ.get("BROWSER_ENGINE", "camoufox").strip().lower()
        if engine_env and engine_env != "camoufox":
            raise RuntimeError(
                f"BROWSER_ENGINE={engine_env!r} is not supported. Resurrección "
                "runs Camoufox only — remove BROWSER_ENGINE or set it to camoufox"
            )
        self.browser_engine = "camoufox"
        # Camoufox extras (see app/camoufox_engine.py):
        self.camoufox_humanize = os.environ.get("CAMOUFOX_HUMANIZE", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        # Derive timezone/locale/geo from the egress IP (requires the geoip
        # extra: pip install "camoufox[geoip]"; download at first launch).
        self.camoufox_geoip = os.environ.get("CAMOUFOX_GEOIP", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.browser_headless = os.environ.get("BROWSER_HEADLESS", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.browser_idle_shutdown_seconds = _int_env("BROWSER_IDLE_SHUTDOWN_SECONDS", 300)
        # Keepalive cadence while the model is thinking. A research iteration
        # is dominated by the LLM call (often 30-180s) and nothing loads in the
        # browser during it, so the connection to the current host goes idle.
        # Firefox reaps idle connections after ~115s; Cloudflare fronts after
        # ~400s. One invisible same-origin request per period keeps the proxy
        # path warm well inside the tightest of those. 0 disables.
        self.browser_keepalive_seconds = max(0, min(_int_env("BROWSER_KEEPALIVE_SECONDS", 45), 600))
        self.browser_default_timeout_seconds = _int_env("BROWSER_DEFAULT_TIMEOUT_SECONDS", 45)
        self.browser_navigation_timeout_seconds = _int_env("BROWSER_NAV_TIMEOUT_SECONDS", 60)
        self.browser_max_open_pages = _int_env("BROWSER_MAX_OPEN_PAGES", 4)
        # Firefox content processes (dom.ipc.processCount). Each one is a few
        # hundred MB, so this is the main lever for fitting the engine to the
        # host: 2 on a 2-core/4 GB box, 3-4 when the VPS has room. Clamped to
        # a sane band so a typo can never starve or overcommit the browser.
        self.browser_content_processors = max(1, min(_int_env("BROWSER_CONTENT_PROCESSORS", 2), 8))
        self.browser_user_agent = os.environ.get("BROWSER_USER_AGENT", "").strip()
        self.browser_locale = os.environ.get("BROWSER_LOCALE", "en-US").strip()
        self.browser_timezone = os.environ.get("BROWSER_TIMEZONE", "America/New_York").strip()
        # Outbound proxy for the research browser. Cloud hosts (HF Spaces) run
        # on datacenter IPs that anti-bot systems (reCAPTCHA, Amazon) silently
        # distrust; the supported shape is the owner's own Cloudflare WARP
        # bridge on a VPS (scripts/vps_proxy_setup.sh) exposed as SOCKS5 or a
        # plain CONNECT proxy. Optional.
        # Accepts host:port, or user:pass@host:port (preferred via env so the
        # secret lives outside the codebase).
        self.browser_proxy = os.environ.get("BROWSER_PROXY", "").strip()
        # --- owner-device egress relay ("phone IP" mode) ---------------------
        # Every browser request exits through the owner's own device (mobile
        # carrier / home IP) instead of the server's datacenter IP. See
        # app/relay.py. Optional: without a paired device the browser falls
        # back to BROWSER_PROXY, then direct egress (if allowed).
        self.relay_enabled = os.environ.get("RELAY_ENABLED", "").strip().lower() in (
            "1", "true", "yes", "on"
        )
        # Egress preference when the device is offline:
        #   phone_first  — device → BROWSER_PROXY → direct
        #   phone_only   — device only; requests fail while it is away
        #   direct_only  — never use the device (relay parked)
        self.relay_mode = os.environ.get("RELAY_MODE", "phone_first").strip().lower()
        # Offline fallback: allow direct server egress when neither the
        # device nor BROWSER_PROXY is available. Disabling makes the browser
        # strictly device-or-proxy-only (no datacenter-IP requests at all).
        self.relay_allow_direct = os.environ.get("RELAY_ALLOW_DIRECT", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        # Explicit relay bearer token; when unset, one is generated once and
        # stored atomically under DATA_DIR (never committed anywhere).
        self.relay_token = os.environ.get("RELAY_TOKEN", "").strip()
        # Research pages (and desktop-class Amazon sign-in pages) render at
        # an explicit full-PC viewport: every screenshot shows the whole
        # desktop layout — no fold cutoffs for the AI's scraping or the
        # owner's live view — instead of whatever size the spoofed window
        # happened to get at launch.
        self.browser_viewport_w = _int_env("BROWSER_VIEWPORT_W", 1920)
        self.browser_viewport_h = _int_env("BROWSER_VIEWPORT_H", 1080)
        # Interactive (owner-driven) sessions open pages at a phone-class
        # viewport: the owner taps the live stream from a phone, so a 1:1
        # page scale keeps every control full-size and taps land exactly.
        # Research pages keep the wide desktop viewport for KDSpy panels.
        self.browser_interactive_viewport_w = _int_env("BROWSER_INTERACTIVE_VIEWPORT_W", 390)
        self.browser_interactive_viewport_h = _int_env("BROWSER_INTERACTIVE_VIEWPORT_H", 844)
        # Mobile UA for owner-driven pages — Firefox-shaped by construction:
        # a Chrome UA would contradict the Camoufox engine itself. An explicit
        # Firefox UA passes through unchanged; anything else is ignored in
        # favor of the coherent Firefox-mobile default.
        self.browser_interactive_user_agent = os.environ.get(
            "BROWSER_INTERACTIVE_USER_AGENT",
            "Mozilla/5.0 (Android 13; Mobile; rv:130.0) Gecko/130.0 Firefox/130.0",
        ).strip()
        self.browser_downloads_dir = self.data_dir / "browser_downloads"

        # KDSpy Pro: the Firefox add-on (the ONLY KDSpy package — loaded
        # natively by the Camoufox engine).
        self.kdspy_expected_min_version = os.environ.get("KDSPY_MIN_VERSION", "").strip()
        self.kdspy_firefox_path = Path(
            os.environ.get("KDSPY_FIREFOX_PATH", str(self.data_dir / "extensions" / "kdspy-firefox"))
        ).expanduser().resolve()
        # One-tap Firefox add-on install: the owner-verified AMO download URL
        # (addons.mozilla.org only). A version bump is just a new URL here.
        self.kdspy_amo_url = os.environ.get(
            "KDSPY_AMO_URL",
            "https://addons.mozilla.org/firefox/downloads/file/4802008/kdspy_v5-5.13.56.xpi",
        ).strip()

        # Login window security: owner must be authenticated to Resurrección and
        # a shared secret must be presented to open the one-shot login window.
        self.browser_login_window_seconds = _int_env("BROWSER_LOGIN_WINDOW_SECONDS", 900)
        self.browser_login_window_secret = os.environ.get("BROWSER_LOGIN_SECRET", "").strip()

        # --- automatic backups --------------------------------------------------
        # Crash-safe (WAL+FULL in db.py) is not disk-safe: snapshots via
        # VACUUM INTO into backups_dir on an interval, verified + pruned.
        # See app/backup.py and the Backups section of VPS.md.
        self.backups_dir = Path(
            os.environ.get("BACKUP_DIR", str(self.data_dir / "backups"))
        ).expanduser().resolve()
        self.backup_enabled = os.environ.get("BACKUP_ENABLED", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.backup_interval_hours = max(1, _int_env("BACKUP_INTERVAL_HOURS", 24))
        self.backup_keep = max(1, _int_env("BACKUP_KEEP", 14))

        # --- serving -----------------------------------------------------------
        self.host = os.environ.get("HOST", "0.0.0.0")
        self.port = _int_env("PORT", 7860)
        self.log_level = os.environ.get("LOG_LEVEL", "info").lower()

    # ------------------------------------------------------------------ paths
    def ensure_dirs(self) -> None:
        """Create all persistent storage directories if missing."""
        for d in (
            self.data_dir,
            self.artifacts_dir,
            self.uploads_dir,
            self.browser_profiles_dir,
            self.browser_downloads_dir,
            self.browser_profiles_dir / "kdspy",  # shared research profile
            self.exports_dir,
            self.reports_dir,
            self.backups_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _hash_owner_password(password: str) -> str:
        # Deferred import: app.security imports app.config at module level, so
        # a top-level import here would be circular.
        from app.security import hash_password

        return hash_password(password)

    @staticmethod
    def _looks_like_scrypt_hash(value: str) -> bool:
        parts = value.split("$")
        return len(parts) == 3 and parts[0] == "scrypt" and all(parts[1:])

    def validate_auth(self) -> None:
        """Fail loudly at boot if single-user auth isn't configured."""
        problems: list[str] = []
        if not self.owner_username:
            problems.append(
                "OWNER_USERNAME is empty. Set it, or remove the line to use 'owner'."
            )
        if not self.owner_password_hash:
            problems.append(
                "OWNER_PASSWORD (the login password) is not set. Set it to the password "
                "you want to sign in with, or generate OWNER_PASSWORD_HASH with: "
                "python -m app.scripts.set_password"
            )
        elif not self._looks_like_scrypt_hash(self.owner_password_hash):
            problems.append(
                "OWNER_PASSWORD_HASH is not a scrypt hash — did you paste the password "
                "itself into that line? Set OWNER_PASSWORD instead, or regenerate with: "
                "python -m app.scripts.set_password"
            )
        if not self.auth_secret:
            problems.append("AUTH_SECRET is not set. Set a long random string.")
        if problems:
            raise RuntimeError("; ".join(problems))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
