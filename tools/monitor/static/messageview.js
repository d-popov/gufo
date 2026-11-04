'use strict';

// How a reply's working is drawn, the same on the chat page and in a shared chat's room: its thinking, the tool
// steps with what they found (pictures, videos, figures), and the line of numbers under it.
window.GufoMessageView = (() => {
  // Pictures and videos a tool found, each linked to its page. Only addresses on the media hosts
  // are drawn, whatever a saved chat or a room holds.
  function mediaBox(items) {
    const box = document.createElement('div');
    box.className = 'media';
    for (const raw of items) {
      const item = window.GufoWebTools?.safeMedia(raw);
      if (!item) continue;
      const figure = document.createElement('figure');
      if (item.kind === 'video') {
        const video = document.createElement('video');
        video.controls = true;
        video.preload = 'none';
        video.playsInline = true;
        video.poster = item.thumb;
        video.src = item.src;
        figure.className = 'wide';
        figure.append(video);
      } else {
        const link = document.createElement('a');
        link.href = item.page;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        const img = document.createElement('img');
        img.src = item.thumb;
        img.alt = item.title;
        img.loading = 'lazy';
        img.referrerPolicy = 'no-referrer';
        link.append(img);
        figure.append(link);
      }
      const caption = document.createElement('figcaption');
      const title = document.createElement('a');
      title.href = item.page;
      title.target = '_blank';
      title.rel = 'noopener noreferrer';
      title.textContent = item.title || 'Wikimedia Commons';
      caption.append(title, document.createTextNode([item.license, item.author].filter(Boolean).map((part) => ` · ${part}`).join('')));
      figure.append(caption);
      box.append(figure);
    }
    return box;
  }

  // The figures a tool drew, shown in full with a way to keep each one.
  function figureBox(figures) {
    const box = document.createElement('div');
    box.className = 'figures';
    figures.forEach((src, index) => {
      const figure = document.createElement('figure');
      const img = document.createElement('img');
      img.src = src;
      img.alt = `Figure ${index + 1} drawn by Python`;
      const caption = document.createElement('figcaption');
      const save = document.createElement('a');
      save.href = src;
      save.download = `figure-${index + 1}.png`;
      save.textContent = 'Save PNG';
      caption.append(save);
      figure.append(img, caption);
      box.append(figure);
    });
    return box;
  }

  // Appends the message's thinking (open while there is no answer yet) and its tool steps to the block.
  function fillThinkingAndSteps(block, message) {
    if (message.reasoning) {
      const details = document.createElement('details');
      details.className = 'thinking';
      details.open = !message.content;
      const summary = document.createElement('summary');
      summary.textContent = 'Thinking';
      const text = document.createElement('div');
      text.textContent = message.reasoning;
      details.append(summary, text);
      block.append(details);
    }
    for (const step of message.steps ?? []) {
      const details = document.createElement('details');
      details.className = `step ${step.state}`;
      const summary = document.createElement('summary');
      const dot = document.createElement('span');
      dot.className = 'dot';
      const label = document.createElement('span');
      label.textContent = step.label;
      summary.append(dot, label);
      if (step.via === 'reader') {
        const via = document.createElement('span');
        via.className = 'via';
        via.textContent = 'via r.jina.ai';
        summary.append(via);
      }
      details.append(summary);
      if (step.code) {
        const code = document.createElement('pre');
        code.className = 'code';
        code.textContent = step.code;
        details.append(code);
      }
      const result = document.createElement('pre');
      result.textContent = step.excerpt || '';
      details.append(result);
      block.append(details);
      if (step.media?.length) block.append(mediaBox(step.media));
      if (step.figures?.length) {
        block.append(figureBox(step.figures));
      } else if (step.figureCount) {
        const note = document.createElement('div');
        note.className = 'note';
        note.textContent = `${step.figureCount} figure${step.figureCount === 1 ? '' : 's'} drawn; images are not kept after a reload.`;
        block.append(note);
      }
    }
  }

  // The line under a reply: speed, wait, tokens and tool calls, then `extra` (the chat page's price) when given.
  function describe(stats, extra = null) {
    const parts = [];
    if (stats.tps) parts.push(`${stats.tps.toFixed(1)} tok/s`);
    if (stats.ttft_ms != null) parts.push(`${(stats.ttft_ms / 1000).toFixed(1)} s to first token`);
    if (stats.completion_tokens != null) {
      const cached = stats.cached_tokens ? ` (${stats.cached_tokens} cached)` : '';
      parts.push(`${stats.prompt_tokens} in${cached} · ${stats.completion_tokens} out`);
    }
    if (stats.calls) parts.push(`${stats.calls} tool ${stats.calls === 1 ? 'call' : 'calls'}`);
    if (extra) parts.push(extra);
    return parts.join(' · ');
  }

  return { mediaBox, figureBox, fillThinkingAndSteps, describe };
})();
