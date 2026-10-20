/**
 * pm2 profile for Resurrección on a standalone VPS.
 *
 * Usage (see VPS.md for the full first-run walkthrough):
 *   pm2 start ecosystem.config.cjs
 *   pm2 save                     # remember the process list
 *   pm2 startup                 # resurrect after a reboot
 *
 * ---------------------------------------------------------------------------
 * Why this shape
 * ---------------------------------------------------------------------------
 * Resurrección hoists a real anti-detect Firefox (Camoufox) INSIDE the app
 * process, together with the research agent, the recorder and the SQLite
 * database. That state is not shareable between processes, so:
 *
 *   - exactly ONE instance, fork mode. Never `instances: N` or cluster mode:
 *     two instances would fight over the same browser profile directory and
 *     the same SQLite file.
 *   - the memory guardrail is the important knob on an 8 GB box. Firefox is
 *     the only process that can runaway, and pm2's max_memory_restart kills
 *     and respawns the app BEFORE the kernel OOM killer reaches in and takes
 *     a random process. Restart is safe: every session transition is
 *     committed to SQLite first, and on boot transient sessions are moved to
 *     `interrupted` and picked up again by the job orchestrator.
 *   - a slow, bounded restart loop instead of a tight crash loop.
 *
 * Secrets (OWNER_PASSWORD_HASH, AUTH_SECRET, MODEL_API_KEY, BROWSER_PROXY…)
 * do NOT belong here — they live in the private .env file, which run.sh
 * loads and which overrides anything set below. This file only carries
 * non-secret host tuning, so it is safe to commit.
 */

module.exports = {
  apps: [
    {
      name: "resurreccion",

      // run.sh owns venv selection, .env loading and the one-time camoufox
      // fetch; pm2 just supervises it. Invoked through `sh` so the config
      // does not depend on the executable bit surviving a git checkout.
      script: "run.sh",
      interpreter: "sh",
      cwd: __dirname,
      autorestart: true,

      // Explicit, so nobody has to infer it from the defaults: fork mode and
      // one instance. `instances: "auto"` or cluster mode would run a second
      // copy that fights over the same browser profile and SQLite file.
      exec_mode: "fork",
      instances: 1,

      // --- resource guardrails (4 vCPU / 8 GB host) -------------------------
      // Headroom below: OS + Docker-free host baseline is ~0.5-0.8 GB, and
      // the Node PDF sidecar is a short-lived child, not a resident. 6 GB is
      // the line where we would rather restart deliberately than be OOM-killed.
      max_memory_restart: "6G",
      // CPU above ~3.5 sustained cores for minutes means the browser is
      // thrashing; restart recovers it far more cleanly than letting it crawl.
      max_cpu_restart: 90,

      // --- crash-loop protection --------------------------------------------
      min_uptime: "30s", // a process that dies sooner counts as a failure
      max_restarts: 10, // stop retrying after 10 quick failures
      restart_delay: 5000, // 5s between restarts (never a tight loop)
      exp_backoff_restart_delay: 1000, // then 1s, 2s, 4s… capped at 15s
      kill_timeout: 15000, // give the browser time to close cleanly on stop

      // --- logging -----------------------------------------------------------
      // pm2's own log dir (`pm2 logs resurreccion`); the app also writes a
      // durable event log to SQLite that survives restarts.
      merge_logs: true,
      time: true,

      // --- non-secret host tuning (override in .env if needed) --------------
      env: {
        HOST: "0.0.0.0",
        PORT: "7860",
        PYTHONUNBUFFERED: "1", // stream logs instead of buffering them
        // 4 vCPU gives the engine room the 2-core default assumed:
        BROWSER_CONTENT_PROCESSORS: "3",
        BROWSER_MAX_OPEN_PAGES: "6",
        BROWSER_HEADLESS: "true",
        // One browser, one research session at a time on this box.
        MAX_SESSIONS: "5",
        MAX_CONCURRENT_SESSIONS: "1",
      },
    },
  ],
};
