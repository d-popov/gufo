'use strict';

// Run with node tests/tools/monitor_icons_test.js. Keep every UI icon reference local.
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../../tools/monitor');
const source = fs.readFileSync(path.join(root, 'static/icons.js'), 'utf8');
const names = new Set([...source.matchAll(/^    (?:'([^']+)'|(\w+)): /gm)].map((m) => m[1] || m[2]));
const used = new Set();
for (const dir of ['static', 'portal_static']) {
  for (const file of fs.readdirSync(path.join(root, dir))) {
    if (!/\.(js|html)$/.test(file) || file === 'icons.js') continue;
    const text = fs.readFileSync(path.join(root, dir, file), 'utf8');
    const patterns = [
      /data-icon="([^"]+)"/g,
      /(?:\.svg|\bicon)\('([^']+)'/g,
      /\.label\([^,]+, '([^']+)'/g,
      /icon: '([^']+)'/g,
      /menuItem\('[^']+', '([^']+)'/g,
      /link\('(?:portal|monitor)', '[^']+', '([^']+)'/g,
      /\['[^']+', '(?:state-prompt|state-busy|bad)', '([^']+)'\]/g,
    ];
    for (const pattern of patterns) for (const match of text.matchAll(pattern)) used.add(match[1]);
    for (const match of text.matchAll(/const actionIcons = \{([\s\S]*?)\};/g)) {
      for (const item of match[1].matchAll(/: '([^']+)'/g)) used.add(item[1]);
    }
  }
}
const missing = [...used].filter((name) => !names.has(name));
const unused = [...names].filter((name) => !used.has(name));
if (missing.length || unused.length) {
  console.error({ missing, unused });
  process.exitCode = 1;
} else {
  console.log(`${names.size} icon definitions; all references exist and all icons are used.`);
}
