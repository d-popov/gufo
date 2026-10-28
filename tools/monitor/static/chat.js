'use strict';

const STORAGE_KEY = 'gufo-chat-histories-v1';
const EFFORT_KEY = 'gufo-chat-effort';
const MAX_HISTORIES = 10;
const MAX_IMAGE_BYTES = 4 * 1024 * 1024;
const $ = (id) => document.getElementById(id);
const fmtUsd = window.GufoHeader.fmtUsd;
let histories = loadHistories();
let activeId = histories[0]?.id ?? null;
let pendingImages = [];
let prices = null;  // {model: {input, cached_input, output}} micro-USD per 1M tokens, or null
let billed = false;  // false for the admin: prices are shown, nothing is charged
let busy = false;

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
    messages: history.messages.map(({ role, content, hadImages, images, stats, error }) => ({
      role, content, hadImages: Boolean(hadImages || images?.length), stats, error
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

// POSTs the chat and reads Gufo's server-sent events: onDelta gets each {content,
// reasoning_content}; the usage and timings chunks come back at the end.
async function streamCompletion(model, messages, effort, onDelta) {
  const response = await fetch('/api/chat/completions', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Gufo-Portal': '1' },
    body: JSON.stringify({
      model, messages, stream: true, stream_options: { include_usage: true },
      ...(effort ? { reasoning_effort: effort } : {}),
    })
  });
  if (!response.ok) {
    let detail;
    try { const body = await response.json(); detail = body.error?.message || body.error || body.message; } catch { /* not JSON */ }
    throw new Error(detail || `Request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const result = { usage: null, timings: null };
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
        if (delta) onDelta(delta);
        if (chunk.usage) result.usage = chunk.usage;
        if (chunk.timings) result.timings = chunk.timings;
      }
    }
  }
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

async function send() {
  const text = $('prompt').value.trim();
  const model = $('model').value;
  if (busy || (!text && pendingImages.length === 0)) return;
  if (!model) { $('status').textContent = 'No served model yet; try again in a moment.'; return; }
  if (!active()) newChat();
  const chat = active();
  chat.messages.push({ role: 'user', content: text, images: pendingImages, hadImages: pendingImages.length > 0 });
  const outgoing = toApiMessages(chat.messages);
  const reply = { role: 'assistant', content: '', reasoning: '', pending: true };
  chat.messages.push(reply);
  if (chat.title === 'New chat') chat.title = text.slice(0, 48) || 'Image chat';
  $('prompt').value = '';
  autosize();
  pendingImages = [];
  renderPreview();
  renderMessages();
  const block = $('messages').lastElementChild;
  updateHistory();
  busy = true;
  $('send').disabled = true;
  $('status').textContent = '';

  const sentAt = performance.now();
  let firstAt = null;
  let pieces = 0;
  let queued = false;
  const paint = () => {
    queued = false;
    const root = $('messages').parentElement;
    const atEnd = root.scrollHeight - root.scrollTop - root.clientHeight < 40;
    fillMessage(block, reply);
    if (atEnd) root.scrollTop = root.scrollHeight;
    if (firstAt && pieces > 1) {
      const rate = pieces / ((performance.now() - firstAt) / 1000);
      $('status').textContent = `Generating… ~${rate.toFixed(1)} tok/s`;
    }
  };
  try {
    const { usage, timings } = await streamCompletion(model, outgoing, $('effort').value, (delta) => {
      if (!delta.content && !delta.reasoning_content) return;
      firstAt ??= performance.now();
      pieces += 1;
      reply.content += delta.content || '';
      reply.reasoning += delta.reasoning_content || '';
      if (!queued) { queued = true; requestAnimationFrame(paint); }
    });
    const elapsed = firstAt ? (performance.now() - firstAt) / 1000 : 0;
    reply.stats = {
      tps: usage?.completion_tokens_per_second || timings?.predicted_per_second
        || (elapsed > 0 && pieces > 1 ? pieces / elapsed : null),
      ttft_ms: firstAt ? firstAt - sentAt : null,
      prompt_tokens: usage?.prompt_tokens,
      cached_tokens: usage?.prompt_tokens_details?.cached_tokens ?? usage?.cached_tokens,
      completion_tokens: usage?.completion_tokens,
      cost_micro: costMicro(model, usage),
    };
    $('status').textContent = '';
  } catch (error) {
    if (reply.content) reply.content += `\n\n[stopped: ${error.message}]`;
    else { reply.content = `Error: ${error.message}`; reply.error = true; }
    $('status').textContent = '';
  } finally {
    busy = false;
    $('send').disabled = false;
    delete reply.pending;
    reply.reasoning = reply.reasoning || undefined;
    updateHistory();
    renderMessages();
  }
}

$('new-chat').addEventListener('click', newChat);
$('history').addEventListener('change', () => {
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
  $('composer').requestSubmit();
});
$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  send();
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

async function loadModels() {
  try {
    const response = await fetch('/api/chat/models');
    if (!response.ok) throw new Error('Could not load served models.');
    const data = await response.json();
    prices = data.pricing;
    billed = data.billed;
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
loadModels();

window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});
