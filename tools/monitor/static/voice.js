'use strict';

// The voice settings page: which voice reads replies aloud and how fast, kept on the server per
// account (GET /api/chat/voice, PUT /api/chat/voice-prefs). The sample plays what is chosen,
// saved or not.
window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});

const $ = (id) => document.getElementById(id);
let reader = null;

function say(text, bad = false) {
  $('state').textContent = text;
  $('state').classList.toggle('bad', bad);
}

async function failure(response) {
  try {
    const body = await response.json();
    return typeof body.error === 'string' ? body.error : body.error?.message || `${response.status}`;
  } catch { return `${response.status}`; }
}

async function load() {
  const response = await fetch('/api/chat/voice');
  if (response.status === 401 || response.status === 404) {
    say(response.status === 401 ? 'Voice settings are for the signed-in admin.' : 'Talk mode is not set up on this server.', true);
    $('voice-form').querySelectorAll('select, textarea, button').forEach((node) => { node.disabled = true; });
    return;
  }
  if (!response.ok) throw new Error(await failure(response));
  const info = await response.json();
  $('voice').replaceChildren(...info.voices.map((name) => {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = name;
    return option;
  }));
  const wanted = info.prefs.voice ?? info.voice;
  if (info.voices.includes(wanted)) $('voice').value = wanted;
  // The option nearest the saved rate.
  const rates = [...$('speed').options].map((option) => Number(option.value));
  const nearest = rates.reduce((best, rate) => (Math.abs(rate - info.prefs.speed) < Math.abs(best - info.prefs.speed) ? rate : best));
  $('speed').value = String(nearest);
  $('avatar').checked = info.prefs.avatar !== false;
  $('reply-style').value = info.prefs.reply_style ?? '';
  $('models').textContent = [info.asr && `Listening: ${info.asr}`, info.tts && `Speaking: ${info.tts}`]
    .filter(Boolean).join(' · ') || 'The speech servers are not answering.';
  if (!info.voices.length) say('The speech server lists no voices.', true);
}

async function synthesize(text) {
  const response = await fetch('/api/chat/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, ...($('voice').value ? { voice: $('voice').value } : {}) }),
  });
  if (!response.ok) throw new Error(await failure(response));
  return response.arrayBuffer();
}

$('play').addEventListener('click', () => {
  reader ??= window.GufoTalk.createReader({
    synthesize,
    speed: () => Number($('speed').value) || 1,
    error: (message) => say(message, true),
  });
  if (reader.speaking) { reader.stop(); return; }
  const text = $('sample').value.trim();
  if (!text) { say('Type something to hear.', true); return; }
  say('');
  $('play').textContent = 'Stop';
  reader.speak(text, () => { $('play').textContent = 'Play sample'; });
});

$('voice-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('save').disabled = true;
  say('Saving…');
  try {
    const response = await fetch('/api/chat/voice-prefs', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ voice: $('voice').value || null, speed: Number($('speed').value), avatar: $('avatar').checked,
        reply_style: $('reply-style').value }),
    });
    if (!response.ok) throw new Error(await failure(response));
    say('Saved.');
  } catch (error) { say(error.message, true); }
  $('save').disabled = false;
});

window.addEventListener('pagehide', () => reader?.stop());
load().catch((error) => say(`Could not load the voice settings: ${error.message}`, true));
