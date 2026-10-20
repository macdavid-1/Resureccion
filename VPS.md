# Running Resurrección on your own VPS

This is the guide for putting Resurrección alone on a dedicated server. Every
command is copy-paste.

The reference machine is **4 vCPU / 8 GB RAM** (Ubuntu 22.04 or 24.04, or
Debian 12). It works on bigger or smaller boxes — the setup script sizes swap
to match whatever it runs on.

## What runs on the box

One app, in one process, supervised by pm2:

```
pm2  ──>  run.sh  ──>  uvicorn (Python)
              │
              ├─ loads your .env          (secrets live here, never committed)
              ├─ makes sure Node deps exist (PDF export sidecar)
              └─ makes sure the Camoufox browser binary is downloaded
```

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
   **pm2** registered to start on boot, and the **Camoufox browser binary**.
2. **Creates `.env`** with generated secrets: your login password (asked for
   on the terminal; generated and printed once when headless) and a random
   `AUTH_SECRET`. An existing `.env` is never overwritten.
3. **Creates the data directory** (`~/resurreccion-data`) — the SQLite
   database and every artifact live there.
4. **Installs the PDF sidecar's Node dependencies.**
5. **Starts the app under pm2**, saves the process list, and registers the
   boot hook so it survives a reboot.
6. **Waits for `/api/health`**, then prints the URL — or fails loudly with the
   exact command to look at.

Re-running it is the **update path**: it is idempotent, re-provisions whatever
is missing, leaves `.env` alone, and restarts the app.

Flags are forwarded to the provisioner:

| Flag | Effect |
|---|---|
| `--no-swap` | Leave the host's existing swap alone |
| `--with-ufw` | Also enable the firewall: SSH + port 7860 only |

The firewall is **off by default** so you cannot accidentally lock yourself
out. `--with-ufw` detects your SSH port first and allows it before enabling.

## 3. Your DeepInfra key

`.env` (next to `run.sh`) is where your secrets live. It is loaded
automatically when the app boots and is never committed to git. The deploy
script already wrote your login password, `AUTH_SECRET`, `DATA_DIR` and the
model settings into it; the one thing you may still need to add is the API key:

```bash
nano ~/resurreccion/.env     # set MODEL_API_KEY=<your DeepInfra key>
pm2 restart resurreccion
```

The default model is **MiMo-V2.6-Flash** (`XiaomiMiMo/MiMo-V2.6-Flash`), a
thinking model on DeepInfra. Anything in `.env` overrides the values in
`ecosystem.config.cjs`, so you can tune the app without touching the pm2 file.
Full list: `ENV_VARS.md`.

**Do not put `.env` in git.** It is already ignored.

## 4. First run, from your phone

Everything the research browser needs is set up from Settings — no shell
required:

1. Open `http://<your-server-ip>:7860` and sign in.
2. **Amazon authentication → Sign in.** A live browser streams to your phone;
   complete the sign-in (captcha/OTP included), then Close. Cookies persist in
   the shared profile, so every future run is already signed in.
3. **KDSpy Pro → Install.** One tap pulls the official Firefox add-on from
   Mozilla Add-ons; the browser then opens on kdspy.com for your account
   sign-in and license activation.
4. **AI model** should read `XiaomiMiMo/MiMo-V2.6-Flash ready`. If it says the
   key is missing, add `MODEL_API_KEY` to `.env` and `pm2 restart resurreccion`.

## 5. Day-to-day commands

| Task | Command |
|---|---|
| See status | `pm2 status` |
| Watch logs | `pm2 logs resurreccion` |
| Stop | `pm2 stop resurreccion` |
| Start | `pm2 start resurreccion` |
| Restart after a config change | `pm2 restart resurreccion` |
| Start automatically on boot | `pm2 startup` (run the command it prints, once) |

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
```

A blank page or a browser error usually means a compile or import error is
blocking startup — the logs will name it. If the browser specifically fails
with "official/stable is not installed", the binary was not downloaded:

```bash
.venv/bin/python scripts/camoufox_ensure.py
```

If logs show a memory kill, lower `BROWSER_MAX_OPEN_PAGES` and
`BROWSER_CONTENT_PROCESSORS` in `.env`, then `pm2 restart resurreccion`.

## Why not Docker here

The `Dockerfile` in this repo is the Hugging Face Spaces deployment. On your
own VPS, running directly under pm2 is simpler: no container layer to debug,
the browser profile and database sit on your own disk, and you can inspect
them directly. If you prefer containers, the Dockerfile still works as-is.
