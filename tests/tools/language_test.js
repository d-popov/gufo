'use strict';

// Run with node tests/tools/language_test.js. What a chat assumes its language is: the first one heard
// continues (or English), a clearly different one takes over, and /lang beats detection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const window = {};
vm.runInNewContext(fs.readFileSync('tools/monitor/static/language.js', 'utf8'), { window });
const { EMPTY, track, parseCommand, declare, note, label } = window.GufoLanguage;

// Objects from the module's own context differ in prototype; compare what they hold.
const plain = (value) => JSON.parse(JSON.stringify(value));
const hear = (state, ...heard) => heard.reduce((s, [language, text]) => track(s, language, text), state);
const SHORT = 'да моля';
const LONG = 'искам да ми обясниш как работи това нещо стъпка по стъпка';

// The first language heard is assumed; English in a chat of another language does not change it.
let state = hear(EMPTY, ['bulgarian', SHORT]);
assert.equal(state.language, 'bulgarian');
state = hear(state, ['english', 'what does that mean exactly']);
assert.equal(state.language, 'bulgarian', 'English is expected beside the chat language');
assert.equal(state.spoken, 'english', 'but the reply is likely English, so it is spoken in English');

// One short message in another language is not enough; a second in a row, or a long one, is.
state = hear(EMPTY, ['bulgarian', SHORT], ['german', 'ja bitte']);
assert.equal(state.language, 'bulgarian');
assert.equal(state.pending.language, 'german');
assert.equal(hear(state, ['german', 'noch einmal']).language, 'german');
assert.equal(hear(state, ['bulgarian', SHORT], ['german', 'ja bitte'], ['bulgarian', SHORT]).pending, null,
  'a message in the chat language ends the wait');
assert.equal(hear(EMPTY, ['bulgarian', SHORT], ['german', 'ich möchte das bitte noch einmal ganz genau erklärt bekommen']).language, 'german');

// A single word is too little to trust the detector.
assert.equal(hear(EMPTY, ['french', 'oui']), EMPTY);
assert.equal(hear(EMPTY, ['', 'two words']), EMPTY);

// What the user names is what the chat uses; a detected language outside it is a wrong guess,
// and Macedonian is how this speech recognizer hears Bulgarian.
state = declare(['bulgarian', 'english']);
assert.equal(label(state), 'Bulgarian (chosen)');
assert.equal(hear(state, ['macedonian', LONG]).language, 'bulgarian');
assert.equal(hear(state, ['macedonian', LONG]).spoken, 'bulgarian');
assert.equal(hear(state, ['english', 'and now in english please']).spoken, 'english');
assert.equal(hear(state, ['english', 'and now in english please']).language, 'english');
assert.equal(hear(state, ['german', 'ich möchte das bitte noch einmal erklärt bekommen']), state);
assert.equal(hear(declare(['german']), ['macedonian', LONG]).language, 'german', 'only a named pair is confused');

// /lang.
assert.equal(parseCommand('hello'), null);
assert.equal(parseCommand('/language'), null);
assert.deepEqual(plain(parseCommand('/lang')), { show: true });
assert.deepEqual(plain(parseCommand('/lang auto')), { auto: true });
assert.deepEqual(plain(parseCommand('/lang bg, en')), { declared: ['bulgarian', 'english'] });
assert.deepEqual(plain(parseCommand('/lang Bulgarian and English')), { declared: ['bulgarian', 'english'] });
assert.ok(parseCommand('/lang 42').error);

// What the model is told.
assert.equal(note(EMPTY), null);
assert.match(note(hear(EMPTY, ['english', 'good morning to you'])), /in English; the user may also write English/);
assert.match(note(declare(['bulgarian', 'english'])), /uses Bulgarian and English/);
assert.match(note(declare(['bulgarian'])), /Macedonian/);
assert.doesNotMatch(note(declare(['german'])), /Macedonian/);

console.log('language_test: ok');
