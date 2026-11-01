'use strict';

// Run with node tests/tools/markdown_test.js. What the chat's Markdown turns a reply into:
// the supported syntax, what stays plain text, and that nothing a model writes becomes markup.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const window = {};
vm.runInNewContext(fs.readFileSync('tools/monitor/static/markdown.js', 'utf8'), { window, URL });
const { parse, render } = window.GufoMarkdown;

// A minimal DOM, enough to print what render() builds.
const escape = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; }
  append(...kids) { this.children.push(...kids); }
  set className(v) { this.attrs.class = v; }
  set href(v) { this.attrs.href = v; }
  set target(v) { this.attrs.target = v; }
  set rel(v) { this.attrs.rel = v; }
  set start(v) { this.attrs.start = v; }
  set textContent(v) { this.children = [{ text: v }]; }
  html() {
    if (this.tag === 'fragment') return this.children.map((c) => c.html?.() ?? escape(c.text)).join('');
    const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${escape(String(v))}"`).join('');
    if (this.tag === 'br' || this.tag === 'hr') return `<${this.tag}${attrs}>`;
    return `<${this.tag}${attrs}>${this.children.map((c) => c.html?.() ?? escape(c.text)).join('')}</${this.tag}>`;
  }
}
const doc = {
  createElement: (tag) => new Node(tag),
  createTextNode: (text) => ({ text }),
  createDocumentFragment: () => new Node('fragment'),
};
const html = (text) => render(text, doc).html();

// The reply this was written for: bold with Cyrillic text, italics, inline links, a heading.
const reply = `- **Половината време нека е видео/музика**, не дискусия. Кратко видео от **tv.jw.org** (JW Broadcasting) + 2 въпроса.
- Запази **свое privately** изучаване — **Притчи 4:23**.

### Източници (на български)
- „Идеи" — *Стражева кула*, януари 2024: [jw.org.bg](https://www.jw.org/bg/библиотека/списания/w20140315/Идеи-за-лично-изучаване/)`;
const out = html(reply);
assert.match(out, /<ul><li><strong>Половината време нека е видео\/музика<\/strong>, не дискусия\./);
assert.match(out, /<strong>tv\.jw\.org<\/strong> \(JW Broadcasting\) \+ 2/);
assert.match(out, /<strong>Притчи 4:23<\/strong>\.<\/li>/);
assert.match(out, /<h3>Източници \(на български\)<\/h3>/);
assert.match(out, /<em>Стражева кула<\/em>, януари 2024: <a href="https:\/\/www\.jw\.org\/bg\/[^"]+" target="_blank" rel="noopener noreferrer">jw\.org\.bg<\/a>/);
assert.doesNotMatch(out, /\*\*/);

// Emphasis: nesting, the star/underscore difference, and text that only looks like markup.
assert.equal(html('***both*** and **a *b***'), '<p><em><strong>both</strong></em> and <strong>a <em>b</em></strong></p>');
assert.equal(html('snake_case_name and 2 * 3 * 4 and ~~gone~~'), '<p>snake_case_name and 2 * 3 * 4 and <del>gone</del></p>');
assert.equal(html('**unclosed and `code **x** `'), '<p>**unclosed and <code>code **x** </code></p>');
assert.equal(html('_one_ and a\\*b\\*'), '<p><em>one</em> and a*b*</p>');
assert.equal(html('a\nb'), '<p>a<br>b</p>');

// Nothing a model writes becomes markup or a script-capable link.
assert.equal(html('<img src=x onerror=alert(1)> **b**'), '<p>&lt;img src=x onerror=alert(1)&gt; <strong>b</strong></p>');
assert.equal(html('[x](javascript:alert(1)) [y](data:text/html,hi)'), '<p>[x](javascript:alert(1)) [y](data:text/html,hi)</p>');
assert.equal(html('![pic](https://example.com/a.png)'),
  '<p><a href="https://example.com/a.png" target="_blank" rel="noopener noreferrer">pic</a></p>');

// Links: balanced parentheses stay in the address; a bare address loses the sentence's punctuation.
assert.match(html('[w](https://en.wikipedia.org/wiki/A_(b))!'), /href="https:\/\/en\.wikipedia\.org\/wiki\/A_\(b\)"/);
assert.match(html('See https://example.com/x, or (https://example.com/y).'),
  /<a href="https:\/\/example\.com\/x"[^>]*>https:\/\/example\.com\/x<\/a>, or \(<a href="https:\/\/example\.com\/y"[^>]*>[^<]*<\/a>\)\./);

// Blocks.
assert.equal(html('# One\n\n---\n\n> quoted\n> **text**'), '<h1>One</h1><hr><blockquote><p>quoted<br><strong>text</strong></p></blockquote>');
assert.equal(html('#hashtag stays'), '<p>#hashtag stays</p>');
assert.equal(html('```js\nlet a = "<b>";\n  x\n```'), '<pre><code class="language-js">let a = "&lt;b&gt;";\n  x</code></pre>');
// A fence still being streamed has no closing line yet.
assert.equal(html('text\n```\nopen **not bold**'), '<p>text</p><pre><code>open **not bold**</code></pre>');

// Lists: ordered start, nesting, a wrapped line, a list straight after a paragraph.
assert.equal(html('Steps:\n3. a\n4. b'), '<p>Steps:</p><ol start="3"><li>a</li><li>b</li></ol>');
assert.equal(html('- a\n  - b\n  - c\n- d wrapped\nmore'),
  '<ul><li><p>a</p><ul><li>b</li><li>c</li></ul></li><li>d wrapped<br>more</li></ul>');
assert.equal(html('1. a\n   - x\n\n2. b'), '<ol><li><p>a</p><ul><li>x</li></ul></li><li>b</li></ol>');
assert.equal(html('- a\n\ntext'), '<ul><li>a</li></ul><p>text</p>');
assert.equal(html('**bold** line\n* * *'), '<p><strong>bold</strong> line</p><hr>');

// Tables: alignment, escaped pipes, short rows.
assert.equal(html('| A | B |\n|:--|--:|\n| 1 \\| 2 | **3** |\n| 4 |'),
  '<div class="table"><table><thead><tr><th class="al-left">A</th><th class="al-right">B</th></tr></thead>'
  + '<tbody><tr><td class="al-left">1 | 2</td><td class="al-right"><strong>3</strong></td></tr>'
  + '<tr><td class="al-left">4</td><td class="al-right"></td></tr></tbody></table></div>');
assert.equal(html('a | b\nplain'), '<p>a | b<br>plain</p>');

// The parse is data: empty and odd input never throws.
assert.equal(parse('').length, 0);
assert.equal(parse(null).length, 0);
for (const text of ['**', '[', '[]()', '![', '`', '```', '>', '-', '1.', '|', '|-|', '\\', '#', '* *', '_ _']) {
  assert.doesNotThrow(() => html(text), text);
}

console.log('markdown ok');
