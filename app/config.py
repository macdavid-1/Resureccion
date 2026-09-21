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
        self.owner_password_hash = os.environ.get("OWNER_PASSWORD_HASH", "").strip()
        self.auth_secret = os.environ.get("AUTH_SECRET", "").strip()
        self.auth_session_ttl_hours = _int_env("SESSION_TTL_HOURS", 24 * 7)
        self.auth_cookie_name = "resurreccion_session"

        # --- research ----------------------------------------------------------
        self.max_sessions = _int_env("MAX_SESSIONS", 5)
        self.max_concurrent_sessions = _int_env("MAX_CONCURRENT_SESSIONS", 1)
        self.session_soft_timeout_hours = _int_env("SESSION_SOFT_TIMEOUT_HOURS", 8)
        self.session_hard_timeout_hours = _int_env("SESSION_HARD_TIMEOUT_HOURS", 12)
        self.heartbeat_timeout_seconds = _int_env("HEARTBEAT_TIMEOUT_SECONDS", 180)
        self.checkpoint_interval_seconds = _int_env("CHECKPOINT_INTERVAL_SECONDS", 120)

        # --- research model (OpenAI-compatible; DeepInfra GLM-5.3-Flash default)
        # GLM-5.3-Flash: native vision + strong reasoning + prompt caching —
        # it reasons, orchestrates, reads screenshots, and emits JSON actions.
        self.model_api_key = os.environ.get("MODEL_API_KEY", "").strip()
        self.model_base_url = os.environ.get("MODEL_BASE_URL", "https://api.deepinfra.com/v1/openai").strip()
        self.model_name = os.environ.get("MODEL_NAME", "zai-org/GLM-5.3-Flash").strip()
        self.model_max_tokens = _int_env("MODEL_MAX_TOKENS", 8192)
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
        # Persistent Chromium profiles live under DATA_DIR so they survive restarts.
        self.browser_profiles_dir = self.data_dir / "browser_profiles"
        self.browser_headless = os.environ.get("BROWSER_HEADLESS", "true").strip().lower() in (
            "1", "true", "yes", "on"
        )
        self.browser_channel = os.environ.get("BROWSER_CHANNEL", "chromium").strip()
        self.browser_idle_shutdown_seconds = _int_env("BROWSER_IDLE_SHUTDOWN_SECONDS", 300)
        self.browser_default_timeout_seconds = _int_env("BROWSER_DEFAULT_TIMEOUT_SECONDS", 45)
        self.browser_navigation_timeout_seconds = _int_env("BROWSER_NAV_TIMEOUT_SECONDS", 60)
        self.browser_max_open_pages = _int_env("BROWSER_MAX_OPEN_PAGES", 4)
        self.browser_user_agent = os.environ.get("BROWSER_USER_AGENT", "").strip()
        self.browser_locale = os.environ.get("BROWSER_LOCALE", "en-US").strip()
        self.browser_timezone = os.environ.get("BROWSER_TIMEZONE", "America/New_York").strip()
        # Containers (Hugging Face Spaces, Docker) run Chromium as root without
        # user namespaces — Chromium refuses to start without --no-sandbox there.
        # Default: auto-detect (root or container); overridable via env.
        container_detected = os.geteuid() == 0 if hasattr(os, "geteuid") else False
        if not container_detected and Path("/.dockerenv").exists():
            container_detected = True
        self.browser_no_sandbox = os.environ.get(
            "BROWSER_NO_SANDBOX", "true" if container_detected else "false"
        ).strip().lower() in ("1", "true", "yes", "on")
        # Space/comma separated extra Chromium args for deploy-specific tuning.
        self.browser_extra_args = [
            a for a in os.environ.get("BROWSER_EXTRA_ARGS", "").replace(",", " ").split() if a
        ]
        # Outbound proxy for the research browser (Chromium --proxy-server).
        # Cloud hosts (HF Spaces) run on datacenter IPs that anti-bot systems
        # (reCAPTCHA, Amazon) silently distrust; a residential/quality proxy
        # is the supported way for the owner to fix that. Optional.
        # Accepts host:port, or user:pass@host:port (preferred via env so the
        # secret lives outside the codebase).
        self.browser_proxy = os.environ.get("BROWSER_PROXY", "").strip()
        # Interactive (owner-driven) sessions open pages at a phone-class
        # viewport: the owner taps the live stream from a phone, so a 1:1
        # page scale keeps every control full-size and taps land exactly.
        # Research pages keep the wide desktop viewport for KDSpy panels.
        self.browser_interactive_viewport_w = _int_env("BROWSER_INTERACTIVE_VIEWPORT_W", 390)
        self.browser_interactive_viewport_h = _int_env("BROWSER_INTERACTIVE_VIEWPORT_H", 844)
        # Mobile Chrome UA for owner-driven pages. Empty = keep the desktop UA
        # (not recommended: mobile UA is what makes sites serve touch layouts).
        self.browser_interactive_user_agent = os.environ.get(
            "BROWSER_INTERACTIVE_USER_AGENT",
            "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36",
        ).strip()
        self.browser_downloads_dir = self.data_dir / "browser_downloads"

        # KDSpy Pro extension: unpacked extension directory (owner-supplied).
        self.kdspy_extension_path = Path(
            os.environ.get("KDSPY_EXTENSION_PATH", str(self.data_dir / "extensions" / "kdspy"))
        ).expanduser().resolve()
        self.kdspy_expected_min_version = os.environ.get("KDSPY_MIN_VERSION", "").strip()
        self.kdspy_extension_id = os.environ.get("KDSPY_EXTENSION_ID", "").strip()
        # Chrome Web Store ID of KDSpy — pins which package the one-tap store
        # installer may ever accept (CRX3 key must hash to this ID).
        self.kdspy_webstore_id = os.environ.get("KDSPY_WEBSTORE_ID", "").strip()

        # Login window security: owner must be authenticated to Resurrección and
        # a shared secret must be presented to open the one-shot login window.
        self.browser_login_window_seconds = _int_env("BROWSER_LOGIN_WINDOW_SECONDS", 900)
        self.browser_login_window_secret = os.environ.get("BROWSER_LOGIN_SECRET", "").strip()

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
        ):
            d.mkdir(parents=True, exist_ok=True)

    def validate_auth(self) -> None:
        """Fail loudly at boot if single-user auth isn't configured."""
        problems: list[str] = []
        if not self.owner_password_hash:
            problems.append(
                "OWNER_PASSWORD_HASH is not set. Generate one with: "
                "python -m app.scripts.set_password"
            )
        if not self.auth_secret:
            problems.append("AUTH_SECRET is not set. Set a long random string.")
        if problems:
            raise RuntimeError("; ".join(problems))


@lru_cache(maxsize=1)
def get_config() -> Config:
    return Config()
