"""Env-driven configuration for Resurrección.

All configuration comes from environment variables so the app can be
configured entirely through the Hugging Face Space settings UI. Nothing is
hardcoded and no secrets live in the repo.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


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
        self.heartbeat_timeout_seconds = _int_env("HEARTBEAT_TIMEOUT_SECONDS", 180)
        self.checkpoint_interval_seconds = _int_env("CHECKPOINT_INTERVAL_SECONDS", 120)

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
