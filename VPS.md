# Running Resurrección on your own VPS

This is the guide for putting Resurrección alone on a dedicated server. Every
command is copy-paste.

The reference machine is **4 vCPU / 8 GB RAM** (Ubuntu 22.04 or 24.04, or
Debian 12). It works on bigger or smaller boxes — the setup script sizes swap
to match whatever it runs on.

## What runs on the box

The public path, end to end:

```
you ──HTTPS──> Cloudflare ──> Caddy :80/:443 ──> pm2 ──> run.sh ──> uvicorn (:7860)
                (proxy)       (certificates,      (one                      )
                              renewed by itself)   process)
```

One app, in one process, supervised by pm2:

```
pm2  ──>  run.sh  ──>  uvicorn (Python)
              │
              ├─ loads your .env          (secrets live here, never committed)
              ├─ makes sure Node deps exist (PDF export sidecar)
              └─ makes sure the Camoufox browser binary is downloaded
```

Caddy is installed and configured by the deploy script. It proxies to the
app without changing anything about how the app runs — same port, same
process, same pm2 supervision.

The browser is **inside** the app process, not a separate service. That is why
there is exactly one process, and why pm2 is supervising it rather than
scaling it out.

## 1. Put the code on the server

```bash
git clone <your-repo-url> ~/resurreccion
cd ~/resurreccion
```

## 2. Deploy — one command

```bash
sudo sh scripts/vps_deploy.sh
```

That is the entire deploy. It runs, in order:

1. **Provisions the host** — Python, Node (for the PDF sidecar), the browser's
   system libraries, a **4 GB swap file** with low swappiness, the virtualenv,
   **pm2** registered to start on boot, the **Camoufox browser binary**, and
   **Caddy** — the reverse proxy that serves your domain over HTTPS with
   automatically renewed certificates.
2. **Creates `.env`** with generated secrets: your login password (asked for
   on the terminal; generated and printed once when headless) and a random
   `AUTH_SECRET`. An existing `.env` is never overwritten.
3. **Creates the data directory** (`~/resurreccion-data`) — the SQLite
   database and every artifact live there.
4. **Installs the PDF sidecar's Node dependencies.**
5. **Starts the app under pm2**, saves the process list, and registers the
   boot hook so it survives a reboot.
6. **Waits for `/api/health`**, checks the domain through Cloudflare, then
   prints the URL — or fails loudly with the exact command to look at.

Re-running it is the **update path**: it is idempotent, re-provisions whatever
is missing, leaves `.env` alone, and restarts the app.

Flags are forwarded to the provisioner:

| Flag | Effect |
|---|---|
| `--no-swap` | Leave the host's existing swap alone |
| `--with-ufw` | Also enable the firewall: SSH + 80 + 443 + 7860 |
| `--domain=<host>` | Serve a different domain (default `resurreccion.omnivex-academy.xyz`) |

The firewall is **off by default** so you cannot accidentally lock yourself
out. `--with-ufw` detects your SSH port first and allows it before enabling.

## 3. Your DeepInfra key

`.env` (next to `run.sh`) is where your secrets live. It is loaded
automatically when the app boots and is never committed to git. The deploy
script already wrote your login password hash, `AUTH_SECRET`, `DATA_DIR` and
the model settings into it; the one thing you may still need to add is the API
key:

```bash
nano ~/resurreccion/.env     # set MODEL_API_KEY=<your DeepInfra key>
pm2 restart resurreccion
```

The default model is **MiMo-V2.6-Flash** (`XiaomiMiMo/MiMo-V2.6-Flash`), a
thinking model on DeepInfra. Anything in `.env` overrides the values in
`ecosystem.config.cjs`, so you can tune the app without touching the pm2 file.
Full list: `ENV_VARS.md`.

Prefer editing a plain password instead of a hash? Replace the
`OWNER_PASSWORD_HASH=...` line with `OWNER_PASSWORD=<the password>` — the app
hashes it at boot (quote the value if it contains `$` or spaces).

**Do not put `.env` in git.** It is already ignored.

## 4. First run, from your phone

Everything the research browser needs is set up from Settings — no shell
required:

1. Open `http://<your-server-ip>:7860` — or
   `https://resurreccion.omnivex-academy.xyz` once the domain section below
   is set up — and sign in.
2. **Amazon authentication → Sign in.** A live browser streams to your phone;
   complete the sign-in (captcha/OTP included), then Close. Cookies persist in
   the shared profile, so every future run is already signed in.
3. **KDSpy Pro → Install.** One tap pulls the official Firefox add-on from
   Mozilla Add-ons; the browser then opens on kdspy.com for your account
   sign-in and license activation.
4. **AI model** should read `XiaomiMiMo/MiMo-V2.6-Flash ready`. If it says the
   key is missing, add `MODEL_API_KEY` to `.env` and `pm2 restart resurreccion`.

## Your domain and HTTPS (Caddy + Cloudflare)

`https://resurreccion.omnivex-academy.xyz` is served by **Caddy**, which the
deploy script installs and keeps running:

- It proxies to the app on `127.0.0.1:7860` — the app itself is unchanged.
- It obtains a **Let's Encrypt certificate** on its own — the validation
  request arrives through Cloudflare's proxy on port 80 — and renews it
  automatically, forever. No certbot, no renew cron.
- It **starts on boot** via systemd, alongside pm2's boot hook: after a
  reboot both come back by themselves (Caddy answers 502 for the few
  seconds until the app is up, then serves normally).
- Live research traffic — SSE traces, live view frames, long model calls —
  passes through unbuffered; Caddy streams responses by default, and
  Cloudflare proxies WebSockets and event streams without any extra setup.

### After pulling changes, run this on the VPS

```bash
cd ~/resurreccion        # the directory you cloned into
git pull
sudo sh scripts/vps_deploy.sh
```

It installs Caddy if missing, writes `/etc/caddy/Caddyfile` (a config that
is not ours is backed up to `Caddyfile.pre-resurreccion` first, never
deleted), restarts what needs restarting, verifies the app on `:7860` and
the domain end to end, and prints the URL. `.env` is never touched.

### Cloudflare settings

The DNS record already exists and is **proxied** (orange cloud). What
matters:

| Setting | Value | Why |
|---|---|---|
| DNS | A record `resurreccion.omnivex-academy.xyz` → VPS IP, **proxied** | hides the origin IP |
| SSL/TLS mode | **Full (strict)** | Caddy presents a real Let's Encrypt certificate; strict validates it. On *Flexible*, switch — Flexible + Caddy's http→https redirect can loop. |
| Ports | 80 and 443 proxied (the default) | Let's Encrypt's HTTP-01 check comes in on port 80 |

No other Cloudflare setting needs changing.

### Verify

```bash
systemctl status caddy                       # active (running)
curl -fsSk --resolve resurreccion.omnivex-academy.xyz:443:127.0.0.1 \
     https://resurreccion.omnivex-academy.xyz/api/health   # via Caddy directly
curl -fsS https://resurreccion.omnivex-academy.xyz/api/health  # through Cloudflare
journalctl -u caddy -n 50                    # certificate issues, if any
```

### Make the session cookie HTTPS-only (optional)

Once you always use the https domain:

```bash
nano ~/resurreccion/.env      # add a line: COOKIE_SECURE=true
pm2 restart resurreccion && pm2 save
```

Skip this if you still sign in over `http://<ip>:7860` — a `Secure` cookie
is not sent over plain http, so that login would stop working.

### If the domain does not load

| Symptom | Cause | Fix |
|---|---|---|
| Cloudflare 502/504 | the app is down | `pm2 status`, `pm2 logs resurreccion` |
| Cloudflare 521/522 | nothing answering on the VPS | `systemctl status caddy` |
| Redirect loop | Cloudflare SSL mode is *Flexible* | set it to **Full (strict)** |
| `journalctl -u caddy` shows certificate errors | the HTTP-01 check never reached Caddy | record must be proxied, port 80 open (`--with-ufw` allows it); if "Always Use HTTPS" is on, turn it off until the first certificate is issued |
| Port 7860 open to the internet | no firewall | rerun with `--with-ufw` |

To serve a different hostname later:
`sudo sh scripts/vps_deploy.sh --domain=other.example.com`. If you change
`PORT` in `.env`, change the port in `/etc/caddy/Caddyfile` to match.

## 5. Day-to-day commands

| Task | Command |
|---|---|
| See status | `pm2 status` |
| Watch logs | `pm2 logs resurreccion` |
| Stop (stays stopped — even across a reboot) | `pm2 stop resurreccion && pm2 save` |
| Start (stays running — even across a reboot) | `pm2 start resurreccion && pm2 save` |
| Restart after a config change | `pm2 restart resurreccion` |
| Start automatically on boot | `pm2 startup` (run the command it prints, once) |

The `pm2 save` half is not optional: pm2 resurrects the **saved** list on
boot, so a plain `pm2 stop` would be undone by the next reboot and the app
would wake itself. `pm2 save` is what makes your stop (or start) stick
across reboots.

## How it survives trouble

**Memory guardrail.** pm2 restarts the app if it passes **6 GB** or **90%
sustained CPU**. That is deliberate: a controlled restart is far better than
the kernel reaching in and killing a random process. Restart is safe because
every state change is written to SQLite *before* it is acted on, and on boot
anything left mid-flight is marked `interrupted` and picked up again.

**Crash loop protection.** If the app dies within 30 seconds of starting, pm2
retries up to 10 times with increasing delay, then stops — so a genuine
misconfiguration fails loudly instead of restarting forever.

**Crash recovery.** Research sessions checkpoint continuously. After a
restart, a session that was running resumes from its last checkpoint; nothing
already gathered is lost.

## Automatic backups

Crash-safe is not disk-safe, so the app snapshots the database itself. Every
`BACKUP_INTERVAL_HOURS` (default **24 h**) it copies the live SQLite database
with `VACUUM INTO` — a consistent online copy, no downtime — verifies it with
`PRAGMA quick_check`, and keeps the newest **14** (`BACKUP_KEEP`). A failed or
corrupt snapshot is deleted and logged (`[backup] …` in `pm2 logs`), never
kept as a false sense of safety. Snapshots land in:

```
~/resurreccion-data/backups/resurreccion-<UTC timestamp>-<id>.db
```

Tune or disable in `.env`: `BACKUP_ENABLED`, `BACKUP_INTERVAL_HOURS`,
`BACKUP_KEEP`, `BACKUP_DIR` (full list: `ENV_VARS.md`).

**Restore** (replaces the live database with a snapshot):

```bash
pm2 stop resurreccion && pm2 save
cp ~/resurreccion-data/backups/resurreccion-<TIMESTAMP>-<id>.db ~/resurreccion-data/resurreccion.db
pm2 start resurreccion && pm2 save
```

**Copy them off the box.** Same-disk snapshots survive corruption and
accidental deletes, not a dead SSD. A daily cron keeps them somewhere else:

```bash
crontab -e
# every day at 03:15, copy snapshots to another machine/drive:
15 3 * * * rsync -a --quiet ~/resurreccion-data/backups/ user@otherhost:~/resurreccion-backups/
```

For the Docker path the same applies: data lives in the `resurreccion-data`
volume, snapshots in `/data/backups` inside it — rsync from the host via
`docker cp`, or set `BACKUP_DIR` to a bind-mounted host directory.

## Fitting the browser to your box

The browser is the only thing that can runaway, so it is the only thing you
would tune:

| Setting | On 8 GB | Meaning |
|---|---|---|
| `BROWSER_CONTENT_PROCESSORS` | `3` | Firefox content processes. Each costs a few hundred MB. `2` on a small box, up to `8` on a large one. |
| `BROWSER_MAX_OPEN_PAGES` | `6` | Pages open at once. More pages = more memory and more CPU. |
| `BROWSER_IDLE_SHUTDOWN_SECONDS` | `300` | Browser stops after 5 idle minutes, freeing its memory. |

These are already set for the 4 vCPU / 8 GB box in `ecosystem.config.cjs`.
Change them in `.env` if your machine differs.

## If something is wrong

```bash
pm2 logs resurreccion          # most errors show up here
pm2 status                    # is it online, or restarting?
curl -fsS http://127.0.0.1:7860/api/health
systemctl status caddy         # is the reverse proxy up?
```

A blank page or a browser error usually means a compile or import error is
blocking startup — the logs will name it. Browser failures have two exact
causes, and one command fixes both — it installs the Camoufox Python package
if it is missing (a hand-built virtualenv can lack it) and downloads the
browser binary if it is missing (a fresh box has no cache):

| Symptom | Missing piece |
|---|---|
| Toast: "camoufox is not installed" | The Python package — every browser path (Amazon sign-in, KDSpy, research) fails at launch |
| Settings says KDSpy loaded but Amazon shows no panel / agent gets "panel not present" | The installed Camoufox build predates add-on loading and silently drops it. Run `python scripts/camoufox_ensure.py --fetch` (boot also auto-upgrades), restart the app, then re-check Settings |
| Error: "official/stable is not installed" | The browser binary — the package is there but never fetched |

```bash
.venv/bin/python scripts/camoufox_ensure.py
pm2 restart resurreccion
```

`run.sh` runs the same script before uvicorn on every boot, so a restart
alone also heals it — the manual command is just faster to watch. The script
clears half-installed leftovers first (an interrupted download makes pip
report "already satisfied" while the code is missing) and prints the real
import error if the package still won't load. If it says the virtualenv is
the problem, rebuild it with the idempotent provisioner — it never touches
`.env`:

```bash
sudo sh scripts/vps_provision.sh
pm2 restart resurreccion
```

If logs show a memory kill, lower `BROWSER_MAX_OPEN_PAGES` and
`BROWSER_CONTENT_PROCESSORS` in `.env`, then `pm2 restart resurreccion`.

## Why not Docker here

The `Dockerfile` in this repo also serves Hugging Face Spaces. On a VPS where
Resurrección owns the whole box, running directly under pm2 is simpler: no
container layer to debug, the browser profile and database sit on your own
disk, and you can inspect them directly. If you prefer containers, the
docker path below works as-is.

## Running as a container on a multi-app VPS

When the VPS hosts several apps and Resurrección must stay inside its own
allocation (this setup: **2 cores / 5 GB RAM / 35 GB SSD**, used
intermittently), run it as a container instead of under pm2. The container
caps its own CPU and RAM, carries its own 2 GB swap file, and can be put to
sleep with one command so the other apps get the resources back.

```bash
git clone <your-repo-url> ~/resurreccion
cd ~/resurreccion
nano .env             # OWNER_PASSWORD, AUTH_SECRET, MODEL_API_KEY,
                      # COOKIE_SECURE=true — full list in ENV_VARS.md.
                      # (Or OWNER_PASSWORD_HASH via: python -m app.scripts.set_password)
docker compose up -d  # build + start, capped at 2 CPUs / 5 GB
```

`.env` must exist before `docker compose up` (compose reads it via
`env_file`). It is gitignored and never committed.

What `docker-compose.yml` sets:

| Setting | Value | Meaning |
|---|---|---|
| `mem_limit` / `cpus` | `5g` / `2` | The container can never take more, no matter what the browser does |
| `memswap_limit` | `7g` | 5 GB RAM + the 2 GB swap file |
| `cap_add: SYS_ADMIN` | — | Lets the entrypoint create/enable the 2 GB swap file **inside** the container (on its own disk, gone with the container). Without this cap the app still starts, just without swap |
| `ports` | `127.0.0.1:7860` | Loopback only — the public edge is the reverse proxy below |
| `stop_grace_period` | `60s` | A stop is graceful: checkpoints flush, the browser closes cleanly |
| `restart` | `unless-stopped` | Survives reboots; a slept container stays asleep |

### One-command sleep and awake

```bash
sh scripts/host_sleep.sh    # graceful stop — CPU+RAM released to the other apps
sh scripts/host_awake.sh    # start again, waits for /api/health
```

Sleep is a graceful `docker stop`: running research checkpoints and the
session is marked interrupted. Awake boots through the normal recovery path
— the session auto-resumes from its last checkpoint, nothing is lost. The
SSD side (the `resurreccion-data` volume with the database, artifacts,
browser profile with your Amazon/KDSpy logins, and the swap file) is never
touched by sleeping.

### The domain

`omnivex-academy.xyz` (root) points at your other app; the subdomain points
here:

1. DNS: an **A record** `resurreccion.omnivex-academy.xyz` → your VPS IP.
2. Certificate on the VPS's reverse proxy:
   `certbot --nginx -d resurreccion.omnivex-academy.xyz`.
3. Install `deploy/nginx-resureccion.conf` as that proxy's site config (it
   proxies to `127.0.0.1:7860`, so nothing else needs changing), then
   `nginx -t && systemctl reload nginx`.
4. In `.env`, set `COOKIE_SECURE=true` and restart the container — the
   session cookie then only travels over HTTPS.

Open `https://resurreccion.omnivex-academy.xyz` and sign in; the first-run
flow (Amazon sign-in, KDSpy install) is identical to steps above.

If Resurrección owns the whole VPS under pm2 (the main setup above), you
don't need any of this — Caddy handles the domain automatically. See
*Your domain and HTTPS* above.
