'use strict';

// The voice settings page: which voice reads replies aloud and how fast, kept on the server per
// account (GET /api/chat/voice, PUT /api/chat/voice-prefs). The sample plays what is chosen,
// saved or not.
window.GufoHeader.init();
window.GufoHeader.refresh().catch(() => {});

const $ = (id) => document.getElementById(id);
let reader = null;
let voices = [];     // [{id, language}]: the voices the speech servers offer, with the language each is native to
let languages = [];  // the languages a voice can be chosen for

const title = (name) => name.charAt(0).toUpperCase() + name.slice(1);

// A voice picker, its voices grouped by native language (the first group is the language most voices are for).
function fillVoices(select, none) {
  const groups = new Map();
  for (const voice of voices) {
    if (!groups.has(voice.language)) groups.set(voice.language, []);
    groups.get(voice.language).push(voice.id);
  }
  const nodes = none ? [new Option(none, '')] : [];
  for (const [language, names] of groups) {
    const group = document.createElement('optgroup');
    group.label = title(language);
    group.append(...names.map((name) => new Option(name, name)));
    nodes.push(group);
  }
  select.replaceChildren(...nodes);
}

// What each language is set to; a language left at "Same as the default" is left out.
function languageVoices() {
  return Object.fromEntries([...$('language-voices').querySelectorAll('select')]
    .filter((select) => select.value).map((select) => [select.dataset.language, select.value]));
}

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
  voices = info.voices;
  languages = info.languages;
  const saved = info.prefs.language_voices ?? {};
  fillVoices($('voice'), null);
  const wanted = info.prefs.voice ?? info.voice;
  if (voices.some((v) => v.id === wanted)) $('voice').value = wanted;
  $('language-voices').replaceChildren(...languages.map((language) => {
    const label = document.createElement('label');
    label.textContent = title(language);
    const select = document.createElement('select');
    select.dataset.language = language;
    fillVoices(select, 'Same as the default');
    if (voices.some((v) => v.id === saved[language])) select.value = saved[language];
    label.append(select);
    return label;
  }));
  $('sample-language').replaceChildren(...languages.map((language) => new Option(title(language), language)));
  $('sample-language').value = 'english';
  // The option nearest the saved rate.
  const rates = [...$('speed').options].map((option) => Number(option.value));
  const nearest = rates.reduce((best, rate) => (Math.abs(rate - info.prefs.speed) < Math.abs(best - info.prefs.speed) ? rate : best));
  $('speed').value = String(nearest);
  $('avatar').checked = info.prefs.avatar !== false;
  $('reply-style').value = info.prefs.reply_style ?? '';
  $('models').textContent = [info.asr && `Listening: ${info.asr}`, info.tts && `Speaking: ${info.tts}`]
    .filter(Boolean).join(' · ') || 'The speech servers are not answering.';
  if (!voices.length) say('The speech server lists no voices.', true);
}

async function synthesize(text) {
  const response = await fetch('/api/chat/speak', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, language: $('sample-language').value,
      voice: languageVoices()[$('sample-language').value] || $('voice').value || undefined }),
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
      body: JSON.stringify({ voice: $('voice').value || null, language_voices: languageVoices(), speed: Number($('speed').value), avatar: $('avatar').checked,
        reply_style: $('reply-style').value }),
    });
    if (!response.ok) throw new Error(await failure(response));
    say('Saved.');
  } catch (error) { say(error.message, true); }
  $('save').disabled = false;
});

window.addEventListener('pagehide', () => reader?.stop());
load().catch((error) => say(`Could not load the voice settings: ${error.message}`, true));
