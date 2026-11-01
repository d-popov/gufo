'use strict';

// The Library panel of the monitor page (/monitor): add a PDF, a Markdown or text file or pasted
// text, watch the job that digests, files and links it, see and remove pages, and show the
// token and address an MCP client uses. Every route needs the admin sign-in (GufoAdmin.request
// reports it ending); the panel stays hidden without one or on a monitor without a library.

window.GufoLibraryAdmin = (() => {
  const $ = (id) => document.getElementById(id);
  const { el, request, copy } = window.GufoAdmin;
  const MAX_PDF = 40 * 1024 * 1024;
  const MAX_TEXT = 8 * 1024 * 1024;
  let busy = false;

  function note(id, value) {
    const node = $(id);
    node.textContent = value ? String(value.message ?? value) : '';
    node.hidden = !value;
  }

  function renderPages(listing) {
    $('admin-library-count').textContent = `${listing.total} page${listing.total === 1 ? '' : 's'}`;
    const rows = listing.pages.map((page) => {
      const remove = el('button', { type: 'button', class: 'link' }, 'Delete');
      remove.addEventListener('click', () => removePage(page));
      return el('tr', {},
        el('td', {}, el('strong', {}, page.title), el('div', { class: 'muted' }, page.slug)),
        el('td', {}, page.kind || 'page'),
        el('td', { class: 'num' }, page.chars.toLocaleString()),
        el('td', { class: 'account-actions' }, remove));
    });
    $('admin-library-pages').querySelector('tbody').replaceChildren(...rows);
    $('admin-library-pages').hidden = !rows.length;
  }

  async function refresh() {
    if (busy || document.hidden) return;
    try {
      renderPages(await request('GET', '/api/library/pages?limit=200'));
      $('admin-library-section').hidden = false;
    } catch (error) {
      $('admin-library-section').hidden = error.status === 404 || error.status === 401;
      if (!$('admin-library-section').hidden) note('admin-library-error', error);
    }
  }

  async function removePage(page) {
    if (!window.confirm(`Delete “${page.title}”? Links to it become plain text.`)) return;
    try {
      await request('DELETE', `/api/library/pages/${encodeURIComponent(page.slug)}`);
      note('admin-library-error', null);
      await refresh();
    } catch (error) {
      note('admin-library-error', error);
    }
  }

  function base64Of(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '');
      reader.onerror = () => reject(new Error('The file could not be read.'));
      reader.readAsDataURL(file);
    });
  }

  // What the form holds as an ingest request, or an Error to show.
  async function requestFromForm() {
    const file = $('admin-library-file').files[0];
    const pasted = $('admin-library-text').value;
    const body = { digest: $('admin-library-digest').checked };
    const title = $('admin-library-title').value.trim();
    if (title) body.title = title;
    if (file) {
      body.filename = file.name;
      if (/\.pdf$/i.test(file.name) || file.type === 'application/pdf') {
        if (file.size > MAX_PDF) throw new Error('A PDF can be at most 40 MiB.');
        body.pdf_base64 = await base64Of(file);
      } else {
        if (file.size > MAX_TEXT) throw new Error('A text file can be at most 8 MiB.');
        body.text = await file.text();
      }
    } else if (pasted.trim()) {
      body.text = pasted;
    } else {
      throw new Error('Choose a file or paste some text.');
    }
    return body;
  }

  async function follow(jobId) {
    const log = $('admin-library-log');
    $('admin-library-job').hidden = false;
    for (;;) {
      const job = await request('GET', `/api/library/jobs/${encodeURIComponent(jobId)}`);
      log.replaceChildren(...job.log.map((line) => el('li', {}, line)));
      if (job.state === 'error') throw new Error(job.error || 'Filing failed.');
      if (job.state === 'done') return job.result;
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    busy = true;
    $('admin-library-submit').disabled = true;
    note('admin-library-error', null);
    $('admin-library-state').textContent = 'Filing…';
    try {
      const { job } = await request('POST', '/api/library/ingest', await requestFromForm());
      const result = await follow(job);
      $('admin-library-form').reset();
      const linked = result.pages_linked.length;
      $('admin-library-state').textContent = `Filed “${result.title}”: ${result.concepts_created.length} new concept page(s), ${linked} page(s) linked.`;
    } catch (error) {
      $('admin-library-state').textContent = '';
      note('admin-library-error', error);
    } finally {
      busy = false;
      $('admin-library-submit').disabled = false;
      await refresh();
    }
  }

  async function relink() {
    try {
      const { changed } = await request('POST', '/api/library/relink');
      $('admin-library-state').textContent = `Re-linked: ${changed.length} page(s) changed.`;
      note('admin-library-error', null);
    } catch (error) {
      note('admin-library-error', error);
    }
  }

  async function showToken() {
    try {
      const { token } = await request('GET', '/api/library/token');
      $('admin-library-endpoint').textContent = `${window.location.origin}/mcp`;
      $('admin-library-token').textContent = token;
      $('admin-library-secret').hidden = false;
    } catch (error) {
      note('admin-library-error', error);
    }
  }

  function init() {
    $('admin-library-form').addEventListener('submit', submit);
    $('admin-library-relink').addEventListener('click', relink);
    $('admin-library-show-token').addEventListener('click', showToken);
    $('admin-library-copy-endpoint').addEventListener('click', (event) => copy(event.currentTarget, $('admin-library-endpoint').textContent));
    $('admin-library-copy-token').addEventListener('click', (event) => copy(event.currentTarget, $('admin-library-token').textContent));
    $('admin-library-hide-token').addEventListener('click', () => {
      $('admin-library-token').textContent = '';
      $('admin-library-secret').hidden = true;
    });
    refresh();
    setInterval(refresh, 30000);
    document.addEventListener('visibilitychange', refresh);
  }

  document.addEventListener('DOMContentLoaded', init);
  return { refresh };
})();
