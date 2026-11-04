'use strict';

// The page of a shared chat (/s/TOKEN): one conversation that everyone with the link sees and writes in. The monitor keeps it;
// this page polls it a little over once a second, posts what the visitor types or says, and reads the model's reply as it is
// written (the asker's own connection streams it, which is what feeds Talk). The secret in the URL is the only credential.
//
// The room offers the tools its owner chose. They run in the asker's browser, as on the chat page: when the model calls one,
// this page runs it, shows the steps to the room, and sends the results back for the next round of the same reply.
const $ = (id) => document.getElementById(id);
const TOKEN = location.pathname.split('/')[2] ?? '';
const API = `/api/share/${encodeURIComponent(TOKEN)}`;
const POLL_MS = 1500;
const INFO_MS = 30000;            // how often the room's tools and default effort are read again
const MAX_ROUNDS = 6;             // model calls per message; the monitor offers no tools on the last
const EXTRA_MESSAGES = 40;        // what the monitor takes back of one reply's tool rounds: messages,
const EXTRA_CHARS = 300000;       // characters in all,
const TOOL_CHARS = 30000;         // characters of one tool result,
const ARGS_CHARS = 8000;          // and of one call's arguments
const EFFORT_KEY = 'gufo-share-effort';
const EFFORT_LABEL = { '': 'Default (owner’s setting)', none: 'Off', low: 'Low', medium: 'Medium', high: 'High', max: 'Max' };
const Lang = window.GufoLanguage;
const View = window.GufoMessageView;
// The tools, with Python and the library served under this room's address (the room's library endpoint answers only the
// library tools its owner enabled). Without the tool scripts the room still talks, with no tools.
const toolkit = (() => {
  try {
    const python = window.GufoPython.create({ workerUrl: `${API}/pyworker.js`, assetUrl: `${API}/pyodide/` });
    const library = window.GufoLibraryTools.create({ url: `${API}/library`, credentials: 'omit' });
    return window.GufoTools.create({ library, python });
  } catch { return null; }
})();

let info = null;
let room = { rev: -1, messages: [] };
let older = [];             // earlier messages the visitor scrolled back for, and whether still earlier ones exist
let olderMore = null;
let language = Lang.EMPTY;  // what this visitor's chat is in, from what they say
let heard = null;           // what speech recognition returned last: { text, language }
let own = null;             // the reply this page is writing: { id, turn, content, reasoning, steps, stats }
let ownView = null;         // where it is drawn: { block, message } (the room's copy it overlays)
let busy = false;
let controller = null;
let talk = null;
let reader = null;
let reading = null;
let avatar = null;
let talkState = 'off';

const status = (text) => { $('status').textContent = text; };

const name = () => $('share-name').value.trim() || 'Guest';
$('share-name').value = (() => {
  try {
    let saved = localStorage.getItem('gufo-share-name');
    if (!saved) { saved = `Guest-${Math.floor(1000 + Math.random() * 9000)}`; localStorage.setItem('gufo-share-name', saved); }
    return saved;
  } catch { return `Guest-${Math.floor(1000 + Math.random() * 9000)}`; }
})();
$('share-name').addEventListener('change', () => { try { localStorage.setItem('gufo-share-name', name()); } catch { /* blocked */ } render(); });

async function failure(response) {
  try {
    const body = await response.json();
    return typeof body.error === 'string' ? body.error : body.error?.message || `${response.status}`;
  } catch { return `${response.status}`; }
}

function updateLeft() {
  if (!info) return;
  $('share-left').textContent = `${info.messages_left} message${info.messages_left === 1 ? '' : 's'} left`;
}

function renderLanguage() {
  const text = Lang.label(language);
  $('language').textContent = text;
  $('language').hidden = !text;
}

// ------------------------------------------------------------------------ the room

function fill(block, message) {
  block.replaceChildren();
  if (message.role === 'user') {
    const who = document.createElement('span');
    who.className = 'who';
    who.textContent = message.name === name() ? 'You' : message.name;
    block.append(who);
  }
  if (message.role === 'assistant') View.fillThinkingAndSteps(block, message);
  const writing = message.role === 'assistant' && message.done === false && !message.content;
  const thinking = block.querySelector('details.thinking > summary');
  if (writing && thinking) { thinking.dataset.label = 'Thinking'; thinking.dataset.since = message.at; }
  if (message.role === 'assistant' && message.content) {
    const body = document.createElement('div');
    body.className = 'md';
    body.append(window.GufoMarkdown.render(message.content));
    block.append(body);
  } else {
    const text = document.createElement('div');
    if (message.role === 'assistant' && writing && !message.reasoning && !message.steps?.length) {
      text.className = 'live';
      text.dataset.label = 'Waiting for the model';
      text.dataset.since = message.at;
    } else {
      text.textContent = message.content || (message.done === false ? '…' : '');
    }
    block.append(text);
  }
  if (info?.talk && message.role === 'assistant' && message.done !== false && message.content) {
    const speak = document.createElement('button');
    speak.type = 'button';
    speak.className = 'speak secondary';
    speak.setAttribute('aria-label', 'Read this reply aloud');
    speak.title = 'Read aloud';
    speak.append(window.GufoIcons.svg(reading === message.id ? 'square' : 'volume-2'));
    speak.addEventListener('click', () => readAloud(message, speak));
    block.append(speak);
  }
  if (message.stats) {
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = View.describe(message.stats);
    block.append(meta);
  }
  if (writing) tick(block);
}

let skew = 0;                   // the monitor's clock less this page's, from the room's last answer

// A reply being written shows how long it has been going: waiting for the model, then thinking.
function tick(root = document) {
  for (const node of root.querySelectorAll('[data-since]')) {
    const seconds = Math.max(0, Math.round((Date.now() + skew) / 1000 - Number(node.dataset.since)));
    node.textContent = `${node.dataset.label}… ${seconds} s`;
  }
}
setInterval(tick, 1000);

// The room's copy of the reply this page writes, with what this page has of it: that is newer.
function overlay(message) {
  return {
    ...message,
    content: own.content || message.content,
    reasoning: own.reasoning || message.reasoning,
    steps: own.steps.length ? own.steps : message.steps,
    stats: own.stats ?? message.stats,
  };
}

// Earlier history is fetched on request: the latest messages come with every poll, the rest a page at a time.
async function showEarlier() {
  const first = (older[0] ?? room.messages[0])?.id;
  if (first === undefined) return;
  try {
    const response = await fetch(`${API}/conversation?before=${first}`);
    if (!response.ok) return;
    const page = await response.json();
    older = [...page.messages, ...older];
    olderMore = page.more;
    render();
  } catch { /* try again */ }
}

const scroller = () => $('messages').parentElement;
const nearEnd = () => scroller().scrollHeight - scroller().scrollTop - scroller().clientHeight < 80;

function render() {
  const root = $('messages');
  const atEnd = nearEnd();
  root.replaceChildren();
  ownView = null;
  if (olderMore === null ? room.more : olderMore) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'secondary earlier';
    button.textContent = 'Show earlier messages';
    button.addEventListener('click', showEarlier);
    root.append(button);
  }
  const all = [...older, ...room.messages];
  for (const message of all) {
    const block = document.createElement('article');
    const mine = message.role === 'user' && message.name === name();
    block.className = `message ${message.role}${message.role === 'user' && !mine ? ' other' : ''}`;
    if (own?.id != null && String(message.id) === own.id) {
      ownView = { block, message };
      fill(block, overlay(message));
    } else {
      fill(block, message);
    }
    root.append(block);
  }
  $('empty').hidden = all.length > 0;
  if (atEnd) scroller().scrollTop = scroller().scrollHeight;
}

// Redraws the reply being written, at most once a frame, without redrawing the rest of the room.
let queued = false;
function schedule() {
  if (queued) return;
  queued = true;
  requestAnimationFrame(() => {
    queued = false;
    if (!ownView?.block.isConnected || !own) { render(); return; }
    const atEnd = nearEnd();
    fill(ownView.block, overlay(ownView.message));
    if (atEnd) scroller().scrollTop = scroller().scrollHeight;
  });
}

async function poll() {
  if (document.hidden) return;
  try {
    const response = await fetch(`${API}/conversation?rev=${room.rev}`);
    if (response.status === 404) { closed(); return; }
    if (!response.ok) return;
    const next = await response.json();
    info.messages_left = next.messages_left;
    if (typeof next.now === 'number') skew = next.now * 1000 - Date.now();
    updateLeft();
    if (!next.unchanged) { room = next; render(); }
  } catch { /* the next poll tries again */ }
}

// -------------------------------------------------------------------- asking

// Reads one model round from the room: onDelta gets each delta ({content, reasoning_content, tool_calls}); the usage and
// timings chunks and the assembled tool calls come back at the end.
async function readRound(response, onDelta) {
  const result = { usage: null, timings: null, toolCalls: [] };
  const decoder = new TextDecoder();
  const body = response.body.getReader();
  let pending = '';
  const take = (line) => {
    if (!line.startsWith('data:')) return;
    const data = line.slice(5).trim();
    if (!data || data === '[DONE]') return;
    let chunk;
    try { chunk = JSON.parse(data); } catch { throw new Error('The server sent a reply that could not be read.'); }
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
  };
  for (;;) {
    const { value, done } = await body.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    let end;
    while ((end = pending.indexOf('\n')) >= 0) {
      const line = pending.slice(0, end).trim();
      pending = pending.slice(end + 1);
      take(line);
    }
  }
  take(pending.trim());
  result.toolCalls = result.toolCalls.filter(Boolean);
  return result;
}

// What the room is told of the steps: the monitor checks and clips them again.
function stepsForRoom(steps) {
  return steps.map(({ name: tool, label, state, via, code, excerpt, media, figures }) => ({
    name: tool, label, state, via: via ?? null, excerpt,
    ...(code ? { code } : {}), ...(media?.length ? { media } : {}), ...(figures?.length ? { figures } : {}),
  }));
}

// Shows the reply's steps to the room (they replace what it had); `abort` also ends the reply there.
async function publish(reply, abort = false) {
  const response = await fetch(`${API}/steps`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: abort,
    body: JSON.stringify({ reply: Number(reply.id), turn: reply.turn, steps: stepsForRoom(reply.steps), ...(abort ? { abort: true } : {}) }),
  });
  if (!response.ok) throw new Error(await failure(response));
}

function clipResult(text, max) {
  return text.length <= max ? text : `${text.slice(0, Math.max(0, max - 60))}\n[cut to fit what the room takes]`;
}

// Asks the room and writes the reply, round by round: each round streams from the monitor, and the tools the model calls
// run here. `reply` is filled as it goes; it is open in the room from the first round until a round ends without calls.
async function converse(reply, text) {
  const allowed = new Set(toolkit ? info.tools ?? [] : []);
  const schemas = allowed.size ? toolkit.schemas(allowed) : [];
  const toolNote = schemas.length ? toolkit.note(allowed, new Date().toISOString().slice(0, 10))?.content ?? '' : '';
  const offer = schemas.length ? { tools: schemas, tool_note: toolNote } : {};
  const note = [Lang.note(language), talk?.active ? talk.instruction : null].filter(Boolean).join(' ');
  const effort = () => (talk?.active ? 'none' : $('effort').value);
  const extra = [];  // the model's calls and their results, sent back with every round after the first
  const stepArgs = new Map();
  const sentAt = performance.now();
  let firstAt = null;
  let pieces = 0;
  const totals = { prompt: 0, cached: 0, completion: 0, decode: 0 };
  try {
    for (let round = 0; round < MAX_ROUNDS; round++) {
      const offered = schemas.length > 0 && round < MAX_ROUNDS - 1;
      const body = round === 0 ? { text, name: name(), note, ...offer, effort: effort() }
        : { reply: Number(reply.id), turn: reply.turn, extra, note, ...offer, effort: effort() };
      const response = await fetch(`${API}/chat`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal, body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(await failure(response));
      if (round === 0) {
        reply.id = response.headers.get('X-Room-Reply');
        reply.turn = response.headers.get('X-Room-Turn');
        reply.open = true;
        await poll();  // the visitor's own message is in the room by now
      }
      let separated = !reply.content;  // a blank line between the texts of two rounds
      let roundText = '';
      const { usage, timings, toolCalls } = await readRound(response, (delta) => {
        if (!delta.content && !delta.reasoning_content) return;
        firstAt ??= performance.now();
        pieces += 1;
        if (delta.content) {
          if (!separated) { reply.content += '\n\n'; separated = true; }
          reply.content += delta.content;
          roundText += delta.content;
          talk?.onReply(reply, false);
        }
        reply.reasoning += delta.reasoning_content || '';
        schedule();
      });
      if (usage) {
        totals.prompt += usage.prompt_tokens || 0;
        totals.cached += usage.prompt_tokens_details?.cached_tokens ?? usage.cached_tokens ?? 0;
        totals.completion += usage.completion_tokens || 0;
        const rate = usage.completion_tokens_per_second || timings?.predicted_per_second;
        if (rate > 0 && usage.completion_tokens) totals.decode += usage.completion_tokens / rate;
      }
      if (!offered || toolCalls.length === 0) { reply.open = false; break; }

      // The model asked for the browser: run its calls here, show them to the room, send back what they found. Only the
      // room's tools are taken, and no more calls than the room accepts back; a reply with none left is ended below.
      const calls = toolCalls.filter((call) => allowed.has(call.name)).slice(0, EXTRA_MESSAGES - extra.length - 1).map((call, index) => {
        call.id ||= `call_${round}_${index}`;
        let args = {};
        let problem = null;
        if ((call.arguments || '').length > ARGS_CHARS) problem = `Error: the arguments are longer than ${ARGS_CHARS} characters`;
        else try { args = JSON.parse(call.arguments || '{}'); } catch { problem = 'Error: the arguments are not valid JSON'; }
        const step = { name: call.name, label: toolkit.label(call.name, args, false), state: 'run', excerpt: '' };
        stepArgs.set(step, args);
        if (call.name === 'run_python' && typeof args.code === 'string') step.code = args.code.slice(0, 4000);
        reply.steps.push(step);
        return { call, args, problem, step };
      });
      if (!calls.length) break;
      extra.push({
        role: 'assistant', content: roundText || null,
        tool_calls: calls.map(({ call, problem }) => ({
          id: call.id, type: 'function', function: { name: call.name, arguments: problem ? '{}' : call.arguments || '{}' },
        })),
      });
      status('Using the browser…');
      schedule();
      await publish(reply);
      const results = await Promise.all(calls.map(async ({ call, args, problem, step }) => {
        const result = problem ? { ok: false, text: problem, via: null }
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
      const share = Math.floor((EXTRA_CHARS - JSON.stringify(extra).length - 1000) / calls.length);
      calls.forEach(({ call }, index) => {
        extra.push({ role: 'tool', tool_call_id: call.id, content: clipResult(results[index].text, Math.min(TOOL_CHARS, share)) });
      });
      await publish(reply);
      status('');
    }
  } finally {
    for (const step of reply.steps) {
      if (step.state !== 'run') continue;
      step.state = 'bad';
      step.label = `${toolkit.label(step.name, stepArgs.get(step), true)} (stopped)`;
    }
    if (totals.prompt || totals.completion) {
      const elapsed = firstAt ? (performance.now() - firstAt) / 1000 : 0;
      reply.stats = {
        tps: totals.decode > 0 ? totals.completion / totals.decode : (elapsed > 0 && pieces > 1 ? pieces / elapsed : null),
        ttft_ms: firstAt ? firstAt - sentAt : null,
        prompt_tokens: totals.prompt, cached_tokens: totals.cached, completion_tokens: totals.completion,
        calls: reply.steps.length || undefined,
      };
    }
  }
}

function setBusy(on) {
  busy = on;
  $('send').textContent = on ? 'Stop' : 'Send';
  refreshMood();
}

async function send(text, spoken = null) {
  text = text.trim();
  if (!text || busy) return;
  const command = Lang.parseCommand(text);
  if (command) { runCommand(command); return; }
  if (spoken) language = Lang.track(language, spoken, text);
  renderLanguage();
  $('prompt').value = '';
  autosize();
  const reply = { id: null, turn: null, open: false, content: '', reasoning: '', steps: [], stats: null };
  controller = new AbortController();
  setBusy(true);
  if (!(await loadInfo())) return;  // the tools the owner allows now; or the room is gone
  own = reply;
  try {
    await converse(reply, text);
  } catch (error) {
    if (error.name !== 'AbortError') status(error.message);
  }
  // Stopped, failed, or out of rounds with calls pending: the room is told the reply is over.
  if (reply.open) await publish(reply, true).catch(() => {});
  controller = null;
  setBusy(false);
  talk?.onReply(reply, true);
  await poll();
  own = null;
  render();
}

function runCommand(command) {
  if (command.error) { status(command.error); return; }
  if (command.show) {
    const label = Lang.label(language);
    status(label ? `You are assumed to be speaking ${label}.` : 'No language yet: it is taken from the first spoken message.');
    return;
  }
  language = command.auto ? Lang.EMPTY : Lang.declare(command.declared);
  $('prompt').value = '';
  renderLanguage();
  status(command.auto ? 'The language will be detected again.' : `You now speak ${Lang.label(language)}.`);
}

// ------------------------------------------------------------------------ speech

async function synthesize(text, lang) {
  const response = await fetch(`${API}/speak`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text, ...(lang ? { language: lang } : {}) }),
  });
  if (!response.ok) throw new Error(await failure(response));
  return response.arrayBuffer();
}

const playThroughAvatar = (context, node) => { if (avatar) avatar.attach(context, node); else node.connect(context.destination); };

function readAloud(message, button) {
  reader ??= window.GufoTalk.createReader({
    synthesize: (text) => synthesize(text, language.spoken),
    speed: () => info.speed || 1,
    attach: playThroughAvatar,
    error: status,
  });
  if (reading === message.id) { reader.stop(); return; }
  reading = message.id;
  button.classList.add('reading');
  button.replaceChildren(window.GufoIcons.svg('square'));
  reader.speak(message.content, () => {
    if (reading === message.id) reading = null;
    button.classList.remove('reading');
    button.replaceChildren(window.GufoIcons.svg('volume-2'));
  });
}

const TALK_LABEL = {
  off: 'Talk', starting: 'Starting… (tap to cancel)', listening: 'Listening… (tap to stop)', hearing: 'Hearing you…',
  transcribing: 'Understanding…', thinking: 'Thinking… (tap to interrupt)', speaking: 'Speaking… (tap to interrupt)', error: 'Talk',
};
$('talk').replaceChildren(window.GufoIcons.svg('mic'), 'Talk');

function talkController() {
  return window.GufoTalk.create({
    ask: (text) => { const spoken = heard && heard.text === text ? heard.language : null; heard = null; send(text, spoken); },
    stopReply: () => controller?.abort(),
    speed: () => info.speed || 1,
    transcribe: async (wav) => {
      const response = await fetch(`${API}/transcribe`, { method: 'POST', headers: { 'Content-Type': 'audio/wav' }, body: wav });
      if (!response.ok) throw new Error(await failure(response));
      const answer = await response.json();
      heard = { text: (answer.text ?? '').trim(), language: answer.language ?? null };
      return answer.text ?? '';
    },
    synthesize: (text) => synthesize(text, language.spoken),
    attach: playThroughAvatar,
    setState: (state, detail) => {
      talkState = state;
      refreshMood();
      const button = $('talk');
      button.replaceChildren(window.GufoIcons.svg(state === 'speaking' ? 'volume-2' : 'mic'), TALK_LABEL[state] ?? 'Talk');
      button.setAttribute('aria-pressed', String(state !== 'off'));
      button.classList.toggle('talking', state !== 'off');
      button.classList.toggle('secondary', state === 'off');
      if (state === 'error') status(detail);
      else if (state === 'off' || state === 'hearing' || state === 'listening') status('');
    },
  });
}

$('talk').addEventListener('click', async () => {
  talk ??= talkController();
  try {
    if (!talk.active) await talk.start();
    else if (!talk.interrupt()) await talk.stop();
  } catch (error) { status(error.message); }
});
window.addEventListener('pagehide', () => { talk?.stop(); reader?.stop(); });

// ------------------------------------------------------------------------ avatar

const refreshMood = () => avatar?.setMood(window.GufoAvatar.moodFor(talkState, busy));

function showAvatar(on) {
  $('avatar').hidden = !on;
  if (on && !avatar) { avatar = window.GufoAvatar.create($('avatar-face')); refreshMood(); }
  if (!on && avatar) { avatar.destroy(); avatar = null; }
}
$('avatar-close').append(window.GufoIcons.svg('x'));
$('avatar-close').addEventListener('click', () => { showAvatar(false); try { localStorage.setItem('gufo-share-avatar', 'off'); } catch { /* blocked */ } });

(() => {
  const pip = $('avatar');
  let drag = null;
  pip.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 || event.target.closest('button')) return;
    const box = pip.getBoundingClientRect();
    drag = { dx: event.clientX - box.left, dy: event.clientY - box.top };
    pip.setPointerCapture(event.pointerId);
    pip.classList.add('dragging');
    event.preventDefault();
  });
  pip.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const box = pip.getBoundingClientRect();
    const at = window.GufoAvatar.clampPosition(event.clientX - drag.dx, event.clientY - drag.dy, box.width, box.height, innerWidth, innerHeight);
    Object.assign(pip.style, { left: `${at.x}px`, top: `${at.y}px`, right: 'auto', bottom: 'auto' });
  });
  const stop = () => { drag = null; pip.classList.remove('dragging'); };
  pip.addEventListener('pointerup', stop);
  pip.addEventListener('pointercancel', stop);
})();

// ------------------------------------------------------------------------ page

function autosize() {
  const box = $('prompt');
  box.style.height = 'auto';
  box.style.height = `${Math.min(box.scrollHeight, 160)}px`;
}

function closed() {
  clearInterval(timer);
  clearInterval(infoTimer);
  document.querySelector('main').replaceChildren(Object.assign(document.createElement('p'), {
    className: 'closed', textContent: 'This link is not valid any more: it was switched off, has expired or is mistyped.',
  }));
}

// ---- the room's settings: its tools and thinking effort, chosen by its owner

// The tools the owner allows, shown and not changeable here.
function renderTools() {
  const ids = new Set(toolkit ? info.tools ?? [] : []);
  const shown = (window.GufoTools?.CATALOG ?? []).filter((tool) => ids.has(tool.id));
  $('tools').hidden = false;
  $('tools-count').textContent = shown.length ? String(shown.length) : '';
  $('tools-button').title = shown.length ? `Tools the model may use here: ${shown.map((tool) => tool.name).join(', ')}` : 'No tools are enabled in this room';
  const lead = document.createElement('div');
  lead.className = 'lead';
  lead.textContent = shown.length ? 'Chosen by the owner of this chat. They run in your browser when you ask.' : 'The owner of this chat has not enabled any tools here.';
  $('tools-menu').replaceChildren(lead, ...shown.map((tool) => {
    const option = document.createElement('label');
    option.className = 'tool-option fixed';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = true;
    box.disabled = true;
    const title = document.createElement('span');
    title.className = 'name';
    title.textContent = tool.name;
    const hint = document.createElement('span');
    hint.className = 'hint';
    hint.textContent = tool.hint;
    option.append(box, title, hint);
    return option;
  }));
}

function setToolsOpen(open) {
  $('tools-menu').hidden = !open;
  $('tools-button').setAttribute('aria-expanded', String(open));
}
$('tools-button').addEventListener('click', () => setToolsOpen($('tools-menu').hidden));
document.addEventListener('click', (event) => { if (!$('tools').contains(event.target)) setToolsOpen(false); });
$('tools').addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || $('tools-menu').hidden) return;
  setToolsOpen(false);
  $('tools-button').focus();
});
$('tools').addEventListener('focusout', (event) => {
  if (event.relatedTarget && !$('tools').contains(event.relatedTarget)) setToolsOpen(false);
});

// The visitor's thinking effort for their messages: what they chose in this tab, else the owner's.
function chosenEffort() {
  try { return sessionStorage.getItem(EFFORT_KEY); } catch { return null; }
}

function renderEffort() {
  const select = $('effort');
  const choices = Array.isArray(info.efforts) ? info.efforts.filter((value) => typeof value === 'string') : [];
  select.closest('label').hidden = !choices.length;
  if (document.activeElement === select) return;  // not while the visitor is choosing
  const saved = chosenEffort();
  const value = saved !== null && choices.includes(saved) ? saved : choices.includes(info.effort) ? info.effort : '';
  select.replaceChildren(...choices.map((choice) => Object.assign(document.createElement('option'), {
    value: choice, textContent: EFFORT_LABEL[choice] ?? choice,
  })));
  select.value = value;
}
$('effort').addEventListener('change', () => { try { sessionStorage.setItem(EFFORT_KEY, $('effort').value); } catch { /* blocked */ } });

function applyInfo(next) {
  info = next;
  document.title = info.title;
  $('share-title').textContent = info.title;
  updateLeft();
  renderTools();
  renderEffort();
}

// Reads the room's settings again (before each message, and now and then); false when the room is gone.
async function loadInfo() {
  let response;
  try { response = await fetch(API); } catch { return true; }
  if (response.status === 404) { closed(); return false; }
  if (response.ok) { try { applyInfo(await response.json()); } catch { /* keep what was read before */ } }
  return true;
}

$('composer').addEventListener('submit', (event) => {
  event.preventDefault();
  if (busy) { controller?.abort(); return; }
  send($('prompt').value);
});
$('prompt').addEventListener('input', autosize);
$('prompt').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); }
});

let timer = null;
let infoTimer = null;
(async () => {
  let response;
  try { response = await fetch(API); } catch { status('Could not reach the server.'); return; }
  if (!response.ok) { closed(); return; }
  applyInfo(await response.json());
  if (info.talk) {
    $('talk').hidden = false;
    if (localStorage.getItem('gufo-share-avatar') !== 'off') showAvatar(true);
  }
  await poll();
  timer = setInterval(poll, POLL_MS);
  infoTimer = setInterval(() => { if (!document.hidden && !busy) loadInfo(); }, INFO_MS);
})();
