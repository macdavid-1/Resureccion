/* Resurrección — mobile-first research instrument.
   Vanilla JS, no build step. Efficient: SSE-driven partial renders,
   bounded lists, no full-view re-renders per event. */
(() => {
  "use strict";

  const TOKEN_KEY = "resurreccion_token";
  const state = {
    sessions: [], current: null, counts: null,
    traceAfter: 0, view: "research",
    mode: "prompt", images: [],
    liveSessionId: null, recording: false,
    reportOpen: null,
    ws: { trace: new Map(), cands: [], opps: [], errors: [] },
    liveTimer: null, recTimer: null, elapsedTimer: null,
    lastRefresh: 0,
  };

  // ---------- helpers ----------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));
  const esc = (s) => String(s ?? "").replace(/[&<>\"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const THROTTLE_MS = 4000;

  function authHeaders() {
    const token = localStorage.getItem(TOKEN_KEY);
    return token ? { "X-Auth-Token": token } : {};
  }

  async function api(path, opts = {}) {
    const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", ...authHeaders(), ...(opts.headers || {}) } });
    if (res.status === 401) { showLogin(); throw new Error("Sign in required"); }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail ?? detail; } catch {}
      throw new Error(detail);
    }
    return res.json();
  }

  let toastTimer = null;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  function confirmModal(title, body, confirmLabel) {
    return new Promise((resolve) => {
      const root = $("#modal-root");
      root.innerHTML = `
        <div class="modal-veil">
          <div class="modal">
            <h3>${esc(title)}</h3><p>${esc(body)}</p>
            <div class="row">
              <button class="btn ghost" data-a="no">Cancel</button>
              <button class="btn danger" data-a="yes">${esc(confirmLabel || "Confirm")}</button>
            </div>
          </div>
        </div>`;
      root.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => { root.innerHTML = ""; resolve(b.dataset.a === "yes"); }));
    });
  }

  function fmtElapsed(s) {
    s = Math.max(0, Math.floor(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h > 0 ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  }
  function fmtClock(iso) {
    try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
    catch { return ""; }
  }
  const PHASE_LABEL = {
    initializing: "Initializing", discovery: "Discovery", exploration: "Exploration",
    verification: "Verification", synthesis: "Synthesis", reporting: "Reporting", done: "Complete",
  };

  // ---------- views / router ----------
  function showLogin() {
    $("#login-view").classList.add("active");
    $("#login-view").classList.remove("hidden");
    $("#app-view").style.display = "none";
    stopAllStreams();
  }

  function showApp() {
    $("#login-view").classList.add("hidden");
    $("#login-view").classList.remove("active");
    $("#app-view").style.display = "flex";
    setView("research");
    refreshSessions();
    refreshSettings();
  }

  function setView(name) {
    state.view = name;
    $$(".view").forEach((v) => v.classList.remove("active"));
    $(name === "research" ? "#v-research" : `#v-${name}`).classList.add("active");
    $$("#tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $("#tb-context").textContent =
      name === "research" && state.current ? state.current.name :
      name === "live" && state.liveSessionId ? liveSessionName() : "";
    if (name === "sessions") renderSessionsView();
    if (name === "live") renderLiveView();
    if (name === "reports") renderReportsView();
    if (name === "settings") refreshSettings();
  }

  function liveSessionName() {
    const s = state.sessions.find((x) => x.id === state.liveSessionId);
    return s ? s.name : "";
  }

  // ---------- sessions ----------
  async function refreshSessions() {
    try {
      const data = await api("/api/sessions");
      state.sessions = data.sessions;
      if (state.view === "sessions") renderSessionsView();
      if (state.view === "live" && !state.liveSessionId) renderLiveView();
      if (state.current) {
        const s = state.sessions.find((x) => x.id === state.current.id);
        if (s) { state.current = { ...state.current, ...s }; updateWsHead(state.current); }
      }
    } catch {}
  }

  function statusChip(s) {
    const live = ["running", "queued", "verifying"].includes(s);
    return `<span class="chip ${esc(s)}${live ? " pulse" : ""}"><span class="dot"></span>${esc(s)}</span>`;
  }

  function renderSessionsView() {
    const el = $("#sess-list");
    if (!state.sessions.length) {
      el.innerHTML = `<div class="empty"><div class="mark">◈</div><p>No sessions yet.<br>Start your first research from the Research tab.</p></div>`;
      return;
    }
    el.innerHTML = state.sessions.map((s) => {
      const c = s.counts || {};
      return `
      <div class="sess-row" data-id="${esc(s.id)}">
        <div class="sess-main">
          <div class="sess-name">${esc(s.name)}</div>
          <div class="sess-meta">
            <span>${esc(PHASE_LABEL[s.phase] || s.phase)}</span>
            <span>${fmtElapsed(s.elapsed_seconds)}</span>
            <span>${esc((s.marketplaces || []).join(", ") || "auto")}</span>
          </div>
        </div>
        <div class="sess-side">
          ${statusChip(s.status)}
          <span class="sess-count">${c.verified ?? 0}✓ · ${c.discovered ?? 0}◇</span>
        </div>
      </div>`;
    }).join("");
    el.querySelectorAll(".sess-row").forEach((row) =>
      row.addEventListener("click", () => openSession(row.dataset.id, "research")));
  }

  async function openSession(id, targetView) {
    state.liveSessionId = id;
    state.ws = { trace: new Map(), cands: [], opps: [], errors: [] };
    state.traceAfter = 0;
    state.lastRefresh = 0;
    $("#ws-trace").innerHTML = "";
    $("#start-pane").classList.add("hidden");
    $("#ws-pane").classList.remove("hidden");
    $("#research-empty").classList.add("hidden");
    setView(targetView || "research");
    const { session } = await api(`/api/sessions/${id}`);
    state.current = session;
    updateWsHead(session);
    renderCounts(session.counts || { discovered: 0, rejected: 0, verifying: 0, verified: 0 });
    // Terminal sessions have nothing new to stream; skip the connection.
    if (!["completed", "failed", "cancelled"].includes(session.status)) connectStream(id);
    else state.lastRefresh = Date.now();
    try {
      const [cands, opps] = await Promise.all([
        api(`/api/sessions/${id}/candidates`),
        api(`/api/sessions/${id}/opportunities`),
      ]);
      renderCandidates(cands.candidates);
      renderOpportunities(opps.opportunities);
    } catch (e) { toast(e.message); }
  }

  function updateWsHead(s) {
    $("#ws-name").textContent = s.name || "—";
    $("#ws-mode").textContent = ({ prompt: "Directed", keywords: "Seeded", auto: "Autonomous" })[s.mode] || s.mode;
    $("#ws-elapsed").textContent = fmtElapsed(s.elapsed_seconds);
    const st = $("#ws-status");
    st.className = `chip ${esc(s.status)}${["running", "queued"].includes(s.status) ? " pulse" : ""}`;
    $("#ws-status-t").textContent = s.status;
    $("#ws-progress").style.width = `${Math.round((s.progress || 0) * 100)}%`;
    $("#ws-phase").textContent = PHASE_LABEL[s.phase] || s.phase;
    renderPauseBanner(s);
    $("#tb-context").textContent = state.view === "research" ? s.name : $("#tb-context").textContent;
    renderActions(s);
  }

  // Why research is not moving — always visible, never a bare "paused".
  function renderPauseBanner(s) {
    const el = $("#ws-error-banner");
    const reason = (s.error || "").trim();
    const showFor = ["paused", "failed", "interrupted"].includes(s.status);
    if (!showFor || !reason) { el.classList.add("hidden"); el.textContent = ""; return; }
    const failed = s.status === "failed";
    el.className = `pause-banner${failed ? " error" : ""}`;
    const friendly = reason.replace(/^model failure:\s*/i, "").replace(/^authentication required:\s*/i, "Amazon sign-in needed — ");
    el.innerHTML = `<span class="pb-k"></span>${esc(friendly)}`;
    el.querySelector(".pb-k").textContent = failed ? "Research failed" : "Research paused";
  }

  function renderActions(s) {
    const el = $("#ws-actions");
    const btns = [];
    if (s.status === "draft") btns.push(`<button class="btn primary small" data-act="start">Start research</button>`);
    if (["interrupted", "paused"].includes(s.status)) btns.push(`<button class="btn primary small" data-act="resume">Resume</button>`);
    if (s.status === "running") btns.push(`<button class="btn small" data-act="pause">Pause</button>`);
    if (!["completed", "cancelled"].includes(s.status)) btns.push(`<button class="btn small danger" data-act="cancel">Cancel</button>`);
    el.innerHTML = btns.join("");
    el.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => sessionAction(b.dataset.act)));
  }

  async function sessionAction(act) {
    const id = state.current?.id;
    if (!id) return;
    if (act === "cancel") {
      const ok = await confirmModal(
        "Cancel this session?",
        "The research stops permanently. Everything learned so far stays available.",
        "Cancel session");
      if (!ok) return;
    }
    try {
      const { session } = await api(`/api/sessions/${id}/${act}`, { method: "POST" });
      state.current = session;
      updateWsHead(session);
      toast(({ start: "Research started", pause: "Pausing at a safe boundary", resume: "Resuming", cancel: "Cancelled" })[act] || act);
      refreshSessions();
    } catch (e) { toast(e.message); }
  }

  // ---------- SSE live stream ----------
  let es = null;
  function connectStream(sessionId) {
    stopStream();
    if (!window.EventSource) return;
    try {
      // Cookie-based SSE (EventSource can't send headers); backend also
      // accepts the query-token fallback for quirky mobile browsers.
      es = new EventSource(`/api/sessions/${sessionId}/stream`);
      es.onmessage = (ev) => { try { applySnapshot(JSON.parse(ev.data)); } catch {} };
      es.onerror = () => { /* EventSource auto-reconnects */ };
    } catch {}
  }
  function stopStream() { if (es) { es.close(); es = null; } }
  function stopAllStreams() {
    stopStream();
    clearInterval(state.liveTimer); state.liveTimer = null;
    clearInterval(state.recTimer); state.recTimer = null;
    clearInterval(state.elapsedTimer); state.elapsedTimer = null;
    state.recording = false;
    updateRecUI();
  }

  function applySnapshot(snap) {
    const s = snap.session;
    if (!state.current || s.id !== state.current.id) return;
    const prevStatus = state.current.status;
    state.current = { ...state.current, ...s };
    updateWsHead(state.current);
    renderCounts(snap.counts || state.current.counts);
    // Trace: incremental append only — never re-render the whole feed.
    if (snap.trace?.length) {
      const feed = $("#ws-trace");
      const frag = document.createDocumentFragment();
      for (const t of snap.trace) {
        if (state.ws.trace.has(t.id)) continue;
        state.ws.trace.set(t.id, t);
        const div = document.createElement("div");
        div.className = `trace-item ${esc(t.kind)}`;
        div.innerHTML = `<div class="trace-t">${esc(fmtClock(t.created_at))}</div>
          <div class="trace-x"><span class="k">${esc(t.kind)}</span>${esc(t.text)}</div>`;
        frag.appendChild(div);
      }
      if (frag.childNodes.length) {
        feed.appendChild(frag);
        $("#ws-trace-empty").hidden = true;
        // Bound the feed in DOM and in the dedup map (6h sessions).
        while (feed.children.length > 120) feed.removeChild(feed.firstChild);
        if (state.ws.trace.size > 400) {
          const excess = state.ws.trace.size - 400;
          let dropped = 0;
          for (const key of state.ws.trace.keys()) {
            if (dropped++ >= excess) break;
            state.ws.trace.delete(key);
          }
        }
        if (nearBottom(feed.parentElement)) feed.parentElement.scrollTop = feed.parentElement.scrollHeight;
      }
    }
    if (snap.recent_errors?.length) {
      state.ws.errors = snap.recent_errors;
      $("#ws-errors").textContent = snap.recent_errors
        .map((e) => `[${fmtClock(e.created_at)}] ${e.severity}${e.recoverable ? " · recovered" : ""}: ${e.message}`)
        .join("\n");
    }
    // Status transitions fetch the heavier lists.
    if (s.status !== prevStatus || ["completed", "failed"].includes(s.status)) {
      refreshFindings(s.id);
      if (["completed", "failed", "cancelled"].includes(s.status) && state.recording) stopRecording();
    }
    // Elapsed clock stays honest between SSE pushes (heartbeat every 15s).
    if (!state.elapsedTimer && state.current && ["running", "queued", "verifying"].includes(state.current.status)) {
      state.elapsedTimer = setInterval(() => {
        if (!state.current) return;
        const t0 = Date.parse(state.current.updated_at || "") || 0;
        if (!t0) return;
        const base = state.current.elapsed_seconds || 0;
        $("#ws-elapsed").textContent = fmtElapsed(base + Math.max(0, (Date.now() - t0) / 1000));
      }, 1000);
    } else if (state.elapsedTimer && state.current && !["running", "queued", "verifying"].includes(state.current.status)) {
      clearInterval(state.elapsedTimer); state.elapsedTimer = null;
      $("#ws-elapsed").textContent = fmtElapsed(state.current.elapsed_seconds);
    }
    const now = Date.now();
    if (now - state.lastRefresh > THROTTLE_MS) {
      state.lastRefresh = now;
      refreshSessions();
    }
  }

  function nearBottom(el) { return el.scrollHeight - el.scrollTop - el.clientHeight < 160; }

  async function refreshFindings(id) {
    try {
      const [cands, opps] = await Promise.all([
        api(`/api/sessions/${id}/candidates`),
        api(`/api/sessions/${id}/opportunities`),
      ]);
      renderCandidates(cands.candidates);
      renderOpportunities(opps.opportunities);
    } catch {}
  }

  function renderCounts(c) {
    if (!c) return;
    $("#st-cand").textContent = (c.discovered ?? 0) + (c.verifying ?? 0) + (c.verified ?? 0);
    $("#st-ver").textContent = c.verified ?? 0;
    $("#st-rej").textContent = c.rejected ?? 0;
    $("#st-opp").textContent = c.verified ?? 0;
  }

  function renderCandidates(cands) {
    state.ws.cands = cands;
    const el = $("#ws-cands");
    if (!cands.length) {
      el.innerHTML = `<div class="empty" style="padding:26px"><p class="muted" style="font-size:0.84rem;margin:0">No candidates yet — discovery runs first.</p></div>`;
      return;
    }
    el.innerHTML = cands.slice(0, 30).map((c) => `
      <div class="cand">
        <div><div class="n">${esc(c.niche)}</div>
        <div class="s">${esc(c.marketplace || "any market")}${c.rationale ? " — " + esc(c.rationale.slice(0, 90)) : ""}</div></div>
        <span class="st st-${esc(c.status)}" style="font-size:0.68rem;font-weight:700;letter-spacing:0.08em;text-transform:uppercase">${esc(c.status)}</span>
      </div>`).join("");
    $("#cand-more").textContent = cands.length > 30 ? `showing 30 of ${cands.length}` : "";
  }

  function renderOpportunities(opps) {
    state.ws.opps = opps;
    const el = $("#ws-opps");
    if (!opps.length) {
      el.innerHTML = `<div class="empty" style="padding:26px"><p class="muted" style="font-size:0.84rem;margin:0">Verified opportunities appear here after synthesis.</p></div>`;
      return;
    }
    el.innerHTML = opps.map((o) => {
      const m = o.meta || {};
      const conf = Math.round((o.confidence || 0) * 100);
      return `
      <div class="opp">
        <h3>${esc(o.title)}</h3>
        <div class="niche">${esc(o.niche)} · ${esc(o.marketplace || "")}</div>
        <dl class="kv">
          ${m.target_reader ? `<dt>Reader</dt><dd>${esc(m.target_reader)}</dd>` : ""}
          ${m.market_gap ? `<dt>Market gap</dt><dd>${esc(m.market_gap)}</dd>` : ""}
          ${m.differentiation ? `<dt>Edge</dt><dd>${esc(m.differentiation)}</dd>` : ""}
          ${o.keywords?.length ? `<dt>Keywords</dt><dd>${esc(o.keywords.join(", "))}</dd>` : ""}
          <dt>Verification</dt><dd class="${esc(m.verification_status || "").includes("reject") ? "error-text" : "ok-text"}">${esc(m.verification_status || "verified")}</dd>
        </dl>
        <div class="conf-bar"><i style="width:${conf}%"></i></div>
        <div class="muted" style="font-size:0.7rem;margin-top:4px">confidence ${conf}%</div>
      </div>`;
    }).join("");
  }

  // ---------- live view (browser frames) ----------
  function toggleLiveView() {
    const img = $("#lv-img"), off = $("#lv-off"), btn = $("#lv-toggle");
    if (img.classList.contains("hidden")) {
      img.classList.remove("hidden"); off.classList.add("hidden");
      btn.textContent = "Hide live view";
      loadLiveFrame();
      if (!state.liveTimer) state.liveTimer = setInterval(loadLiveFrame, 2500);
    } else {
      img.classList.add("hidden"); off.classList.remove("hidden");
      btn.textContent = "Show live view";
      clearInterval(state.liveTimer); state.liveTimer = null;
    }
  }
  async function loadLiveFrame() {
    const id = state.current?.id;
    if (!id) return;
    const img = $("#lv-img");
    try {
      const res = await fetch(`/api/sessions/${id}/live-view/frame`, { headers: authHeaders() });
      if (res.status === 200) {
        const blob = await res.blob();
        if (img.src) URL.revokeObjectURL(img.src);
        img.src = URL.createObjectURL(blob);
      }
    } catch {}
  }

  // ---------- recording ----------
  function updateRecUI() {
    const btn = $("#tb-record");
    btn.classList.toggle("hidden", !state.current || !["running", "queued", "paused"].includes(state.current.status));
    btn.classList.toggle("recording", state.recording);
    $("#tb-record-label").textContent = state.recording ? "Stop" : "Record";
    $("#rec-badge").classList.toggle("hidden", !state.recording);
  }

  async function toggleRecording() {
    const id = state.current?.id;
    if (!id) return;
    if (!state.recording) {
      if (state.current.status !== "running") {
        toast("Recording is available while research is running");
        return;
      }
      try {
        await api(`/api/sessions/${id}/recording/start`, { method: "POST" });
        state.recording = true;
        updateRecUI();
        toast("Recording — live view + trace are being captured");
        if (!$("#lv-img").classList.contains("hidden")) toggleLiveView();
      } catch (e) { toast(e.message); }
    } else {
      await stopRecording();
    }
  }

  async function stopRecording() {
    const id = state.current?.id;
    if (!id || !state.recording) return;
    try {
      const { recording } = await api(`/api/sessions/${id}/recording/stop`, { method: "POST" });
      state.recording = false;
      updateRecUI();
      if (recording?.artifact_id) toast("Recording saved to session artifacts");
      else toast("Recording ended (nothing captured)");
    } catch (e) {
      state.recording = false;
      updateRecUI();
      toast(e.message);
    }
  }

  // ---------- live tab ----------
  function renderLiveView() {
    const body = $("#live-body");
    const running = state.sessions.filter((s) => ["running", "queued", "paused"].includes(s.status));
    if (!running.length) {
      body.innerHTML = `<div class="panel"><div class="empty"><div class="mark">◈</div><p>No live sessions.<br>Research appears here the moment it starts.</p></div></div>`;
      return;
    }
    body.innerHTML = running.map((s) => `
      <div class="panel">
        <div class="sess-row" data-id="${esc(s.id)}">
          <div class="sess-main">
            <div class="sess-name">${esc(s.name)}</div>
            <div class="sess-meta"><span>${esc(PHASE_LABEL[s.phase] || s.phase)}</span><span>${fmtElapsed(s.elapsed_seconds)}</span></div>
          </div>
          ${statusChip(s.status)}
        </div>
      </div>`).join("");
    // Opening from Live lands in the Research workspace (the session's home).
    body.querySelectorAll(".sess-row").forEach((row) =>
      row.addEventListener("click", () => openSession(row.dataset.id, "research")));
  }

  // ---------- reports ----------
  async function renderReportsView() {
    const body = $("#reports-body");
    if (!state.sessions.length) {
      body.innerHTML = `<div class="panel"><div class="empty"><div class="mark">▤</div><p>No sessions yet.</p></div></div>`;
      return;
    }
    body.innerHTML = `<div class="panel"><div class="empty"><div class="spinner"></div></div></div>`;
    const perSession = await Promise.all(state.sessions.map(async (s) => {
      try { return { s, reports: (await api(`/api/sessions/${s.id}/reports`)).reports }; }
      catch { return { s, reports: [] }; }
    }));
    const rows = perSession.flatMap(({ s, reports }) => reports.map((r) => ({ s, r })));
    if (!rows.length) {
      body.innerHTML = `<div class="panel"><div class="empty"><div class="mark">▤</div><p>Reports appear as sessions complete.</p></div></div>`;
      return;
    }
    body.innerHTML = `<div class="panel">${rows.map(({ s, r }) => `
      <div class="rep-row" data-sid="${esc(s.id)}" data-rid="${esc(r.id)}">
        <div>
          <div class="rep-title">${esc(r.title)}${r.kind === "final" ? ' <span class="chip completed" style="margin-left:6px">final</span>' : ""}</div>
          <div class="rep-sub">${esc(s.name)} · ${esc(fmtClock(r.created_at))}</div>
        </div>
        <span class="muted">›</span>
      </div>`).join("")}</div>`;
    body.querySelectorAll(".rep-row").forEach((row) =>
      row.addEventListener("click", () => openReport(row.dataset.sid, row.dataset.rid, row.querySelector(".rep-title").textContent.trim())));
  }

  // Minimal, safe markdown → HTML for report bodies.
  function mdToHtml(md) {
    const lines = String(md || "").split("\n");
    const out = [];
    let inUl = false, inOl = false, tableBuf = [], inQuote = false;
    const inline = (t) => esc(t)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");
    const flushTable = () => {
      if (!tableBuf.length) return;
      const rows = tableBuf.filter((r) => !/^\|[\s:|-]+\|$/.test(r.trim()));
      const cells = rows.map((r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
      if (cells.length) {
        const [head, ...body] = cells;
        out.push('<div class="rtable-wrap"><table class="rt"><thead><tr>' +
          head.map((h) => `<th>${inline(h)}</th>`).join("") + "</tr></thead><tbody>" +
          body.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
          "</tbody></table></div>");
      }
      tableBuf = [];
    };
    const flushLists = () => {
      if (inUl) { out.push("</ul>"); inUl = false; }
      if (inOl) { out.push("</ol>"); inOl = false; }
      if (inQuote) { out.push("</blockquote>"); inQuote = false; }
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (/^\s*\|.*\|\s*$/.test(line)) { flushLists(); tableBuf.push(line); continue; }
      flushTable();
      if (!line.trim()) { flushLists(); continue; }
      const h = /^(#{1,4})\s+(.*)/.exec(line);
      if (h) { flushLists(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); continue; }
      if (/^---+\s*$/.test(line)) { flushLists(); out.push("<hr>"); continue; }
      const ul = /^[-*]\s+(.*)/.exec(line);
      if (ul) { if (inOl) { out.push("</ol>"); inOl = false; } if (!inUl) { out.push("<ul>"); inUl = true; } out.push(`<li>${inline(ul[1])}</li>`); continue; }
      const ol = /^\d+[.)]\s+(.*)/.exec(line);
      if (ol) { if (inUl) { out.push("</ul>"); inUl = false; } if (!inOl) { out.push("<ol>"); inOl = true; } out.push(`<li>${inline(ol[1])}</li>`); continue; }
      const q = /^>\s?(.*)/.exec(line);
      if (q) { if (!inQuote) { out.push('<blockquote>'); inQuote = true; } out.push(inline(q[1]) + "<br>"); continue; }
      flushLists();
      out.push(`<p>${inline(line)}</p>`);
    }
    flushTable(); flushLists();
    return out.join("\n");
  }

  async function openReport(sid, rid, title) {
    state.reportOpen = { sid, rid };
    const body = $("#reports-body");
    body.innerHTML = `<div class="panel"><div class="empty"><div class="spinner"></div></div></div>`;
    try {
      // The API returns { report, body_markdown }; v2 reports also carry a
      // structured model we expose as a download (editable source of truth).
      const data = await api(`/api/sessions/${sid}/reports/${rid}`);
      const report = data.report ?? {};
      const md = data.body_markdown ?? "";
      const sections = splitReportSections(md);
      body.innerHTML = `
        <div class="spread mt14" style="margin-bottom:10px">
          <button class="btn small ghost" id="rep-back">‹ All reports</button>
          <span class="row">
            <a class="btn small ghost" href="/api/sessions/${esc(sid)}/reports/${esc(rid)}/model" target="_blank" rel="noopener">Model</a>
            <button class="btn small primary" id="rep-pdf">Export PDF</button>
            <a class="btn small" href="/api/sessions/${esc(sid)}/reports/${esc(rid)}/file" target="_blank" rel="noopener">Download</a>
          </span>
        </div>
        <div class="panel"><div class="panel-h"><h2 style="text-transform:none;letter-spacing:0">${esc(report.title || title)}</h2>
          <span class="chip ${esc(report.kind)}">${esc(report.kind)}</span></div>
        <div class="section report-body" id="rep-body">${sections}</div></div>`;
      $("#rep-back").addEventListener("click", renderReportsView);
      $("#rep-pdf").addEventListener("click", () => exportPdf(sid, rid));
      // Collapsible h2 sections for phone usability on long reports.
      $("#rep-body").querySelectorAll("h2").forEach((h) => {
        const wrap = document.createElement("div");
        h.parentNode.insertBefore(wrap, h);
        const det = document.createElement("details");
        det.className = "sect";
        const sum = document.createElement("summary");
        sum.textContent = h.textContent;
        const bd = document.createElement("div");
        bd.className = "sect-body";
        let n = h.nextSibling;
        while (n && !(n.nodeType === 1 && /^H[12]$/i.test(n.tagName))) {
          const next = n.nextSibling;
          bd.appendChild(n);
          n = next;
        }
        det.appendChild(sum); det.appendChild(bd);
        wrap.appendChild(det);
        det.open = true; // sections start expanded; owner collapses what's read
        h.remove();
      });
    } catch (e) {
      body.innerHTML = `<div class="panel"><div class="empty error-text">${esc(e.message)}</div></div>`;
    }
  }

  function splitReportSections(md) { return mdToHtml(md); }

  async function exportPdf(sid, rid) {
    const btn = $("#rep-pdf");
    const label = btn.textContent;
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Rendering…';
    try {
      const res = await api(`/api/sessions/${sid}/reports/${rid}/export-pdf`, { method: "POST" });
      const a = document.createElement("a");
      a.href = `/api/sessions/${sid}/artifacts/${res.pdf.id}/file`;
      a.download = res.pdf.path ? res.pdf.path.split("/").pop() : "report.pdf";
      document.body.appendChild(a);
      a.click();
      a.remove();
      toast("PDF exported — 6×9 report saved to session artifacts");
    } catch (e) {
      toast(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  // ---------- start research ----------
  function bindStartResearch() {
    $$("#mode-row .mode-btn").forEach((b) => b.addEventListener("click", () => {
      $$("#mode-row .mode-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.mode = b.dataset.mode;
      const wrap = $("#f-prompt-wrap"), ta = $("#ns-prompt");
      if (state.mode === "prompt") {
        wrap.classList.remove("hidden"); $("#ns-prompt-label").textContent = "Research brief";
        ta.placeholder = "e.g. Low-content journals for competitive swimmers recovering from injury…";
        ta.rows = 5;
      } else if (state.mode === "keywords") {
        wrap.classList.remove("hidden"); $("#ns-prompt-label").textContent = "Keywords, ideas, rough concepts";
        ta.placeholder = "grief journal, estate planner, first marathon…";
        ta.rows = 3;
      } else {
        wrap.classList.add("hidden");
      }
      $("#ns-create-label").textContent =
        state.mode === "auto" ? "Start Autonomous Discovery" : "Start Research";
    }));

    $("#ns-add-img").addEventListener("click", () => $("#ns-images").click());
    $("#ns-images").addEventListener("change", () => {
      for (const f of $("#ns-images").files) {
        if (state.images.length >= 6) break;
        state.images.push(f);
      }
      $("#ns-images").value = "";
      renderImgThumbs();
    });
    $("#ns-create").addEventListener("click", createSession);
  }

  function renderImgThumbs() {
    const row = $("#ns-img-row");
    row.innerHTML = "";
    state.images.forEach((f, i) => {
      const d = document.createElement("div");
      d.className = "img-thumb";
      const img = document.createElement("img");
      img.src = URL.createObjectURL(f);
      img.onload = () => URL.revokeObjectURL(img.src);
      const x = document.createElement("button");
      x.textContent = "×";
      x.addEventListener("click", () => { state.images.splice(i, 1); renderImgThumbs(); });
      d.appendChild(img); d.appendChild(x);
      row.appendChild(d);
    });
    $("#ns-img-count").textContent = state.images.length ? `${state.images.length} attached` : "";
  }

  async function createSession() {
    const btn = $("#ns-create");
    const prompt = $("#ns-prompt").value.trim();
    if (state.mode === "prompt" && !prompt) { toast("Enter a research brief first"); return; }
    if (state.mode === "keywords" && !prompt && !state.images.length) { toast("Add keywords or a reference image"); return; }
    btn.disabled = true;
    try {
      const { session } = await api("/api/sessions", {
        method: "POST",
        body: JSON.stringify({
          mode: state.mode,
          prompt: state.mode === "auto" ? "" : prompt,
          objective: $("#ns-objective").value.trim(),
          marketplaces: $("#ns-marketplaces").value.split(",").map((s) => s.trim()).filter(Boolean),
        }),
      });
      for (const f of state.images) {
        const fd = new FormData();
        fd.append("file", f);
        await fetch(`/api/sessions/${session.id}/uploads`, { method: "POST", headers: authHeaders(), body: fd });
      }
      state.images = []; renderImgThumbs();
      $("#ns-prompt").value = ""; $("#ns-objective").value = ""; $("#ns-marketplaces").value = "";
      await refreshSessions();
      await openSession(session.id, "research");
      sessionAction("start");
    } catch (e) { toast(e.message); }
    finally { btn.disabled = false; }
  }

  // ---------- interactive remote browser (sign-in / KDSpy setup) ----------
  const rb = {
    session: null, timer: null, pollTimer: null, busy: false,
  };

  function rbShow(session) {
    rb.session = session;
    $("#rbrowser").classList.remove("hidden");
    document.body.style.overflow = "hidden";
    $("#rb-url").textContent = session?.url || "—";
    renderRbTabs();
    if (!rb.timer) rb.timer = setInterval(rbTick, 1200);
    rbTick();
  }

  // Tabs the SITE opened (target=_blank) — kdspy.com's Login opens its
  // member form in a new tab; without a switcher it sits there invisible.
  function renderRbTabs() {
    const wrap = $("#rb-tabs");
    const tabs = rb.session?.tabs || [];
    if (tabs.length <= 1) { wrap.classList.add("hidden"); wrap.innerHTML = ""; return; }
    wrap.classList.remove("hidden");
    wrap.innerHTML = tabs.map((u, i) =>
      `<button class="rb-tab ${i === (rb.session?.active_tab ?? 0) ? "active" : ""}" data-i="${i}">${esc(shortHost(u, i))}</button>`).join("");
    wrap.querySelectorAll(".rb-tab").forEach((b) =>
      b.addEventListener("click", async () => {
        const { session } = await api("/api/browser/interactive/action", {
          method: "POST", body: JSON.stringify({ action: "switch_tab", args: { index: +b.dataset.i } }),
        });
        rb.session = session;
        renderRbTabs();
        rbTick();
      }));
  }

  function shortHost(u, i) {
    try { return `Tab ${i + 1} · ${new URL(u).hostname.replace(/^www\./, "")}`; }
    catch { return `Tab ${i + 1}`; }
  }

  function rbHide() {
    rb.session = null;
    $("#rbrowser").classList.add("hidden");
    document.body.style.overflow = "";
    clearInterval(rb.timer); rb.timer = null;
    clearInterval(rb.pollTimer); rb.pollTimer = null;
  }

  async function rbTick() {
    if (!rb.session || rb.busy) return;
    rb.busy = true;
    try {
      const res = await fetch("/api/browser/interactive/frame", { headers: authHeaders() });
      if (res.ok) {
        const { frame } = await res.json();
        const img = $("#rb-frame");
        img.src = "data:image/jpeg;base64," + frame;
        img.classList.remove("hidden");
        $("#rb-off").classList.add("hidden");
      } else {
        $("#rb-frame").classList.add("hidden");
        $("#rb-off").classList.remove("hidden");
        $("#rb-off").textContent = res.status === 404 ? "Remote browser is off." : "Reconnecting…";
      }
      // Refresh TTL + URL from state endpoint (cheap, no image).
      const st = await fetch("/api/browser/interactive/state", { headers: authHeaders() }).then((r) => r.json()).catch(() => null);
      if (st?.session) {
        const prevTabCount = rb.session?.tabs?.length ?? 0;
        rb.session = st.session;
        $("#rb-url").textContent = st.session.url || "—";
        if ((st.session.tabs?.length ?? 0) !== prevTabCount) renderRbTabs();
        const secs = st.session.seconds_remaining ?? 0;
        $("#rb-timer-t").textContent = fmtElapsed(secs) + " left";
        if (secs <= 0) { toast("Interactive session expired"); rbDone(); }
      } else {
        rbDone();
      }
    } catch {} finally { rb.busy = false; }
  }

  async function rbAct(action, args) {
    if (!rb.session) return;
    try {
      const data = await api("/api/browser/interactive/action", {
        method: "POST", body: JSON.stringify({ action, args }),
      });
      rb.session = data.session;
      $("#rb-url").textContent = data.session.url || "—";
      renderRbTabs();
      rbTick();
    } catch (e) {
      toast(e.message);
      if (String(e.message).includes("expired") || String(e.message).includes("no open")) rbDone();
    }
  }

  // Immediate visual acknowledgment between tap and the next streamed frame.
  function rbFlash(clientX, clientY) {
    const img = $("#rb-frame");
    const r = img.getBoundingClientRect();
    const d = document.createElement("div");
    d.className = "rb-tap";
    d.style.left = `${clientX - r.left}px`;
    d.style.top = `${clientY - r.top}px`;
    $("#rb-frame-wrap").appendChild(d);
    setTimeout(() => d.remove(), 420);
  }

  async function rbDone() {
    if (!rb.session) { rbHide(); return; }
    const id = rb.session.id;
    rbHide();
    try { await api("/api/browser/interactive/complete", { method: "POST", body: JSON.stringify({ outcome: "completed" }) }); } catch {}
    void id;
    refreshSettings();
  }

  function rbBind() {
    $("#rb-back").addEventListener("click", rbDone);
    // Tap-to-click on the live frame: phone px → 1440×900 page px.
    // object-fit: contain letterboxes the JPEG inside the element box; only
    // the rendered image area accepts taps, and coordinates scale through the
    // actual drawn rect — otherwise landscape taps land far off-target.
    const img = $("#rb-frame");
    let lastTapAt = 0, lastTapX = -1, lastTapY = -1;
    const doClick = (clientX, clientY) => {
      const r = img.getBoundingClientRect();
      const natW = img.naturalWidth || 1440, natH = img.naturalHeight || 900;
      const scale = Math.min(r.width / natW, r.height / natH);
      const drawW = natW * scale, drawH = natH * scale;
      const offX = (r.width - drawW) / 2, offY = (r.height - drawH) / 2;
      const px = clientX - r.left - offX, py = clientY - r.top - offY;
      if (px < 0 || py < 0 || px > drawW || py > drawH) return; // letterbox tap
      rbFlash(clientX, clientY);
      rbAct("click", { x: Math.round(px / scale), y: Math.round(py / scale) });
    };
    // One pointer path: pointerup fires for touch AND mouse, replacing the
    // legacy click/touchend pair that double-fired and raced a 30ms de-dupe.
    img.addEventListener("pointerup", (e) => {
      if (!e.isPrimary) return;
      e.preventDefault();
      const now = Date.now();
      const sameSpot = Math.abs(e.clientX - lastTapX) < 28 && Math.abs(e.clientY - lastTapY) < 28;
      // Same spot within 350ms = accidental double-fire (synthetic click after
      // touch), NOT a second intentional tap — deliberate double-taps pass.
      if (now - lastTapAt < 350 && sameSpot) return;
      lastTapAt = now; lastTapX = e.clientX; lastTapY = e.clientY;
      doClick(e.clientX, e.clientY);
    });
    $("#rb-send").addEventListener("click", () => {
      const v = $("#rb-text").value;
      if (!v) return;
      rbAct("type", { text: v });
      $("#rb-text").value = "";
    });
    $("#rb-text").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); $("#rb-send").click(); }
    });
    $("#rb-key").addEventListener("click", () => {
      const v = prompt("Key to send (e.g. Enter, Tab, Backspace, a, 1):");
      if (v) rbAct("key", { key: v });
    });
    $$("#rbrowser .rb-key-row button").forEach((b) =>
      b.addEventListener("click", () => rbAct("key", { key: b.dataset.key })));
    // Vertical swipe scrolls; batched per 120ms so a long flick is ONE
    // round-trip instead of a storm competing with frame polling.
    let touchY = null, lastScrollAt = 0, pendingDy = 0;
    img.addEventListener("touchstart", (e) => { touchY = e.touches[0].clientY; }, { passive: true });
    img.addEventListener("touchmove", (e) => {
      if (touchY === null || e.touches.length !== 1) return;
      const y = e.touches[0].clientY;
      pendingDy += touchY - y;
      touchY = y;
      const now = Date.now();
      if (Math.abs(pendingDy) > 24 && now - lastScrollAt >= 120) {
        rbAct("scroll", { dy: Math.round(pendingDy * 2) });
        pendingDy = 0;
        lastScrollAt = now;
      }
    }, { passive: true });
    img.addEventListener("touchend", () => { touchY = null; }, { passive: true });
  }

  async function openInteractive(purpose, marketplace) {
    try {
      const data = await api("/api/browser/interactive/start", {
        method: "POST", body: JSON.stringify({ purpose, marketplace: marketplace || "us" }),
      });
      rbShow(data.session);
    } catch (e) { toast(e.message); }
  }

  // ---------- settings ----------
  async function refreshSettings() {
    try {
      const [az, kd, mk] = await Promise.all([
        api("/api/browser/auth/amazon").catch(() => null),
        api("/api/browser/extensions/kdspy").catch(() => null),
        api("/api/browser/marketplaces").catch(() => null),
      ]);
      const setChip = (id, ok, warn) => {
        const c = $(id);
        c.textContent = ok ? "ready" : warn ? "attention" : "—";
        c.className = `chip ${ok ? "completed" : warn ? "paused" : ""}`;
      };
      if (az) {
        const ok = az.auth?.status === "confirmed";
        $("#set-amazon-state").textContent =
          ok ? "Signed in — persistent profile active" :
          az.auth?.status ? `Status: ${az.auth.status}` : "Not signed in yet";
        setChip("#set-amazon-chip", ok, !!az.auth?.status && !ok);
        $("#set-amazon-open").textContent = ok ? "Re-sign in" : "Sign in";
      }
      if (kd) {
        const st = kd.extension?.status;
        const ok = st === "validated" || st === "installed";
        $("#set-kdspy-state").textContent =
          st === "validated" ? `Loaded in browser${kd.extension.version ? ` · v${kd.extension.version}` : ""}` :
          st === "installed" ? "Installed — loads on next browser launch" :
          st === "failed" ? `Problem: ${kd.extension.detail?.error || kd.extension.detail?.reason || "invalid extension"}` :
          "Not installed yet";
        setChip("#set-kdspy-chip", ok, !!st && !ok);
        // Keep the HTML label (“Install from Web Store”) — only the suffix
        // changes once the extension is present. Never clobber the handler's
        // primary action wording here.
        const kdBtn = $("#set-kdspy-open");
        if (!kdBtn.disabled) kdBtn.textContent = ok ? "Update" : "Install from Web Store";
        // Firefox add-on (loads natively on the camoufox engine).
        const fx = kd.firefox_addon || {};
        const fxOk = fx.status === "validated" || fx.status === "installed";
        $("#set-kdspy-fx-state").textContent =
          fx.status === "validated" ? `Loaded in browser${fx.version ? ` · v${fx.version}` : ""}` :
          fx.status === "installed" ? "Installed — loads on next browser launch" :
          fx.status === "failed" ? `Problem: ${fx.detail?.error || fx.detail?.reason || "invalid add-on"}` :
          "Not installed — upload the KDSpy XPI";
        setChip("#set-kdspy-fx-chip", fxOk, !!fx.status && !fxOk);
        // Engine note: on camoufox the Chromium MV3 build cannot load; the
        // Firefox add-on is the supported path there.
        const note = $("#set-kdspy-engine-note");
        if (kd.engine === "camoufox" && !fxOk) {
          note.hidden = false;
          note.textContent = "Chromium MV3 build can't run on this engine — use the Firefox add-on below.";
        } else if (kd.engine === "camoufox" && fxOk) {
          note.hidden = false;
          note.textContent = "Firefox add-on active for research — no Chromium needed.";
        } else {
          note.hidden = true;
        }
        // The primary install button follows the engine: camoufox fetches the
        // Firefox add-on from Mozilla Add-ons (one tap, like the Web Store
        // path on chromium); the sign-in browser opens after either.
        kdBtn.dataset.engine = kd.engine || "camoufox";
        if (kd.engine === "camoufox" && !fxOk && !kdBtn.textContent.startsWith("Install Firefox")) {
          kdBtn.textContent = "Install Firefox Add-on";
        } else if (kd.engine !== "camoufox" && kdBtn.textContent.startsWith("Install Firefox")) {
          kdBtn.textContent = "Install from Web Store";
        }
      }
      try {
        const sys = await api("/api/system");
        const mOk = !!sys.model?.api_key_set;
        $("#set-model-state").textContent = mOk
          ? `${sys.model.model} ready${sys.model.vision ? " · vision on" : ""}`
          : "API key missing — add MODEL_API_KEY in the environment";
        setChip("#set-model-chip", mOk, !mOk);
      } catch {}
      if (mk) {
        $("#set-mkt-count").textContent = `${mk.marketplaces?.length ?? "—"} available`;
      }
      // Device relay status (phone IP egress).
      try {
        const rl = await api("/api/relay/status");
        const r = rl.relay || {};
        const chip = $("#set-relay-chip");
        if (r.enabled && r.device_connected) {
          chip.textContent = "paired";
          chip.className = "chip completed";
          $("#set-relay-state").textContent = "Active — research exits through your device's IP";
          $("#set-relay-enable").textContent = "Disable";
        } else if (r.enabled) {
          chip.textContent = "waiting";
          chip.className = "chip paused";
          $("#set-relay-state").textContent = "Enabled — start the client on your phone/home device to pair";
          $("#set-relay-enable").textContent = "Disable";
        } else {
          chip.textContent = "off";
          chip.className = "chip";
          $("#set-relay-state").textContent = "Off — browser uses the configured proxy or direct egress";
          $("#set-relay-enable").textContent = "Enable";
        }
        $("#relay-help-row").hidden = false;
      } catch {}
    } catch {}
    try {
      const m = await api("/api/methodology");
      $("#set-version").textContent = `${m.title || "9-phase methodology"} · Resurrección`;
    } catch {}
  }

  function bindRelayControls() {
    $("#set-relay-enable").addEventListener("click", async () => {
      const btn = $("#set-relay-enable");
      const enabling = btn.textContent.trim().toLowerCase() !== "disable";
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> …';
      try {
        await api(enabling ? "/api/relay/enable" : "/api/relay/disable", { method: "POST" });
        toast(enabling ? "Relay enabled — start the client on your device" : "Relay disabled");
      } catch (e) { toast(e.message); }
      btn.disabled = false;
      btn.textContent = enabling ? "Disable" : "Enable";
      refreshSettings();
    });
    $("#set-relay-test").addEventListener("click", async () => {
      const out = $("#set-relay-test-out");
      const btn = $("#set-relay-test");
      btn.disabled = true;
      out.hidden = false;
      out.textContent = "Testing egress…";
      try {
        const res = await api("/api/relay/test-egress", { method: "POST" });
        const ip = res.egress_ip || "unknown";
        out.textContent = `Sites see: ${ip} · via ${res.path || "?"}${res.device_connected ? " · device paired" : " · device offline"}`;
      } catch (e) { out.textContent = `Test failed: ${e.message}`; }
      btn.disabled = false;
    });
    $("#set-relay-token-btn").addEventListener("click", async () => {
      const out = $("#set-relay-token-out");
      if (!out.hidden) { out.hidden = true; return; }
      try {
        const res = await api("/api/relay/token");
        out.hidden = false;
        out.textContent = `Token: ${res.token}`;
      } catch (e) { toast(e.message); }
    });
  }
  bindRelayControls();

  function bindKdspyUpload() {
    const installStatus = $("#kdspy-upload-status");
    const status = (msg) => { installStatus.textContent = msg; };

    // Primary: one-tap install — engine-aware. On camoufox the Firefox
    // add-on comes from Mozilla Add-ons; on chromium the MV3 build comes
    // from the Chrome Web Store. Both then open the sign-in browser.
    $("#set-kdspy-open").addEventListener("click", async () => {
      const btn = $("#set-kdspy-open");
      const label = btn.textContent;
      const onCamoufox = (btn.dataset.engine || "camoufox") === "camoufox";
      const url = onCamoufox
        ? "/api/browser/extensions/kdspy/install-amo"
        : "/api/browser/extensions/kdspy/install-store";
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span> Fetching…';
      status(onCamoufox
        ? "Downloading KDSpy Firefox add-on from Mozilla Add-ons…"
        : "Downloading KDSpy from the Chrome Web Store…");
      try {
        const res = await fetch(url, {
          method: "POST", headers: authHeaders(),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        status("");
        await finishInstall(onCamoufox
          ? `KDSpy Firefox add-on ${data.manifest?.version || ""}`.trim()
          : `KDSpy ${data.manifest?.version || ""}`.trim());
      } catch (err) {
        status("");
        toast(err.message);
        // Network problems (e.g. no egress) — reveal the manual fallback row.
        $("#kdspy-upload-row").hidden = false;
      } finally {
        btn.disabled = false;
        btn.textContent = label;
      }
    });

    $("#set-amazon-open").addEventListener("click", () => openInteractive("amazon_signin", "us"));
    $("#kdspy-pick-zip").addEventListener("click", () => $("#kdspy-zip-input").click());
    $("#kdspy-pick-files").addEventListener("click", () => $("#kdspy-files-input").click());
    $("#kdspy-pick-xpi").addEventListener("click", () => $("#kdspy-xpi-input").click());

    // KDSpy Firefox add-on (XPI) — the engine-native path on camoufox.
    $("#kdspy-xpi-input").addEventListener("change", async (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      status(`Uploading ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      try {
        const res = await fetch("/api/browser/extensions/kdspy/install-xpi", {
          method: "POST", headers: authHeaders(), body: fd,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        await finishInstall(`KDSpy Firefox add-on ${data.manifest?.version || ""}`.trim());
      } catch (err) { status(""); toast(err.message); }
      finally { e.target.value = ""; }
    });

    async function finishInstall(label) {
      status(label + " installed. Relaunching browser…");
      await refreshSettings();
      status(label + " installed.");
      toast("KDSpy installed — opening setup browser");
      openInteractive("kdspy_setup");
    }

    $("#kdspy-zip-input").addEventListener("change", async (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      status(`Uploading ${f.name}…`);
      const fd = new FormData();
      fd.append("file", f);
      try {
        const res = await fetch("/api/browser/extensions/kdspy/install", {
          method: "POST", headers: authHeaders(), body: fd,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        await finishInstall(`KDSpy ${data.manifest?.version || ""}`.trim());
      } catch (err) { status(""); toast(err.message); }
      finally { e.target.value = ""; }
    });

    $("#kdspy-files-input").addEventListener("change", async (e) => {
      const files = Array.from(e.target.files || []);
      if (!files.length) return;
      status(`Uploading ${files.length} files…`);
      const fd = new FormData();
      let paths = "";
      for (const f of files) {
        fd.append("files", f);
        // webkitdirectory-relative path when available, else bare name.
        const rel = f.webkitRelativePath || f.name;
        paths += rel + "\n";
      }
      fd.append("paths", paths);
      try {
        const res = await fetch("/api/browser/extensions/kdspy/install-files", {
          method: "POST", headers: authHeaders(), body: fd,
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.detail || res.statusText);
        await finishInstall(`KDSpy ${data.manifest?.version || ""}`.trim());
      } catch (err) { status(""); toast(err.message); }
      finally { e.target.value = ""; }
    });
  }

  // ---------- login ----------
  function bindAuth() {
    $("#login-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const err = $("#login-error");
      err.hidden = true;
      try {
        const data = await api("/api/auth/login", {
          method: "POST",
          body: JSON.stringify({ username: $("#login-username").value, password: $("#login-password").value }),
        });
        localStorage.setItem(TOKEN_KEY, data.token);
        showApp();
      } catch (ex) { err.textContent = ex.message; err.hidden = false; }
    });
    $("#logout-btn").addEventListener("click", async () => {
      try { await api("/api/auth/logout", { method: "POST" }); } catch {}
      localStorage.removeItem(TOKEN_KEY);
      state.current = null; state.sessions = []; state.liveSessionId = null;
      showLogin();
    });
  }

  // ---------- boot ----------
  function bindTabbar() {
    $$("#tabbar button").forEach((b) => b.addEventListener("click", () => setView(b.dataset.view)));
    $("#lv-toggle").addEventListener("click", toggleLiveView);
    $("#tb-record").addEventListener("click", toggleRecording);
    rbBind();
    bindKdspyUpload();
  }

  async function init() {
    bindAuth(); bindTabbar(); bindStartResearch();
    if (!localStorage.getItem(TOKEN_KEY)) { showLogin(); return; }
    try { await api("/api/auth/me"); showApp(); }
    catch { localStorage.removeItem(TOKEN_KEY); showLogin(); }
  }

  init();
})();
