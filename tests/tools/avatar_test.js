'use strict';

// Run with node tests/tools/avatar_test.js. The avatar's mouth: shut in silence, open with loud sound, quick to
// open and slower to close, and always a valid outline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const window = {};
vm.runInNewContext(fs.readFileSync('tools/monitor/static/avatar.js', 'utf8'), { window, Math, Float32Array });
const { mouthOpening, mouthPath, rmsOf, blinkDelay } = window.GufoAvatar;

assert.equal(mouthOpening(0, 0), 0);
assert.equal(mouthOpening(0.01, 0), 0, 'room noise does not move it');
const speech = mouthOpening(0.3, 0);
assert.ok(speech > 0.5 && speech <= 1, 'loud sound opens it quickly');
assert.ok(mouthOpening(0.3, 1) <= 1);
assert.ok(mouthOpening(0, 1) > 0.5, 'it closes more slowly than it opens: one quiet frame leaves it mostly open');
let value = 1;
for (let i = 0; i < 30; i++) value = mouthOpening(0, value);
assert.ok(value < 0.001, 'and shuts in the end');

assert.ok(Math.abs(rmsOf([0.5, -0.5, 0.5, -0.5]) - 0.5) < 1e-9);
assert.equal(rmsOf([]), 0);

for (const open of [0, 0.3, 1]) assert.match(mouthPath(open), /^M40 76 Q60 [\d.]+ 80 76 Q60 [\d.]+ 40 76 Z$/);
const gap = (open) => { const [, top, bottom] = mouthPath(open).match(/Q60 ([\d.]+) 80 76 Q60 ([\d.]+)/); return bottom - top; };
assert.ok(gap(1) > gap(0) + 20, 'the opening grows with the opening');

assert.ok(blinkDelay(0) >= 2000 && blinkDelay(0.999) <= 7000);
console.log('avatar_test: ok');
