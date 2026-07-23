/* Asta UI: WS streaming chat, conversations, graph tab, memory tab, token login. */

const $ = (s) => document.querySelector(s);
const state = {
  token: localStorage.getItem("asta_token") || "",
  convId: null,
  ws: null,
  streaming: false,
  streamEl: null,
  toolsEl: null,
};

/* ---------- auth ---------- */

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + state.token, ...(opts.headers || {}) },
  });
  if (r.status === 401) { showLogin(); throw new Error("unauthorized"); }
  return r.json();
}

function showLogin() { $("#login").classList.remove("hidden"); }

async function tryLogin(token) {
  const r = await fetch("/api/login", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  if (!r.ok) { $("#login-err").textContent = "Wrong token"; return; }
  state.token = token;
  localStorage.setItem("asta_token", token);
  $("#login").classList.add("hidden");
  boot();
}

$("#login-btn").onclick = () => tryLogin($("#login-token").value.trim());
$("#login-token").addEventListener("keydown", (e) => { if (e.key === "Enter") tryLogin(e.target.value.trim()); });

/* ---------- markdown-lite ---------- */

function esc(s) { return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;"); }
function md(s) {
  let out = "", parts = s.split(/```/);
  parts.forEach((part, i) => {
    if (i % 2) { // code block
      const nl = part.indexOf("\n");
      const body = nl >= 0 ? part.slice(nl + 1) : part;
      out += "<pre><code>" + esc(body) + "</code></pre>";
    } else {
      out += esc(part)
        .replace(/`([^`\n]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
        .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
    }
  });
  return out;
}

/* ---------- chat rendering ---------- */

function addMsg(role, text, opts = {}) {
  const el = document.createElement("div");
  el.className = "msg " + role + (opts.streaming ? " streaming" : "");
  el.innerHTML = md(text);
  el.dataset.raw = text;
  $("#messages").appendChild(el);
  scrollBottom();
  return el;
}

function addToolChip(name) {
  if (!state.toolsEl) {
    state.toolsEl = document.createElement("div");
    state.toolsEl.className = "tools";
    $("#messages").insertBefore(state.toolsEl, state.streamEl);
  }
  const chip = document.createElement("span");
  chip.className = "chip running";
  chip.dataset.name = name;
  chip.textContent = "⚙ " + name.replace(/_/g, " ") + "…";
  state.toolsEl.appendChild(chip);
  scrollBottom();
}

function finishToolChip(name) {
  if (!state.toolsEl) return;
  const chip = [...state.toolsEl.children].reverse().find((c) => c.dataset.name === name && c.classList.contains("running"));
  if (chip) { chip.classList.remove("running"); chip.textContent = "✓ " + name.replace(/_/g, " "); }
}

function scrollBottom() { const m = $("#messages"); m.scrollTop = m.scrollHeight; }

/* ---------- websocket ---------- */

function wsConnect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws?token=${encodeURIComponent(state.token)}`);
  ws.onmessage = (ev) => handleWs(JSON.parse(ev.data));
  ws.onclose = () => { state.ws = null; setTimeout(() => { if (!state.ws) state.ws = wsConnect(); }, 1500); };
  return ws;
}

function handleWs(msg) {
  // A turn you started in another chat keeps running; its deltas must not be
  // painted into the conversation you're looking at now.
  if (msg.conversation_id && state.convId && msg.conversation_id !== state.convId
      && msg.type !== "conv") return;
  if (msg.type === "conv") {
    state.convId = msg.conversation.id;
    loadConversations();
  } else if (msg.type === "delta") {
    if (!state.streamEl) state.streamEl = addMsg("assistant", "", { streaming: true });
    state.streamEl.dataset.raw += msg.text;
    state.streamEl.innerHTML = md(state.streamEl.dataset.raw);
    scrollBottom();
    if (state.voiceMode) setVoiceState("speaking…");
    pumpSpeech(state.streamEl.dataset.raw);   // talk while the rest is still arriving
  } else if (msg.type === "tool") {
    if (msg.status === "start") {
      if (!state.streamEl) state.streamEl = addMsg("assistant", "", { streaming: true });
      addToolChip(msg.name);
    } else finishToolChip(msg.name);
  } else if (msg.type === "done") {
    if (state.streamEl) {
      state.streamEl.classList.remove("streaming");
      pumpSpeech(state.streamEl.dataset.raw, true);   // speak whatever is left
    }
    endTurn();
    // Nothing to say (or speech is off): go straight back to listening.
    if (state.voiceMode && !state.sayDraining && !state.barged) voiceListen();
    loadConversations();
  } else if (msg.type === "note") {
    const el = document.createElement("div");
    el.className = "sysnote";
    el.textContent = msg.text;
    $("#messages").appendChild(el);
    scrollBottom();
  } else if (msg.type === "error") {
    if (state.streamEl) state.streamEl.classList.remove("streaming");
    addMsg("error", "⚠ " + msg.message);
    endTurn();
    if (state.voiceMode) { resetSpeech(); speak("Something went wrong: " + msg.message, voiceListen, true); }
  }
}

function endTurn() {
  state.streaming = false; state.streamEl = null; state.toolsEl = null;
  // Voice mode drains its queue after the spoken reply (see voiceListen).
  if (!state.voiceMode) drainPending();
}

/* Commands given while a turn is running are queued and dispatched in order
   once the current reply finishes — nothing you say or type gets lost. */
state.pending = [];

function queueNote(text) {
  const el = document.createElement("div");
  el.className = "sysnote";
  el.textContent = "⏳ next up: " + text.slice(0, 80);
  $("#messages").appendChild(el);
  scrollBottom();
}

function send() {
  const text = $("#input").value.trim();
  if (!text) return;
  if (!state.ws || state.ws.readyState !== 1) { state.ws = wsConnect(); setTimeout(send, 400); return; }
  $("#input").value = ""; autoGrow();
  addMsg("user", text);
  state.streaming = true;
  state.ws.send(JSON.stringify({
    type: "chat",
    conversation_id: state.convId,
    message: text,
    model: $("#model-pick").value,
    workspace: $("#workspace-pick").value || null,
  }));

}

function drainPending() {
  if (state.streaming || !state.pending.length) return false;
  $("#input").value = state.pending.shift();
  send();
  return true;
}

$("#send").onclick = send;
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
function autoGrow() { const i = $("#input"); i.style.height = "auto"; i.style.height = Math.min(i.scrollHeight, 160) + "px"; }
$("#input").addEventListener("input", autoGrow);

/* ---------- conversations ---------- */

async function loadConversations() {
  const convs = await api("/api/conversations");
  const list = $("#conv-list"); list.innerHTML = "";
  convs.forEach((c) => {
    const el = document.createElement("div");
    el.className = "conv" + (c.id === state.convId ? " active" : "");
    el.innerHTML = `<span class="title">${esc(c.title)}</span><button class="del" title="delete">✕</button>`;
    el.querySelector(".title").onclick = () => openConversation(c);
    el.querySelector(".del").onclick = async (e) => {
      e.stopPropagation();
      await api("/api/conversations/" + c.id, { method: "DELETE" });
      if (state.convId === c.id) newChat();
      loadConversations();
    };
    list.appendChild(el);
  });
}

async function openConversation(c) {
  state.convId = c.id;
  $("#model-pick").value = c.model;
  $("#workspace-pick").value = c.workspace || "";
  $("#messages").innerHTML = "";
  const msgs = await api(`/api/conversations/${c.id}/messages`);
  msgs.forEach((m) => {
    if (m.meta && m.meta.tools && m.meta.tools.length) {
      const t = document.createElement("div"); t.className = "tools";
      m.meta.tools.forEach((name) => {
        const chip = document.createElement("span"); chip.className = "chip";
        chip.textContent = "✓ " + name.replace(/_/g, " "); t.appendChild(chip);
      });
      $("#messages").appendChild(t);
    }
    addMsg(m.role, m.content);
  });
  loadConversations();
  closeSidebar();
  switchTab("chat");
}

function newChat() {
  state.convId = null;
  $("#messages").innerHTML = "";
  addMsg("assistant", "Online. What are we working on?");
  closeSidebar();
  switchTab("chat");
}
$("#new-chat").onclick = () => { newChat(); loadConversations(); };

/* ---------- status / models ---------- */

function applyName(name) {
  if (!name) return;
  document.title = name;
  $("#brand-name").textContent = name;
  $("#login-name").textContent = name;
  $("#input").placeholder = `Ask ${name}… (Enter to send, Shift+Enter for newline)`;
  document.querySelectorAll(".wake-name").forEach((el) => (el.textContent = name));
}

async function loadStatus() {
  const s = await api("/api/status");
  applyName(s.name);
  const pick = $("#model-pick");
  const prev = pick.value;
  pick.innerHTML = "";
  Object.entries(s.models).forEach(([name, m]) => {
    const o = document.createElement("option");
    o.value = name; o.textContent = m.label + (m.available ? "" : " (off)");
    o.disabled = !m.available;
    pick.appendChild(o);
  });
  const firstOn = Object.entries(s.models).find(([, m]) => m.available);
  pick.value = prev && [...pick.options].some((o) => o.value === prev && !o.disabled) ? prev : firstOn ? firstOn[0] : "claude";

  renderWorkspacePicker(s.workspaces || {});

  const mcp = s.mcp.map((m) =>
    `<span class="${m.enabled ? "on" : "off"}" title="${esc(m.reason || "connected")}">${m.name}</span>`
  ).join(" · ");
  $("#side-status").innerHTML = `MCP: ${mcp || "none"}<br>memories: ${s.memories}`;
}

/* ---------- tabs ---------- */

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => p.classList.toggle("active", p.id === "pane-" + name));
  if (name === "graph") loadGraphs();
  if (name === "memory") loadMemory();
  if (name === "missions") loadMissions();
  if (name === "settings") loadSettings();
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => switchTab(t.dataset.tab)));

function renderWorkspacePicker(spaces) {
  const pick = $("#workspace-pick");
  const prev = pick.value;
  pick.innerHTML = '<option value="">no workspace</option>';
  Object.keys(spaces).forEach((name) => {
    const o = document.createElement("option");
    o.value = name;
    o.textContent = name + (spaces[name].exists ? "" : " (missing)");
    pick.appendChild(o);
  });
  // Exactly one workspace is the common case — select it so the user never has to.
  const names = Object.keys(spaces);
  pick.value = prev && names.includes(prev) ? prev : (names.length === 1 ? names[0] : "");
}

async function loadGraphs() {
  const ws = $("#workspace-pick").value;
  if (!ws) { $("#graph-frame").src = "about:blank"; $("#graph-pick").innerHTML = ""; return; }
  const pages = await api("/api/graphs/" + ws);
  const pick = $("#graph-pick");
  pick.innerHTML = "";
  pages.forEach((p) => {
    const o = document.createElement("option");
    o.value = p.url; o.textContent = `${ws} / ${p.label}`;
    pick.appendChild(o);
  });
  if (pages.length) setGraph(pick.value);
  else { $("#graph-frame").src = "about:blank"; }
}
function setGraph(url) {
  $("#graph-frame").src = url;
  $("#graph-open").href = url;
}
$("#graph-pick").onchange = (e) => setGraph(e.target.value);
$("#workspace-pick").onchange = () => { if ($("#pane-graph").classList.contains("active")) loadGraphs(); };

/* ---------- memory tab ---------- */

async function loadMemory() {
  const m = await api("/api/memory");
  const list = $("#memory-list"); list.innerHTML = "";
  const idx = document.createElement("div");
  idx.className = "mem-item active";
  idx.innerHTML = '<span class="tag">index</span>MEMORY.md';
  idx.onclick = () => { setActiveMem(idx); $("#memory-view").textContent = m.index; };
  list.appendChild(idx);
  $("#memory-view").textContent = m.index;
  m.items.forEach((it) => {
    const el = document.createElement("div");
    el.className = "mem-item";
    el.innerHTML = `<span class="tag">${esc(it.type)}</span>${esc(it.title)}`;
    el.onclick = async () => {
      setActiveMem(el);
      const f = await api("/api/memory/file?path=" + encodeURIComponent(it.path));
      $("#memory-view").textContent = f.content;
    };
    list.appendChild(el);
  });
}
function setActiveMem(el) {
  document.querySelectorAll(".mem-item").forEach((x) => x.classList.remove("active"));
  el.classList.add("active");
}

/* ---------- voice: dictation + spoken replies ---------- */

state.tts = localStorage.getItem("asta_tts") === "1";
function renderTts() {
  $("#tts").textContent = state.tts ? "🔊" : "🔇";
  $("#tts").classList.toggle("on", state.tts);
}
$("#tts").onclick = () => {
  state.tts = !state.tts;
  localStorage.setItem("asta_tts", state.tts ? "1" : "0");
  if (!state.tts) stopSpeaking();
  renderTts();
};
/* Two speech engines. Voicebox runs locally (real neural voices, and your own
   cloned voice) and is used whenever its backend is up; the browser's built-in
   speechSynthesis is the fallback so voice never dies just because Voicebox
   isn't running. */
state.vb = { available: false, profiles: [] };

async function loadVoicebox() {
  try {
    const s = await api("/api/voice/status");
    state.vb = { available: !!s.available, profiles: s.profiles || [] };
  } catch { state.vb = { available: false, profiles: [] }; }
  pickVoice();
}

/* Voice choice: user-picked (Settings) wins; otherwise prefer the natural-sounding
   voices (Google's neural ones in Chrome, macOS Samantha) over the robotic defaults. */
let ttsVoice = null;
function pickVoice() {
  const vs = ("speechSynthesis" in window) ? speechSynthesis.getVoices() : [];
  const saved = localStorage.getItem("asta_voice");
  ttsVoice =
    (saved && vs.find((v) => v.name === saved)) ||
    vs.find((v) => /google uk english female/i.test(v.name)) ||
    vs.find((v) => /google us english/i.test(v.name)) ||
    vs.find((v) => /samantha/i.test(v.name)) ||
    vs.find((v) => v.lang === "en-IN") ||
    vs.find((v) => /india/i.test(v.name)) ||
    vs.find((v) => v.lang && v.lang.startsWith("en")) || null;
  renderVoicePicker(vs);
}

function savedProfile() { return localStorage.getItem("asta_vb_profile") || ""; }

function renderVoicePicker(vs) {
  const sel = $("#tts-voice");
  if (!sel) return;
  if (state.vb.available && state.vb.profiles.length) {
    const chosen = savedProfile();
    sel.innerHTML = state.vb.profiles
      .map((p) => `<option value="${p.name}"${p.name === chosen ? " selected" : ""}>${p.name}${p.engine ? " (" + p.engine + ")" : ""}</option>`)
      .join("");
    sel.onchange = () => localStorage.setItem("asta_vb_profile", sel.value);
  } else {
    const english = vs.filter((v) => v.lang && v.lang.startsWith("en"));
    sel.innerHTML = english
      .map((v) => `<option value="${v.name}"${ttsVoice && v.name === ttsVoice.name ? " selected" : ""}>${v.name} (${v.lang})</option>`)
      .join("");
    sel.onchange = () => {
      localStorage.setItem("asta_voice", sel.value);
      pickVoice();
    };
  }
  const test = $("#tts-voice-test");
  if (test) test.onclick = () => speak("Hi Arun, this is how I sound now.", null, true);
}
if ("speechSynthesis" in window) speechSynthesis.onvoiceschanged = pickVoice;

/* Echo defense: the mic must never transcribe our own TTS. Chrome fires
   utterance onend before the audio has fully drained, so we (a) block
   recognition results while speech is active or draining, and (b) drop any
   transcript that mostly repeats what we just said. */
let speakingUntil = 0;
let lastSpoken = "";
let lastSpokenAt = 0;
let vbAudio = null;   // <audio> playing a Voicebox clip
let vbBusy = false;   // waiting on Voicebox to render — mic must stay shut here too

function micBlocked() {
  return ("speechSynthesis" in window && speechSynthesis.speaking) ||
    vbBusy || (vbAudio && !vbAudio.paused) || Date.now() < speakingUntil;
}

function stopSpeaking() {
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  if (vbAudio) { try { vbAudio.pause(); } catch {} vbAudio = null; }
}

function isEcho(text) {
  if (!lastSpoken || Date.now() - lastSpokenAt > 20000) return false;
  const words = text.toLowerCase().split(/\s+/).filter((w) => w.length > 2);
  if (!words.length) return false;
  const hits = words.filter((w) => lastSpoken.includes(w)).length;
  return hits / words.length > 0.6;
}

/* ---------- streaming speech: start talking before the answer is finished ----

   Waiting for the whole reply meant 11-18s of silence before Asta said a word,
   which is what made voice mode feel like submitting a ticket. Instead each
   finished sentence is spoken as it streams in, so the first words land in a
   second or two while the rest is still being written. */

state.sayQueue = [];
state.sayDraining = false;
state.spokenUpTo = 0;   // how much of the current reply has been queued already

/* Speak up to the last completed sentence. Devanagari's danda counts too. */
function pumpSpeech(raw, flush = false) {
  if (!state.voiceMode && !state.tts) return;
  const rest = raw.slice(state.spokenUpTo);
  if (!rest) return;
  // Don't start speaking a code block that hasn't closed yet.
  const fences = (raw.slice(0, state.spokenUpTo).match(/```/g) || []).length;
  if (fences % 2) return;
  let cut = flush ? rest.length : 0;
  if (!flush) {
    const m = rest.match(/^[\s\S]*?[.!?।](?=\s|$)/);
    if (!m || m[0].length < 12) return;   // too short to be worth a trip
    cut = m[0].length;
  }
  const chunk = rest.slice(0, cut).trim();
  state.spokenUpTo += cut;
  if (chunk) enqueueSpeech(sanitizeSpoken(chunk));
}

function enqueueSpeech(text) {
  if (!text) return;
  state.sayQueue.push(text);
  if (!state.sayDraining) drainSpeech();
}

async function drainSpeech() {
  state.sayDraining = true;
  while (state.sayQueue.length) {
    const next = state.sayQueue.shift();
    await new Promise((done) => speak(next, done, true));
    if (state.barged) break;           // you cut in — drop the rest of the answer
  }
  state.sayDraining = false;
  state.sayQueue.length = 0;
  // Reply fully spoken and the turn is over: hand the mic back.
  if (state.voiceMode && !state.streaming && !state.barged) voiceListen();
}

function resetSpeech() {
  state.sayQueue.length = 0;
  state.spokenUpTo = 0;
  state.barged = false;
  stopSpeaking();
}

function sanitizeSpoken(text) {
  return text
    .replace(/```[\s\S]*?```/g, " Code block omitted. ")
    .replace(/[*_#`>|]/g, "")
    .slice(0, 1200);
}

function cleanForSpeech(text) {
  let clean = text
    .replace(/```[\s\S]*?```/g, " Code block omitted. ")
    .replace(/[*_#`>|]/g, "");
  if (state.voiceMode && clean.length > 400) {
    // Voice is a conversation, not an audiobook — long answers stay on screen.
    const cut = clean.slice(0, 400);
    return cut.slice(0, Math.max(cut.lastIndexOf(". ") + 1, 200)) + " More on screen.";
  }
  return clean.slice(0, 1200);
}

function speak(text, onEnd, force) {
  const wants = force || state.tts || state.voiceMode;
  if (!wants || !text) { if (onEnd) onEnd(); return; }
  const clean = cleanForSpeech(text);
  lastSpoken = clean.toLowerCase();
  lastSpokenAt = Date.now();
  if (state.vb.available) speakVoicebox(clean, onEnd);
  else speakBrowser(clean, onEnd);
}

/* Local Voicebox: real neural speech, optionally your own cloned voice. */
async function speakVoicebox(clean, onEnd) {
  stopSpeaking();
  vbBusy = true;
  try {
    const r = await fetch("/api/voice/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + state.token },
      body: JSON.stringify({ text: clean, profile: savedProfile() }),
    });
    if (!r.ok) throw new Error("tts " + r.status);
    const url = URL.createObjectURL(await r.blob());
    const a = new Audio(url);
    vbAudio = a;
    const done = () => {
      URL.revokeObjectURL(url);
      if (vbAudio === a) vbAudio = null;
      speakingUntil = Date.now() + 600;
      lastSpokenAt = Date.now();
      if (onEnd) setTimeout(onEnd, 600);
    };
    a.onended = done;
    a.onerror = done;
    vbBusy = false;
    if (state.barged) { done(); return; }   // you cut in while this was rendering
    await a.play();
  } catch {
    // Backend down, or a model still loading on first use. Don't drop the
    // reply on the floor — say it with the browser voice and stop trying.
    vbBusy = false;
    state.vb.available = false;
    speakBrowser(clean, onEnd);
  }
}

function speakBrowser(clean, onEnd) {
  if (!("speechSynthesis" in window)) { if (onEnd) onEnd(); return; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(clean);
  u.lang = "en-IN";
  if (ttsVoice) u.voice = ttsVoice;
  lastSpoken = clean.toLowerCase();
  lastSpokenAt = Date.now();
  const finish = () => {
    const drain = () => {
      if (speechSynthesis.speaking) return setTimeout(drain, 150);
      speakingUntil = Date.now() + 600;
      lastSpokenAt = Date.now();
      if (onEnd) setTimeout(onEnd, 600);
    };
    drain();
  };
  u.onend = finish;
  u.onerror = finish;
  speechSynthesis.speak(u);
}

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
let rec = null;
const canRecord = !!(navigator.mediaDevices && window.MediaRecorder);
if (!SR && !canRecord) { $("#mic").style.display = "none"; $("#voice").style.display = "none"; }

/* Two ways to listen. Whisper wins despite being slower.
   The browser recognizer streams words instantly but mangles the vocabulary
   this assistant runs on — it turned "asta" into "giraffe" and handed the
   model a sentence that meant nothing. Whisper costs ~5s on a short command
   (measured, M1 Pro) against turns that already take 11-18s to think, so it's
   roughly a third more wait for input that's actually correct.
   SpeechRecognition stays for the wake word, where a misfire is harmless and
   always-on speed is the whole point, and as the fallback when Voicebox is
   down. */
function startListening(opts) {
  // Conversation needs speed; dictation needs accuracy. In voice mode a reply
  // that lands instantly and is 90% right beats one that's perfect five
  // seconds later — and the model fixes obvious mishearings from context.
  const wantAccurate = opts.accurate || !state.voiceMode;
  if (wantAccurate && state.vb.available && canRecord) return listenWhisper(opts);
  if (SR) return startListeningSR(opts);
  if (state.vb.available && canRecord) return listenWhisper(opts);
  opts.onDone();
}

let mediaRec = null;

function stopListening() {
  if (rec) { try { rec.stop(); } catch {} }
  if (mediaRec) { try { mediaRec.stop(); } catch {} }
}

async function listenWhisper({ base = "", onDone }) {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    return startListeningSR({ base, onDone }); // mic denied → let the browser ask
  }
  const chunks = [];
  const mr = new MediaRecorder(stream);
  mediaRec = mr;
  mr.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
  mr.onstop = async () => {
    stream.getTracks().forEach((t) => t.stop());
    mediaRec = null;
    const blob = new Blob(chunks, { type: mr.mimeType || "audio/webm" });
    let text = "";
    if (blob.size > 2000) { // near-silence produces a tiny file; don't bother Whisper
      // Whisper takes a few seconds — say so, or the silence reads as a hang.
      if (state.voiceMode) setVoiceState("transcribing…");
      $("#mic").classList.add("busy");
      try {
        const fd = new FormData();
        fd.append("file", blob, "speech.webm");
        const r = await fetch("/api/voice/stt", {
          method: "POST",
          headers: { Authorization: "Bearer " + state.token },
          body: fd,
        });
        if (r.ok) text = (await r.json()).text || "";
        else state.vb.available = false; // backend died mid-session — go back to SR
      } catch { state.vb.available = false; }
      $("#mic").classList.remove("busy");
    }
    if (text) { $("#input").value = (base ? base + " " : "") + text; autoGrow(); }
    onDone();
  };
  mr.start();
  stopOnSilence(stream, mr);
}

/* MediaRecorder has no idea when you stopped talking, so watch the waveform and
   cut the clip after a beat of quiet. */
function stopOnSilence(stream, mr, { maxMs = 20000, silenceMs = 1400, graceMs = 700 } = {}) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  const started = Date.now();
  let quietSince = 0;
  const tick = () => {
    if (mr.state !== "recording") { ctx.close().catch(() => {}); return; }
    analyser.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    const now = Date.now();
    if (peak > 6) quietSince = 0;
    else if (!quietSince) quietSince = now;
    const longEnough = now - started > graceMs;
    if ((longEnough && quietSince && now - quietSince > silenceMs) || now - started > maxMs) {
      try { mr.stop(); } catch {}
      ctx.close().catch(() => {});
      return;
    }
    setTimeout(tick, 100); // not rAF: this must keep running in a background tab
  };
  tick();
}

function startListeningSR({ base = "", onDone }) {
  if (!SR) { onDone(); return; }
  if (rec) rec.stop();
  rec = new SR();
  rec.lang = "en-IN";
  rec.interimResults = true;
  rec.continuous = false;
  rec.onresult = (e) => {
    if (micBlocked()) return; // our own TTS is (still) playing — not the user
    let text = "";
    for (const r of e.results) text += r[0].transcript;
    $("#input").value = (base ? base + " " : "") + text;
    autoGrow();
  };
  rec.onend = () => { rec = null; onDone(); };
  rec.onerror = () => {};
  rec.start();
}

$("#mic").onclick = () => {
  if (state.voiceMode) return;
  if (rec || mediaRec) { stopListening(); return; }
  pauseWake();
  $("#mic").classList.add("rec");
  startListening({
    base: $("#input").value,
    onDone: () => {
      $("#mic").classList.remove("rec");
      if ($("#input").value.trim()) send();
      resumeWake();
    },
  });
};

/* Wake word: passively listen for the assistant's name ("Asta") and open a
   voice conversation when heard. Only one recognizer can run at a time, so the
   wake listener pauses whenever dictation or voice mode has the mic. */
state.wake = localStorage.getItem("asta_wake") !== "0";
let wakeRec = null;
let wakeWanted = false;

function wakeWord() { return ($("#brand-name").textContent || "Asta").trim().toLowerCase(); }

/* Speech-to-text spells the name many ways ("Asta" → "Aastha", "Astha", "asta").
   Collapse h's and doubled letters on both sides before comparing. */
function normName(s) { return s.toLowerCase().replace(/h/g, "").replace(/(.)\1+/g, "$1"); }
function heardWakeWord(heard) { return normName(heard).includes(normName(wakeWord())); }

function wakeLoop() {
  if (!SR || !state.wake || state.voiceMode || rec || mediaRec || wakeRec) return;
  wakeWanted = true;
  wakeRec = new SR();
  wakeRec.lang = "en-IN";
  wakeRec.continuous = true;
  wakeRec.interimResults = false;
  wakeRec.onresult = (e) => {
    if (micBlocked()) return; // don't let Asta wake herself by saying her own name
    const heard = [...e.results].map((r) => r[0].transcript).join(" ").toLowerCase();
    if (heardWakeWord(heard)) {
      wakeWanted = false;
      try { wakeRec.stop(); } catch {}
      wakeRec = null;
      startVoiceMode();
    }
  };
  wakeRec.onend = () => { wakeRec = null; if (wakeWanted) setTimeout(wakeLoop, 800); };
  wakeRec.onerror = (e) => { if (e.error === "not-allowed") { state.wake = false; renderWake(); } };
  try { wakeRec.start(); } catch {}
}

function pauseWake() {
  wakeWanted = false;
  if (wakeRec) { try { wakeRec.stop(); } catch {} wakeRec = null; }
}
function resumeWake() { if (state.wake) setTimeout(wakeLoop, 300); }

function renderWake() {
  const cb = $("#wake-enabled");
  if (cb) cb.checked = state.wake;
}

/* Voice conversation mode: listen → send → reply spoken aloud → listen again,
   until you end it. The agent keeps full context, so it's a real back-and-forth. */
state.voiceMode = false;
state.voiceIdleRounds = 0;

function setVoiceState(label) { $("#voice-state").textContent = label; }

/* While the model is thinking, keep the mic open: anything you say is queued
   and runs right after the current reply — no more dead air where commands vanish. */
/* ---------- barge-in ----------------------------------------------------

   The old loop shut the mic while Asta spoke, so you couldn't cut in — that's
   what made it a walkie-talkie. Now one mic stream stays open for the whole
   conversation and watches the room. When you start talking over her, the
   audio stops mid-sentence and she listens.

   Echo is the hard part: on laptop speakers the mic hears Asta herself. Rather
   than guessing the output device, the threshold calibrates against whatever
   echo is actually coming back — loud echo raises the bar automatically, so
   speakers need a real interruption while headphones trigger on a whisper. */

state.micStream = null;
let stopBargeWatch = null;

async function openMic() {
  if (state.micStream) return state.micStream;
  try {
    state.micStream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  } catch { state.micStream = null; }
  return state.micStream;
}

function closeMic() {
  if (stopBargeWatch) { stopBargeWatch(); stopBargeWatch = null; }
  if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop());
    state.micStream = null;
  }
}

function astaIsSpeaking() {
  return ("speechSynthesis" in window && speechSynthesis.speaking) ||
    (vbAudio && !vbAudio.paused) || vbBusy;
}

function watchForBargeIn(stream) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  ctx.createMediaStreamSource(stream).connect(analyser);
  const buf = new Uint8Array(analyser.fftSize);
  const QUIET = 4;
  let floor = QUIET;
  let loudMs = 0;
  let timer = null;

  const tick = () => {
    analyser.getByteTimeDomainData(buf);
    let peak = 0;
    for (const v of buf) peak = Math.max(peak, Math.abs(v - 128));
    if (astaIsSpeaking()) {
      floor = Math.max(floor, peak * 0.75);       // learn this room's echo level
      if (peak > floor * 1.6 && peak > 12) loudMs += 80;
      else loudMs = 0;
      if (loudMs >= 240) { loudMs = 0; bargeIn(); }   // ~a quarter second of you
    } else {
      floor = floor * 0.9 + QUIET * 0.1;          // decay back once she's quiet
      loudMs = 0;
    }
    timer = setTimeout(tick, 80);
  };
  tick();
  return () => { clearTimeout(timer); ctx.close().catch(() => {}); };
}

function bargeIn() {
  if (!state.voiceMode || state.barged) return;
  state.barged = true;
  state.sayQueue.length = 0;
  stopSpeaking();
  speakingUntil = 0;   // don't let the post-speech echo guard swallow your words
  setVoiceState("go ahead…");
  setTimeout(() => { state.barged = false; voiceListen(); }, 120);
}

function voiceListen() {
  if (!state.voiceMode) return;
  if (drainPending()) return; // a queued command goes out before we listen again
  setVoiceState("listening…");
  $("#voice").classList.add("rec");
  startListening({
    onDone: () => {
      $("#voice").classList.remove("rec");
      if (!state.voiceMode) return;
      const text = $("#input").value.trim();
      if (text && isEcho(text)) {
        // Heard ourselves through the speakers — discard, listen again.
        $("#input").value = "";
        voiceListen();
      } else if (text) {
        state.voiceIdleRounds = 0;
        setVoiceState("thinking…");
        send();
      } else if (++state.voiceIdleRounds >= 4) {
        stopVoiceMode(); // ~a minute of silence — assume you walked away
      } else {
        voiceListen();
      }
    },
  });
}

async function startVoiceMode() {
  pauseWake();
  state.voiceMode = true;
  state.voiceIdleRounds = 0;
  state.barged = false;
  $("#voice").classList.add("on");
  $("#voice-banner").classList.remove("hidden");
  const stream = await openMic();
  if (stream) stopBargeWatch = watchForBargeIn(stream);
  speak("Yes, I'm listening.", voiceListen, true);
}

function stopVoiceMode() {
  state.voiceMode = false;
  resetSpeech();
  closeMic();
  $("#voice").classList.remove("on", "rec");
  $("#voice-banner").classList.add("hidden");
  stopListening();
  stopSpeaking();
  resumeWake();
}

$("#voice").onclick = () => (state.voiceMode ? stopVoiceMode() : startVoiceMode());
$("#voice-stop").onclick = stopVoiceMode;

/* ---------- settings (WhatsApp + watchers) ---------- */

async function loadSettings() {
  loadWorkspaces();
  try {
    const wa = await api("/api/wa/status");
    const stateEl = $("#wa-state");
    if (!wa.up) {
      stateEl.textContent = "Bridge not running.";
      $("#wa-qr-wrap").classList.add("hidden");
    } else if (!wa.paired) {
      stateEl.textContent = "Bridge up — not paired yet.";
      if (wa.qr) { $("#wa-qr").src = wa.qr; $("#wa-qr-wrap").classList.remove("hidden"); }
      else stateEl.textContent += " Waiting for QR…";
    } else {
      stateEl.textContent = `Paired ✓ — chat: ${wa.jid || "self"}${wa.enabled ? "" : " (disabled)"}`;
      $("#wa-qr-wrap").classList.add("hidden");
    }
    if (document.activeElement !== $("#wa-jid")) $("#wa-jid").value = wa.allowed_jid || "";
    $("#wa-enabled").checked = wa.enabled !== false;
  } catch (e) {
    $("#wa-state").textContent = "Bridge not reachable.";
  }
  try {
    const s = await api("/api/status");
    const t = s.teams_watcher || {};
    $("#teams-state").textContent = !t.enabled
      ? "Disabled."
      : t.ok
        ? `Active ✓ — watching for: ${(t.keywords || []).join(", ")}`
        : "Enabled but " + (t.reason || "not working");
    const tg = s.telegram || {};
    $("#tg-state").textContent = !tg.enabled
      ? "Not configured — " + (tg.hint || "add TELEGRAM_BOT_TOKEN")
      : tg.bound
        ? "Connected ✓ — chat + notifications active"
        : "Bot token set — " + (tg.hint || "bind your chat with /start");
    const tb = s.teams_bridge || {};
    $("#teamsbridge-state").textContent = !tb.enabled
      ? "Disabled (TEAMS_BRIDGE=1 to enable)."
      : !tb.profile
        ? "Enabled — login needed (see below)."
        : tb.session_ok
          ? "Logged in ✓ — Asta can read + send Teams messages"
          : "Session expired — re-run the login command below.";
  } catch (e) {}
  loadTraces();
}

async function loadTraces() {
  try {
    const t = await api("/api/traces?limit=12");
    if (t.summary.length) {
      $("#trace-summary").innerHTML = t.summary.map((s) =>
        `<b>${esc(s.model)}</b>: ${s.turns} turns · avg ${(s.avg_ms/1000).toFixed(1)}s · ` +
        `tok ${s.input}/${s.output}${s.cached ? ` (${s.cached} cached)` : ""} · ` +
        `prompt ~${Math.round(s.avg_instr_chars/4)} tok${s.errors ? ` · <span style="color:var(--err)">${s.errors} errors</span>` : ""}`
      ).join("<br>");
    }
    $("#trace-list").innerHTML = t.recent.map((r) => {
      const when = new Date(r.created_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      const tools = r.tools.length ? ` · ${r.tools.join(", ")}` : "";
      const err = r.error ? ` · <span style="color:var(--err)">${esc(r.error.slice(0, 60))}</span>` : "";
      return `<div class="trace-row">${when} · <b>${esc(r.model)}</b>/${esc(r.channel)} · ${(r.total_ms/1000).toFixed(1)}s` +
        (r.first_token_ms ? ` (first ${(r.first_token_ms/1000).toFixed(1)}s)` : "") +
        ` · tok ${r.input_tokens}/${r.output_tokens}${tools}${err}</div>`;
    }).join("");
  } catch (e) {}
}

$("#wake-enabled").onchange = (e) => {
  state.wake = e.target.checked;
  localStorage.setItem("asta_wake", state.wake ? "1" : "0");
  state.wake ? resumeWake() : pauseWake();
};

$("#wa-save").onclick = async () => {
  $("#wa-save-msg").textContent = "saving…";
  try {
    const r = await api("/api/wa/config", {
      method: "POST",
      body: JSON.stringify({ enabled: $("#wa-enabled").checked, allowed_jid: $("#wa-jid").value.trim() }),
    });
    $("#wa-save-msg").textContent = r.ok ? "saved ✓" : (r.detail || "failed");
  } catch (e) {
    $("#wa-save-msg").textContent = "failed — is the bridge running?";
  }
  loadSettings();
};

setInterval(() => {
  if ($("#pane-settings").classList.contains("active")) loadSettings();
}, 5000);

/* ---------- notifications ---------- */

async function loadNotifs() {
  try {
    const n = await api("/api/notifications");
    const badge = $("#bell-badge");
    if (n.unseen > 0) { badge.textContent = n.unseen; badge.classList.remove("hidden"); }
    else badge.classList.add("hidden");
    const list = $("#notif-list");
    list.innerHTML = n.items.length ? "" : '<div class="notif dim">Nothing yet.</div>';
    n.items.forEach((it) => {
      const el = document.createElement("div");
      el.className = "notif" + (it.seen ? "" : " unseen");
      const when = new Date(it.created_at * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      el.innerHTML = `<div class="when">${when} · ${esc(it.level)}</div>${esc(it.text)}`;
      list.appendChild(el);
    });
  } catch (e) {}
}
$("#bell").onclick = (e) => {
  e.stopPropagation();
  $("#notif-panel").classList.toggle("hidden");
  loadNotifs();
};
$("#notif-clear").onclick = async () => { await api("/api/notifications/seen", { method: "POST" }); loadNotifs(); };
document.addEventListener("click", (e) => {
  const p = $("#notif-panel");
  if (!p.classList.contains("hidden") && !p.contains(e.target) && e.target.id !== "bell") p.classList.add("hidden");
});

/* ---------- background tasks ---------- */

async function loadTasks() {
  try {
    const rows = await api("/api/tasks");
    const list = $("#task-list");
    if (!rows.length) {
      list.innerHTML = '<p class="dim">No background tasks yet. Ask in chat: “delegate a task to …”.</p>';
      return;
    }
    list.innerHTML = "";
    rows.forEach((t) => {
      const el = document.createElement("div");
      el.className = "mission";
      const dur = t.finished_at && t.started_at ? ` · ${(t.finished_at - t.started_at).toFixed(0)}s` : "";
      const target = t.teams_chat ? ` → ${esc(t.teams_chat)}` : "";
      el.innerHTML = `<div class="m-title"><span class="badge ${esc(t.status)}">${esc(t.status.replace(/_/g, " "))}</span>${esc(t.title)}</div>
        <div class="m-sub">#${t.id} · ${esc(t.kind)}${t.workspace ? " · " + esc(t.workspace) : ""}${target}${dur}</div>`;
      if (t.error) el.innerHTML += `<div class="m-sub" style="color:var(--err)">⚠ ${esc(t.error.slice(0, 120))}</div>`;
      if (t.result) {
        const pre = document.createElement("pre");
        pre.className = "task-result hidden";
        pre.textContent = t.result.slice(0, 3000);
        el.appendChild(pre);
        el.querySelector(".m-title").onclick = () => pre.classList.toggle("hidden");
      }
      if (t.status === "awaiting_approval") {
        const act = document.createElement("div");
        act.className = "m-actions";
        act.innerHTML = `<button class="t-approve">✓ Approve & send</button><button class="t-reject">✕ Reject</button>`;
        act.querySelector(".t-approve").onclick = async () => {
          await api(`/api/tasks/${t.id}/approve`, { method: "POST" }); loadTasks();
        };
        act.querySelector(".t-reject").onclick = async () => {
          await api(`/api/tasks/${t.id}/reject`, { method: "POST" }); loadTasks();
        };
        el.appendChild(act);
      }
      list.appendChild(el);
    });
  } catch (e) {}
}

/* ---------- missions ---------- */

state.missionId = null;
async function loadMissions() {
  loadTasks();
  const rows = await api("/api/missions");
  const list = $("#mission-list");
  list.innerHTML = rows.length ? "" : '<p class="dim">No missions yet. Ask in chat: "implement JIRA-123 in booking" — or use + New mission.</p>';
  rows.forEach((m) => {
    const el = document.createElement("div");
    el.className = "mission" + (m.id === state.missionId ? " active" : "");
    el.innerHTML = `<div class="m-title"><span class="badge ${esc(m.status)}">${esc(m.status.replace("_", " "))}</span>${esc(m.title)}</div>
      <div class="m-sub">#${m.id} · ${esc(m.workspace)}/${esc(m.repo || "-")} · ${esc(m.executor)}${m.jira_key ? " · " + esc(m.jira_key) : ""}</div>`;
    el.onclick = () => openMission(m.id);
    list.appendChild(el);
  });
}

async function openMission(id) {
  state.missionId = id;
  const m = await api("/api/missions/" + id);
  const d = $("#mission-detail");
  let html = `<h2><span class="badge ${esc(m.status)}">${esc(m.status.replace("_", " "))}</span> ${esc(m.title)}</h2>
    <p class="dim">#${m.id} · ${esc(m.workspace)}/${esc(m.repo || "-")} · executor: ${esc(m.executor)}${m.jira_key ? " · " + esc(m.jira_key) : ""}</p>`;
  if (m.error) html += `<p style="color:var(--err)">⚠ ${esc(m.error)}</p>`;
  if (m.status === "awaiting_approval")
    html += `<div class="m-actions"><button class="m-approve">✓ Approve & implement</button><button class="m-reject">✕ Reject</button></div>`;
  if (m.plan) html += `<h3 class="dim">PLAN</h3><pre>${esc(m.plan)}</pre>`;
  if (m.log_tail) html += `<h3 class="dim">LOG</h3><pre>${esc(m.log_tail)}</pre>`;
  d.innerHTML = html;
  const ap = d.querySelector(".m-approve");
  if (ap) ap.onclick = async () => { await api(`/api/missions/${id}/approve`, { method: "POST" }); refreshMissions(); };
  const rj = d.querySelector(".m-reject");
  if (rj) rj.onclick = async () => { await api(`/api/missions/${id}/reject`, { method: "POST" }); refreshMissions(); };
  loadMissions();
}

function refreshMissions() { loadMissions(); if (state.missionId) openMission(state.missionId); }

$("#mission-new").onclick = async () => {
  const title = prompt("Mission title (or Jira key + short description):");
  if (!title) return;
  const jira = prompt("Jira key (optional, e.g. ABC-123):") || "";
  const ws = prompt("Workspace:", $("#workspace-pick").value || "") || "";
  if (!ws) { alert("Pick a workspace first (Settings → Workspaces)."); return; }
  const repo = prompt("Repo/service dir (optional):") || "";
  await api("/api/missions", { method: "POST", body: JSON.stringify({ title, jira_key: jira, workspace: ws, repo }) });
  loadMissions();
};

setInterval(() => {
  loadNotifs();
  if ($("#pane-missions").classList.contains("active")) refreshMissions();
}, 8000);

/* ---------- sidebar (mobile) ---------- */

$("#burger").onclick = (e) => { e.stopPropagation(); $("#sidebar").classList.toggle("open"); };
function closeSidebar() { $("#sidebar").classList.remove("open"); }
document.addEventListener("click", (e) => {
  const sb = $("#sidebar");
  if (sb.classList.contains("open") && !sb.contains(e.target)) closeSidebar();
});

/* ---------- boot ---------- */

async function boot() {
  try {
    await loadStatus();
  } catch (e) { return; } // 401 -> login shown
  loadConversations();
  newChat();
  renderTts();
  renderWake();
  loadVoicebox();
  loadNotifs();
  state.ws = wsConnect();
  setInterval(loadStatus, 60000);
  resumeWake();
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/ui/sw.js").catch(() => {});
boot();


/* ---------- workspaces ---------- */

async function loadWorkspaces() {
  const el = $("#ws-list");
  try {
    const spaces = await api("/api/workspaces");
    const names = Object.keys(spaces);
    if (!names.length) { el.innerHTML = "<p class='dim'>None yet — add one below.</p>"; return; }
    el.innerHTML = names.map((n) => {
      const w = spaces[n];
      const bits = [
        w.provider_label || w.provider,
        (w.repos ?? 0) + " repo" + (w.repos === 1 ? "" : "s"),
      ];
      if (w.jira_projects && w.jira_projects.length) bits.push("jira: " + w.jira_projects.join(", "));
      const warn = w.exists ? "" : " <span class='off'>path missing</span>";
      const note = w.note ? `<div class="dim">${esc(w.note)}</div>` : "";
      return `<div class="m-item">
        <div><b>${esc(n)}</b>${warn} <button class="ws-del" data-ws="${esc(n)}">remove</button></div>
        <div class="m-sub">${esc(w.root)}</div>
        <div class="m-sub">${esc(bits.join(" · "))}</div>${note}</div>`;
    }).join("");
    el.querySelectorAll(".ws-del").forEach((b) => (b.onclick = () => removeWorkspace(b.dataset.ws)));
  } catch (e) {
    el.textContent = "Could not load workspaces.";
  }
}

async function removeWorkspace(name) {
  if (!confirm(`Remove workspace "${name}"? Your files and project context are not touched.`)) return;
  await api("/api/workspaces/" + encodeURIComponent(name), { method: "DELETE" });
  await loadWorkspaces();
  await loadStatus();
}

$("#ws-detect").onclick = async () => {
  const path = $("#ws-path").value.trim();
  const msg = $("#ws-detect-msg");
  if (!path) { msg.textContent = "Enter a folder path."; return; }
  msg.textContent = "detecting…";
  try {
    const info = await api("/api/workspaces/detect", {
      method: "POST", body: JSON.stringify({ path }),
    });
    msg.textContent = `${info.provider_label} · ${info.repos.length} repo(s)`;
    $("#ws-repos").innerHTML = info.repos.length
      ? info.repos.map((r) =>
          `<label class="ws-repo"><input type="checkbox" value="${esc(r)}" checked> ${esc(r)}</label>`
        ).join("")
      : "<p class='dim'>No repos found directly under that folder.</p>";
    $("#ws-name").value = path.replace(/\/+$/, "").split("/").pop().toLowerCase().replace(/[^a-z0-9_-]/g, "-");
    $("#ws-found").classList.remove("hidden");
    $("#ws-found").dataset.root = info.root;
  } catch (e) {
    msg.textContent = String(e.message || e);
    $("#ws-found").classList.add("hidden");
  }
};

$("#ws-add").onclick = async () => {
  const msg = $("#ws-add-msg");
  const checked = [...$("#ws-repos").querySelectorAll("input:checked")].map((i) => i.value);
  const all = [...$("#ws-repos").querySelectorAll("input")].map((i) => i.value);
  const body = {
    name: $("#ws-name").value.trim(),
    root: $("#ws-found").dataset.root,
    // Everything selected == no restriction, so new repos appear automatically.
    repos: checked.length === all.length ? [] : checked,
    jira_projects: $("#ws-jira").value.split(",").map((x) => x.trim()).filter(Boolean),
  };
  if (!body.name) { msg.textContent = "Name required."; return; }
  msg.textContent = "adding…";
  try {
    await api("/api/workspaces", { method: "POST", body: JSON.stringify(body) });
    msg.textContent = "added ✓";
    $("#ws-found").classList.add("hidden");
    $("#ws-path").value = "";
    await loadWorkspaces();
    await loadStatus();
  } catch (e) {
    msg.textContent = String(e.message || e);
  }
};
