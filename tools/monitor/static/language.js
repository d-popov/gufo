'use strict';

// The language of a chat, kept per conversation. Speech recognition names the language of each spoken
// message; this decides what the conversation is in: the first language heard is assumed to continue
// (or English), and only a clearly different one changes it. The user can say what the chat uses with
// /lang, which beats detection. tests/tools/language_test.js runs this file as it is.
(() => {
  // State: { language, spoken, declared, pending }
  //   language  what the conversation is in (sticky); null before anything is known
  //   spoken    the language of the latest message, which the reply is most likely in (picks the voice)
  //   declared  the languages the user named with /lang; when present only those are assumed
  //   pending   a different language heard once, waiting for a second sign before it takes over
  const EMPTY = Object.freeze({ language: null, spoken: null, declared: [], pending: null });

  // A new language takes over on its second message in a row, or at once for a long message: a short
  // one ("да", "ok") is too little for the detector to be sure.
  const SWITCH_AFTER = 2;
  const LONG_MESSAGE_WORDS = 8;
  const SHORT_MESSAGE_WORDS = 2;

  const CODES = {
    bg: 'bulgarian', en: 'english', de: 'german', fr: 'french', es: 'spanish', it: 'italian',
    pt: 'portuguese', ru: 'russian', zh: 'chinese', ja: 'japanese', ko: 'korean', mk: 'macedonian',
    uk: 'ukrainian', pl: 'polish', nl: 'dutch', tr: 'turkish', ar: 'arabic', hi: 'hindi',
  };
  // Qwen3-ASR has no Bulgarian: it hears Bulgarian as Macedonian and the other way round. When the user
  // has named one of the pair, the other is read as it.
  const CONFUSED_WITH = { macedonian: 'bulgarian', bulgarian: 'macedonian' };

  const normalize = (name) => {
    const lower = String(name ?? '').trim().toLowerCase();
    return CODES[lower] ?? lower;
  };
  const words = (text) => String(text ?? '').split(/\s+/).filter(Boolean).length;
  const title = (name) => name.charAt(0).toUpperCase() + name.slice(1);

  function track(state, detected, text) {
    const language = normalize(detected);
    if (!language || words(text) < SHORT_MESSAGE_WORDS) return state;
    if (state.declared.length) {
      const named = state.declared.includes(language) ? language
        : state.declared.includes(CONFUSED_WITH[language]) ? CONFUSED_WITH[language] : null;
      // Something the user did not name is more likely a wrong guess than a new language.
      return named ? { ...state, language: named, spoken: named, pending: null } : state;
    }
    if (!state.language || language === state.language) {
      return { ...state, language: state.language ?? language, spoken: language, pending: null };
    }
    if (language === 'english') return { ...state, spoken: language, pending: null };
    const seen = state.pending?.language === language ? state.pending.count + 1 : 1;
    if (seen >= SWITCH_AFTER || words(text) >= LONG_MESSAGE_WORDS) {
      return { ...state, language, spoken: language, pending: null };
    }
    return { ...state, spoken: language, pending: { language, count: seen } };
  }

  // "/lang" shows the language; "/lang bulgarian english" names what the chat uses; "/lang auto" goes
  // back to detection. null when the text is not a /lang command.
  function parseCommand(text) {
    const match = /^\/lang(?:\s+(.*))?$/is.exec(String(text ?? '').trim());
    if (!match) return null;
    const rest = (match[1] ?? '').trim();
    if (!rest) return { show: true };
    if (/^(auto|off|detect)$/i.test(rest)) return { auto: true };
    const names = rest.split(/[\s,;/+]+/).map(normalize).filter((name) => name && name !== 'and');
    if (!names.length || names.some((name) => !/^\p{L}+$/u.test(name))) {
      return { error: 'Name the languages, for example /lang bulgarian english, or /lang auto.' };
    }
    return { declared: [...new Set(names)] };
  }

  function declare(names) {
    return { language: names[0], spoken: names[0], declared: names, pending: null };
  }

  // What the model is told, or null when nothing is known yet.
  function note(state) {
    if (!state.language) return null;
    const parts = [];
    if (state.declared.length) {
      parts.push(`The user said this conversation uses ${state.declared.map(title).join(' and ')}.`);
    } else {
      parts.push(`This conversation is in ${title(state.language)}; the user may also write English.`);
    }
    parts.push("Reply in the language of the user's latest message.");
    if (state.declared.includes('bulgarian') || state.language === 'bulgarian') {
      parts.push('Spoken messages are transcribed by a model that hears Bulgarian as Macedonian: read Macedonian '
        + 'spelling in them as Bulgarian.');
    }
    return parts.join(' ');
  }

  function label(state) {
    if (!state.language) return '';
    return `${title(state.language)} (${state.declared.length ? 'chosen' : 'detected'})`;
  }

  // The language with a voice of its own that a text is written in, judged by its letters and a few telltale words, or
  // null when that is not clear. Bulgarian is Cyrillic with the letter ъ or one of its own common words (това, ще,
  // много, какво…), with none of the letters only Russian, Ukrainian, Belarusian or Macedonian use and none of
  // Russian's own words (что, это, очень…). This picks the voice for a reply that was typed or whose language was never
  // heard, so a Bulgarian sentence is not read by an English voice.
  const BULGARIAN_WORDS = new Set(['това', 'съм', 'ще', 'много', 'какво', 'още', 'защото', 'който', 'която', 'което',
    'нещо', 'няма', 'тук', 'мога', 'може', 'които', 'какво', 'здравей', 'благодаря', 'моля', 'добре', 'разбира']);
  const RUSSIAN_WORDS = new Set(['что', 'это', 'очень', 'есть', 'был', 'была', 'было', 'быть', 'меня', 'тебя', 'нет',
    'потому', 'который', 'которая', 'если', 'когда', 'сегодня', 'хорошо', 'спасибо', 'пожалуйста', 'привет', 'можно']);
  function textLanguage(text) {
    text = String(text ?? '');
    const letters = text.match(/\p{L}/gu) ?? [];
    if (letters.length < 4) return null;
    const cyrillic = letters.filter((letter) => /\p{Script=Cyrillic}/u.test(letter)).length;
    if (cyrillic / letters.length < 0.6) return null;
    if (/[ыэёўіїєґѓќѕљњџ]/iu.test(text)) return null;
    const words = text.toLowerCase().match(/[\p{L}]+/gu) ?? [];
    if (words.some((word) => RUSSIAN_WORDS.has(word))) return null;
    return /ъ/iu.test(text) || words.some((word) => BULGARIAN_WORDS.has(word)) ? 'bulgarian' : null;
  }

  window.GufoLanguage = { EMPTY, track, parseCommand, declare, note, label, normalize, textLanguage };
})();
