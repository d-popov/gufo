'use strict';

// The page of a shared chat link (/s/TOKEN): a lean chat for a visitor who has no account. It keeps the conversation in
// this page only, talks to /api/share/TOKEN/* (the secret in the URL is the only credential) and shows what the owner
// allowed: the model, the voice, and how many messages and speech requests are left.
const $ = (id) => document.getElementById(id);
const TOKEN = location.pathname.split('/')[2] ?? '';
const API = `/api/share/${encodeURIComponent(TOKEN)}`;
const Lang = window.GufoLanguage;

let info = null;
let messages = [];          // { role, content, language? }
let language = Lang.EMPTY;  // what the chat is in, from what is heard
let heard = null;           // what speech recognition returned last: { text, language }
let busy = false;
let controller = null;
let talk = null;
let reader = null;
let reading = null;
let avatar = null;
let talkState = 'off';

const status = (text) => { $('status').textContent = text; };

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

function fill(block, message) {
  block.replaceChildren();
  if (message.role === 'assistant' && message.content && !message.error && !message.pending) {
    const body = document.createElement('div');
    body.className = 'md';
    body.append(window.GufoMarkdown.render(message.content));
    block.append(body);
  } else {
    block.append(document.createTextNode(message.content || (message.pending ? '…' : '')));
  }
  if (message.role === 'user' && message.language) {
    const tag = document.createElement('div');
    tag.className = 'tag';
    tag.textContent = `Heard as ${message.language[0].toUpperCase()}${message.language.slice(1)}`;
    block.append(tag);
  }
  if (info?.talk && message.role === 'assistant' && !message.error && !message.pending && message.content) {
    const speak = document.createElement('button');
    speak.type = 'button';
    speak.className = 'speak secondary';
    speak.setAttribute('aria-label', 'Read this reply aloud');
    speak.title = 'Read aloud';
    speak.append(window.GufoIcons.svg(reading === message ? 'square' : 'volume-2'));
    speak.addEventListener('click', () => readAloud(message, speak));
    block.append(speak);
  }
}

function render() {
  const root = $('messages');
  root.replaceChildren();
  for (const message of messages) {
    const block = document.createElement('article');
    block.className = `message ${message.role}${message.error ? ' error' : ''}`;
    fill(block, message);
    root.append(block);
  }
  $('empty').hidden = messages.length > 0;
  root.parentElement.scrollTop = root.parentElement.scrollHeight;
}

// -------------------------------------------------------------------- the model

async function streamReply(reply) {
  controller = new AbortController();
  const note = [Lang.note(language), talk?.active ? talk.instruction : null].filter(Boolean).join(' ');
  const response = await fetch(`${API}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, signal: controller.signal,
    body: JSON.stringify({ messages: messages.filter((m) => !m.error && !m.pending).map(({ role, content }) => ({ role, content })), note }),
  });
  if (!response.ok) throw new Error(await failure(response));
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
      if (piece) {
        reply.content += piece;
        talk?.onReply(reply, false);
        paint(reply);
      }
    }
  }
}

function paint(reply) {
  const blocks = $('messages').children;
  const block = blocks[messages.indexOf(reply)];
  if (block) fill(block, reply);
  $('messages').parentElement.scrollTop = $('messages').parentElement.scrollHeight;
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
  messages.push({ role: 'user', content: text, ...(spoken ? { language: spoken } : {}) });
  const reply = { role: 'assistant', content: '', pending: true, ...(language.spoken ? { language: language.spoken } : {}) };
  messages.push(reply);
  $('prompt').value = '';
  autosize();
  render();
  setBusy(true);
  info.messages_left = Math.max(0, info.messages_left - 1);
  updateLeft();
  try {
    await streamReply(reply);
    reply.pending = false;
    if (!reply.content) { reply.content = 'No answer came back.'; reply.error = true; }
  } catch (error) {
    reply.pending = false;
    if (error.name === 'AbortError') {
      if (!reply.content) messages.splice(messages.indexOf(reply), 1);
    } else {
      reply.error = true;
      reply.content = reply.content || error.message;
      await refreshInfo();
    }
  }
  controller = null;
  render();
  setBusy(false);
  talk?.onReply(reply, true);
}

function runCommand(command) {
  if (command.error) { status(command.error); return; }
  if (command.show) {
    const label = Lang.label(language);
    status(label ? `This chat is assumed to be in ${label}.` : 'No language yet: it is taken from the first spoken message.');
    return;
  }
  language = command.auto ? Lang.EMPTY : Lang.declare(command.declared);
  $('prompt').value = '';
  renderLanguage();
  status(command.auto ? 'The language will be detected again.' : `This chat now uses ${Lang.label(language)}.`);
}

async function refreshInfo() {
  try {
    const response = await fetch(API);
    if (response.ok) { info = { ...info, ...(await response.json()) }; updateLeft(); } else if (response.status === 404) closed();
  } catch { /* the page keeps working with what it knows */ }
}

// ------------------------------------------------------------------------ speech

const speakBody = (text, lang) => JSON.stringify({ text, ...(lang ? { language: lang } : {}) });

async function synthesize(text, spoken) {
  const lang = Lang.textLanguage(text) ?? spoken;
  const response = await fetch(`${API}/speak`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: speakBody(text, lang) });
  if (!response.ok) throw new Error(await failure(response));
  return response.arrayBuffer();
}

const playThroughAvatar = (context, node) => { if (avatar) avatar.attach(context, node); else node.connect(context.destination); };

function readAloud(message, button) {
  reader ??= window.GufoTalk.createReader({
    synthesize: (text) => synthesize(text, reading?.language),
    speed: () => info.speed || 1,
    attach: playThroughAvatar,
    error: status,
  });
  if (reading === message) { reader.stop(); return; }
  reading = message;
  button.classList.add('reading');
  button.replaceChildren(window.GufoIcons.svg('square'));
  reader.speak(message.content, () => {
    if (reading === message) reading = null;
    button.classList.remove('reading');
    button.replaceChildren(window.GufoIcons.svg('volume-2'));
  });
}

const TALK_LABEL = {
  off: 'Talk', starting: 'Starting… (tap to cancel)', listening: 'Listening… (tap to stop)', hearing: 'Hearing you…',
  transcribing: 'Understanding…', thinking: 'Thinking… (tap to interrupt)', speaking: 'Speaking… (tap to interrupt)', error: 'Talk',
};

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
      button.textContent = TALK_LABEL[state] ?? 'Talk';
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

(async () => {
  let response;
  try { response = await fetch(API); } catch { status('Could not reach the server.'); return; }
  if (!response.ok) { closed(); return; }
  info = await response.json();
  document.title = info.title;
  $('share-title').textContent = info.title;
  messages = info.seed.map((m) => ({ role: m.role, content: m.content }));
  updateLeft();
  render();
  if (info.talk) {
    $('talk').hidden = false;
    if (localStorage.getItem('gufo-share-avatar') !== 'off') showAvatar(true);
  }
})();
