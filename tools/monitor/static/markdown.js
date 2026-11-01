'use strict';

// Markdown for the chat's replies: headings, emphasis, code, links, lists, quotes and tables.
// parse() turns text into plain data and render() builds DOM from it with createElement and
// textContent only, so the model's text never becomes markup (the page's CSP allows no inline
// script or style anyway). tests/tools/markdown_test.js runs this file as it is.
(() => {
  const ESCAPABLE = '\\`*_{}[]()#+-.!|~<>"\'';
  const WORD = /[\p{L}\p{N}]/u;
  const SAFE_LINK = new Set(['http:', 'https:', 'mailto:']);

  const isWord = (ch) => ch !== undefined && WORD.test(ch);
  const isSpace = (ch) => ch === undefined || /\s/.test(ch);

  // ------------------------------------------------------------ inline

  // The address a link may point at: http(s) and mailto only, never javascript: or data:.
  function safeHref(raw) {
    try {
      const url = new URL(raw.trim());
      return SAFE_LINK.has(url.protocol) ? url.href : null;
    } catch { return null; }
  }

  // The end of the inline code span opened by the backtick run at I, or -1.
  function codeEnd(s, i) {
    let n = 0;
    while (s[i + n] === '`') n++;
    for (let j = i + n; j < s.length; j++) {
      if (s[j] !== '`') continue;
      let m = 0;
      while (s[j + m] === '`') m++;
      if (m === n) return j;
      j += m - 1;
    }
    return -1;
  }

  // The index of the bracket closing the one at I, or -1. Code spans and escapes are skipped.
  function bracketEnd(s, i) {
    let depth = 0;
    for (let j = i; j < s.length; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === '`') {
        let n = 0;
        while (s[j + n] === '`') n++;
        const end = codeEnd(s, j);
        j = end > 0 ? end + n - 1 : j + n - 1;
        continue;
      }
      if (s[j] === '[') depth++;
      else if (s[j] === ']' && --depth === 0) return j;
    }
    return -1;
  }

  // "[label](address "title")": the address and where the link ends, or null.
  function linkTarget(s, i) {
    if (s[i] !== '(') return null;
    let depth = 0;
    for (let j = i; j < s.length; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === '(') depth++;
      else if (s[j] === ')' && --depth === 0) {
        const inner = s.slice(i + 1, j).trim().replace(/^<(.*)>$/, '$1');
        const href = /^(\S*)(?:\s+(?:"[^"]*"|'[^']*'))?$/.exec(inner)?.[1];
        return href ? { href, end: j + 1 } : null;
      }
    }
    return null;
  }

  const MARKS = [
    ['***', ['em', 'strong']], ['**', ['strong']], ['__', ['strong']],
    ['~~', ['del']], ['*', ['em']], ['_', ['em']],
  ];

  // Where the emphasis opened by MARK at I closes, or -1. The closer follows a non-space; an
  // underscore closer does not sit inside a word; a longer run of the same character closes
  // with its last characters so "**a *b***" nests.
  function emphasisEnd(s, i, mark) {
    const ch = mark[0];
    for (let j = i + mark.length; j < s.length; j++) {
      if (s[j] === '\\') { j++; continue; }
      if (s[j] === '`') {
        const end = codeEnd(s, j);
        if (end > 0) { let m = 0; while (s[j + m] === '`') m++; j = end + m - 1; }
        else while (s[j + 1] === '`') j++;
        continue;
      }
      if (s[j] !== ch) continue;
      let run = 0;
      while (s[j + run] === ch) run++;
      const closer = run >= mark.length && mark.length > 1 ? j + run - mark.length : j;
      const eligible = mark.length > 1 ? run >= mark.length : run === 1;
      if (eligible && closer > i + mark.length - 1 && !isSpace(s[closer - 1])
          && !(ch === '_' && isWord(s[closer + mark.length]))) return closer;
      j += run - 1;
    }
    return -1;
  }

  // A bare http(s) address at I and where it ends: trailing punctuation and an unmatched ")"
  // belong to the sentence, not the address.
  function bareUrl(s, i) {
    const match = /^https?:\/\/[^\s<]+/.exec(s.slice(i));
    if (!match) return null;
    let url = match[0];
    for (;;) {
      const last = url[url.length - 1];
      const open = (url.match(/\(/g) || []).length;
      const close = (url.match(/\)/g) || []).length;
      if (/[.,;:!?*_~'"]/.test(last) || (last === ')' && close > open)) url = url.slice(0, -1);
      else break;
    }
    return url.length > 8 && safeHref(url) ? url : null;
  }

  function inline(s) {
    const out = [];
    let text = '';
    const flush = () => { if (text) { out.push({ t: 'text', v: text }); text = ''; } };
    let i = 0;
    while (i < s.length) {
      const ch = s[i];
      if (ch === '\\' && i + 1 < s.length) {
        if (ESCAPABLE.includes(s[i + 1])) { text += s[i + 1]; i += 2; continue; }
        if (s[i + 1] === '\n') { flush(); out.push({ t: 'br' }); i += 2; continue; }
      }
      if (ch === '\n') {
        text = text.replace(/ +$/, '');
        flush();
        out.push({ t: 'br' });
        i++;
        while (s[i] === ' ') i++;
        continue;
      }
      if (ch === '`') {
        let n = 0;
        while (s[i + n] === '`') n++;
        const end = codeEnd(s, i);
        if (end > 0) {
          let code = s.slice(i + n, end).replace(/\n/g, ' ');
          if (/^ .*[^ ].* $/.test(code)) code = code.slice(1, -1);
          flush();
          out.push({ t: 'code', v: code });
          i = end + n;
        } else {
          text += '`'.repeat(n);
          i += n;
        }
        continue;
      }
      if (ch === '[' || (ch === '!' && s[i + 1] === '[')) {
        const image = ch === '!';
        const open = image ? i + 1 : i;
        const close = bracketEnd(s, open);
        const target = close > 0 ? linkTarget(s, close + 1) : null;
        const href = target && safeHref(target.href);
        if (href) {
          const label = s.slice(open + 1, close);
          flush();
          // The chat loads no pictures a model points at; the image is its link.
          out.push({ t: 'link', href, c: inline(label || target.href) });
          i = target.end;
          continue;
        }
      }
      if (ch === '*' || ch === '_' || ch === '~') {
        const mark = MARKS.find(([m]) => s.startsWith(m, i));
        if (mark && s[i + mark[0].length] !== ch) {
          const [m, tags] = mark;
          const opens = !isSpace(s[i + m.length]) && !(ch === '_' && isWord(s[i - 1]));
          const end = opens ? emphasisEnd(s, i, m) : -1;
          if (end > 0) {
            flush();
            let node = { t: tags[tags.length - 1], c: inline(s.slice(i + m.length, end)) };
            for (let k = tags.length - 2; k >= 0; k--) node = { t: tags[k], c: [node] };
            out.push(node);
            i = end + m.length;
            continue;
          }
        }
        // A run that opens nothing is text; copy it whole so its tail is not tried again.
        let n = 0;
        while (s[i + n] === ch) n++;
        text += ch.repeat(n);
        i += n;
        continue;
      }
      if (ch === 'h' && !isWord(s[i - 1])) {
        const url = bareUrl(s, i);
        if (url) {
          flush();
          out.push({ t: 'link', href: safeHref(url), c: [{ t: 'text', v: url }] });
          i += url.length;
          continue;
        }
      }
      text += ch;
      i++;
    }
    flush();
    return out;
  }

  // ------------------------------------------------------------ blocks

  const FENCE = /^ {0,3}(`{3,}|~{3,})\s*([^\s`]*)[^`]*$/;
  const HEADING = /^ {0,3}(#{1,6})(?:\s+(.*?))?(?:\s+#+)?\s*$/;
  const RULE = /^ {0,3}([-*_])(?:\s*\1){2,}\s*$/;
  const QUOTE = /^ {0,3}>/;
  const ITEM = /^( *)([-*+]|\d{1,9}[.)])(?:\s+(.*))?$/;
  const DELIMITER = /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$/;

  const indentOf = (line) => line.length - line.trimStart().length;

  function cells(line) {
    let row = line.trim();
    if (row.startsWith('|')) row = row.slice(1);
    if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
    return row.split(/(?<!\\)\|/).map((cell) => cell.trim().replace(/\\\|/g, '|'));
  }

  const isTableStart = (lines, i) => lines[i].includes('|') && i + 1 < lines.length
    && lines[i + 1].includes('-') && DELIMITER.test(lines[i + 1])
    && cells(lines[i]).length === cells(lines[i + 1]).length;

  // A line that ends a paragraph: another kind of block starts here.
  const interrupts = (lines, i) => FENCE.test(lines[i]) || HEADING.test(lines[i]) || RULE.test(lines[i])
    || QUOTE.test(lines[i]) || ITEM.test(lines[i]) || isTableStart(lines, i);

  // The list starting at I: its node and the line after it.
  function list(lines, start) {
    const first = ITEM.exec(lines[start]);
    const base = first[1].length;
    const ordered = /\d/.test(first[2]);
    const node = { t: 'list', ordered, start: ordered ? parseInt(first[2], 10) : 1, items: [] };
    let i = start;
    let current = null;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim() === '') {
        let next = i + 1;
        while (next < lines.length && lines[next].trim() === '') next++;
        if (next >= lines.length || !current) break;
        const m = ITEM.exec(lines[next]);
        const indent = indentOf(lines[next]);
        const sibling = m && indent >= base && indent < base + 2 && /\d/.test(m[2]) === ordered;
        if (!sibling && indent < base + 2) break;
        for (; i < next; i++) current.push('');
        continue;
      }
      const m = ITEM.exec(line);
      const indent = indentOf(line);
      if (m && indent < base) break;
      if (m && indent < base + 2) {
        if (/\d/.test(m[2]) !== ordered) break;
        current = [m[3] ?? ''];
        node.items.push(current);
        i++;
        continue;
      }
      if (indent >= base + 2) {
        current.push(line.slice(Math.min(indent, base + 2)));
        i++;
        continue;
      }
      if (interrupts(lines, i) || !current) break;
      current.push(line.trim());  // a wrapped line of the item above
      i++;
    }
    node.items = node.items.map((body) => blocks(body));
    return { node, next: i };
  }

  function blocks(lines) {
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      if (line.trim() === '') { i++; continue; }

      const fence = FENCE.exec(line);
      if (fence) {
        const body = [];
        const closer = new RegExp(`^ {0,3}${fence[1][0]}{${fence[1].length},}\\s*$`);
        i++;
        while (i < lines.length && !closer.test(lines[i])) body.push(lines[i++]);
        i++;  // the closing fence; a reply still being written has none yet
        const strip = indentOf(line);
        out.push({ t: 'fence', lang: fence[2], v: body.map((l) => l.slice(Math.min(strip, indentOf(l)))).join('\n') });
        continue;
      }
      const heading = HEADING.exec(line);
      if (heading) {
        out.push({ t: 'heading', level: heading[1].length, c: inline(heading[2] ?? '') });
        i++;
        continue;
      }
      if (RULE.test(line)) { out.push({ t: 'rule' }); i++; continue; }
      if (QUOTE.test(line)) {
        const body = [];
        while (i < lines.length && QUOTE.test(lines[i])) body.push(lines[i++].replace(/^ {0,3}> ?/, ''));
        out.push({ t: 'quote', c: blocks(body) });
        continue;
      }
      if (ITEM.test(line)) {
        const { node, next } = list(lines, i);
        out.push(node);
        i = next;
        continue;
      }
      if (isTableStart(lines, i)) {
        const align = cells(lines[i + 1]).map((cell) => (cell.startsWith(':') ? (cell.endsWith(':') ? 'center' : 'left')
          : cell.endsWith(':') ? 'right' : ''));
        const head = cells(line).map((cell) => inline(cell));
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim() !== '' && lines[i].includes('|')
               && !FENCE.test(lines[i]) && !QUOTE.test(lines[i])) {
          const row = cells(lines[i++]);
          rows.push(align.map((_, k) => inline(row[k] ?? '')));
        }
        out.push({ t: 'table', align, head, rows });
        continue;
      }
      const body = [line];
      i++;
      while (i < lines.length && lines[i].trim() !== '' && !interrupts(lines, i)) body.push(lines[i++]);
      out.push({ t: 'para', c: inline(body.join('\n').trim()) });
    }
    return out;
  }

  function parse(text) {
    return blocks(String(text ?? '').replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n'));
  }

  // ------------------------------------------------------------ DOM

  function make(doc, tag, className, children) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    for (const child of children ?? []) node.append(child);
    return node;
  }

  function renderInline(doc, nodes) {
    return nodes.map((node) => {
      switch (node.t) {
        case 'text': return doc.createTextNode(node.v);
        case 'br': return doc.createElement('br');
        case 'code': { const code = doc.createElement('code'); code.textContent = node.v; return code; }
        case 'link': {
          const a = make(doc, 'a', '', renderInline(doc, node.c));
          a.href = node.href;
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          return a;
        }
        default: return make(doc, node.t, '', renderInline(doc, node.c));
      }
    });
  }

  function renderBlocks(doc, nodes) {
    return nodes.map((node) => {
      switch (node.t) {
        case 'para': return make(doc, 'p', '', renderInline(doc, node.c));
        case 'heading': return make(doc, `h${node.level}`, '', renderInline(doc, node.c));
        case 'rule': return doc.createElement('hr');
        case 'quote': return make(doc, 'blockquote', '', renderBlocks(doc, node.c));
        case 'fence': {
          const code = doc.createElement('code');
          code.textContent = node.v;
          if (/^[\w+#.-]+$/.test(node.lang)) code.className = `language-${node.lang}`;
          return make(doc, 'pre', '', [code]);
        }
        case 'list': {
          const el = make(doc, node.ordered ? 'ol' : 'ul', '', node.items.map((item) => make(doc, 'li', '',
            // A lone paragraph sits in the item itself, so a tight list stays tight.
            item.length === 1 && item[0].t === 'para' ? renderInline(doc, item[0].c) : renderBlocks(doc, item))));
          if (node.ordered && node.start !== 1) el.start = node.start;
          return el;
        }
        default: {  // table
          const cell = (tag, content, k) => make(doc, tag, node.align[k] ? `al-${node.align[k]}` : '', renderInline(doc, content));
          return make(doc, 'div', 'table', [make(doc, 'table', '', [
            make(doc, 'thead', '', [make(doc, 'tr', '', node.head.map((c, k) => cell('th', c, k)))]),
            make(doc, 'tbody', '', node.rows.map((row) => make(doc, 'tr', '', row.map((c, k) => cell('td', c, k))))),
          ])]);
        }
      }
    });
  }

  // The DOM for TEXT, as a fragment ready to append.
  function render(text, doc = document) {
    const fragment = doc.createDocumentFragment();
    for (const node of renderBlocks(doc, parse(text))) fragment.append(node);
    return fragment;
  }

  window.GufoMarkdown = { parse, render };
})();
