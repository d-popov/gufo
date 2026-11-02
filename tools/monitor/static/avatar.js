'use strict';

// A small animated face for the chat, drawn in SVG with no model or asset: it blinks, sways a little, and its
// mouth follows how loud the spoken reply is. attach() takes the audio node that is about to play and routes it
// through an analyser to the speakers, so the mouth is driven by the sound that is actually heard, whatever the
// playback rate. tests/tools/avatar_test.js runs the pure parts of this file as they are.
(() => {
  const SVG = 'http://www.w3.org/2000/svg';
  const SILENCE = 0.015;     // RMS below which the mouth stays shut
  const LOUD = 0.22;         // RMS at which it is fully open
  const ATTACK = 0.6;        // how fast the mouth opens and closes, as a share of the gap per frame
  const RELEASE = 0.25;

  // The mouth opening, 0 to 1, after one more frame of sound with this RMS.
  function mouthOpening(rms, previous) {
    const target = Math.min(1, Math.max(0, (rms - SILENCE) / (LOUD - SILENCE)));
    return previous + (target - previous) * (target > previous ? ATTACK : RELEASE);
  }

  // The root-mean-square of a block of samples.
  function rmsOf(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return samples.length ? Math.sqrt(sum / samples.length) : 0;
  }

  // Milliseconds to the next blink, given a random number in [0, 1).
  const blinkDelay = (random) => 2500 + random * 3500;

  // The mouth outline for an opening: a gentle smile when shut, an open oval when wide.
  function mouthPath(open) {
    const top = (72 + 8 * (1 - open)).toFixed(1);
    const bottom = (84 + 20 * open).toFixed(1);
    return `M40 76 Q60 ${top} 80 76 Q60 ${bottom} 40 76 Z`;
  }

  // The mood the face shows for what the chat is doing: attentive while listening, leaning in while it hears you,
  // thinking while the words are understood and the model answers (also for a typed message), plain otherwise
  // (the mouth shows speaking). `state` is talk mode's state, `busy` whether a reply is being written.
  function moodFor(state, busy) {
    if (state === 'hearing') return 'hearing';
    if (state === 'listening') return 'listening';
    if (state === 'transcribing' || state === 'thinking') return 'thinking';
    if (state === 'speaking') return 'idle';
    return busy ? 'thinking' : 'idle';
  }

  // How each mood draws the parts that move: the eyes' height and gaze, and the two eyebrows.
  const MOODS = {
    idle: { eye: 7, gaze: [0, 0], brows: ['M30 40 Q38 34 46 40', 'M74 40 Q82 34 90 40'] },
    listening: { eye: 8.5, gaze: [0, 0], brows: ['M30 36 Q38 29 46 36', 'M74 36 Q82 29 90 36'] },
    hearing: { eye: 9, gaze: [0, 1], brows: ['M30 34 Q38 26 46 34', 'M74 34 Q82 26 90 34'] },
    thinking: { eye: 6, gaze: [4, -4], brows: ['M30 35 Q38 28 46 35', 'M74 42 Q82 40 90 42'] },
  };

  function element(name, attributes, parent) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    parent?.append(node);
    return node;
  }

  // Draws the face into `root` and returns { attach(context, node), destroy() }.
  function create(root) {
    const svg = element('svg', { viewBox: '0 0 120 120', role: 'img', 'aria-label': 'Assistant avatar', class: 'avatar-face' });
    const head = element('g', { class: 'avatar-head' }, svg);
    element('circle', { cx: 60, cy: 62, r: 46, class: 'avatar-skin' }, head);
    const eyes = [38, 82].map((cx) => element('ellipse', { cx, cy: 54, rx: 6, ry: 7, class: 'avatar-eye' }, head));
    const brows = MOODS.idle.brows.map((d) => element('path', { d, class: 'avatar-brow' }, head));
    const dots = element('g', { class: 'avatar-dots' }, svg);
    [48, 60, 72].forEach((cx, i) => element('circle', { cx, cy: 9, r: 3, class: `avatar-dot avatar-dot-${i}` }, dots));
    const mouth = element('path', { d: mouthPath(0), class: 'avatar-mouth' }, head);
    root.append(svg);

    let open = 0;
    let loops = 0;       // sounds being measured
    let frame = 0;
    let blink = 0;

    const draw = (value) => {
      open = value;
      mouth.setAttribute('d', mouthPath(value));
    };
    let mood = 'idle';
    const setMood = (name) => {
      if (!MOODS[name] || name === mood) return;
      mood = name;
      const { eye, gaze, brows: shapes } = MOODS[name];
      eyes.forEach((node, i) => {
        node.setAttribute('ry', String(eye));
        node.setAttribute('cx', String([38, 82][i] + gaze[0]));
        node.setAttribute('cy', String(54 + gaze[1]));
      });
      brows.forEach((node, i) => node.setAttribute('d', shapes[i]));
      svg.setAttribute('class', `avatar-face mood-${name}`);
    };
    const blinkOnce = () => {
      for (const eye of eyes) eye.setAttribute('ry', '1');
      setTimeout(() => { for (const eye of eyes) eye.setAttribute('ry', String(MOODS[mood].eye)); }, 120);
      blink = setTimeout(blinkOnce, blinkDelay(Math.random()));
    };
    blink = setTimeout(blinkOnce, blinkDelay(Math.random()));

    return {
      setMood,
      attach(context, node) {
        const analyser = context.createAnalyser();
        analyser.fftSize = 512;
        node.connect(analyser);
        analyser.connect(context.destination);
        const block = new Float32Array(analyser.fftSize);
        loops += 1;
        let running = true;
        const step = () => {
          if (!running) return;
          analyser.getFloatTimeDomainData(block);
          draw(mouthOpening(rmsOf(block), open));
          frame = requestAnimationFrame(step);
        };
        node.addEventListener('ended', () => {
          running = false;
          loops -= 1;
          if (loops === 0) { cancelAnimationFrame(frame); draw(0); }
        });
        frame = requestAnimationFrame(step);
      },
      destroy() {
        clearTimeout(blink);
        cancelAnimationFrame(frame);
        svg.remove();
      },
    };
  }

  // Where a window of this size may sit so that it stays on screen: the top-left corner clamped to the viewport.
  function clampPosition(x, y, width, height, viewWidth, viewHeight, margin = 4) {
    const clamp = (value, size, view) => Math.max(margin, Math.min(value, Math.max(margin, view - size - margin)));
    return { x: clamp(x, width, viewWidth), y: clamp(y, height, viewHeight) };
  }

  window.GufoAvatar = { create, mouthOpening, mouthPath, rmsOf, blinkDelay, clampPosition, moodFor, MOODS };
})();
