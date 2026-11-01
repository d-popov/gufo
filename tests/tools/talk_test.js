'use strict';

// Run with node tests/tools/talk_test.js. What the chat's talk mode says aloud, when it decides
// an utterance has ended, and the WAV file it sends to speech recognition.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

// A microphone whose permission prompt the test answers when it chooses, and an audio context
// that records what was opened and closed.
const prompts = [];
const contexts = [];
const navigator = {
  mediaDevices: {
    getUserMedia: () => new Promise((resolve, reject) => prompts.push({ resolve, reject })),
  },
};
class AudioContext {
  constructor() { this.closed = false; this.sampleRate = 48000; this.destination = {}; contexts.push(this); }
  resume() { return this.resumeGate ?? Promise.resolve(); }
  close() { this.closed = true; return Promise.resolve(); }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() { return { connect() {}, disconnect() {} }; }
  createGain() { return { gain: {}, connect() {} }; }
}
const microphone = () => {
  const track = { stopped: false, stop() { this.stopped = true; } };
  return { track, getTracks: () => [track] };
};

const window = {};
vm.runInNewContext(fs.readFileSync('tools/monitor/static/talk.js', 'utf8'),
  { window, Float32Array, ArrayBuffer, DataView, navigator, AudioContext, setTimeout });
const { speakable, SentenceFeed, Vad, downsample, encodeWav } = window.GufoTalk;

// Markup is not read out: links keep their label, addresses, code and emoji go.
assert.equal(speakable('**Hello** there, see [the docs](https://example.com/x) or https://example.com/y now 🙂'),
  'Hello there, see the docs or now');
assert.equal(speakable('# Title\n- one\n- two\n1. three\n> quote'), 'Title one two three quote');
assert.equal(speakable('Before\n```js\nlet a = 1;\n```\nAfter `x` and snake_case_name.'),
  'Before After x and snake_case_name.');
assert.equal(speakable('| a | b |\n|---|---|\n| 1 | 2 |'), ', a , b , , 1 , 2 ,');
assert.equal(speakable('Тя каза *"да"* и __си тръгна__.'), 'Тя каза "да" и си тръгна.');

// Sentences come out once each, in order, as the reply grows; a short one waits for the next.
const feed = new SentenceFeed();
assert.deepEqual([...feed.feed('Hello th')], []);
assert.deepEqual([...feed.feed('Hello there, my friend. How ar')], ['Hello there, my friend.']);
assert.deepEqual([...feed.feed('Hello there, my friend. How are you doing today? Fine')], ['How are you doing today?']);
assert.deepEqual([...feed.feed('Hello there, my friend. How are you doing today? Fine, thanks', true)], ['Fine, thanks']);
assert.deepEqual([...feed.feed('Hello there, my friend. How are you doing today? Fine, thanks', true)], []);
const short = new SentenceFeed();
assert.deepEqual([...short.feed('OK. That works for me. Yes')], ['OK. That works for me.']);
// A code block is skipped even when it arrives in pieces, and a decimal point is no sentence end.
const code = new SentenceFeed();
const spoken = [];
for (const text of ['Here it is. ```py\nprint(1)', 'Here it is. ```py\nprint(1)\n```\nPi is 3.14159, roughly. Done', 'Here it is. ```py\nprint(1)\n```\nPi is 3.14159, roughly. Done.']) {
  spoken.push(...code.feed(text, text.endsWith('Done.')));
}
assert.deepEqual(spoken, ['Here it is. Pi is 3.14159, roughly.', 'Done.']);
// A very long unit is cut at a comma near the limit.
const long = new SentenceFeed().feed(`${'word, '.repeat(120)}end.`, true);
assert.ok(long.length > 1 && long.every((part) => part.length <= 321), long.map((p) => p.length).join());

// Voice activity: a pause ends an utterance, a click is dropped, steady noise is not speech.
const run = (vad, levels, ms = 85) => levels.map((rms) => vad.push(rms, ms));
let vad = new Vad();
const talk = [...Array(10).fill(0.002), ...Array(12).fill(0.08), ...Array(11).fill(0.002)];
const verdicts = run(vad, talk);
assert.equal(verdicts.indexOf('start'), 11);
assert.equal(verdicts.filter((v) => v === 'end').length, 1);
assert.equal(verdicts.at(-1), 'end');
vad = new Vad();
assert.ok(!run(vad, [...Array(10).fill(0.002), 0.08, ...Array(20).fill(0.002)]).includes('start'));  // one loud frame
vad = new Vad();
const click = run(vad, [...Array(10).fill(0.002), 0.08, 0.08, ...Array(12).fill(0.002)]);
assert.ok(click.includes('start') && click.includes('cancel') && !click.includes('end'));
vad = new Vad();
assert.ok(run(vad, Array(200).fill(0.009)).every((v) => v === 'idle'));  // below the floor of 0.012
// Speech never runs past the limit.
vad = new Vad({ maxMs: 2000 });
assert.ok(run(vad, Array(40).fill(0.08)).includes('end'));

// Down-sampling averages the input each output sample covers.
assert.deepEqual([...downsample(Float32Array.from([1, 1, 1, 3, 3, 3]), 48000, 16000)], [1, 3]);
assert.equal(downsample(new Float32Array(4410), 44100, 16000).length, 1600);
assert.equal(downsample(new Float32Array(10), 8000, 16000).length, 10);

// The WAV is a 16-bit mono PCM file at the given rate.
const wav = new DataView(encodeWav(Float32Array.from([0, 1, -1, 0.5]), 16000));
const tag = (at) => String.fromCharCode(...[0, 1, 2, 3].map((i) => wav.getUint8(at + i)));
assert.deepEqual([tag(0), tag(8), tag(12), tag(36)], ['RIFF', 'WAVE', 'fmt ', 'data']);
assert.equal(wav.getUint32(4, true), 36 + 8);
assert.equal(wav.getUint16(20, true), 1);
assert.equal(wav.getUint16(22, true), 1);
assert.equal(wav.getUint32(24, true), 16000);
assert.equal(wav.getUint16(34, true), 16);
assert.equal(wav.getUint32(40, true), 8);
assert.deepEqual([2, 3, 4, 5].map((i) => wav.getInt16(44 + (i - 2) * 2, true)), [0, 32767, -32768, 16383]);
assert.equal(wav.byteLength, 52);

// Starting waits on the microphone permission: a second tap or a stop meanwhile cancels it, and
// a microphone granted late is given back instead of left open.
(async () => {
  const states = [];
  const host = { ask() {}, stopReply() {}, transcribe: async () => '', synthesize: async () => new ArrayBuffer(0), setState: (s) => states.push(s) };
  const reset = () => { prompts.length = 0; contexts.length = 0; states.length = 0; };

  reset();
  let talk = window.GufoTalk.create(host);
  const first = talk.start();
  await talk.start();                                   // a double tap while the prompt is open
  assert.equal(prompts.length, 1);
  assert.equal(talk.state, 'starting');
  assert.equal(talk.active, true);
  await talk.stop();                                    // the second tap cancels
  assert.equal(talk.state, 'off');
  const late = microphone();
  prompts[0].resolve(late);
  await first;
  assert.equal(late.track.stopped, true);               // the late microphone is released
  assert.equal(contexts.length, 0);
  assert.equal(talk.state, 'off');
  assert.deepEqual(states, ['starting', 'off']);

  reset();                                              // a stop while the audio context resumes
  talk = window.GufoTalk.create(host);
  const second = talk.start();
  const held = microphone();
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  const original = AudioContext.prototype.resume;
  AudioContext.prototype.resume = function resume() { this.resumeGate = gate; return gate; };
  prompts[0].resolve(held);
  await new Promise((resolve) => setImmediate(resolve));
  await talk.stop();
  release();
  await second;
  AudioContext.prototype.resume = original;
  assert.equal(held.track.stopped, true);
  assert.equal(contexts.length, 1);
  assert.equal(contexts[0].closed, true);
  assert.equal(talk.state, 'off');

  reset();                                              // a start, then a stop, then a start again
  talk = window.GufoTalk.create(host);
  const third = talk.start();
  await talk.stop();
  const fourth = talk.start();
  const stale = microphone();
  const current = microphone();
  prompts[0].resolve(stale);
  prompts[1].resolve(current);
  await Promise.all([third, fourth]);
  assert.equal(stale.track.stopped, true);              // only the newest start keeps its microphone
  assert.equal(current.track.stopped, false);
  assert.equal(talk.state, 'listening');
  await talk.stop();
  assert.equal(current.track.stopped, true);
  assert.equal(contexts.filter((c) => !c.closed).length, 0);

  reset();                                              // a refused permission ends in off, with a reason
  talk = window.GufoTalk.create(host);
  const refused = talk.start();
  prompts[0].reject(Object.assign(new Error('denied'), { name: 'NotAllowedError' }));
  await assert.rejects(refused, /Microphone access was refused/);
  assert.equal(talk.state, 'off');

  console.log('talk ok');
})().catch((error) => { console.error(error); process.exit(1); });
