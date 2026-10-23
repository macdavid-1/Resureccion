#!/bin/sh
# Provision a standalone VPS for Resurrección (4 vCPU / 8 GB reference, but it
# adapts to whatever it runs on). Idempotent — safe to re-run.
#
#   sudo sh scripts/vps_provision.sh              # full provisioning
#   sudo sh scripts/vps_provision.sh --no-swap    # keep the host's own swap
#   sudo sh scripts/vps_provision.sh --with-ufw   # also lock the firewall
#   sudo sh scripts/vps_provision.sh --domain=X   # serve domain X instead
#
# What it does, in order:
#   1. installs system packages (python, node for the PDF sidecar, browser libs)
#   2. creates a 4 GB swap file with low swappiness — the safety net that keeps
#      a runaway Firefox from being OOM-killed mid-research
#   3. builds the project virtualenv and installs Python deps
#   4. installs pm2 and makes it start on boot
#   5. downloads the Camoufox browser binary
#   6. reverse proxy (Caddy): HTTPS for your domain, certificates renewed
#      automatically, service started on boot
#   7. optional firewall (SSH + 80 + 443 + app port)
#
# It does NOT touch your secrets. Put OWNER_PASSWORD_HASH, AUTH_SECRET and
# MODEL_API_KEY in the project's .env file yourself (see VPS.md) — this script
# never reads or writes it.
set -eu

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)

NO_SWAP=0
WANT_UFW=0
# Domain the reverse proxy serves. --domain= overrides; deploy forwards it.
APP_DOMAIN="resurreccion.omnivex-academy.xyz"
for arg in "$@"; do
  case "$arg" in
    --no-swap)  NO_SWAP=1 ;;
    --with-ufw) WANT_UFW=1 ;;
    --domain=*) APP_DOMAIN="${arg#--domain=}" ;;
    -h|--help)  sed -n '2,22p' "$0"; exit 0 ;;
    *)          echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done
if [ -z "$APP_DOMAIN" ]; then
  echo "--domain= needs a value, e.g. --domain=app.example.com" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "This script needs root for packages and swap. Re-run with sudo." >&2
  exit 1
fi

say() { printf '\n==> %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
say "Installing system packages"

if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y

  # libasound2 was renamed to libasound2t64 by the 64-bit time_t transition
  # (Ubuntu 24.04+, Debian 13+). On those releases `libasound2` is a virtual
  # package with SEVERAL providers, so apt refuses to choose one and the
  # whole install dies with "Package 'libasound2' has no installation
  # candidate". Ask apt which name this distro actually ships. Must run
  # after `apt-get update` so the package lists are present.
  if apt-cache show libasound2t64 >/dev/null 2>&1; then
    ALSA_PKG=libasound2t64
  else
    ALSA_PKG=libasound2
  fi

  # Core tooling + the shared-library set Firefox/Camoufox needs at runtime
  # (GTK, DBus, X11/XCB, PulseAudio, XT). Without these the browser binary is
  # present but will not start.
  apt-get install -y --no-install-recommends \
    curl ca-certificates git gnupg ufw \
    build-essential \
    libnss3 libnspr4 libdbus-1-3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libatspi2.0-0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 "$ALSA_PKG" \
    libpango-1.0-0 libcairo2 \
    libgtk-3-0 libdbus-glib-1-2 libx11-xcb1 libxt6 libpci3 \
    fonts-liberation fonts-noto-core fonts-noto-cjk
  apt-get clean
else
  echo "No apt-get found — install python3.11+, nodejs 20+, git and the" >&2
  echo "Firefox runtime libraries manually, then re-run." >&2
  exit 1
fi

# Node 22 for the PDFKit sidecar (report -> 6x9in PDF).
if ! command -v node >/dev/null 2>&1 || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  say "Installing Node.js 22"
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y --no-install-recommends nodejs
fi

# ---------------------------------------------------------------------------
# 2. Swap — the OOM safety net
# ---------------------------------------------------------------------------
MEM_KB=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
MEM_GB=$((MEM_KB / 1024 / 1024))

if [ "$NO_SWAP" -eq 1 ]; then
  say "Swap: left as-is (--no-swap)"
elif swapon --show | grep -q .; then
  say "Swap: already present, leaving it alone"
  swapon --show
else
  # 4 GB is enough to absorb a spike without turning the box into a slideshow.
  SWAP_GB=4
  [ "$MEM_GB" -ge 16 ] && SWAP_GB=8
  say "Creating ${SWAP_GB} GB swap file"
  fallocate -l "${SWAP_GB}G" /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=$((SWAP_GB * 1024))
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Low swappiness: prefer reclaiming the browser's cache over thrashing, and
# prefer killing a runaway process over stalling the whole host.
if [ "$NO_SWAP" -eq 0 ]; then
  sysctl -w vm.swappiness=10 >/dev/null
  grep -q '^vm.swappiness' /etc/sysctl.conf \
    || echo 'vm.swappiness=10' >> /etc/sysctl.conf
fi

# ---------------------------------------------------------------------------
# 3. Project virtualenv + Python dependencies
# ---------------------------------------------------------------------------
# pyproject requires >=3.11; prefer the newest available interpreter.
PY_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then PY_BIN="$candidate"; break; fi
done
if [ -z "$PY_BIN" ]; then
  echo "No python3 found — install Python 3.11+ and re-run." >&2
  exit 1
fi
PY_VER=$("$PY_BIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
say "Using $PY_BIN (Python $PY_VER) in $PROJECT_DIR"
case "$PY_VER" in
  3.1[1-9]|3.[2-9]*) : ;;
  *) echo "WARNING: Python $PY_VER is older than the recommended 3.11+." >&2 ;;
esac

# Debian/Ubuntu ship ensurepip in a separate `<interpreter>-venv` package.
# Without it `python -m venv` dies with "ensurepip is not available" and
# leaves a half-built .venv behind — bin/python exists, bin/pip does not.
# So the gate checks for pip too (a husk from a failed run gets rebuilt),
# and the matching venv package is installed for the interpreter we picked
# BEFORE creating. This must come after `apt-get update` (section 1).
if [ ! -x .venv/bin/python ] || [ ! -x .venv/bin/pip ]; then
  if [ -d .venv ]; then
    say "Removing incomplete virtualenv (python present, pip missing)"
    rm -rf .venv
  fi
  if [ "$PY_BIN" = "python3" ]; then
    VENV_PKG=python3-venv   # meta-package: follows whatever python3 points at
  else
    VENV_PKG="${PY_BIN}-venv"   # python3.12 -> python3.12-venv
  fi
  # `dpkg -s` exits 0 even for a package whose files were removed but whose
  # config remains — check the Status field, not just the exit code.
  if command -v apt-get >/dev/null 2>&1 && \
     ! dpkg -s "$VENV_PKG" 2>/dev/null | grep -q '^Status: install ok installed'; then
    say "Installing $VENV_PKG (Debian/Ubuntu keeps ensurepip there)"
    apt-get install -y --no-install-recommends "$VENV_PKG" || \
      echo "NOTE: $VENV_PKG not installable; 'python -m venv' may fail" >&2
  fi
  say "Creating virtualenv"
  "$PY_BIN" -m venv .venv
fi
say "Installing Python dependencies"
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -e .

# ---------------------------------------------------------------------------
# 4. pm2
# ---------------------------------------------------------------------------
if ! command -v pm2 >/dev/null 2>&1; then
  say "Installing pm2"
  npm install -g pm2 --no-audit --no-fund
fi
say "Registering pm2 to start on boot"
pm2 startup systemd -u "$(logname 2>/dev/null || echo root)" --hp "/home/$(logname 2>/dev/null || echo root)" >/dev/null 2>&1 || \
  echo "NOTE: pm2 startup could not be registered automatically; run it yourself." >&2

# ---------------------------------------------------------------------------
# 5. Camoufox browser binary
# ---------------------------------------------------------------------------
say "Ensuring the Camoufox browser binary is present"
.venv/bin/python scripts/camoufox_ensure.py || \
  echo "WARNING: camoufox fetch failed (usually no network). run.sh will retry at boot." >&2

# ---------------------------------------------------------------------------
# 6. Reverse proxy — HTTPS for the domain (Caddy)
# ---------------------------------------------------------------------------
# Caddy terminates HTTPS and proxies to the app on 127.0.0.1:7860. It
# obtains and renews its own Let's Encrypt certificate — the validation
# request arrives through Cloudflare's proxy on port 80 — and its systemd
# service starts on boot. Everything here is deliberately non-fatal: a
# proxy problem must never stop the app itself from coming up.
say "Setting up the reverse proxy (Caddy)"

caddy_install() {
  command -v caddy >/dev/null 2>&1 && return 0
  # Distro package first — nothing extra to trust.
  if apt-cache show caddy >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends caddy && return 0
  fi
  # Not in this release's archives (e.g. Ubuntu < 24.04): official repo.
  # https://caddyserver.com/docs/install#debian-ubuntu-raspbian
  say "Adding the official Caddy apt repository"
  KEYRING=/usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' -o "$KEYRING.new" \
    || { rm -f "$KEYRING.new"; return 1; }
  gpg --dearmor < "$KEYRING.new" > "$KEYRING" || { rm -f "$KEYRING.new"; return 1; }
  rm -f "$KEYRING.new"
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    -o /etc/apt/sources.list.d/caddy-stable.list || return 1
  chmod o+r "$KEYRING" /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y --no-install-recommends caddy
}

# A web server already holding :80/:443 (nginx?) would block caddy — worth
# saying out loud while caddy is still absent.
if ! command -v caddy >/dev/null 2>&1 \
   && ss -lnt 2>/dev/null | grep -qE ':(80|443)[[:space:]]'; then
  echo "NOTE: something is already listening on :80/:443 — if caddy fails" >&2
  echo "      to start, stop that service first (systemctl status)" >&2
fi

if caddy_install; then
  CADDYFILE=/etc/caddy/Caddyfile
  # Never clobber a config we did not write: keep one backup, then install
  # ours (the pristine package placeholder gets the same treatment).
  if [ -f "$CADDYFILE" ] && ! grep -q '# Managed by Resurreccion' "$CADDYFILE"; then
    say "Existing $CADDYFILE is not ours — keeping a copy at $CADDYFILE.pre-resurreccion"
    # Preserve exactly what is about to be replaced — unless that exact
    # content is already stored (no churn on re-runs). One generation.
    if [ ! -f "$CADDYFILE.pre-resurreccion" ] \
       || ! cmp -s "$CADDYFILE" "$CADDYFILE.pre-resurreccion"; then
      cp "$CADDYFILE" "$CADDYFILE.pre-resurreccion" 2>/dev/null || true
    fi
  fi
  NEW_CFG=$(mktemp)
  cat > "$NEW_CFG" <<CEOF
# Managed by Resurreccion — scripts/vps_provision.sh writes this file.
# To serve a different host, re-run the deploy script with --domain=<host>.
# The app keeps listening on 127.0.0.1:7860 — match PORT in .env if you
# change it there.
$APP_DOMAIN {
    reverse_proxy 127.0.0.1:7860
}
CEOF
  CADDY_CHANGED=0
  if cmp -s "$NEW_CFG" "$CADDYFILE" 2>/dev/null; then
    : # identical — nothing to do
  elif mkdir -p /etc/caddy && install -m 644 "$NEW_CFG" "$CADDYFILE"; then
    CADDY_CHANGED=1
  else
    echo "WARNING: could not write $CADDYFILE — https://$APP_DOMAIN will not work" >&2
  fi
  rm -f "$NEW_CFG"

  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable caddy >/dev/null 2>&1 \
      || echo "NOTE: could not enable caddy on boot — run: systemctl enable caddy" >&2
    if [ "$CADDY_CHANGED" -eq 1 ]; then
      systemctl restart caddy >/dev/null 2>&1 || true
    else
      systemctl start caddy >/dev/null 2>&1 || true
    fi
    sleep 1
    if systemctl is-active --quiet caddy; then
      say "Caddy is up: https://$APP_DOMAIN -> 127.0.0.1:7860"
    else
      echo "WARNING: caddy is not running — check: journalctl -u caddy -n 30" >&2
    fi
  else
    echo "NOTE: no systemd here — start it manually:" >&2
    echo "      caddy run --config /etc/caddy/Caddyfile" >&2
  fi
else
  echo "WARNING: Caddy could not be installed — the app still runs on" >&2
  echo "         127.0.0.1:7860, but https://$APP_DOMAIN needs a proxy." >&2
fi

# ---------------------------------------------------------------------------
# 7. Optional firewall
# ---------------------------------------------------------------------------
if [ "$WANT_UFW" -eq 1 ]; then
  say "Locking the firewall (SSH + HTTP/HTTPS + app port)"
  SSH_PORT=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')
  SSH_PORT=${SSH_PORT:-22}
  ufw allow "${SSH_PORT}/tcp" >/dev/null   # never lock yourself out
  ufw allow 80/tcp >/dev/null              # Caddy: HTTPS + certificate renewal
  ufw allow 443/tcp >/dev/null             # Caddy: HTTPS
  ufw allow 7860/tcp >/dev/null            # Resurrección (direct access)
  ufw --force enable >/dev/null
  ufw status verbose
else
  say "Firewall: not changed (pass --with-ufw to allow only SSH + 80/443 + 7860)"
fi

# ---------------------------------------------------------------------------
say "Done. Next steps (see VPS.md):"
echo "  1. Create .env next to run.sh with OWNER_PASSWORD_HASH, AUTH_SECRET, MODEL_API_KEY"
echo "     (generate the hash:  .venv/bin/python -m app.scripts.set_password)"
echo "  2. Start:    pm2 start ecosystem.config.cjs"
echo "  3. Persist:  pm2 save && pm2 logs resurreccion"
echo "  4. Verify:   curl -fsS http://127.0.0.1:7860/api/health"
