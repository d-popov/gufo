'use strict';

// Line icons for the portal and monitor pages. All but `brand` (the site's own mark, also
// favicon.svg) are Lucide icons (https://lucide.dev, ISC; THIRD_PARTY_NOTICES.md) from
// lucide-icons/lucide c2dd25753da3f25d9ddc351ddb0ee72332189692, kept as the child elements
// of their 24x24 drawing: a string is a <path d>, otherwise [tag, attributes]. Only icons
// the pages use are here. GufoIcons.svg(name) builds one, drawn in currentColor;
// GufoIcons.hydrate(root) puts one in place of each <span data-icon="name"> in static
// markup; GufoIcons.label(node, name, text) sets a control's icon and text together.

window.GufoIcons = (() => {
  const NS = 'http://www.w3.org/2000/svg';
  const ICONS = {
    brand: ['M12 2.5 20.5 7.25v9.5L12 21.5l-8.5-4.75v-9.5Z', ['circle', { cx: '12', cy: '12', r: '2.5' }], 'M12 9.5v-7', 'm14.17 13.25 6.33 3.5', 'm9.83 13.25-6.33 3.5'],
    activity: ['M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2'],
    'arrow-down-to-line': ['M12 17V3', 'm6 11 6 6 6-6', 'M19 21H5'],
    'badge-check': ['M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z', 'm16 9-5.5 5.5L8 12'],
    ban: [['circle', { cx: '12', cy: '12', r: '10' }], 'M4.929 4.929 19.07 19.071'],
    'calendar-clock': ['M16 14v2.2l1.6 1', 'M16 2v3', 'M21 7.338V5a2 2 0 00-2-2H5a2 2 0 00-2 2v14a2 2 0 002 2h2.338', 'M3 9h5.859', 'M8 2v3', ['circle', { cx: '16', cy: '16', r: '6' }]],
    'chart-column': ['M3 3v16a2 2 0 0 0 2 2h16', 'M18 17V9', 'M13 17V5', 'M8 17v-3'],
    check: ['M20 6 9 17l-5-5'],
    'circle-check': [['circle', { cx: '12', cy: '12', r: '10' }], 'm16 9-5.5 5.5L8 12'],
    'circle-plus': [['circle', { cx: '12', cy: '12', r: '10' }], 'M8 12h8', 'M12 8v8'],
    clock: [['circle', { cx: '12', cy: '12', r: '10' }], 'M12 6v6l4 2'],
    code: ['m16 18 6-6-6-6', 'm8 6-6 6 6 6'],
    copy: [['rect', { width: '14', height: '14', x: '8', y: '8', rx: '2', ry: '2' }], 'M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2'],
    cpu: ['M12 20v2', 'M12 2v2', 'M17 20v2', 'M17 2v2', 'M2 12h2', 'M2 17h2', 'M2 7h2', 'M20 12h2', 'M20 17h2', 'M20 7h2', 'M7 20v2', 'M7 2v2', ['rect', { x: '4', y: '4', width: '16', height: '16', rx: '2' }], ['rect', { x: '8', y: '8', width: '8', height: '8', rx: '1' }]],
    database: [['ellipse', { cx: '12', cy: '5', rx: '9', ry: '3' }], 'M3 5V19A9 3 0 0 0 21 19V5', 'M3 12A9 3 0 0 0 21 12'],
    download: ['M12 15V3', 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5'],
    'key-round': ['M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z', ['circle', { cx: '16.5', cy: '7.5', r: '.5', fill: 'currentColor' }]],
    landmark: ['M10 18v-7', 'M11.119 2.205a2 2 0 0 1 1.762 0l7.84 3.846A.5.5 0 0 1 20.5 7h-17a.5.5 0 0 1-.22-.949z', 'M14 18v-7', 'M18 18v-7', 'M3 22h18', 'M6 18v-7'],
    layers: ['M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z', 'M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12', 'M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17'],
    link: ['M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71', 'M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71'],
    list: ['M3 5h.01', 'M3 12h.01', 'M3 19h.01', 'M8 5h13', 'M8 12h13', 'M8 19h13'],
    'log-in': ['m10 17 5-5-5-5', 'M15 12H3', 'M15 3h4a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-4'],
    'log-out': ['m16 17 5-5-5-5', 'M21 12H9', 'M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4'],
    'memory-stick': ['M12 12v-2', 'M12 18v-2', 'M16 12v-2', 'M16 18v-2', 'M2 11h1.5', 'M20 18v-2', 'M20.5 11H22', 'M4 18v-2', 'M8 12v-2', 'M8 18v-2', ['rect', { x: '2', y: '6', width: '20', height: '10', rx: '2' }]],
    'messages-square': ['M16 10a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 14.286V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z', 'M20 9a2 2 0 0 1 2 2v10.286a.71.71 0 0 1-1.212.502l-2.202-2.202A2 2 0 0 0 17.172 19H10a2 2 0 0 1-2-2v-1'],
    pencil: ['M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z', 'm15 5 4 4'],
    play: ['M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z'],
    'plug-zap': ['M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z', 'm2 22 3-3', 'M7.5 13.5 10 11', 'M10.5 16.5 13 14', 'm18 3-4 4h6l-4 4'],
    plus: ['M5 12h14', 'M12 5v14'],
    receipt: ['M12 17V7', 'M16 8h-6a2 2 0 0 0 0 4h4a2 2 0 0 1 0 4H8', 'M4 3a1 1 0 0 1 1-1 1.3 1.3 0 0 1 .7.2l.933.6a1.3 1.3 0 0 0 1.4 0l.934-.6a1.3 1.3 0 0 1 1.4 0l.933.6a1.3 1.3 0 0 0 1.4 0l.933-.6a1.3 1.3 0 0 1 1.4 0l.934.6a1.3 1.3 0 0 0 1.4 0l.933-.6A1.3 1.3 0 0 1 19 2a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1 1.3 1.3 0 0 1-.7-.2l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.934.6a1.3 1.3 0 0 1-1.4 0l-.933-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-1.4 0l-.934-.6a1.3 1.3 0 0 0-1.4 0l-.933.6a1.3 1.3 0 0 1-.7.2 1 1 0 0 1-1-1z'],
    'refresh-cw': ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
    'shield-check': ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z', 'm9 12 2 2 4-4'],
    tag: ['M12.586 2.586A2 2 0 0 0 11.172 2H4a2 2 0 0 0-2 2v7.172a2 2 0 0 0 .586 1.414l8.704 8.704a2.426 2.426 0 0 0 3.42 0l6.58-6.58a2.426 2.426 0 0 0 0-3.42z', ['circle', { cx: '7.5', cy: '7.5', r: '.5', fill: 'currentColor' }]],
    target: [['circle', { cx: '12', cy: '12', r: '10' }], ['circle', { cx: '12', cy: '12', r: '6' }], ['circle', { cx: '12', cy: '12', r: '2' }]],
    thermometer: ['M14 4v10.54a4 4 0 1 1-4 0V4a2 2 0 0 1 4 0Z'],
    trash: ['M10 11v6', 'M14 11v6', 'M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6', 'M3 6h18', 'M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'],
    'triangle-alert': ['m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3', 'M12 9v4', 'M12 17h.01'],
    user: ['M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2', ['circle', { cx: '12', cy: '7', r: '4' }]],
    'user-plus': ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', ['circle', { cx: '9', cy: '7', r: '4' }], ['line', { x1: '19', x2: '19', y1: '8', y2: '14' }], ['line', { x1: '22', x2: '16', y1: '11', y2: '11' }]],
    users: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', 'M16 3.128a4 4 0 0 1 0 7.744', 'M22 21v-2a4 4 0 0 0-3-3.87', ['circle', { cx: '9', cy: '7', r: '4' }]],
    wallet: ['M19 7V4a1 1 0 0 0-1-1H5a2 2 0 0 0 0 4h15a1 1 0 0 1 1 1v4h-3a2 2 0 0 0 0 4h3a1 1 0 0 0 1-1v-2a1 1 0 0 0-1-1', 'M3 5v14a2 2 0 0 0 2 2h15a1 1 0 0 0 1-1v-4'],
    x: ['M18 6 6 18', 'm6 6 12 12'],
    zap: ['M15.914 4a1.5 1.5 0 00-2.474-1.561l-9 9A1.5 1.5 0 005.5 14h4.002a.5.5 0 01.471.666L8.086 20a1.5 1.5 0 002.475 1.56l9-9A1.5 1.5 0 0018.5 10h-3.997a.5.5 0 01-.472-.667z'],
  };

  // An unknown name is a bug in the page, so it throws rather than leaving a gap.
  function svg(name, { size } = {}) {
    const parts = ICONS[name];
    if (!parts) throw new Error(`unknown icon: ${name}`);
    const node = document.createElementNS(NS, 'svg');
    const attrs = { class: 'icon', viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      'aria-hidden': 'true', focusable: 'false' };
    if (size) Object.assign(attrs, { width: String(size), height: String(size) });
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    for (const part of parts) {
      const [tag, shape] = typeof part === 'string' ? ['path', { d: part }] : part;
      const child = document.createElementNS(NS, tag);
      for (const [key, value] of Object.entries(shape)) child.setAttribute(key, value);
      node.append(child);
    }
    return node;
  }

  function hydrate(root = document) {
    for (const span of root.querySelectorAll('span[data-icon]')) span.replaceWith(svg(span.dataset.icon));
  }

  // Replaces NODE's content with icon NAME and TEXT, e.g. a button whose label changes.
  function label(node, name, text) {
    node.classList.add('with-icon');
    node.replaceChildren(svg(name), text);
  }

  return { svg, hydrate, label };
})();
