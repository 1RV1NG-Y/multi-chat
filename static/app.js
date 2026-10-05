"use strict";

const NAME = { user: "You", claude: "Claude", codex: "Codex", system: "System" };
const OTHER = { claude: "codex", codex: "claude" };
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  chats: [],
  chat: null,
  status: {},
  es: null,
  target: "both",
  forward: null, // { id, from, to }
  uploads: [], // files added to the composer: { key, file, url, att (server meta, once uploaded), error }
};

// ---------------------------------------------------------------- api

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function toast(err) {
  alert(err.message || String(err));
}

// ---------------------------------------------------------------- markdown

function md(text) {
  if (window.marked && window.DOMPurify) {
    return DOMPurify.sanitize(marked.parse(text, { breaks: false, gfm: true }));
  }
  const div = document.createElement("div");
  div.textContent = text;
  return div.innerHTML;
}

// ---------------------------------------------------------------- sidebar

async function loadChats() {
  state.chats = await api("GET", "/api/chats");
  renderSidebar();
}

function renderSidebar() {
  const ul = $("#chat-list");
  ul.innerHTML = "";
  for (const c of state.chats) {
    const li = document.createElement("li");
    li.textContent = c.title;
    li.title = c.title;
    li.classList.toggle("on", state.chat?.id === c.id);
    li.onclick = () => {
      openChat(c.id);
      document.body.classList.remove("side-open");
    };
    ul.append(li);
  }
}

async function newChat() {
  const chat = await api("POST", "/api/chats", {});
  await loadChats();
  openChat(chat.id);
}

// ---------------------------------------------------------------- chat lifecycle

function openChat(id) {
  if (state.es) state.es.close();
  state.forward = null;
  if (state.chat) for (const up of [...state.uploads]) removeUpload(up); // unsent files of the chat we're leaving
  clearUploads();
  location.hash = id;
  const es = new EventSource(`/api/chats/${id}/events`);
  es.onmessage = (e) => onEvent(JSON.parse(e.data));
  es.onerror = () => {
    // EventSource reconnects on its own; the server re-sends a snapshot.
  };
  state.es = es;
}

function onEvent(ev) {
  if (ev.type === "snapshot") {
    state.chat = ev.chat;
    state.status = ev.status;
    renderAll();
  } else if (ev.type === "message") {
    const msgs = state.chat.messages;
    const i = msgs.findIndex((m) => m.id === ev.message.id);
    if (i >= 0) msgs[i] = ev.message;
    else msgs.push(ev.message);
    upsertMessage(ev.message);
  } else if (ev.type === "status") {
    state.status[ev.agent] = { state: ev.state, activity: ev.activity };
    renderStatus();
    refreshTyping(ev.agent);
  } else if (ev.type === "chat") {
    Object.assign(state.chat, ev.chat);
    const c = state.chats.find((c) => c.id === ev.chat.id);
    if (c && c.title !== ev.chat.title) {
      c.title = ev.chat.title;
      renderSidebar();
    }
    if (document.activeElement !== $("#title")) $("#title").value = state.chat.title;
    renderShare();
    renderStatus();
    refreshSeen();
  }
}

function renderShare() {
  for (const b of $$("#share button")) b.classList.toggle("on", b.dataset.share === state.chat.settings.share);
}

async function setShare(share) {
  if (share === state.chat.settings.share) return;
  try {
    await api("PATCH", `/api/chats/${state.chat.id}`, { settings: { share } });
  } catch (err) {
    toast(err);
  }
}

function renderAll() {
  const has = !!state.chat;
  $("#header").hidden = !has;
  $("#composer").hidden = !has;
  $("#thread").hidden = !has;
  $("#empty").hidden = has;
  if (!has) return;
  $("#title").value = state.chat.title;
  renderSidebar();
  renderShare();
  renderStatus();
  renderComposer();
  const thread = $("#thread");
  thread.innerHTML = "";
  for (const m of state.chat.messages) upsertMessage(m, false);
  thread.scrollTop = thread.scrollHeight;
  $("#input").focus();
}

// ---------------------------------------------------------------- status

function renderStatus() {
  const box = $("#statuses");
  box.innerHTML = "";
  let busy = false;
  for (const a of ["claude", "codex"]) {
    const st = state.status[a] || { state: "idle" };
    const on = st.state !== "idle";
    busy ||= on;
    const chip = document.createElement("span");
    chip.className = `chip ${a}${on ? " busy" : ""}`;
    const label = st.state === "queued" ? "queued" : on ? st.activity || "working…" : "idle";
    chip.innerHTML = `<span class="dot"></span><b></b> <span></span>`;
    chip.querySelector("b").textContent = NAME[a];
    chip.querySelector("span:last-child").textContent = label;
    chip.title = `${label}\n${agentSetup(a)}`;
    box.append(chip);
  }
  const pending = state.chat?.messages.some((m) => m.status === "pending" || m.status === "streaming");
  $("#stop").disabled = !(busy || pending);
}

const TOOLS = { none: "talk only", read: "read-only tools", write: "can edit files" };

function agentSetup(a) {
  const s = state.chat?.settings;
  if (!s) return "";
  const info = state.models?.[a];
  const id = s.agents[a].model || info?.default.model;
  const model = (id && (info?.models.find((m) => m.id === id)?.name || id)) || "CLI default model";
  const effort = s.agents[a].effort || (s.agents[a].model ? "" : info?.default.effort);
  return [model + (s.agents[a].model ? "" : " (default)"), effort && `${effort} effort`, TOOLS[s.tools]]
    .filter(Boolean)
    .join(" · ");
}

// ---------------------------------------------------------------- messages

function nearBottom() {
  const t = $("#thread");
  return t.scrollHeight - t.scrollTop - t.clientHeight < 120;
}

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function upsertMessage(m, scroll = true) {
  const thread = $("#thread");
  const stick = scroll && nearBottom();
  let el = thread.querySelector(`[data-id="${m.id}"]`);
  const fresh = buildMessage(m);
  if (el) {
    el.replaceWith(fresh);
  } else if (m.group) {
    let g = thread.querySelector(`.group[data-group="${m.group}"]`);
    if (!g) {
      g = document.createElement("div");
      g.className = "group";
      g.dataset.group = m.group;
      thread.append(g);
    }
    g.append(fresh);
  } else {
    thread.append(fresh);
  }
  if (stick) thread.scrollTop = thread.scrollHeight;
  renderStatus();
}

function buildMessage(m) {
  if (m.author === "system") {
    return eventLine(m, "system", m.text);
  }
  if (m.author === "user" && m.kind !== "msg") {
    if (m.kind === "forward") {
      const src = findMsg(m.of);
      const where = src?.debate ? ` (${debateLabel(src)})` : "";
      return eventLine(m, "", `You forwarded ${NAME[m.of_author] || "a"}'s reply${where} to ${NAME[m.to[0]]}`, m.text);
    }
    if (m.kind === "debate") {
      return eventLine(m, "", `Debate: ${debateSummary(m.rounds, m.first)}`, m.text);
    }
    if (m.kind === "synthesize") {
      return eventLine(m, "", `You asked ${NAME[m.to[0]]} to synthesize the discussion`);
    }
  }

  const el = document.createElement("div");
  el.className = `msg ${m.author}${m.synthesis ? " synthesis" : ""}`;
  el.dataset.id = m.id;

  const head = document.createElement("div");
  head.className = "msg-head";
  const who = document.createElement("span");
  who.className = "who";
  who.textContent = NAME[m.author] || m.author;
  head.append(who);
  const tags = [];
  if (m.author === "user" && m.to && m.to.length === 1) tags.push(`→ ${NAME[m.to[0]]}`);
  if (m.reply_to && !m.debate) tags.push(`on ${NAME[findMsg(m.reply_to)?.author] || "?"}'s reply`);
  if (m.debate) tags.push(debateLabel(m));
  if (m.synthesis) tags.push("synthesis");
  if (m.status === "stopped") tags.push("stopped");
  for (const t of tags) {
    const s = document.createElement("span");
    s.className = "tag";
    s.textContent = t;
    head.append(s);
  }
  if (OTHER[m.author]) {
    const seen = document.createElement("span");
    seen.className = "seen";
    head.append(seen);
    updateSeen(seen, m);
  }
  const time = document.createElement("span");
  time.className = "time";
  time.textContent = fmtTime(m.ts);
  head.append(time);
  el.append(head);

  const body = document.createElement("div");
  body.className = "msg-body";
  if (m.text) {
    body.innerHTML = md(m.text);
    if (m.status === "streaming") body.lastElementChild?.classList.add("cursor");
  }
  el.append(body);
  if (m.attachments?.length) el.append(attachmentsBlock(m));

  if ((m.status === "pending" || m.status === "streaming") && !m.text) {
    el.append(typing(m.author));
  }
  for (const [key, cls, label] of [["error", "error-box", "Error"], ["warning", "warning-box", "Warning"]]) {
    if (!m[key]) continue;
    const box = document.createElement("div");
    box.className = cls;
    box.textContent = `${label} from ${NAME[m.author]}: ${m[key]}`;
    el.append(box);
  }

  if (OTHER[m.author] && m.status !== "pending" && m.status !== "streaming") {
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    const other = OTHER[m.author];
    if (m.status === "error" || m.status === "stopped") {
      actions.append(button("Retry", "retry", () => retry(m), "Run this turn again"));
    }
    if (m.text) {
      actions.append(
        button(`→ ${NAME[other]}`, `fwd-${other}`, () => startForward(m, other), `Ask ${NAME[other]} for its take on this`),
        button("Copy", "", (e) => {
          navigator.clipboard.writeText(m.text);
          e.target.textContent = "Copied";
          setTimeout(() => (e.target.textContent = "Copy"), 1200);
        }),
      );
    }
    actions.append(button("What was sent", "", () => showPrompt(m), `The exact prompt ${NAME[m.author]} received for this reply`));
    el.append(actions);
  } else if (m.author === "user" && m.text) {
    const actions = document.createElement("div");
    actions.className = "msg-actions";
    actions.append(button("Copy", "", () => navigator.clipboard.writeText(m.text)), button("Edit & resend", "", () => {
      $("#input").value = m.text;
      autosize();
      $("#input").focus();
    }));
    el.append(actions);
  }
  return el;
}

function debateLabel(m) {
  const [step, total] = m.debate.split("/");
  return `debate ${step}/${total}${step === total ? " · final" : ""}`;
}

function debateSummary(rounds, first) {
  rounds = Math.max(1, Math.min(10, +rounds || 1));
  return `${rounds} round${rounds > 1 ? "s" : ""} · ${rounds * 2} replies · ${NAME[first]} starts`;
}

function updateSeen(span, m) {
  const other = OTHER[m.author];
  const seen = state.chat.agents?.[other]?.delivered?.includes(m.id);
  span.textContent = m.status === "pending" || m.status === "streaming" || !m.text ? "" : seen ? `seen by ${NAME[other]}` : `not shared with ${NAME[other]}`;
  span.classList.toggle("unseen", !seen);
}

function refreshSeen() {
  for (const span of $$("#thread .seen")) {
    const m = findMsg(span.closest("[data-id]").dataset.id);
    if (m) updateSeen(span, m);
  }
}

async function retry(m) {
  try {
    await api("POST", `/api/chats/${state.chat.id}/messages/${m.id}/retry`);
  } catch (err) {
    toast(err);
  }
}

async function showPrompt(m) {
  const dlg = $("#prompt-view");
  $("#prompt-title").textContent = `What ${NAME[m.author]} was sent`;
  $("#prompt-cmd").textContent = "";
  $("#prompt-native").hidden = true;
  $("#prompt-text").textContent = "Loading…";
  dlg.showModal();
  try {
    const sent = await api("GET", `/api/chats/${state.chat.id}/messages/${m.id}/prompt`);
    $("#prompt-cmd").textContent = sent.command.map((a) => (/[\s"']/.test(a) || a === "" ? JSON.stringify(a) : a)).join(" ");
    if (sent.native?.length) {
      $("#prompt-native").hidden = false;
      $("#prompt-native").textContent = `Also attached directly (as images/PDFs, not text): ${sent.native.join(", ")}`;
    }
    $("#prompt-text").textContent = sent.prompt;
  } catch (err) {
    $("#prompt-text").textContent = err.message;
  }
}

function eventLine(m, cls, text, note) {
  const el = document.createElement("div");
  el.className = `event ${cls}`;
  el.dataset.id = m.id;
  el.textContent = text;
  if (note) {
    const n = document.createElement("span");
    n.className = "note";
    n.textContent = `“${note}”`;
    el.append(n);
  }
  return el;
}

function typing(agent) {
  const t = document.createElement("div");
  t.className = "typing";
  t.dataset.agent = agent;
  t.innerHTML = "<i></i><i></i><i></i><span></span>";
  const st = state.status[agent];
  t.querySelector("span").textContent =
    st?.state === "queued" ? "waiting for its previous turn…" : st?.state === "running" ? st.activity : "starting…";
  return t;
}

function refreshTyping(agent) {
  for (const t of $$(`.typing[data-agent="${agent}"]`)) t.replaceWith(typing(agent));
}

function button(label, cls, onclick, title) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  if (cls) b.className = cls;
  if (title) b.title = title;
  b.onclick = onclick;
  return b;
}

function findMsg(id) {
  return state.chat?.messages.find((m) => m.id === id);
}

// ---------------------------------------------------------------- attachments

const KIND_ICON = { image: "🖼", pdf: "📄", docx: "📝", text: "📃", other: "📦" };

function attUrl(id) {
  return `/api/chats/${state.chat.id}/attachments/${id}`;
}

function fmtSize(n) {
  return n < 1024 ? `${n} B` : n < 2 ** 20 ? `${Math.round(n / 1024)} KB` : `${(n / 2 ** 20).toFixed(1)} MB`;
}

function addFiles(files) {
  if (!state.chat || state.forward) return;
  for (let file of files) {
    if (file.name === "image.png" || !file.name) {
      // Pasted screenshots all arrive as "image.png"; give them distinguishable names.
      const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
      file = new File([file], `pasted-${stamp}.${(file.type.split("/")[1] || "png").replace("jpeg", "jpg")}`, { type: file.type });
    }
    const up = { key: Math.random().toString(36).slice(2), file, url: file.type.startsWith("image/") ? URL.createObjectURL(file) : null };
    state.uploads.push(up);
    fetch(`/api/chats/${state.chat.id}/attachments`, {
      method: "POST",
      headers: { "Content-Type": file.type || "application/octet-stream", "X-Filename": encodeURIComponent(file.name) },
      body: file,
    })
      .then(async (r) => {
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || r.statusText);
        up.att = data;
        (state.chat.attachments ||= {})[data.id] = data;
      })
      .catch((err) => (up.error = err.message))
      .finally(renderUploads);
  }
  renderUploads();
}

function removeUpload(up) {
  state.uploads = state.uploads.filter((u) => u !== up);
  if (up.url) URL.revokeObjectURL(up.url);
  if (up.att) {
    delete state.chat.attachments?.[up.att.id];
    api("DELETE", `/api/chats/${state.chat.id}/attachments/${up.att.id}`).catch(() => {});
  }
  renderUploads();
}

function clearUploads() {
  for (const up of state.uploads) if (up.url) URL.revokeObjectURL(up.url);
  state.uploads = [];
  renderUploads();
}

function renderUploads() {
  const box = $("#pending-files");
  box.hidden = !state.uploads.length;
  box.innerHTML = "";
  for (const up of state.uploads) {
    const chip = document.createElement("div");
    chip.className = `file-chip${up.error ? " failed" : up.att ? "" : " uploading"}`;
    if (up.url) {
      const img = document.createElement("img");
      img.src = up.url;
      img.alt = "";
      chip.append(img);
    } else {
      const icon = document.createElement("span");
      icon.className = "file-icon";
      icon.textContent = KIND_ICON[up.att?.kind] || "📄";
      chip.append(icon);
    }
    const label = document.createElement("span");
    label.className = "file-label";
    label.textContent = up.file.name;
    const meta = document.createElement("small");
    meta.textContent = up.error ? up.error : up.att ? fmtSize(up.file.size) : "uploading…";
    label.append(meta);
    chip.append(label, button("✕", "file-remove", () => removeUpload(up), "Remove"));
    box.append(chip);
  }
}

function attachmentsBlock(m) {
  const box = document.createElement("div");
  box.className = "atts";
  for (const id of m.attachments) {
    const att = state.chat.attachments?.[id];
    if (!att) continue;
    const a = document.createElement("a");
    a.href = attUrl(id);
    a.target = "_blank";
    a.rel = "noopener";
    a.title = `${att.name} · ${fmtSize(att.size)}`;
    if (att.kind === "image") {
      a.className = "att-image";
      const img = document.createElement("img");
      img.src = a.href;
      img.alt = att.name;
      img.loading = "lazy";
      a.append(img);
    } else {
      a.className = "att-file";
      a.textContent = `${KIND_ICON[att.kind] || "📄"} ${att.name}`;
      const s = document.createElement("small");
      s.textContent = fmtSize(att.size);
      a.append(s);
    }
    box.append(a);
  }
  return box;
}

// ---------------------------------------------------------------- composer

function renderComposer() {
  const f = state.forward;
  $("#fwd-banner").hidden = !f;
  $("#targets").classList.toggle("disabled", !!f);
  $("#attach").disabled = !!f;
  $("#pending-files").classList.toggle("disabled", !!f);
  for (const b of $$("#targets button")) b.classList.toggle("on", b.dataset.t === state.target);
  if (f) {
    $("#fwd-banner").className = `to-${f.to}`;
    $("#fwd-text").textContent = `Forwarding ${NAME[f.from]}'s reply → ${NAME[f.to]}. Add a note (optional), then send.`;
    $("#input").placeholder = `Optional note for ${NAME[f.to]}, e.g. "do you agree with point 2?"`;
    $("#send").textContent = "Forward";
  } else {
    const who = state.target === "both" ? "both" : NAME[state.target];
    $("#input").placeholder = `Message ${who}…  (@claude / @codex to address one)`;
    $("#send").textContent = "Send";
  }
}

function startForward(m, to) {
  state.forward = { id: m.id, from: m.author, to };
  renderComposer();
  $("#input").focus();
}

function autosize() {
  const i = $("#input");
  i.style.height = "auto";
  i.style.height = Math.min(i.scrollHeight, window.innerHeight * 0.4) + "px";
}

async function submit(e) {
  e?.preventDefault();
  const input = $("#input");
  const text = input.value;
  const cid = state.chat.id;
  try {
    if (state.forward) {
      await api("POST", `/api/chats/${cid}/forward`, { message_id: state.forward.id, to: state.forward.to, note: text });
      state.forward = null;
      renderComposer();
    } else {
      if (state.uploads.some((u) => !u.att && !u.error)) return toast(new Error("Files are still uploading."));
      const attachments = state.uploads.filter((u) => u.att).map((u) => u.att.id);
      if (!text.trim() && !attachments.length) return;
      const to = state.target === "both" ? ["claude", "codex"] : [state.target];
      await api("POST", `/api/chats/${cid}/send`, { text, to, attachments });
      clearUploads();
    }
    input.value = "";
    autosize();
    $("#thread").scrollTop = $("#thread").scrollHeight;
  } catch (err) {
    toast(err);
  }
}

// ---------------------------------------------------------------- settings

function openSettings() {
  const c = state.chat;
  const f = $("#settings-form");
  f.share.value = c.settings.share;
  f.tools.value = c.settings.tools;
  f.workdir.value = c.workdir;
  const locked = Object.values(sessionsOf(c)).some(Boolean);
  f.workdir.disabled = locked;
  $("#workdir-hint").textContent = locked
    ? "Locked: the agents' sessions live in this directory."
    : "Point this at a project to give them context. Locked after the first reply.";
  for (const fs of $$("fieldset[data-agent]", f)) {
    const a = c.settings.agents[fs.dataset.agent];
    fillModels(fs, a.model || "", a.effort || "");
    $("[name=persona]", fs).value = a.persona || "";
  }
  $("#settings").showModal();
}

// ---------------------------------------------------------------- model pickers

const CUSTOM = "__custom";

function option(value, label, title) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = label;
  if (title) o.title = title;
  return o;
}

function modelInfo(agent, id) {
  return state.models?.[agent]?.models.find((m) => m.id === id);
}

function fillModels(fs, model, effort) {
  const agent = fs.dataset.agent;
  const info = state.models?.[agent] || { default: {}, models: [] };
  const sel = $("[name=model]", fs);
  sel.innerHTML = "";
  const def = info.default.model ? `Default (${modelInfo(agent, info.default.model)?.name || info.default.model})` : "Default";
  sel.append(option("", def, "Whatever the CLI uses on its own"));
  for (const m of info.models) sel.append(option(m.id, m.name, m.description));
  const known = !model || info.models.some((m) => m.id === model);
  sel.append(option(CUSTOM, "Other…"));
  sel.value = known ? model : CUSTOM;
  $("[name=model-custom]", fs).value = known ? "" : model;
  sel.onchange = () => syncModel(fs);
  syncModel(fs, effort);
}

function syncModel(fs, effort) {
  const agent = fs.dataset.agent;
  const info = state.models?.[agent] || { default: {}, models: [] };
  const sel = $("[name=model]", fs);
  const custom = $("[name=model-custom]", fs);
  custom.hidden = sel.value !== CUSTOM;
  if (!custom.hidden && document.activeElement === sel) custom.focus();

  const m = modelInfo(agent, sel.value === "" ? info.default.model : sel.value);
  $(".model-desc", fs).textContent = m?.description || "";

  const eff = $("[name=effort]", fs);
  const keep = effort ?? eff.value;
  const levels = m?.efforts?.length ? m.efforts : info.models[0]?.efforts || [];
  const defEffort = sel.value === "" ? info.default.effort || m?.default_effort : m?.default_effort;
  eff.innerHTML = "";
  eff.append(option("", defEffort ? `Default (${defEffort})` : "Default"));
  for (const l of levels) eff.append(option(l, l));
  if (keep && !levels.includes(keep)) eff.append(option(keep, `${keep} (not listed for this model)`));
  eff.value = keep || "";
}

function pickedModel(fs) {
  const v = $("[name=model]", fs).value;
  return v === CUSTOM ? $("[name=model-custom]", fs).value.trim() : v;
}

function sessionsOf(c) {
  return Object.fromEntries(Object.entries(c.agents || {}).map(([a, s]) => [a, s.session]));
}

async function saveSettings() {
  const f = $("#settings-form");
  const agents = {};
  for (const fs of $$("fieldset[data-agent]", f)) {
    agents[fs.dataset.agent] = {
      model: pickedModel(fs),
      effort: $("[name=effort]", fs).value,
      persona: $("[name=persona]", fs).value,
    };
  }
  const body = { settings: { share: f.share.value, tools: f.tools.value, agents } };
  if (!f.workdir.disabled && f.workdir.value !== state.chat.workdir) body.workdir = f.workdir.value;
  try {
    await api("PATCH", `/api/chats/${state.chat.id}`, body);
  } catch (err) {
    toast(err);
  }
}

// ---------------------------------------------------------------- wiring

function closePops(except) {
  for (const p of $$(".pop")) if (p !== except) p.hidden = true;
}

function init() {
  $("#new-chat").onclick = () => newChat().catch(toast);
  $("#empty-new").onclick = () => newChat().catch(toast);
  $("#toggle-side").onclick = () => document.body.classList.toggle("side-open");
  $("#composer").onsubmit = submit;
  $("#fwd-cancel").onclick = () => {
    state.forward = null;
    renderComposer();
  };

  const input = $("#input");
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submit(e);
    if (e.key === "Escape" && state.forward) $("#fwd-cancel").click();
  });

  for (const b of $$("#targets button")) {
    b.onclick = () => {
      state.target = b.dataset.t;
      renderComposer();
      input.focus();
    };
  }

  $("#title").addEventListener("change", (e) =>
    api("PATCH", `/api/chats/${state.chat.id}`, { title: e.target.value }).then(loadChats).catch(toast),
  );
  $("#title").addEventListener("keydown", (e) => e.key === "Enter" && e.target.blur());

  for (const b of $$("[data-pop]")) {
    b.onclick = (e) => {
      e.stopPropagation();
      const pop = $("#" + b.dataset.pop);
      closePops(pop);
      pop.hidden = !pop.hidden;
    };
  }
  for (const p of $$(".pop")) p.onclick = (e) => e.stopPropagation();
  document.addEventListener("click", () => closePops());

  const debateSum = () => ($("#debate-summary").textContent = debateSummary($("#debate-rounds").value, $("#debate-first").value));
  $("#debate-rounds").oninput = debateSum;
  $("#debate-first").onchange = debateSum;
  debateSum();
  $("#debate-go").onclick = () => {
    closePops();
    api("POST", `/api/chats/${state.chat.id}/debate`, {
      rounds: +$("#debate-rounds").value || 2,
      first: $("#debate-first").value,
      topic: $("#debate-topic").value,
    })
      .then(() => ($("#debate-topic").value = ""))
      .catch(toast);
  };
  for (const b of $$("#share button")) b.onclick = () => setShare(b.dataset.share);

  // Attachments: paperclip, paste (e.g. screenshots), drag & drop anywhere on the page.
  $("#attach").onclick = () => $("#file-input").click();
  $("#file-input").onchange = (e) => {
    addFiles([...e.target.files]);
    e.target.value = "";
  };
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();
      addFiles(files);
    }
  });
  let dragDepth = 0;
  const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  document.addEventListener("dragenter", (e) => {
    if (!hasFiles(e) || !state.chat || state.forward) return;
    dragDepth++;
    $("#drop-overlay").hidden = false;
  });
  document.addEventListener("dragleave", (e) => {
    if (hasFiles(e) && --dragDepth <= 0) {
      dragDepth = 0;
      $("#drop-overlay").hidden = true;
    }
  });
  document.addEventListener("dragover", (e) => hasFiles(e) && e.preventDefault());
  document.addEventListener("drop", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    dragDepth = 0;
    $("#drop-overlay").hidden = true;
    addFiles([...e.dataTransfer.files]);
  });
  $("#prompt-close").onclick = () => $("#prompt-view").close();
  for (const b of $$("[data-synth]")) {
    b.onclick = () => {
      closePops();
      api("POST", `/api/chats/${state.chat.id}/synthesize`, { by: b.dataset.synth }).catch(toast);
    };
  }
  $("#stop").onclick = () => api("POST", `/api/chats/${state.chat.id}/stop`).catch(toast);

  $("#open-settings").onclick = openSettings;
  $("#settings").addEventListener("close", () => {
    if ($("#settings").returnValue === "save") saveSettings();
  });
  $("#delete-chat").onclick = async () => {
    if (!confirm(`Delete “${state.chat.title}”? This can't be undone.`)) return;
    $("#settings").close();
    await api("DELETE", `/api/chats/${state.chat.id}`).catch(toast);
    state.es?.close();
    state.chat = null;
    location.hash = "";
    await loadChats();
    renderAll();
  };

  api("GET", "/api/models").then((m) => (state.models = m)).catch(() => {});
  api("GET", "/api/info").then((info) => {
    $("#bins").textContent = ["claude", "codex"]
      .map((a) => `${NAME[a]}: ${info.versions[a] || "not working — " + info.bins[a]}`)
      .join("  ·  ");
  });

  loadChats().then(() => {
    const id = location.hash.slice(1);
    if (id && state.chats.some((c) => c.id === id)) openChat(id);
    else renderAll();
  });
}

init();
