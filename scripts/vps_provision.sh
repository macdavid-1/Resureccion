#!/bin/sh
# Provision a standalone VPS for Resurrección (4 vCPU / 8 GB reference, but it
# adapts to whatever it runs on). Idempotent — safe to re-run.
#
#   sudo sh scripts/vps_provision.sh              # full provisioning
#   sudo sh scripts/vps_provision.sh --no-swap    # keep the host's own swap
#   sudo sh scripts/vps_provision.sh --with-ufw   # also lock the firewall
#
# What it does, in order:
#   1. installs system packages (python, node for the PDF sidecar, browser libs)
#   2. creates a 4 GB swap file with low swappiness — the safety net that keeps
#      a runaway Firefox from being OOM-killed mid-research
#   3. builds the project virtualenv and installs Python deps
#   4. installs pm2 and makes it start on boot
#   5. downloads the Camoufox browser binary
#
# It does NOT touch your secrets. Put OWNER_PASSWORD_HASH, AUTH_SECRET and
# MODEL_API_KEY in the project's .env file yourself (see VPS.md) — this script
# never reads or writes it.
set -eu

cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)

NO_SWAP=0
WANT_UFW=0
for arg in "$@"; do
  case "$arg" in
    --no-swap)  NO_SWAP=1 ;;
    --with-ufw) WANT_UFW=1 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *)          echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

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

if [ ! -x .venv/bin/python ]; then
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
# 6. Optional firewall
# ---------------------------------------------------------------------------
if [ "$WANT_UFW" -eq 1 ]; then
  say "Locking the firewall (SSH + app port only)"
  SSH_PORT=$(sshd -T 2>/dev/null | awk '/^port /{print $2; exit}')
  SSH_PORT=${SSH_PORT:-22}
  ufw allow "${SSH_PORT}/tcp" >/dev/null   # never lock yourself out
  ufw allow 7860/tcp >/dev/null            # Resurrección
  ufw --force enable >/dev/null
  ufw status verbose
else
  say "Firewall: not changed (pass --with-ufw to allow only SSH + 7860)"
fi

# ---------------------------------------------------------------------------
say "Done. Next steps (see VPS.md):"
echo "  1. Create .env next to run.sh with OWNER_PASSWORD_HASH, AUTH_SECRET, MODEL_API_KEY"
echo "     (generate the hash:  .venv/bin/python -m app.scripts.set_password)"
echo "  2. Start:    pm2 start ecosystem.config.cjs"
echo "  3. Persist:  pm2 save && pm2 logs resurreccion"
echo "  4. Verify:   curl -fsS http://127.0.0.1:7860/api/health"
