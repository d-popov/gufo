'use strict';

const OLD_STORAGE_KEY = 'gufo-chat-histories-v1';  // the last 10 chats of the first version, moved to the store
const SCOPE_KEY = 'gufo-chat-project';
const EFFORT_KEY = 'gufo-chat-effort';
const BROWSER_KEY = 'gufo-chat-browser';
const TOOLS_KEY = 'gufo-chat-tools';
const MODEL_KEY = 'gufo-chat-model';
const LIST_MAX = 200;           // chats listed at once
const MEMORY_IDLE_MS = 60_000;  // after a reply, how long before the project memory is updated
const NEW_PROJECT = '__new';
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const $ = (id) => document.getElementById(id);
const fmtUsd = window.GufoHeader.fmtUsd;
// Without projects.js the chat still works, with no project text.
const Projects = window.GufoProjects ?? {
  MEMORY_MAX: 0, GUIDELINES_MAX: 0, context: () => null, unsummarized: () => false,
  systemMessage: (...texts) => {
    const parts = texts.filter((text) => typeof text === 'string' && text.trim());
    return parts.length ? { role: 'system', content: parts.join('\n\n') } : null;
  },
};

// Without language.js the chat works as before, with no language handling.
const Lang = window.GufoLanguage ?? {
  EMPTY: null, track: (state) => state, parseCommand: () => null, declare: () => null, note: () => null, label: () => '',
};
// What speech recognition heard last: the text and its language, handed to send() with that text.
let heard = null;

// The page header first, so it is there whatever fails below.
window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});
let store = null;     // the browser database, once opened (see chatstore.js)
let chats = [];       // every conversation, in every project
let projects = [];
let scope = localStorage.getItem(SCOPE_KEY) || '';  // the project shown, or '' for chats outside any
let activeId = null;
let sync = null;       // keeps these chats and projects in step with the monitor's copy (chatsync.js)
let replyingId = null; // the chat a reply is being written into
let ownerKnown = null; // who this browser is signed in as, from the monitor: {owner, sync}
const ownerReady = new Promise((resolve) => { ownerKnown = resolve; });
let pendingImages = [];
let prices = null;  // {model: {input, cached_input, output}} micro-USD per 1M tokens, or null
let billed = false;  // false for the admin: prices are shown, nothing is charged
let busy = false;
let controller = null;  // aborts the reply being made, and the page fetches it asked for
// The tool scripts are separate files. If one did not load, the chat goes on without tools.
const toolkit = (() => { try { return window.GufoTools.create(); } catch { return null; } })();
let allowedTools = loadAllowedTools();  // what the user ticked in the Tools menu, kept between visits
let pythonInstalled = false;            // set by loadModels: is the Python runtime on the monitor?
let talk = null;                      // the spoken conversation (talk.js), once the Talk button is first used
let talkAvailable = false;            // set by loadModels: has the monitor speech servers, and is this the admin?
let libraryAvailable = false;           // set by loadModels: does this account have a library (the admin, on a monitor that keeps one)?

// ---------------------------------------------------------------- history (this browser, kept in step with the monitor)

function failedToSave() {
  $('status').textContent = 'The browser would not store this change (storage full or blocked).';
}

// Image bytes and the live "thinking" text stay out of storage; the stats stay in.
function plainChat(chat) {
  return {
    ...chat,
    messages: chat.messages.map(({ role, content, hadImages, images, stats, error, steps, steering, language }) => ({
      role, content, hadImages: Boolean(hadImages || images?.length), stats, error, steering, language,
      steps: steps?.map(({ figures, ...rest }) => (figures ? { ...rest, figureCount: figures.length } : rest)),
    })),
  };
}

function saveChat(chat) {
  chat.modified = Date.now();  // what decides between two devices' edits; `updated` only orders the list
  store.put('chats', plainChat(chat)).catch(failedToSave);
  sync?.touch('chat', chat.id);
}
function saveProject(project) {
  project.updated = Date.now();
  store.put('projects', { ...project }).catch(failedToSave);
  sync?.touch('project', project.id);
}

// What stands in for the browser database when chatstore.js did not load.
function volatileStore() {
  const tables = { chats: new Map(), projects: new Map() };
  return {
    persistent: false,
    getAll: async (table) => [...tables[table].values()],
    put: async (table, value) => { tables[table].set(value.id, structuredClone(value)); },
    remove: async (table, id) => { tables[table].delete(id); },
  };
}

// One database per signed-in account, so two people on one browser never see each other's chats.
// The admin's keeps the name the first version used.
function databaseFor(owner) { return owner === 'admin' ? 'gufo-chat' : `gufo-chat-${String(owner).replace(/[^\w-]/g, '_')}`; }

async function loadStore(owner) {
  store = window.GufoStore ? await window.GufoStore.open(databaseFor(owner)) : volatileStore();
  if (!store.persistent && window.GufoStore) $('status').textContent = 'This browser blocks storage: chats last until you close the page.';
  [chats, projects] = await Promise.all([store.getAll('chats'), store.getAll('projects')]);
  // The first version kept 10 chats in localStorage; move them over once.
  if (window.GufoStore && owner === 'admin') chats.push(...await window.GufoStore.migrate(store, localStorage, OLD_STORAGE_KEY, chats));
  chats = chats.filter((chat) => chat && typeof chat.id === 'string' && Array.isArray(chat.messages));
  if (scope && !projects.some((project) => project.id === scope)) scope = '';
}

// ---- sync with the monitor

function applyRemote(kind, id, data) {
  if (kind === 'chat') {
    if (!data || typeof data.id !== 'string' || !Array.isArray(data.messages)) return;
    const at = chats.findIndex((item) => item.id === id);
    if (at >= 0) chats[at] = data; else chats.push(data);
    store.put('chats', data).catch(failedToSave);
  } else {
    if (!data || typeof data.id !== 'string') return;
    const at = projects.findIndex((item) => item.id === id);
    if (at >= 0) projects[at] = data; else projects.push(data);
    store.put('projects', data).catch(failedToSave);
  }
}

function dropRemote(kind, id) {
  if (kind === 'chat') { chats = chats.filter((item) => item.id !== id); store.remove('chats', id).catch(failedToSave); }
  else { projects = projects.filter((item) => item.id !== id); store.remove('projects', id).catch(failedToSave); }
}

// The monitor had newer chats or projects: show them, without disturbing what is being typed or written.
function showSynced(changes) {
  if (scope && !projects.some((project) => project.id === scope)) {
    scope = '';
    localStorage.setItem(SCOPE_KEY, '');
    setProjectOpen(false);
  }
  renderProjectList();
  if (!active()) {
    activeId = scoped()[0]?.id ?? null;
    if (activeId) showChat(activeId); else newChat();
  } else {
    renderHistoryList();
    if (changes.chats.includes(activeId) && !busy) { renderMessages(); renderTotal(); }
  }
  const project = currentProject();
  if (project && changes.projects.includes(project.id) && !$('project-panel').hidden) {
    for (const [id, value] of [['project-name', project.name], ['project-guidelines', project.guidelines], ['project-memory', project.memory]]) {
      if (document.activeElement !== $(id)) $(id).value = value;
    }
  }
}

function startSync(owner) {
  sync = window.GufoSync.create({
    owner, storage: localStorage,
    request: async (method, path, body) => {
      const response = await fetch(path, {
        method, headers: { 'X-Gufo-Portal': '1', ...(body ? { 'Content-Type': 'application/json' } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
      let json = null;
      try { json = await response.json(); } catch { /* no body */ }
      return { status: response.status, json };
    },
    list: () => [...chats.map((item) => ({ kind: 'chat', id: item.id })), ...projects.map((item) => ({ kind: 'project', id: item.id }))],
    read: (kind, id) => {
      if (kind === 'chat') {
        const item = chats.find((chat) => chat.id === id);
        return !item || item.messages.length === 0 ? null : { updated: item.modified ?? item.updated, data: plainChat(item) };
      }
      const item = projects.find((project) => project.id === id);
      return item ? { updated: item.updated, data: { ...item } } : null;
    },
    stamp: (kind, id) => {
      const item = (kind === 'chat' ? chats : projects).find((entry) => entry.id === id);
      return item ? (item.modified ?? item.updated ?? 0) : null;
    },
    apply: applyRemote, drop: dropRemote,
    busy: (kind, id) => kind === 'chat' && id === replyingId,
    onChange: showSynced,
    onError: (message) => { $('status').textContent = message; },
  });
  const run = () => { sync.sync().catch(() => {}); };
  run();
  setInterval(() => { if (!document.hidden) run(); }, 45000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) run(); });
  window.addEventListener('online', run);
}

function active() { return chats.find((item) => item.id === activeId); }
function currentProject() { return projects.find((project) => project.id === scope) ?? null; }
function projectOf(chat) { return projects.find((project) => project.id === chat.projectId) ?? null; }
function scoped() {
  return chats.filter((chat) => (chat.projectId || '') === scope).sort((a, b) => b.updated - a.updated);
}

function updateHistory(current = active()) {
  if (!current) return;
  current.updated = Date.now();
  saveChat(current);
  renderHistoryList();
  renderTotal();
}

function renderHistoryList() {
  const select = $('history');
  select.replaceChildren();
  const list = scoped();
  list.slice(0, LIST_MAX).forEach((item) => {
    const option = document.createElement('option');
    option.value = item.id;
    const day = new Date(item.updated).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    option.textContent = `${item.title || 'New chat'} · ${day}`;
    select.append(option);
  });
  if (list.length > LIST_MAX) select.title = `The ${LIST_MAX} most recent of ${list.length} chats`;
  else select.removeAttribute('title');
  if (activeId) select.value = activeId;
}

// A chat that was opened and never written in is not worth keeping.
function dropIfEmpty(chat) {
  if (!chat || chat.messages.length) return;
  chats = chats.filter((item) => item !== chat);
  store.remove('chats', chat.id).catch(failedToSave);
}

// Called when the open chat is about to be left: its project learns what it said.
function leaveChat() {
  const chat = active();
  clearTimeout(memoryTimer);
  if (!chat) return;
  dropIfEmpty(chat);
  if (!busy) maybeUpdateMemory(chat);
}

function showChat(id) {
  activeId = id;
  pendingImages = [];
  renderPreview();
  renderHistoryList();
  renderMessages();
  renderTotal();
}

function newChat() {
  disarmDelete();
  const current = active();
  // One empty chat is enough.
  if (current && !current.messages.length && (current.projectId || '') === scope) { showChat(current.id); return; }
  leaveChat();
  const item = { id: crypto.randomUUID(), title: 'New chat', updated: Date.now(), messages: [], projectId: scope || null };
  chats.unshift(item);
  showChat(item.id);
  saveChat(item);
}

function openChat(id) {
  if (id === activeId) return;
  leaveChat();
  showChat(id);
}

// Deleting takes two clicks: the first arms the button for three seconds, the second deletes.
let disarmTimer = null;

function disarmDelete() {
  clearTimeout(disarmTimer);
  disarmTimer = null;
  const button = $('delete-chat');
  button.textContent = 'Delete';
  button.classList.remove('danger');
}

// Removes the open chat and opens the most recent one left in the project, or a fresh one.
function deleteChat() {
  const gone = active();
  clearTimeout(memoryTimer);
  chats = chats.filter((item) => item !== gone);
  if (gone) {
    store.remove('chats', gone.id).catch(failedToSave);
    if (gone.messages.length) sync?.forget('chat', gone.id);
  }
  activeId = scoped()[0]?.id ?? null;
  if (!activeId) { newChat(); return; }
  showChat(activeId);
}

// ---------------------------------------------------------------- projects

function renderProjectList() {
  const select = $('project');
  select.replaceChildren();
  const add = (value, text) => {
    const option = document.createElement('option');
    option.value = value;
    option.textContent = text;
    select.append(option);
  };
  add('', 'No project');
  for (const project of [...projects].sort((a, b) => a.name.localeCompare(b.name))) add(project.id, project.name);
  add(NEW_PROJECT, '+ New project…');
  select.value = scope;
  $('edit-project').hidden = !scope;
  $('empty-text').textContent = scope
    ? 'This project’s guidelines and memory go with every message. Chats are kept in this browser.'
    : 'Ask anything. Attach or paste (Ctrl+V) images up to 4 MiB each.\nEnter sends, Shift+Enter adds a line. Chats are kept in this browser.';
}

function setScope(id) {
  leaveChat();
  scope = id;
  localStorage.setItem(SCOPE_KEY, scope);
  setProjectOpen(false);
  renderProjectList();
  activeId = scoped()[0]?.id ?? null;
  if (!activeId) { newChat(); return; }
  showChat(activeId);
}

function createProject() {
  leaveChat();
  const project = {
    id: crypto.randomUUID(), name: 'New project', guidelines: '', memory: '', autoMemory: true,
    memoryUpdated: null, updated: Date.now(),
  };
  projects.push(project);
  saveProject(project);
  setScope(project.id);
  setProjectOpen(true);
  $('project-name').select();
}

function setProjectOpen(open) {
  const project = currentProject();
  const show = open && project !== null;
  $('project-panel').hidden = !show;
  $('edit-project').setAttribute('aria-expanded', String(show));
  if (!show) return;
  $('project-name').value = project.name;
  $('project-guidelines').value = project.guidelines;
  $('project-memory').value = project.memory;
  $('auto-memory').checked = project.autoMemory !== false;
  $('memory-status').textContent = project.memoryUpdated
    ? `Memory updated ${new Date(project.memoryUpdated).toLocaleString()}` : '';
  $('project-guidelines').focus();
}

// Edits to the project's text are saved a moment after the last key.
let projectSaveTimer = null;
function projectEdited() {
  const project = currentProject();
  if (!project) return;
  project.name = $('project-name').value.trim() || 'Untitled project';
  project.guidelines = $('project-guidelines').value;
  project.memory = $('project-memory').value;
  project.autoMemory = $('auto-memory').checked;
  clearTimeout(projectSaveTimer);
  projectSaveTimer = setTimeout(() => { saveProject(project); renderProjectList(); }, 400);
}

let memoryTimer = null;
let memoryJob = null;  // the memory update in progress, if any

// After a reply, the memory is updated once the user has been quiet for a while.
function scheduleMemory(chat) {
  clearTimeout(memoryTimer);
  if (projectOf(chat)?.autoMemory === false) return;
  memoryTimer = setTimeout(() => { if (!busy && chat === active()) maybeUpdateMemory(chat); }, MEMORY_IDLE_MS);
}

function maybeUpdateMemory(chat) {
  const owner = projectOf(chat);
  if (owner && owner.autoMemory !== false && Projects.unsummarized(chat)) updateMemory(chat).catch(() => {});
}

function setMemoryStatus(text) {
  if (!$('project-panel').hidden) $('memory-status').textContent = text;
}

// Asks the model to fold the chat into the project's memory, one update at a time.
function updateMemory(chat) {
  if (memoryJob) return Promise.resolve(false);
  const job = runMemoryUpdate(chat).finally(() => { if (memoryJob === job) memoryJob = null; });
  memoryJob = job;
  return job;
}

// A hand edit made while the model wrote wins over what it wrote.
async function runMemoryUpdate(chat) {
  const owner = projectOf(chat);
  const model = $('model').value;
  if (!owner || !model) return false;
  const before = owner.memory;
  const seen = chat.messages.length;
  setMemoryStatus('Updating memory…');
  try {
    let text = '';
    await streamCompletion({
      model, messages: Projects.memoryRequest(owner, chat), effort: 'none',
      onDelta: (delta) => { text += delta.content || ''; },
    });
    const next = Projects.clean(text, Projects.MEMORY_MAX);
    if (!next) throw new Error('the model sent no memory');
    if (owner.memory !== before) { setMemoryStatus('You edited the memory meanwhile, so it was not replaced.'); return false; }
    owner.memory = next;
    owner.memoryUpdated = Date.now();
    chat.memoryCount = seen;
    saveProject(owner);
    if (chats.includes(chat)) saveChat(chat);
    if (!$('project-panel').hidden && owner.id === scope) $('project-memory').value = next;
    setMemoryStatus(`Memory updated ${new Date(owner.memoryUpdated).toLocaleString()}`);
    return true;
  } catch (error) {
    setMemoryStatus(`Memory not updated: ${error.message}`);
    return false;
  }
}

async function draftGuidelines() {
  const project = currentProject();
  const brief = $('project-brief').value.trim();
  const model = $('model').value;
  if (!project || busy || !model) return;
  if (!brief) { setMemoryStatus('Say what the project is or what rules you want, then Draft.'); return; }
  const box = $('project-guidelines');
  const before = box.value;
  $('draft-guidelines').disabled = true;
  setMemoryStatus('Drafting guidelines…');
  try {
    let text = '';
    await streamCompletion({
      model, messages: Projects.guidelinesRequest({ ...project, guidelines: before }, brief), effort: 'none',
      onDelta: (delta) => { text += delta.content || ''; },
    });
    const next = Projects.clean(text, Projects.GUIDELINES_MAX);
    if (!next) throw new Error('the model sent nothing');
    if (box.value !== before) { setMemoryStatus('You edited the guidelines meanwhile, so the draft was dropped.'); return; }
    box.value = next;
    projectEdited();
    setMemoryStatus('Guidelines drafted: read them, edit what you like.');
  } catch (error) {
    setMemoryStatus(`Draft failed: ${error.message}`);
  } finally { $('draft-guidelines').disabled = false; }
}

function exportProject() {
  const project = currentProject();
  if (!project) return;
  const link = document.createElement('a');
  link.href = URL.createObjectURL(new Blob([Projects.toMarkdown(project)], { type: 'text/markdown' }));
  link.download = `${project.name.replace(/[^\w.-]+/g, '-').replace(/^-+|-+$/g, '') || 'project'}.md`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function importProject(file) {
  const project = currentProject();
  if (!project || !file) return;
  const found = Projects.fromMarkdown(await file.text());
  if (!found) { setMemoryStatus('No “# Guidelines” or “# Memory” heading in that file.'); return; }
  if ('guidelines' in found) $('project-guidelines').value = found.guidelines.slice(0, Projects.GUIDELINES_MAX);
  if ('memory' in found) $('project-memory').value = found.memory.slice(0, Projects.MEMORY_MAX);
  projectEdited();
  setMemoryStatus(`Imported ${Object.keys(found).join(' and ')}.`);
}

// Deleting a project takes two clicks, like deleting a chat. Its chats are kept, outside any project.
let projectDisarmTimer = null;
function disarmProjectDelete() {
  clearTimeout(projectDisarmTimer);
  projectDisarmTimer = null;
  $('delete-project').textContent = 'Delete project';
  $('delete-project').classList.remove('danger');
}

function deleteProject() {
  const project = currentProject();
  if (!project) return;
  clearTimeout(projectSaveTimer);
  clearTimeout(memoryTimer);
  for (const chat of chats.filter((item) => item.projectId === project.id)) {
    chat.projectId = null;
    saveChat(chat);
  }
  projects = projects.filter((item) => item !== project);
  store.remove('projects', project.id).catch(failedToSave);
  sync?.forget('project', project.id);
  scope = '';
  localStorage.setItem(SCOPE_KEY, '');
  setProjectOpen(false);
  renderProjectList();
  const open = active();
  if (open) dropIfEmpty(open);
  activeId = scoped()[0]?.id ?? null;
  if (!activeId) { newChat(); return; }
  showChat(activeId);
}

// ---------------------------------------------------------------- price and speed

// Micro-USD, rounded up, as the server bills it: the whole prompt less its cached part at
// the input price, the cached part and the output at theirs. BigInt keeps it exact.
function costMicro(model, usage) {
  const price = prices?.[model];
  if (!price || !usage) return null;
  const prompt = BigInt(usage.prompt_tokens || 0);
  const cached = BigInt(Math.min(usage.prompt_tokens_details?.cached_tokens ?? usage.cached_tokens ?? 0,
    usage.prompt_tokens || 0));
  const out = BigInt(usage.completion_tokens || 0);
  const scaled = (prompt - cached) * BigInt(price.input) + cached * BigInt(price.cached_input)
    + out * BigInt(price.output);
  return Number((scaled + 999_999n) / 1_000_000n);
}

function describe(stats) {
  const parts = [];
  if (stats.tps) parts.push(`${stats.tps.toFixed(1)} tok/s`);
  if (stats.ttft_ms != null) parts.push(`${(stats.ttft_ms / 1000).toFixed(1)} s to first token`);
  if (stats.completion_tokens != null) {
    const cached = stats.cached_tokens ? ` (${stats.cached_tokens} cached)` : '';
    parts.push(`${stats.prompt_tokens} in${cached} · ${stats.completion_tokens} out`);
  }
  if (stats.calls) parts.push(`${stats.calls} tool ${stats.calls === 1 ? 'call' : 'calls'}`);
  if (stats.cost_micro != null) parts.push(billed ? fmtUsd(stats.cost_micro) : `${fmtUsd(stats.cost_micro)} list price`);
  return parts.join(' · ');
}

// The chat's running total, always in view: what it has cost (with prices), and the tokens
// billed so far. Every turn resends the history, so tokens count the whole prompts.
function renderTotal() {
  const root = $('chat-total');
  root.replaceChildren();
  const answered = (active()?.messages ?? []).filter((message) => message.stats);
  if (!prices && !answered.length) return;
  const tokens = answered.reduce((sum, message) => sum + (message.stats.prompt_tokens || 0)
    + (message.stats.completion_tokens || 0), 0);
  const cost = answered.reduce((sum, message) => sum + (message.stats.cost_micro || 0), 0);
  const part = (tag, text, className) => {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  root.append(part('span', 'Chat total'));
  if (prices) root.append(part('strong', fmtUsd(cost)));
  root.append(part('span', `${tokens.toLocaleString()} tokens`));
  if (prices && !billed) root.append(part('span', 'not charged', 'note'));
}

// ---------------------------------------------------------------- messages

// Pictures and videos a tool found, each linked to its page. Only addresses on the media hosts
// are drawn, whatever a saved chat holds.
function mediaBox(items) {
  const box = document.createElement('div');
  box.className = 'media';
  for (const raw of items) {
    const item = window.GufoWebTools?.safeMedia(raw);
    if (!item) continue;
    const figure = document.createElement('figure');
    if (item.kind === 'video') {
      const video = document.createElement('video');
      video.controls = true;
      video.preload = 'none';
      video.playsInline = true;
      video.poster = item.thumb;
      video.src = item.src;
      figure.className = 'wide';
      figure.append(video);
    } else {
      const link = document.createElement('a');
      link.href = item.page;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const img = document.createElement('img');
      img.src = item.thumb;
      img.alt = item.title;
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      link.append(img);
      figure.append(link);
    }
    const caption = document.createElement('figcaption');
    const title = document.createElement('a');
    title.href = item.page;
    title.target = '_blank';
    title.rel = 'noopener noreferrer';
    title.textContent = item.title || 'Wikimedia Commons';
    caption.append(title, document.createTextNode([item.license, item.author].filter(Boolean).map((part) => ` · ${part}`).join('')));
    figure.append(caption);
    box.append(figure);
  }
  return box;
}

// The figures a tool drew, shown in full with a way to keep each one.
function figureBox(figures) {
  const box = document.createElement('div');
  box.className = 'figures';
  figures.forEach((src, index) => {
    const figure = document.createElement('figure');
    const img = document.createElement('img');
    img.src = src;
    img.alt = `Figure ${index + 1} drawn by Python`;
    const caption = document.createElement('figcaption');
    const save = document.createElement('a');
    save.href = src;
    save.download = `figure-${index + 1}.png`;
    save.textContent = 'Save PNG';
    caption.append(save);
    figure.append(img, caption);
    box.append(figure);
  });
  return box;
}

// Fills one message block: optional thinking, the text, attached images, then its stats.
function fillMessage(block, message) {
  block.replaceChildren();
  if (message.reasoning) {
    const details = document.createElement('details');
    details.className = 'thinking';
    details.open = !message.content;
    const summary = document.createElement('summary');
    summary.textContent = 'Thinking';
    const text = document.createElement('div');
    text.textContent = message.reasoning;
    details.append(summary, text);
    block.append(details);
  }
  for (const step of message.steps ?? []) {
    const details = document.createElement('details');
    details.className = `step ${step.state}`;
    const summary = document.createElement('summary');
    const dot = document.createElement('span');
    dot.className = 'dot';
    const label = document.createElement('span');
    label.textContent = step.label;
    summary.append(dot, label);
    if (step.via === 'reader') {
      const via = document.createElement('span');
      via.className = 'via';
      via.textContent = 'via r.jina.ai';
      summary.append(via);
    }
    details.append(summary);
    if (step.code) {
      const code = document.createElement('pre');
      code.className = 'code';
      code.textContent = step.code;
      details.append(code);
    }
    const result = document.createElement('pre');
    result.textContent = step.excerpt || '';
    details.append(result);
    block.append(details);
    if (step.media?.length) block.append(mediaBox(step.media));
    if (step.figures?.length) {
      block.append(figureBox(step.figures));
    } else if (step.figureCount) {
      const note = document.createElement('div');
      note.className = 'note';
      note.textContent = `${step.figureCount} figure${step.figureCount === 1 ? '' : 's'} drawn; images are not kept after a reload.`;
      block.append(note);
    }
  }
  const text = document.createElement('div');
  const waiting = message.pending && !message.content && !message.reasoning;
  if (waiting) text.className = 'live';
  const shown = waiting ? 'Waiting for the model…'
    : message.content || (message.hadImages ? '[Image attached; image bytes are not retained after reload]' : '');
  // Replies are Markdown; what you typed, an error and the placeholders stay as written.
  if (message.role === 'assistant' && !message.error && !waiting && message.content && window.GufoMarkdown) {
    text.className = 'md';
    text.append(window.GufoMarkdown.render(shown));
  } else {
    text.textContent = shown;
  }
  block.append(text);
  for (const image of message.images ?? []) {
    const img = document.createElement('img');
    img.src = image.data;
    img.alt = image.name;
    block.append(img);
  }
  if (message.role === 'user' && message.language) {
    const tag = document.createElement('div');
    tag.className = 'tag';
    tag.textContent = `Heard as ${message.language[0].toUpperCase()}${message.language.slice(1)}`;
    block.append(tag);
  }
  if (talkAvailable && message.role === 'assistant' && !message.error && !message.pending && message.content) {
    const speak = document.createElement('button');
    speak.type = 'button';
    speak.className = 'speak secondary';
    speak.setAttribute('aria-label', 'Read this reply aloud');
    speak.setAttribute('aria-pressed', 'false');
    speak.title = 'Read aloud';
    speak.append(window.GufoIcons.svg('volume-2'));
    if (reading === message) {
      speak.classList.add('reading');
      speak.setAttribute('aria-pressed', 'true');
      speak.replaceChildren(window.GufoIcons.svg('square'));
    }
    speak.addEventListener('click', () => readAloud(message, speak));
    block.append(speak);
  }
  if (message.stats) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = describe(message.stats);
    block.append(meta);
  }
}

// The language this chat is assumed to be in, beside the controls; hidden until it is known.
function renderLanguage() {
  const chip = $('language');
  const text = Lang.label(active()?.language ?? Lang.EMPTY);
  chip.textContent = text;
  chip.hidden = !text;
}

function renderMessages() {
  renderLanguage();
  const root = $('messages');
  root.replaceChildren();
  const messages = active()?.messages ?? [];
  for (const message of messages) {
    const block = document.createElement('article');
    block.className = `message ${message.role}${message.error ? ' error' : ''}${message.steering ? ' steering' : ''}`;
    fillMessage(block, message);
    root.append(block);
  }
  $('empty').hidden = messages.length > 0;
  root.parentElement.scrollTop = root.parentElement.scrollHeight;
}

// ---------------------------------------------------------------- images: pick or paste

function renderPreview() {
  const root = $('preview');
  root.replaceChildren();
  pendingImages.forEach((image, index) => {
    const figure = document.createElement('figure');
    const img = document.createElement('img');
    img.src = image.data;
    img.alt = image.name;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove ${image.name}`);
    remove.addEventListener('click', () => { pendingImages.splice(index, 1); renderPreview(); });
    figure.append(img, remove);
    root.append(figure);
  });
}

async function addImages(files) {
  for (const file of files) {
    if (!file.type.startsWith('image/') || file.size > MAX_IMAGE_BYTES) {
      $('status').textContent = `${file.name || 'Pasted image'}: choose an image no larger than 4 MiB.`;
      continue;
    }
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = reject;
      reader.readAsDataURL(file);
    }).catch(() => null);
    if (data) pendingImages.push({ name: file.name || 'pasted image', data });
  }
  renderPreview();
}

$('images').addEventListener('change', (event) => {
  const files = [...event.target.files];
  event.target.value = '';
  addImages(files);
});

// Ctrl+V of a screenshot or copied image attaches it, wherever the focus is; pasting text
// has no image files and goes on as usual.
document.addEventListener('paste', (event) => {
  const files = [...(event.clipboardData?.files ?? [])].filter((file) => file.type.startsWith('image/'));
  if (!files.length) return;
  event.preventDefault();
  addImages(files);
});

// ---------------------------------------------------------------- sending

const MAX_ROUNDS = 6;  // model calls per message with tools on; the last one gets none

// POSTs the chat and reads Gufo's server-sent events: onDelta gets each delta ({content,
// reasoning_content, tool_calls}); the usage and timings chunks and the assembled tool calls
// come back at the end.
//
// The monitor writes the reply in a run that goes on without this connection. A phone that puts
// the page in the background, or loses its network for a while, therefore comes back and reads
// the rest: the stream is reattached at the number of events already taken. Only Stop (the
// caller's signal) ends a run early. A stream that stays silent for STALL_MS while the page is
// visible, or that was left in the background for AWAY_MS, is replaced the same way.
const RESUME_PAUSES_MS = [500, 1000, 2000, 4000, 8000];
const RESUME_TRIES = 30;
const STALL_MS = 45000;
const AWAY_MS = 3000;

let link = null;     // the abort handle of the connection being read
let hiddenAt = 0;
document.addEventListener('visibilitychange', () => {
  if (document.hidden) { hiddenAt = Date.now(); return; }
  if (link && hiddenAt && Date.now() - hiddenAt >= AWAY_MS) link.abort();  // read it again, from where we were
  hiddenAt = 0;
});

function whenVisible() {
  if (!document.hidden) return Promise.resolve();
  return new Promise((resolve) => {
    const check = () => {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', check);
      resolve();
    };
    document.addEventListener('visibilitychange', check);
  });
}

function cancelRun(id) {
  fetch(`/api/chat/runs/${encodeURIComponent(id)}/cancel`, {
    method: 'POST', headers: { 'X-Gufo-Portal': '1' }, keepalive: true,
  }).catch(() => {});
}

function stopped() { return new DOMException('The reply was stopped.', 'AbortError'); }
function fatal(message) { return Object.assign(new Error(message), { fatal: true }); }

async function streamCompletion({ model, messages, effort, tools, signal, onDelta }) {
  const result = { usage: null, timings: null, toolCalls: [] };
  let runId = null;
  let handled = 0;      // events of the reply taken so far
  let finished = false;

  const take = (event) => {
    const lines = event.split('\n').filter((line) => line.startsWith('data:'));
    if (!lines.length) return;  // a keepalive comment
    handled += 1;
    for (const line of lines) {
      const data = line.slice(5).trim();
      if (!data) continue;
      if (data === '[DONE]') { finished = true; continue; }
      let chunk;
      try { chunk = JSON.parse(data); } catch { throw fatal('The server sent a reply that could not be read.'); }
      if (chunk.error) throw fatal(chunk.error.message || 'The server reported an error.');
      const delta = chunk.choices?.[0]?.delta;
      if (delta) {
        for (const part of delta.tool_calls ?? []) {
          const call = (result.toolCalls[part.index ?? 0] ??= { id: '', name: '', arguments: '' });
          if (part.id) call.id = part.id;
          if (part.function?.name) call.name = part.function.name;
          call.arguments += part.function?.arguments ?? '';
        }
        onDelta(delta);
      }
      if (chunk.usage) result.usage = chunk.usage;
      if (chunk.timings) result.timings = chunk.timings;
    }
  };

  // One connection to the reply: the first POST, or a reattachment. `connection` aborts it
  // when the page returns from the background or the stream goes quiet, and when Stop is pressed.
  const connect = (url, init) => {
    const connection = new AbortController();
    link = connection;
    if (signal?.aborted) connection.abort();
    else signal?.addEventListener('abort', () => connection.abort(), { once: true });
    return { connection, response: fetch(url, { ...init, signal: connection.signal }) };
  };

  const failure = async (response) => {
    let detail;
    try { const body = await response.json(); detail = body.error?.message || body.error || body.message; } catch { /* not JSON */ }
    return fatal(detail || `Request failed (${response.status})`);
  };

  const read = async (connection, response) => {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let lastByte = Date.now();
    const watchdog = setInterval(() => {
      if (!document.hidden && Date.now() - lastByte > STALL_MS) connection.abort();
    }, 5000);
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        lastByte = Date.now();
        buffer += decoder.decode(value, { stream: true });
        let end;
        while ((end = buffer.indexOf('\n\n')) >= 0) {
          const event = buffer.slice(0, end);
          buffer = buffer.slice(end + 2);
          take(event);
        }
      }
    } finally { clearInterval(watchdog); }
  };

  const reattach = async () => {
    for (let attempt = 0; attempt < RESUME_TRIES; attempt++) {
      await whenVisible();
      await new Promise((resolve) => setTimeout(resolve, RESUME_PAUSES_MS[Math.min(attempt, RESUME_PAUSES_MS.length - 1)]));
      if (signal?.aborted) throw stopped();
      const { connection, response } = connect(`/api/chat/runs/${encodeURIComponent(runId)}?from=${handled}`, {});
      try {
        const reply = await response;
        if (reply.ok) return { connection, reply };
        if (reply.status === 401 || reply.status === 404) throw fatal('The connection was lost and the reply could not be continued.');
      } catch (error) {
        if (error.fatal) throw error;
        if (signal?.aborted) throw stopped();
      }
    }
    throw fatal('The connection was lost and could not be restored.');
  };

  let { connection, response: pending } = connect('/api/chat/completions', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Gufo-Portal': '1' },
    body: JSON.stringify({
      model, messages, stream: true, stream_options: { include_usage: true },
      ...(effort ? { reasoning_effort: effort } : {}),
      ...(tools ? { tools } : {}),
    }),
  });
  let response = await pending;
  if (!response.ok) throw await failure(response);
  runId = response.headers.get('X-Gufo-Run');
  for (;;) {
    try {
      await read(connection, response);
      if (finished) break;
      throw new TypeError('The reply stream ended early.');
    } catch (error) {
      if (signal?.aborted) {
        if (runId) cancelRun(runId);
        throw stopped();
      }
      if (error.fatal || !runId) throw error;
      ({ connection, reply: response } = await reattach());
    }
  }
  result.toolCalls = result.toolCalls.filter(Boolean);
  return result;
}

function toApiMessages(messages) {
  return messages.filter((message) => !message.error).map((message) => {
    if (message.images?.length) {
      const content = [];
      if (message.content) content.push({ type: 'text', text: message.content });
      for (const image of message.images) content.push({ type: 'image_url', image_url: { url: image.data } });
      return { role: message.role, content };
    }
    return { role: message.role, content: message.content || (message.hadImages ? '[Image attached]' : '') };
  });
}

function setBusy(on) {
  busy = on;
  updateSendLabel();
  $('delete-chat').disabled = on;  // not while a reply is being written into it
  for (const id of ['update-memory', 'draft-guidelines']) $(id).disabled = on;  // the model is busy with the reply
  if (on) disarmDelete();
}

// ---------------------------------------------------------------- steering
// A message sent while a reply is being written steers it: it waits in this queue, shown above
// the message box, and is delivered at the next chance. A model call under way is interrupted at
// once (what it wrote so far stays); tool calls under way finish first. Either way the reply goes
// on from the steering message, as a new answer below it.
let steerQueue = [];        // {text, images}
let interruptRound = null;  // set while a model call streams: ends that call

function renderSteerQueue() {
  const root = $('steer-queue');
  root.replaceChildren();
  steerQueue.forEach((item, index) => {
    const chip = document.createElement('div');
    chip.className = 'chip';
    const text = document.createElement('span');
    text.textContent = item.text || `${item.images.length} image${item.images.length === 1 ? '' : 's'}`;
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'secondary';
    remove.setAttribute('aria-label', 'Remove this queued message');
    remove.textContent = '×';
    remove.addEventListener('click', () => { steerQueue.splice(index, 1); renderSteerQueue(); });
    chip.append(text, remove);
    root.append(chip);
  });
  root.hidden = steerQueue.length === 0;
}

function updateSendLabel() {
  const typed = $('prompt').value.trim() !== '' || pendingImages.length > 0;
  $('send').textContent = !busy ? 'Send' : typed ? 'Steer' : 'Stop';
  $('send').classList.toggle('secondary', busy && !typed);
}

// Queues what is in the message box as steering; false when the box is empty.
function queueSteer() {
  const text = $('prompt').value.trim();
  if (!text && pendingImages.length === 0) return false;
  // Steering that was spoken also tells the chat what language it is in.
  const spoken = heard && heard.text === text ? heard.language : null;
  heard = null;
  const chat = active();
  if (spoken && chat) { chat.language = Lang.track(chat.language ?? Lang.EMPTY, spoken, text); renderLanguage(); }
  steerQueue.push({ text, images: pendingImages, language: spoken });
  $('prompt').value = '';
  autosize();
  pendingImages = [];
  renderPreview();
  renderSteerQueue();
  updateSendLabel();
  interruptRound?.();
  return true;
}

// /lang shows the chat's language, names the languages it uses, or goes back to detection.
function runLanguageCommand(chat, command) {
  const say = (text) => { $('status').textContent = text; };
  if (command.error) { say(command.error); return; }
  if (command.show) {
    const label = Lang.label(chat.language ?? Lang.EMPTY);
    say(label ? `This chat is assumed to be in ${label}. /lang NAME… names its languages; /lang auto detects them.`
      : 'No language yet: it is taken from the first spoken message. /lang NAME… names it.');
    return;
  }
  chat.language = command.auto ? Lang.EMPTY : Lang.declare(command.declared);
  chat.updated = Date.now();
  saveChat(chat);
  $('prompt').value = '';
  autosize();
  renderLanguage();
  say(command.auto ? 'The language will be detected again.' : `This chat now uses ${Lang.label(chat.language)}.`);
}

async function send() {
  // A memory update that is still running would be missed by this chat's first message.
  if (memoryJob) {
    $('status').textContent = 'Updating the project memory first…';
    await memoryJob.catch(() => {});
    $('status').textContent = '';
  }
  const text = $('prompt').value.trim();
  const model = $('model').value;
  if (busy || (!text && pendingImages.length === 0)) return;
  if (!model) { $('status').textContent = 'No served model yet; try again in a moment.'; return; }
  if (!active()) newChat();
  const chat = active();
  const command = Lang.parseCommand(text);
  if (command) { runLanguageCommand(chat, command); return; }
  clearTimeout(memoryTimer);
  const allowed = selectedTools();
  const owner = projectOf(chat);
  // A chat keeps the memory it started with, so the model's own memory updates do not change
  // the start of its prompt and force it to be read again.
  chat.memory ??= owner?.memory ?? '';
  // A spoken message brings the language the recognizer heard; it settles what the chat is in.
  const spoken = heard && heard.text === text ? heard.language : null;
  heard = null;
  if (spoken) chat.language = Lang.track(chat.language ?? Lang.EMPTY, spoken, text);
  renderLanguage();
  chat.messages.push({ role: 'user', content: text, images: pendingImages, hadImages: pendingImages.length > 0,
    ...(spoken ? { language: spoken } : {}) });
  // Earlier tool steps stay in the thread but not in what is sent: the answers carry them.
  const note = allowed.size ? toolkit.note(allowed, new Date().toISOString().slice(0, 10)) : null;
  const system = Projects.systemMessage(owner ? Projects.context(owner, chat.memory) : null, note?.content,
    talk?.active ? talk.instruction : null, Lang.note(chat.language ?? Lang.EMPTY));
  const outgoing = [...(system ? [system] : []), ...toApiMessages(chat.messages)];
  let reply = { role: 'assistant', content: '', reasoning: '', pending: true, steps: [],
    ...(chat.language?.spoken ? { language: chat.language.spoken } : {}) };
  chat.messages.push(reply);
  if (chat.title === 'New chat') chat.title = text.slice(0, 48) || 'Image chat';
  $('prompt').value = '';
  autosize();
  pendingImages = [];
  renderPreview();
  renderMessages();
  let block = $('messages').lastElementChild;
  updateHistory(chat);
  controller = new AbortController();
  replyingId = chat.id;
  setBusy(true);
  $('status').textContent = '';

  let sentAt = performance.now();
  let firstAt = null;      // first piece of the whole reply
  let roundFirstAt = null; // first piece of this model call, for the live rate
  let roundPieces = 0;
  let pieces = 0;
  let queued = false;
  let totals = { prompt: 0, cached: 0, completion: 0, cost: null, decode: 0 };
  const stepArgs = new Map();  // each step's arguments, to word it if it is cut off
  const paint = () => {
    queued = false;
    const root = $('messages').parentElement;
    const atEnd = root.scrollHeight - root.scrollTop - root.clientHeight < 40;
    fillMessage(block, reply);
    talk?.onReply(reply, false);
    if (atEnd) root.scrollTop = root.scrollHeight;
    if (roundFirstAt && roundPieces > 1) {
      const rate = roundPieces / ((performance.now() - roundFirstAt) / 1000);
      $('status').textContent = `Generating… ~${rate.toFixed(1)} tok/s`;
    }
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(paint); } };
  // Ends the reply being built: its stopped tool steps, its totals, its place in the saved chat.
  const closeReply = () => {
    for (const step of reply.steps) {
      if (step.state !== 'run') continue;
      step.state = 'bad';
      step.label = `${toolkit.label(step.name, stepArgs.get(step), true)} (stopped)`;
    }
    if (totals.prompt || totals.completion) {
      const elapsed = firstAt ? (performance.now() - firstAt) / 1000 : 0;
      reply.stats = {
        tps: totals.decode > 0 ? totals.completion / totals.decode
          : (elapsed > 0 && pieces > 1 ? pieces / elapsed : null),
        ttft_ms: firstAt ? firstAt - sentAt : null,
        prompt_tokens: totals.prompt, cached_tokens: totals.cached, completion_tokens: totals.completion,
        cost_micro: totals.cost, calls: reply.steps.length || undefined,
      };
    }
    delete reply.pending;
    talk?.onReply(reply, true);
    reply.reasoning = reply.reasoning || undefined;
    if (!reply.steps.length) delete reply.steps;
  };
  // Delivers the queued steering: the reply so far is closed, the messages go under it, and a new
  // reply starts below them. The model is sent them as user messages after what it had written.
  const deliverSteering = () => {
    closeReply();
    for (const item of steerQueue) {
      const message = { role: 'user', content: item.text, images: item.images, hadImages: item.images.length > 0, steering: true,
        ...(item.language ? { language: item.language } : {}) };
      chat.messages.push(message);
      outgoing.push(toApiMessages([message])[0]);
    }
    steerQueue = [];
    renderSteerQueue();
    reply = { role: 'assistant', content: '', reasoning: '', pending: true, steps: [] };
    chat.messages.push(reply);
    totals = { prompt: 0, cached: 0, completion: 0, cost: null, decode: 0 };
    firstAt = null;
    pieces = 0;
    sentAt = performance.now();
    renderMessages();
    block = $('messages').lastElementChild;
    updateHistory(chat);
  };
  try {
    for (let round = 0; ; round++) {
      if (steerQueue.length) deliverSteering();
      const useTools = allowed.size > 0 && round < MAX_ROUNDS;
      let roundText = '';
      let separated = !reply.content;  // a blank line between the texts of two rounds
      roundFirstAt = null;
      roundPieces = 0;
      const roundControl = new AbortController();
      const stopRound = () => roundControl.abort();
      controller.signal.addEventListener('abort', stopRound, { once: true });
      interruptRound = stopRound;
      let streamed;
      try {
        streamed = await streamCompletion({
        model, messages: outgoing, effort: talk?.active ? 'none' : $('effort').value, signal: roundControl.signal,
        tools: useTools ? toolkit.schemas(allowed) : undefined,
        onDelta: (delta) => {
          if (!delta.content && !delta.reasoning_content) return;
          firstAt ??= performance.now();
          roundFirstAt ??= performance.now();
          pieces += 1;
          roundPieces += 1;
          if (delta.content) {
            if (!separated) { reply.content += '\n\n'; separated = true; }
            reply.content += delta.content;
            roundText += delta.content;
          }
          reply.reasoning += delta.reasoning_content || '';
          schedule();
        },
      });
      } catch (error) {
        // Interrupted for steering, not stopped: keep what was written and go on from the message.
        if (error.name === 'AbortError' && !controller.signal.aborted && steerQueue.length) {
          if (roundText) outgoing.push({ role: 'assistant', content: roundText });
          continue;
        }
        throw error;
      } finally {
        interruptRound = null;
        controller.signal.removeEventListener('abort', stopRound);
      }
      const { usage, timings, toolCalls } = streamed;
      if (usage) {
        totals.prompt += usage.prompt_tokens || 0;
        totals.cached += usage.prompt_tokens_details?.cached_tokens ?? usage.cached_tokens ?? 0;
        totals.completion += usage.completion_tokens || 0;
        const cost = costMicro(model, usage);
        if (cost != null) totals.cost = (totals.cost ?? 0) + cost;
        const rate = usage.completion_tokens_per_second || timings?.predicted_per_second;
        if (rate > 0 && usage.completion_tokens) totals.decode += usage.completion_tokens / rate;
      }
      if (!useTools || toolCalls.length === 0) {
        if (steerQueue.length) {  // it arrived as the answer ended: answer it too
          if (roundText) outgoing.push({ role: 'assistant', content: roundText });
          continue;
        }
        break;
      }

      // The model asked for the browser: run its calls here, show them, send back what they found.
      const calls = toolCalls.map((call, index) => {
        call.id ||= `call_${round}_${index}`;
        let args = {};
        let invalid = false;
        try { args = JSON.parse(call.arguments || '{}'); } catch { invalid = true; }
        const step = { name: call.name, label: toolkit.label(call.name, args, false), state: 'run', excerpt: '' };
        stepArgs.set(step, args);
        if (call.name === 'run_python' && typeof args.code === 'string') step.code = args.code.slice(0, 4000);
        reply.steps.push(step);
        return { call, args, invalid, step };
      });
      outgoing.push({
        role: 'assistant', content: roundText || null,
        tool_calls: calls.map(({ call, invalid }) => ({
          id: call.id, type: 'function',
          function: { name: call.name, arguments: invalid ? '{}' : call.arguments || '{}' },
        })),
      });
      $('status').textContent = 'Using the browser…';
      schedule();
      const results = await Promise.all(calls.map(async ({ call, args, invalid, step }) => {
        const result = invalid ? { ok: false, text: 'Error: the arguments are not valid JSON', via: null }
          : await toolkit.run(call.name, args, controller.signal, allowed);
        step.state = result.ok ? 'ok' : 'bad';
        step.label = toolkit.label(call.name, args, true);
        step.via = result.via;
        step.excerpt = result.text.slice(0, 1500);
        if (result.figures?.length) step.figures = result.figures;
        if (result.media?.length) step.media = result.media;
        schedule();
        return result;
      }));
      calls.forEach(({ call }, index) => outgoing.push({ role: 'tool', tool_call_id: call.id, content: results[index].text }));
      $('status').textContent = '';
    }
  } catch (error) {
    const stopped = error.name === 'AbortError';
    const note = stopped ? '[stopped]' : `[stopped: ${error.message}]`;
    if (reply.content || reply.steps.length) reply.content += `${reply.content ? '\n\n' : ''}${note}`;
    else if (stopped) reply.content = note;
    else { reply.content = `Error: ${error.message}`; reply.error = true; }
  } finally {
    closeReply();
    controller = null;
    replyingId = null;
    setBusy(false);
    $('status').textContent = '';
    // Steering that was never delivered (Stop, an error) goes back into the message box, not lost.
    if (steerQueue.length) {
      const typed = $('prompt').value.trim();
      $('prompt').value = [...steerQueue.map((item) => item.text), typed].filter(Boolean).join('\n\n');
      pendingImages = [...steerQueue.flatMap((item) => item.images), ...pendingImages];
      steerQueue = [];
      renderSteerQueue();
      renderPreview();
      autosize();
    }
    updateSendLabel();
    updateHistory(chat);
    renderMessages();
    scheduleMemory(chat);
  }
}

$('new-chat').addEventListener('click', newChat);
$('delete-chat').addEventListener('click', () => {
  if (busy) return;
  if (disarmTimer === null) {
    $('delete-chat').textContent = 'Delete chat?';
    $('delete-chat').classList.add('danger');
    disarmTimer = setTimeout(disarmDelete, 3000);
    return;
  }
  disarmDelete();
  deleteChat();
});
$('history').addEventListener('change', () => {
  disarmDelete();
  openChat($('history').value);
});
$('project').addEventListener('change', () => {
  disarmDelete();
  if ($('project').value === NEW_PROJECT) createProject(); else setScope($('project').value);
});
$('edit-project').addEventListener('click', () => setProjectOpen($('project-panel').hidden));
$('close-project').addEventListener('click', () => setProjectOpen(false));
for (const id of ['project-name', 'project-guidelines', 'project-memory', 'auto-memory']) {
  $(id).addEventListener('input', projectEdited);
}
$('draft-guidelines').addEventListener('click', draftGuidelines);
$('update-memory').addEventListener('click', () => {
  const chat = active();
  if (!chat || busy) return;
  if (!chat.messages.length || !Projects.unsummarized(chat)) { setMemoryStatus('Nothing new in this chat to add.'); return; }
  updateMemory(chat).catch(() => {});
});
$('export-project').addEventListener('click', exportProject);
$('import-project').addEventListener('change', async () => {
  const [file] = $('import-project').files;
  $('import-project').value = '';
  await importProject(file);
});
$('delete-project').addEventListener('click', () => {
  if (projectDisarmTimer === null) {
    $('delete-project').textContent = 'Delete project? Chats are kept';
    $('delete-project').classList.add('danger');
    projectDisarmTimer = setTimeout(disarmProjectDelete, 3000);
    return;
  }
  disarmProjectDelete();
  deleteProject();
});
// Enter sends, Shift+Enter adds a line; Enter while an input method is composing does neither.
$('prompt').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  $('composer').requestSubmit();  // while a reply is written this steers it
});
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (!busy) send();
  else if (!queueSteer()) controller?.abort();
});
// The box grows with its text, up to its CSS limit.
function autosize() {
  const box = $('prompt');
  box.style.height = 'auto';
  box.style.height = `${box.scrollHeight}px`;
}
$('prompt').addEventListener('input', () => { autosize(); updateSendLabel(); });
$('effort').value = localStorage.getItem(EFFORT_KEY) || '';
$('model').addEventListener('change', () => localStorage.setItem(MODEL_KEY, $('model').value));
$('effort').addEventListener('change', () => localStorage.setItem(EFFORT_KEY, $('effort').value));

// ---------------------------------------------------------------- talk mode

const TALK_LABEL = {
  off: 'Talk', starting: 'Starting… (tap to cancel)', listening: 'Listening… (tap to stop)', hearing: 'Hearing you…', transcribing: 'Understanding…',
  thinking: 'Thinking… (tap to interrupt)', speaking: 'Speaking… (tap to interrupt)', error: 'Talk',
};

async function speechError(response) {
  let message = `${response.status}`;
  try {
    const body = await response.json();
    message = typeof body.error === 'string' ? body.error : body.error?.message || message;
  } catch { /* not JSON */ }
  return new Error(`Speech: ${message}`);
}

// One piece of text as a WAV file, in the voice chosen on the voice settings page.
async function synthesizeSpeech(text, language) {
  const response = await fetch('/api/chat/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, ...(voicePrefs.voice ? { voice: voicePrefs.voice } : {}), ...(language ? { language } : {}) }),
  });
  if (!response.ok) throw await speechError(response);
  return response.arrayBuffer();
}

// Reads one reply aloud when its speaker button is pressed; pressing it again stops it.
let reader = null;
let reading = null;  // the message being read
function readAloud(message, button) {
  reader ??= window.GufoTalk.createReader({
    synthesize: (text) => synthesizeSpeech(text, reading?.language),
    attach: playThroughAvatar,
    speed: () => voicePrefs.speed,
    error: (text) => { $('status').textContent = text; },
  });
  if (reading === message) { reader.stop(); return; }
  const mark = (on) => {
    button.classList.toggle('reading', on);
    button.setAttribute('aria-pressed', String(on));
    button.title = on ? 'Stop reading' : 'Read aloud';
    button.replaceChildren(window.GufoIcons.svg(on ? 'square' : 'volume-2'));
  };
  reading = message;
  mark(true);
  reader.speak(message.content, () => {
    if (reading === message) reading = null;
    mark(false);
  });
}

function talkController() {
  return window.GufoTalk.create({
    ask: (text) => { $('prompt').value = text; if (busy) queueSteer(); else send(); },
    stopReply: () => controller?.abort(),
    speed: () => voicePrefs.speed,
    transcribe: async (wav) => {
      const response = await fetch('/api/chat/transcribe', { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav });
      if (!response.ok) throw await speechError(response);
      const answer = await response.json();
      heard = { text: (answer.text ?? '').trim(), language: answer.language ?? null };
      return answer.text ?? '';
    },
    synthesize: (text) => synthesizeSpeech(text, active()?.language?.spoken),
    attach: playThroughAvatar,
    setState: (state, detail) => {
      const button = $('talk');
      button.textContent = TALK_LABEL[state] ?? 'Talk';
      button.setAttribute('aria-pressed', String(state !== 'off'));
      button.classList.toggle('talking', state !== 'off');
      button.classList.toggle('secondary', state === 'off');
      if (state === 'error') $('status').textContent = detail;
      else if (state === 'off') $('status').textContent = '';
      else if (state === 'hearing' || state === 'listening') $('status').textContent = '';
    },
  });
}

// The avatar: a small face that mouths the spoken replies. It is there while the account has it on (Voice
// settings; on by default) and talk mode exists, and its close button turns it off for the account.
let avatar = null;
function showAvatar(on) {
  const pip = $('avatar');
  if (!window.GufoAvatar) return;
  pip.hidden = !on;
  if (on && !avatar) avatar = window.GufoAvatar.create($('avatar-face'));
  if (!on && avatar) { avatar.destroy(); avatar = null; }
}
$('avatar-close').append(window.GufoIcons.svg('x'));
$('avatar-close').addEventListener('click', async () => {
  showAvatar(false);
  voicePrefs.avatar = false;
  try {
    const response = await fetch('/api/chat/avatar', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: false }),
    });
    if (!response.ok) throw new Error(`${response.status}`);
  } catch (error) {
    voicePrefs.avatar = true;
    showAvatar(true);
    $('status').textContent = `The avatar setting was not saved (${error.message}).`;
  }
});
// Spoken audio goes through the avatar, which listens on the way to the speakers.
const playThroughAvatar = (context, node) => {
  if (avatar) avatar.attach(context, node); else node.connect(context.destination);
};

// How replies are spoken: the voice and speed from the account's voice settings (/voice), read
// when the chat learns that talk mode exists.
let voicePrefs = { voice: null, speed: 1, avatar: true };
async function loadVoicePrefs() {
  try {
    const response = await fetch('/api/chat/voice');
    if (!response.ok) return;
    const info = await response.json();
    voicePrefs = { voice: info.prefs?.voice ?? info.voice ?? null, speed: Number(info.prefs?.speed) || 1,
      avatar: info.prefs?.avatar !== false };
    showAvatar(voicePrefs.avatar);
  } catch { /* the defaults speak */ }
}

$('talk').addEventListener('click', async () => {
  talk ??= talkController();
  try {
    if (!talk.active) await talk.start();
    else if (!talk.interrupt()) await talk.stop();
  } catch (error) { $('status').textContent = error.message; }
});
window.addEventListener('pagehide', () => { talk?.stop(); reader?.stop(); });

// ---------------------------------------------------------------- tools menu

function loadAllowedTools() {
  const known = new Set((window.GufoTools?.CATALOG ?? []).map((tool) => tool.id));
  try {
    const saved = JSON.parse(localStorage.getItem(TOOLS_KEY));
    if (Array.isArray(saved)) return new Set(saved.filter((id) => known.has(id)));
  } catch { /* nothing saved yet */ }
  // The Browser switch this menu replaced.
  return localStorage.getItem(BROWSER_KEY) === '1' ? new Set(['web_search', 'fetch_url']) : new Set();
}

// What a message may use: ticked, and installed.
function selectedTools() {
  if (!toolkit) return new Set();
  const ids = new Set(allowedTools);
  if (!pythonInstalled) ids.delete('run_python');
  if (!libraryAvailable) for (const id of ['search_library', 'read_library']) ids.delete(id);
  return ids;
}

function updateToolsCount() {
  const count = selectedTools().size;
  $('tools-count').textContent = count ? String(count) : '';
  $('tools-button').title = count ? `Tools the model may use: ${count}` : 'Choose the tools the model may use';
}

function renderToolsMenu() {
  const menu = $('tools-menu');
  menu.replaceChildren();
  for (const tool of toolkit ? window.GufoTools.CATALOG : []) {
    const library = tool.id === 'search_library' || tool.id === 'read_library';
    const missing = (tool.id === 'run_python' && !pythonInstalled) || (library && !libraryAvailable);
    const option = document.createElement('label');
    option.className = `tool-option${missing ? ' off' : ''}`;
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.value = tool.id;
    box.checked = allowedTools.has(tool.id) && !missing;
    box.disabled = missing;
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = tool.name;
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = !missing ? tool.hint
      : library ? 'Only the admin has a library.' : 'Not installed on the monitor: run tools/monitor/fetch-pyodide.py';
    option.append(box, name, hint);
    menu.append(option);
  }
  if (!toolkit) {
    const failed = document.createElement('div');
    failed.className = 'tool-option off';
    failed.textContent = 'The tools did not load. Reload the page.';
    menu.append(failed);
  }
  updateToolsCount();
}

function setToolsOpen(open) {
  $('tools-menu').hidden = !open;
  $('tools-button').setAttribute('aria-expanded', String(open));
}

$('tools-button').addEventListener('click', () => setToolsOpen($('tools-menu').hidden));
$('tools-menu').addEventListener('change', (event) => {
  const box = event.target;
  if (box.checked) allowedTools.add(box.value); else allowedTools.delete(box.value);
  localStorage.setItem(TOOLS_KEY, JSON.stringify([...allowedTools]));
  updateToolsCount();
  if (box.checked && box.value === 'run_python') window.GufoPython.warm();  // fetch the runtime now
});
document.addEventListener('click', (event) => { if (!$('tools').contains(event.target)) setToolsOpen(false); });
$('tools').addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || $('tools-menu').hidden) return;
  setToolsOpen(false);
  $('tools-button').focus();
});
$('tools').addEventListener('focusout', (event) => {
  if (event.relatedTarget && !$('tools').contains(event.relatedTarget)) setToolsOpen(false);
});

// Fills the model chooser, and asks again every few seconds until a model is listed: a monitor
// that was just started, or one that cannot reach Gufo yet, fills the chooser by itself instead
// of leaving it empty until the page is reloaded.
let modelRetry = null;
let modelNote = '';  // the status text this loader wrote, so it never erases another message
function noteModels(text) {
  const status = $('status');
  if (status.textContent === '' || status.textContent === modelNote) status.textContent = text;
  modelNote = text;
}

async function loadModels() {
  clearTimeout(modelRetry);
  let found = 0;
  try {
    const response = await fetch('/api/chat/models');
    if (!response.ok) throw new Error(`Could not load served models (${response.status}).`);
    const data = await response.json();
    prices = data.pricing;
    billed = data.billed;
    pythonInstalled = Boolean(data.python);
    libraryAvailable = data.library === true;
    const couldSpeak = talkAvailable;
    talkAvailable = data.talk === true && Boolean(window.GufoTalk);
    if (talkAvailable !== couldSpeak && active()) renderMessages();
    $('talk').hidden = !talkAvailable;
    if (talkAvailable) loadVoicePrefs();
    ownerKnown({ owner: typeof data.owner === 'string' ? data.owner : null, sync: Boolean(data.sync) });  // no owner (an older monitor): no database is opened
    found = data.models.length;
    const select = $('model');
    const chosen = select.value;
    select.replaceChildren(...data.models.map((id) => {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id;
      return option;
    }));
    // What the user picked, here or on an earlier visit; else the monitor's default; else the first.
    const remembered = localStorage.getItem(MODEL_KEY);
    const pick = [chosen, remembered, data.default].find((id) => id && data.models.includes(id));
    if (pick) select.value = pick;
    noteModels(found ? '' : 'No served model found yet; trying again…');
    renderToolsMenu();
    renderTotal();
    renderMessages();
  } catch (error) { noteModels(`${error.message} Trying again…`); }
  if (!found) modelRetry = setTimeout(loadModels, 5000);
}
document.addEventListener('visibilitychange', () => { if (!document.hidden && !$('model').value) loadModels(); });

(async () => {
  loadModels();  // first and on its own: nothing below may keep the model list from loading
  if (!window.GufoProjects || !window.GufoStore) {
    $('project').hidden = true;
    $('status').textContent = 'Projects did not load (restart the monitor so it serves the new scripts).';
  }
  // Whose chats these are comes from the monitor; without an answer the chats last for this visit only.
  const who = await Promise.race([ownerReady, new Promise((resolve) => setTimeout(() => resolve(null), 20000))]);
  try {
    if (who?.owner) await loadStore(who.owner);
    else {
      // Never guess whose chats these are: an unknown account gets a private in-memory copy, not a shared database.
      store = volatileStore();
      $('status').textContent = who ? 'The monitor is out of date (restart it): chats last until you close the page.'
        : 'The monitor did not answer: chats last until you close the page.';
    }
  } catch { $('status').textContent = 'Could not read the saved chats from this browser.'; }
  try {
    renderProjectList();
    activeId = scoped()[0]?.id ?? null;
    if (activeId) showChat(activeId); else newChat();
    renderToolsMenu();
  } catch (error) { $('status').textContent = `The chat could not start: ${error.message}`; }
  if (who?.sync && window.GufoSync) startSync(who.owner);
})();
