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
    element('path', { d: 'M30 40 Q38 34 46 40', class: 'avatar-brow' }, head);
    element('path', { d: 'M74 40 Q82 34 90 40', class: 'avatar-brow' }, head);
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
    const blinkOnce = () => {
      for (const eye of eyes) eye.setAttribute('ry', '1');
      setTimeout(() => { for (const eye of eyes) eye.setAttribute('ry', '7'); }, 120);
      blink = setTimeout(blinkOnce, blinkDelay(Math.random()));
    };
    blink = setTimeout(blinkOnce, blinkDelay(Math.random()));

    return {
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

  window.GufoAvatar = { create, mouthOpening, mouthPath, rmsOf, blinkDelay };
})();
