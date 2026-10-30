'use strict';

// Run with node tests/tools/projects_test.js. What a project tells the model, what its memory
// update is asked to read, and how its two notes travel as one Markdown file.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const window = {};
vm.runInNewContext(fs.readFileSync('tools/monitor/static/projects.js', 'utf8'), { window });
const P = window.GufoProjects;

const project = { name: 'Atlas', guidelines: '- Answer in French.', memory: 'Stored note.' };

// The guidelines are always sent; the memory is the one the chat started with, not the project's
// current one, so a memory update never changes the start of a chat's prompt.
const text = P.context(project, 'Snapshot note.');
assert.match(text, /project "Atlas"/);
assert.match(text, /- Answer in French\./);
assert.match(text, /Snapshot note\./);
assert.doesNotMatch(text, /Stored note\./);
assert.equal(P.context({ name: 'A', guidelines: '  ', memory: '' }, ''), null);
assert.doesNotMatch(P.context({ name: 'A', guidelines: 'Rule.' }, ''), /memory/i);
assert.doesNotMatch(P.context({ name: 'A', guidelines: '' }, 'Fact.'), /guidelines/i);

// Project text and the tool note share one system message; empty parts leave no gap.
assert.equal(JSON.stringify(P.systemMessage('a', null, ' ', 'b')), JSON.stringify({ role: 'system', content: 'a\n\nb' }));
assert.equal(P.systemMessage(null, ''), null);

// The memory update reads only what it has not seen, skips failed and empty messages, and keeps
// the newest text when there is too much.
const messages = [
  { role: 'user', content: 'old question' },
  { role: 'assistant', content: 'old answer' },
  { role: 'user', content: 'new question' },
  { role: 'assistant', content: 'broken', error: true },
  { role: 'user', content: '', hadImages: true },
  { role: 'assistant', content: 'new answer' },
];
assert.equal(P.transcript(messages, 2), 'User: new question\n\nUser: [Image attached]\n\nAssistant: new answer');
const long = [{ role: 'user', content: 'x'.repeat(2000) }, { role: 'assistant', content: 'END' }];
const cut = P.transcript(long, 0, 500);
assert.ok(cut.startsWith('[earlier messages left out]'));
assert.ok(cut.endsWith('Assistant: END'));

// A chat needs a memory update only after an answer the memory has not seen, and never mid-reply.
const chat = { messages: [{ role: 'user', content: 'hi' }] };
assert.equal(P.unsummarized(chat), false);
chat.messages.push({ role: 'assistant', content: 'hello' });
assert.equal(P.unsummarized(chat), true);
chat.memoryCount = 2;
assert.equal(P.unsummarized(chat), false);
chat.messages.push({ role: 'user', content: 'more' }, { role: 'assistant', content: '', pending: true });
assert.equal(P.unsummarized(chat), false);
chat.messages.pop();
chat.messages.push({ role: 'assistant', content: 'again' });
assert.equal(P.unsummarized(chat), true);

const request = P.memoryRequest(project, { messages, memoryCount: 2 });
assert.equal(request[0].role, 'system');
assert.match(request[1].content, /Stored note\./);
assert.match(request[1].content, /new question/);
assert.doesNotMatch(request[1].content, /old question/);

// Whatever the model wraps its reply in, the stored note is plain Markdown within its limit.
assert.equal(P.clean('<think>plan</think>\n- one\n- two\n', 100), '- one\n- two');
assert.equal(P.clean('```markdown\n- one\n```', 100), '- one');
assert.equal(P.clean(null, 10), '');
const clipped = P.clean('line one\nline two\nline three', 20);
assert.equal(clipped, 'line one\nline two');

// One file holds both notes. Importing a file that has one section leaves the other alone, and
// the notes may use any heading of their own.
const file = P.toMarkdown({ guidelines: '## Style\n- Short.', memory: 'Fact.' });
assert.equal(JSON.stringify(P.fromMarkdown(file)), JSON.stringify({ guidelines: '## Style\n- Short.', memory: 'Fact.' }));
assert.equal(JSON.stringify(P.fromMarkdown('# Memory\r\nOnly this.\r\n')), JSON.stringify({ memory: 'Only this.' }));
assert.equal(P.fromMarkdown('# Notes\nnothing here'), null);

console.log('projects_test: ok');
