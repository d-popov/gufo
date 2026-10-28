'use strict';

const STORAGE_KEY = 'gufo-chat-histories-v1';
const EFFORT_KEY = 'gufo-chat-effort';
const BROWSER_KEY = 'gufo-chat-browser';
const TOOLS_KEY = 'gufo-chat-tools';
const MAX_HISTORIES = 10;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const $ = (id) => document.getElementById(id);
const fmtUsd = window.GufoHeader.fmtUsd;

// The page header first, so it is there whatever fails below.
window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});
let histories = loadHistories();
let activeId = histories[0]?.id ?? null;
let pendingImages = [];
let prices = null;  // {model: {input, cached_input, output}} micro-USD per 1M tokens, or null
let billed = false;  // false for the admin: prices are shown, nothing is charged
let busy = false;
let controller = null;  // aborts the reply being made, and the page fetches it asked for
// The tool scripts are separate files. If one did not load, the chat goes on without tools.
const toolkit = (() => { try { return window.GufoTools.create(); } catch { return null; } })();
let allowedTools = loadAllowedTools();  // what the user ticked in the Tools menu, kept between visits
let pythonInstalled = false;            // set by loadModels: is the Python runtime on the monitor?

// ---------------------------------------------------------------- history (this browser only)

function loadHistories() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]');
    return Array.isArray(saved) ? saved.filter((item) => item && typeof item.id === 'string' && Array.isArray(item.messages)).slice(0, MAX_HISTORIES) : [];
  } catch { return []; }
}

// Image bytes and the live "thinking" text stay out of storage; the stats stay in.
function saveHistories() {
  const saved = histories.slice(0, MAX_HISTORIES).map((history) => ({
    ...history,
    messages: history.messages.map(({ role, content, hadImages, images, stats, error, steps }) => ({
      role, content, hadImages: Boolean(hadImages || images?.length), stats, error,
      steps: steps?.map(({ figures, ...rest }) => (figures ? { ...rest, figureCount: figures.length } : rest)),
    }))
  }));
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); }
  catch { $('status').textContent = 'Browser storage is full; this conversation will not be saved.'; }
}

function active() { return histories.find((item) => item.id === activeId); }

function updateHistory() {
  const current = active();
  if (!current) return;
  current.updated = Date.now();
  histories.sort((a, b) => b.updated - a.updated);
  histories = histories.slice(0, MAX_HISTORIES);
  activeId = current.id;
  saveHistories();
  renderHistoryList();
  renderTotal();
}

function renderHistoryList() {
  const select = $('history');
  select.replaceChildren();
  histories.forEach((item) => {
    const option = document.createElement('option');
    option.value = item.id;
    option.textContent = item.title || 'New chat';
    select.append(option);
  });
  if (activeId) select.value = activeId;
}

function newChat() {
  disarmDelete();
  const item = { id: crypto.randomUUID(), title: 'New chat', updated: Date.now(), messages: [] };
  histories.unshift(item);
  histories = histories.slice(0, MAX_HISTORIES);
  activeId = item.id;
  pendingImages = [];
  renderPreview();
  renderHistoryList();
  renderMessages();
  renderTotal();
  saveHistories();
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

// Removes the open chat and opens the most recent one left, or a fresh one if none is.
function deleteChat() {
  histories = histories.filter((item) => item.id !== activeId);
  activeId = histories[0]?.id ?? null;
  if (!activeId) { newChat(); return; }
  pendingImages = [];
  renderPreview();
  renderHistoryList();
  renderMessages();
  renderTotal();
  saveHistories();
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
  text.textContent = waiting ? 'Waiting for the model…'
    : message.content || (message.hadImages ? '[Image attached; image bytes are not retained after reload]' : '');
  block.append(text);
  for (const image of message.images ?? []) {
    const img = document.createElement('img');
    img.src = image.data;
    img.alt = image.name;
    block.append(img);
  }
  if (message.stats) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = describe(message.stats);
    block.append(meta);
  }
}

function renderMessages() {
  const root = $('messages');
  root.replaceChildren();
  const messages = active()?.messages ?? [];
  for (const message of messages) {
    const block = document.createElement('article');
    block.className = `message ${message.role}${message.error ? ' error' : ''}`;
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
async function streamCompletion({ model, messages, effort, tools, signal, onDelta }) {
  const response = await fetch('/api/chat/completions', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Gufo-Portal': '1' }, signal,
    body: JSON.stringify({
      model, messages, stream: true, stream_options: { include_usage: true },
      ...(effort ? { reasoning_effort: effort } : {}),
      ...(tools ? { tools } : {}),
    })
  });
  if (!response.ok) {
    let detail;
    try { const body = await response.json(); detail = body.error?.message || body.error || body.message; } catch { /* not JSON */ }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const result = { usage: null, timings: null, toolCalls: [] };
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let end;
    while ((end = buffer.indexOf('\n\n')) >= 0) {
      const event = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      for (const line of event.split('\n')) {
        if (!line.startsWith('data:')) continue;
        const data = line.slice(5).trim();
        if (!data || data === '[DONE]') continue;
        const chunk = JSON.parse(data);
        if (chunk.error) throw new Error(chunk.error.message || 'The server reported an error.');
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
  $('send').textContent = on ? 'Stop' : 'Send';
  $('send').classList.toggle('secondary', on);
  $('delete-chat').disabled = on;  // not while a reply is being written into it
  if (on) disarmDelete();
}

async function send() {
  const text = $('prompt').value.trim();
  const model = $('model').value;
  if (busy || (!text && pendingImages.length === 0)) return;
  if (!model) { $('status').textContent = 'No served model yet; try again in a moment.'; return; }
  if (!active()) newChat();
  const chat = active();
  const allowed = selectedTools();
  chat.messages.push({ role: 'user', content: text, images: pendingImages, hadImages: pendingImages.length > 0 });
  // Earlier tool steps stay in the thread but not in what is sent: the answers carry them.
  const note = allowed.size ? toolkit.note(allowed, new Date().toISOString().slice(0, 10)) : null;
  const outgoing = [...(note ? [note] : []), ...toApiMessages(chat.messages)];
  const reply = { role: 'assistant', content: '', reasoning: '', pending: true, steps: [] };
  chat.messages.push(reply);
  if (chat.title === 'New chat') chat.title = text.slice(0, 48) || 'Image chat';
  $('prompt').value = '';
  autosize();
  pendingImages = [];
  renderPreview();
  renderMessages();
  const block = $('messages').lastElementChild;
  updateHistory();
  controller = new AbortController();
  setBusy(true);
  $('status').textContent = '';

  const sentAt = performance.now();
  let firstAt = null;      // first piece of the whole reply
  let roundFirstAt = null; // first piece of this model call, for the live rate
  let roundPieces = 0;
  let pieces = 0;
  let queued = false;
  const totals = { prompt: 0, cached: 0, completion: 0, cost: null, decode: 0 };
  const stepArgs = new Map();  // each step's arguments, to word it if it is cut off
  const paint = () => {
    queued = false;
    const root = $('messages').parentElement;
    const atEnd = root.scrollHeight - root.scrollTop - root.clientHeight < 40;
    fillMessage(block, reply);
    if (atEnd) root.scrollTop = root.scrollHeight;
    if (roundFirstAt && roundPieces > 1) {
      const rate = roundPieces / ((performance.now() - roundFirstAt) / 1000);
      $('status').textContent = `Generating… ~${rate.toFixed(1)} tok/s`;
    }
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(paint); } };
  try {
    for (let round = 0; ; round++) {
      const useTools = allowed.size > 0 && round < MAX_ROUNDS;
      let roundText = '';
      let separated = !reply.content;  // a blank line between the texts of two rounds
      roundFirstAt = null;
      roundPieces = 0;
      const { usage, timings, toolCalls } = await streamCompletion({
        model, messages: outgoing, effort: $('effort').value, signal: controller.signal,
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
      if (usage) {
        totals.prompt += usage.prompt_tokens || 0;
        totals.cached += usage.prompt_tokens_details?.cached_tokens ?? usage.cached_tokens ?? 0;
        totals.completion += usage.completion_tokens || 0;
        const cost = costMicro(model, usage);
        if (cost != null) totals.cost = (totals.cost ?? 0) + cost;
        const rate = usage.completion_tokens_per_second || timings?.predicted_per_second;
        if (rate > 0 && usage.completion_tokens) totals.decode += usage.completion_tokens / rate;
      }
      if (!useTools || toolCalls.length === 0) break;

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
    controller = null;
    setBusy(false);
    $('status').textContent = '';
    delete reply.pending;
    reply.reasoning = reply.reasoning || undefined;
    if (!reply.steps.length) delete reply.steps;
    updateHistory();
    renderMessages();
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
  activeId = $('history').value;
  pendingImages = [];
  renderPreview();
  renderMessages();
  renderTotal();
});
// Enter sends, Shift+Enter adds a line; Enter while an input method is composing does neither.
$('prompt').addEventListener('keydown', (event) => {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return;
  event.preventDefault();
  if (!busy) $('composer').requestSubmit();
});
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (busy) controller?.abort(); else send();
});
// The box grows with its text, up to its CSS limit.
function autosize() {
  const box = $('prompt');
  box.style.height = 'auto';
  box.style.height = `${box.scrollHeight}px`;
}
$('prompt').addEventListener('input', autosize);
$('effort').value = localStorage.getItem(EFFORT_KEY) || '';
$('effort').addEventListener('change', () => localStorage.setItem(EFFORT_KEY, $('effort').value));

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
    const missing = tool.id === 'run_python' && !pythonInstalled;
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
    hint.textContent = missing ? 'Not installed on the monitor: run tools/monitor/fetch-pyodide.py' : tool.hint;
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

async function loadModels() {
  try {
    const response = await fetch('/api/chat/models');
    if (!response.ok) throw new Error('Could not load served models.');
    const data = await response.json();
    prices = data.pricing;
    billed = data.billed;
    pythonInstalled = Boolean(data.python);
    renderToolsMenu();
    const select = $('model');
    for (const id of data.models) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id;
      select.append(option);
    }
    if (!data.models.length) $('status').textContent = 'No served model found yet; wait for the monitor to discover it.';
    renderTotal();
    renderMessages();
  } catch (error) { $('status').textContent = error.message; }
}

if (!activeId) newChat();
renderHistoryList();
renderMessages();
renderTotal();
renderToolsMenu();
loadModels();
