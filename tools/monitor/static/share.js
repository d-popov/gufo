'use strict';

// The page of a shared chat (/s/TOKEN): one conversation that everyone with the link sees and writes in. The monitor keeps it;
// this page polls it a little over once a second, posts what the visitor types or says, and reads the model's reply as it is
// written (the asker's own connection streams it, which is what feeds Talk). The secret in the URL is the only credential.
const $ = (id) => document.getElementById(id);
const TOKEN = location.pathname.split('/')[2] ?? '';
const API = `/api/share/${encodeURIComponent(TOKEN)}`;
const POLL_MS = 1500;
const Lang = window.GufoLanguage;

let info = null;
let room = { rev: -1, messages: [] };
let language = Lang.EMPTY;  // what this visitor's chat is in, from what they say
let heard = null;           // what speech recognition returned last: { text, language }
let own = null;             // the reply being streamed to this page: { content } while it is
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
  if (message.role === 'assistant' && message.content) {
    const body = document.createElement('div');
    body.className = 'md';
    body.append(window.GufoMarkdown.render(message.content));
    block.append(body);
  } else {
    block.append(document.createTextNode(message.content || (message.done === false ? '…' : '')));
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
}

function render() {
  const root = $('messages');
  const scroller = root.parentElement;
  const atEnd = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
  root.replaceChildren();
  room.messages.forEach((message, index) => {
    // While this page streams its own reply, the text it has is newer than the room's copy.
    const shown = own && index === room.messages.length - 1 && message.role === 'assistant' && message.done === false
      ? { ...message, content: own.content } : message;
    const block = document.createElement('article');
    const mine = message.role === 'user' && message.name === name();
    block.className = `message ${message.role}${message.role === 'user' && !mine ? ' other' : ''}`;
    fill(block, shown);
    root.append(block);
  });
  $('empty').hidden = room.messages.length > 0;
  if (atEnd) scroller.scrollTop = scroller.scrollHeight;
}

async function poll() {
  if (document.hidden) return;
  try {
    const response = await fetch(`${API}/conversation?rev=${room.rev}`);
    if (response.status === 404) { closed(); return; }
    if (!response.ok) return;
    const next = await response.json();
    info.messages_left = next.messages_left;
    updateLeft();
    if (!next.unchanged) { room = next; render(); }
  } catch { /* the next poll tries again */ }
}

// -------------------------------------------------------------------- asking

async function streamReply(reply, text, spoken) {
  controller = new AbortController();
  const note = [Lang.note(language), talk?.active ? talk.instruction : null].filter(Boolean).join(' ');
  const response = await fetch(`${API}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
    body: JSON.stringify({ text, name: name(), note }),
  });
  if (!response.ok) throw new Error(await failure(response));
  own = reply;
  await poll();  // the visitor's own message is in the room by now
  const decoder = new TextDecoder();
  const body = response.body.getReader();
  let pending = '';
  for (;;) {
    const { value, done } = await body.read();
    if (done) break;
    pending += decoder.decode(value, { stream: true });
    let end;
    while ((end = pending.indexOf('\n')) >= 0) {
      const line = pending.slice(0, end).trim();
      pending = pending.slice(end + 1);
      if (!line.startsWith('data:') || line === 'data: [DONE]') continue;
      let event;
      try { event = JSON.parse(line.slice(5)); } catch { continue; }
      const piece = event.choices?.[0]?.delta?.content;
      if (piece) { reply.content += piece; talk?.onReply(reply, false); render(); }
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
  const reply = { content: '' };
  setBusy(true);
  try {
    await streamReply(reply, text, spoken);
  } catch (error) {
    if (error.name !== 'AbortError') status(error.message);
  }
  own = null;
  controller = null;
  setBusy(false);
  talk?.onReply(reply, true);
  await poll();
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
  document.querySelector('main').replaceChildren(Object.assign(document.createElement('p'), {
    className: 'closed', textContent: 'This link is not valid any more: it was switched off, has expired or is mistyped.',
  }));
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
(async () => {
  let response;
  try { response = await fetch(API); } catch { status('Could not reach the server.'); return; }
  if (!response.ok) { closed(); return; }
  info = await response.json();
  document.title = info.title;
  $('share-title').textContent = info.title;
  updateLeft();
  if (info.talk) {
    $('talk').hidden = false;
    if (localStorage.getItem('gufo-share-avatar') !== 'off') showAvatar(true);
  }
  await poll();
  timer = setInterval(poll, POLL_MS);
})();
