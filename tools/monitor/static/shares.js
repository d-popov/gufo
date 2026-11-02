'use strict';

// The owner's list of shared chat links (GET /api/chat/shares): copy a link, revoke it, remove it once it is over.
window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});

const $ = (id) => document.getElementById(id);
const when = (seconds) => (seconds ? new Date(seconds * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }) : '—');
let armed = null;  // the revoke button waiting for its confirming second click

async function api(method, path) {
  const response = await fetch(path, { method });
  if (!response.ok) {
    let message = `${response.status}`;
    try { const body = await response.json(); message = body.error || message; } catch { /* not JSON */ }
    throw new Error(message);
  }
  return response.json();
}

function cell(text, className) {
  const td = document.createElement('td');
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function row(share) {
  const tr = document.createElement('tr');
  if (share.status !== 'active') tr.className = 'inactive';
  tr.append(cell(share.title + (share.has_seed ? ' (with the conversation)' : '') + (share.logged ? ' · messages kept' : '')), cell(when(share.created)),
    cell(share.expires ? when(share.expires) : 'never'), cell(`${share.messages} / ${share.max_messages}`),
    cell(`${share.audio} / ${share.max_audio}`), cell(share.last_used ? when(share.last_used) : 'never'),
    cell(share.status, `state ${share.status}`));
  const actions = document.createElement('td');
  actions.className = 'actions';
  if (share.status === 'active') {
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.className = 'secondary';
    copy.textContent = 'Copy link';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(share.url); copy.textContent = 'Copied'; } catch { window.prompt('Copy this link', share.url); }
      setTimeout(() => { copy.textContent = 'Copy link'; }, 1500);
    });
    actions.append(copy);
  }
  if (share.logged) {
    const read = document.createElement('button');
    read.type = 'button';
    read.className = 'secondary';
    read.textContent = 'Read';
    read.addEventListener('click', () => readConversation(share));
    actions.append(read);
  }
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = share.status === 'active' ? 'secondary danger' : 'secondary';
  remove.textContent = share.status === 'active' ? 'Revoke' : 'Remove';
  remove.addEventListener('click', async () => {
    if (share.status === 'active' && armed !== remove) {
      if (armed) armed.textContent = 'Revoke';
      armed = remove;
      remove.textContent = 'Really revoke?';
      return;
    }
    armed = null;
    try { await api('DELETE', `/api/chat/shares/${share.id}`); await load(); } catch (error) { $('note').textContent = error.message; }
  });
  actions.append(remove);
  tr.append(actions);
  return tr;
}

// What the visitors of a link said and were answered, for links made with "keep their messages".
async function readConversation(share) {
  try {
    const { messages } = await api('GET', `/api/chat/shares/${share.id}/messages`);
    $('transcript-title').textContent = `${share.title}: ${messages.length} message${messages.length === 1 ? '' : 's'}`;
    $('transcript-body').replaceChildren(...messages.map((message) => {
      const turn = document.createElement('p');
      turn.className = `turn ${message.role}`;
      const who = document.createElement('span');
      who.className = 'who';
      who.textContent = `${message.role === 'user' ? 'Visitor' : 'Model'} · ${when(message.at)}`;
      turn.append(who, document.createTextNode(message.content));
      return turn;
    }));
    if (!messages.length) $('transcript-body').textContent = 'Nothing has been said on this link yet.';
    $('transcript').hidden = false;
    $('transcript').scrollIntoView({ behavior: 'smooth' });
  } catch (error) { $('note').textContent = error.message; }
}
$('transcript-close').addEventListener('click', () => { $('transcript').hidden = true; });

async function load() {
  let data;
  try {
    const response = await fetch('/api/chat/shares');
    if (response.status === 401) { $('note').textContent = 'Shared chats are for the signed-in admin.'; return; }
    if (!response.ok) throw new Error(`${response.status}`);
    data = await response.json();
  } catch (error) { $('note').textContent = `Could not load the links: ${error.message}`; return; }
  $('note').textContent = data.public ? '' : 'GUFO_PUBLIC_URL is not set, so these links only work on this machine.';
  $('rows').replaceChildren(...data.shares.map(row));
  $('table').hidden = data.shares.length === 0;
  $('empty').hidden = data.shares.length !== 0;
}

load();
setInterval(() => { if (!armed) load(); }, 15000);
