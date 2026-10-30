'use strict';

// Projects for the chat: a folder of chats that share two plain-text notes.
//   guidelines  rules the user writes (or asks the model to draft), like a CLAUDE.md;
//   memory      a summary the model keeps up to date from the project's chats.
// Both are Markdown the user can edit at any time. This file holds only the text logic, with
// no page or storage in it, so tests/tools/projects_test.js can run it as it is.
(() => {
  const GUIDELINES_MAX = 12000;  // characters kept
  const MEMORY_MAX = 6000;
  const TRANSCRIPT_MAX = 12000;  // characters of conversation sent to update the memory
  const MESSAGE_MAX = 3000;      // of one message in that transcript

  // The system text that opens every chat of a project: its guidelines, then the memory the
  // chat started with. Null when the project has neither.
  function context(project, memory) {
    const guidelines = (project?.guidelines || '').trim();
    const notes = (memory ?? '').trim();
    if (!guidelines && !notes) return null;
    const parts = [`You are working in the project "${project.name}".`];
    if (guidelines) {
      parts.push(`## Project guidelines\nFollow these in every reply unless the user says otherwise.\n\n${guidelines}`);
    }
    if (notes) {
      parts.push('## Project memory\nNotes kept from earlier chats in this project. They can be out of date: '
        + `what the user says now comes first.\n\n${notes}`);
    }
    return parts.join('\n\n');
  }

  // One system message from what the page wants to say: the project text and the tool note.
  function systemMessage(...texts) {
    const parts = texts.filter((text) => typeof text === 'string' && text.trim());
    return parts.length ? { role: 'system', content: parts.join('\n\n') } : null;
  }

  const LABEL = { user: 'User', assistant: 'Assistant' };

  // The messages from `from` on as plain text, newest kept when it is too long.
  function transcript(messages, from = 0, max = TRANSCRIPT_MAX) {
    const lines = [];
    for (const message of messages.slice(from)) {
      if (message.error || message.pending || !LABEL[message.role]) continue;
      let text = (message.content || '').trim();
      if (!text && message.hadImages) text = '[Image attached]';
      if (!text) continue;
      if (text.length > MESSAGE_MAX) text = `${text.slice(0, MESSAGE_MAX)} […]`;
      lines.push(`${LABEL[message.role]}: ${text}`);
    }
    let out = lines.join('\n\n');
    if (out.length > max) out = `[earlier messages left out]\n\n${out.slice(out.length - max)}`;
    return out;
  }

  // Whether a chat has a finished answer the memory has not seen (and no reply being written).
  function unsummarized(chat) {
    if (chat.messages.some((message) => message.pending)) return false;
    return chat.messages.slice(chat.memoryCount ?? 0)
      .some((message) => message.role === 'assistant' && message.content && !message.error);
  }

  // The request that folds a chat into the project memory.
  function memoryRequest(project, chat) {
    const text = transcript(chat.messages, chat.memoryCount ?? 0);
    const current = (project.memory || '').trim();
    return [
      {
        role: 'system', content: 'You keep the memory of a project: short Markdown notes that give a new chat the '
          + 'context it needs. Update the memory from the conversation. Keep what lasts: goals, decisions and why, '
          + 'names, conventions, facts the user gave, open tasks. Drop what is finished, wrong or small talk. '
          + 'Merge, do not repeat. At most 400 words. Reply with the updated memory only, with no preface.'
      },
      {
        role: 'user', content: `Project: ${project.name}\n\n## Current memory\n${current || '(empty)'}\n\n`
          + `## New conversation\n${text}`
      },
    ];
  }

  // The request that drafts guidelines from a short brief, refining the ones already there.
  function guidelinesRequest(project, brief) {
    const current = (project.guidelines || '').trim();
    return [
      {
        role: 'system', content: 'You write guidelines for an AI assistant: rules in Markdown that it follows in '
          + 'every chat of a project, like a CLAUDE.md. Be specific and short: the tone, the format of answers, '
          + 'the tools and conventions to use, and what to avoid. One bullet per rule. Reply with the guidelines '
          + 'only, with no preface.'
      },
      {
        role: 'user', content: `Project: ${project.name}\n\n## What the user wants\n${brief.trim()}\n\n`
          + `## Current guidelines\n${current || '(none yet)'}${project.memory?.trim()
            ? `\n\n## What is known about the project\n${project.memory.trim()}` : ''}`
      },
    ];
  }

  // The model's reply as stored text: no reasoning tags, no code fence around it, a size limit
  // cut at a line end.
  function clean(reply, max) {
    let text = String(reply ?? '').replace(/<think>[\s\S]*?<\/think>/g, '').trim();
    const fenced = /^```(?:markdown|md)?\n([\s\S]*?)\n```$/.exec(text);
    if (fenced) text = fenced[1].trim();
    if (text.length <= max) return text;
    const cut = text.slice(0, max);
    return cut.slice(0, Math.max(cut.lastIndexOf('\n'), max / 2)).trimEnd();
  }

  // Both notes as one Markdown file to edit elsewhere, and back. The two headings are the only
  // ones the parser reads; the notes may use any other.
  function toMarkdown(project) {
    return `# Guidelines\n\n${(project.guidelines || '').trim()}\n\n# Memory\n\n${(project.memory || '').trim()}\n`;
  }

  function fromMarkdown(text) {
    const lines = {};
    let key = null;
    for (const line of String(text).replace(/\r\n?/g, '\n').split('\n')) {
      const heading = /^#\s+(guidelines|memory)\s*$/i.exec(line);
      if (heading) { key = heading[1].toLowerCase(); lines[key] = []; continue; }
      if (key) lines[key].push(line);
    }
    // Only the sections the file has, so importing one leaves the other note alone.
    const found = Object.fromEntries(Object.entries(lines).map(([name, body]) => [name, body.join('\n').trim()]));
    return Object.keys(found).length ? found : null;  // null: not a project file
  }

  window.GufoProjects = {
    GUIDELINES_MAX, MEMORY_MAX, context, systemMessage, transcript, unsummarized,
    memoryRequest, guidelinesRequest, clean, toMarkdown, fromMarkdown,
  };
})();
