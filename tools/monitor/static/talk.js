'use strict';

// Talk mode for the chat: a spoken conversation. The microphone is listened to, an utterance (ended
// by a pause) is turned into text by Gufo's speech recognition, sent as a chat message, and the
// reply is spoken sentence by sentence as it is written. Turns do not overlap: the microphone is
// ignored while a reply is thought out and spoken, so the model does not hear itself; the Talk
// button interrupts a reply and goes back to listening.
//
// The first half of this file is plain functions (speech-ready text, sentences, voice activity,
// WAV) that tests/tools/talk_test.js runs as they are; create() below needs a browser.
(() => {
  const VOICE_NOTE = 'This is a spoken conversation: your reply is read aloud. Answer the way a person '
    + 'would say it, in short plain sentences, without Markdown, lists, tables, code, emoji or web '
    + 'addresses. Keep it brief unless asked for detail, and reply in the language the user speaks.';

  // ---------------------------------------------------------------- text for the voice

  // What a reply sounds like once the markup is dropped: link labels stay, addresses, code blocks
  // and emoji go, table bars and bullets become pauses.
  function speakable(markdown) {
    return String(markdown ?? '')
      .replace(/```[\s\S]*?(```|$)/g, ' ')
      .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/https?:\/\/\S+/g, ' ')
      .replace(/`([^`]*)`/g, '$1')
      .replace(/^\s{0,3}#{1,6}\s+/gm, '')
      .replace(/^\s*(?:[-*+]|\d{1,3}[.)])\s+/gm, '')
      .replace(/^\s*>\s?/gm, '')
      .replace(/^\s*[-*_]{3,}\s*$/gm, ' ')
      .replace(/^\s*\|?[\s:|-]+\|?\s*$/gm, ' ')
      .replace(/\|/g, ', ')
      .replace(/(\*\*|__|~~)/g, '')
      .replace(/(^|[\s(])[*_]+(?=\S)|(?<=\S)[*_]+(?=[\s).,;:!?]|$)/g, '$1')
      .replace(/[\p{Extended_Pictographic}\u200d\ufe0f]/gu, '')
      .replace(/\s+/g, ' ')
      .trim();
  }

  const END = /[.!?…。！？]+["'”’)\]]*(?=\s|$)/g;
  const MIN_UNIT = 14;   // shorter sentences ride with the next one: "OK." alone sounds clipped
  const MAX_UNIT = 320;  // longer runs are cut at a comma or space

  // Cuts a long unit at a comma or space near the limit.
  function chunk(text) {
    const out = [];
    let rest = text.trim();
    while (rest.length > MAX_UNIT) {
      let cut = Math.max(rest.lastIndexOf(', ', MAX_UNIT), rest.lastIndexOf('; ', MAX_UNIT));
      if (cut < MAX_UNIT / 2) cut = rest.lastIndexOf(' ', MAX_UNIT);
      if (cut <= 0) cut = MAX_UNIT;
      out.push(rest.slice(0, cut + 1).trim());
      rest = rest.slice(cut + 1).trim();
    }
    if (rest) out.push(rest);
    return out;
  }

  // Turns a reply that is still being written into spoken sentences, each once, in order. feed()
  // takes the whole text so far and returns the sentences completed since the last call; with
  // done=true the unfinished tail is returned too.
  class SentenceFeed {
    constructor() { this.offset = 0; this.fenced = false; this.held = ''; }

    feed(content, done = false) {
      const raw = String(content ?? '');
      const fresh = raw.slice(this.offset);
      let cut = 0;
      let text = this.held;
      const units = [];
      END.lastIndex = 0;
      // A boundary is a sentence end or a line break; the text after the last one waits.
      const boundaries = [];
      for (let m; (m = END.exec(fresh));) boundaries.push(m.index + m[0].length);
      for (let i = fresh.indexOf('\n'); i >= 0; i = fresh.indexOf('\n', i + 1)) boundaries.push(i + 1);
      boundaries.sort((a, b) => a - b);
      for (const end of boundaries) {
        if (end <= cut) continue;
        text += this.outside(fresh.slice(cut, end));
        cut = end;
        if (speakable(text).length >= MIN_UNIT) { units.push(text); text = ''; }
      }
      this.offset += cut;
      this.held = text;
      if (done) {
        const tail = this.outside(raw.slice(this.offset));
        this.offset = raw.length;
        units.push(this.held + tail);
        this.held = '';
      }
      return units.flatMap((unit) => chunk(speakable(unit))).filter(Boolean);
    }

    // The part of `piece` that is not inside a ``` block; the block state carries across pieces.
    outside(piece) {
      let kept = '';
      piece.split('```').forEach((part, i) => {
        if (i > 0) this.fenced = !this.fenced;
        if (!this.fenced) kept += part;
      });
      return kept;
    }
  }

  // ---------------------------------------------------------------- voice activity

  // Decides from the loudness of each short frame when an utterance starts and ends. The noise
  // floor follows the quiet frames, so a fan or a distant TV does not count as speech.
  class Vad {
    constructor({ startFrames = 2, hangMs = 900, minSpeechMs = 300, maxMs = 30000 } = {}) {
      Object.assign(this, { startFrames, hangMs, minSpeechMs, maxMs });
      this.reset();
    }

    reset() {
      this.floor = 0.004;
      this.loud = 0;
      this.speaking = false;
      this.speechMs = 0;
      this.silenceMs = 0;
    }

    threshold() { return Math.max(0.012, this.floor * 3.5); }

    // One frame's RMS level and length: 'idle', 'start', 'speech', 'end' (a whole utterance) or
    // 'cancel' (a click or cough too short to be one).
    push(rms, ms) {
      const loud = rms > this.threshold();
      if (!this.speaking) {
        if (!loud) this.floor += (rms - this.floor) * 0.05;
        this.loud = loud ? this.loud + 1 : 0;
        if (this.loud < this.startFrames) return 'idle';
        this.speaking = true;
        this.speechMs = this.loud * ms;
        this.silenceMs = 0;
        return 'start';
      }
      this.speechMs += ms;
      this.silenceMs = loud ? 0 : this.silenceMs + ms;
      if (this.silenceMs < this.hangMs && this.speechMs < this.maxMs) return 'speech';
      const voiced = this.speechMs - this.silenceMs;
      this.speaking = false;
      this.loud = 0;
      return voiced >= this.minSpeechMs ? 'end' : 'cancel';
    }
  }

  // ---------------------------------------------------------------- audio data

  // Mono samples at `from` Hz to `to` Hz: each output sample averages the input it covers.
  function downsample(samples, from, to) {
    if (to >= from) return samples;
    const ratio = from / to;
    const out = new Float32Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < out.length; i++) {
      const start = i * ratio;
      const end = Math.min(samples.length, start + ratio);
      let sum = 0;
      let weight = 0;
      for (let j = Math.floor(start); j < end; j++) {
        const w = Math.min(j + 1, end) - Math.max(j, start);
        sum += samples[j] * w;
        weight += w;
      }
      out[i] = weight ? sum / weight : 0;
    }
    return out;
  }

  // A 16-bit mono WAV file.
  function encodeWav(samples, rate) {
    const bytes = new ArrayBuffer(44 + samples.length * 2);
    const view = new DataView(bytes);
    const text = (at, value) => { for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i)); };
    text(0, 'RIFF');
    view.setUint32(4, 36 + samples.length * 2, true);
    text(8, 'WAVEfmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);          // PCM
    view.setUint16(22, 1, true);          // mono
    view.setUint32(24, rate, true);
    view.setUint32(28, rate * 2, true);   // bytes per second
    view.setUint16(32, 2, true);          // bytes per frame
    view.setUint16(34, 16, true);
    text(36, 'data');
    view.setUint32(40, samples.length * 2, true);
    for (let i = 0; i < samples.length; i++) {
      const s = Math.max(-1, Math.min(1, samples[i]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return bytes;
  }

  function rmsOf(samples) {
    let sum = 0;
    for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
    return Math.sqrt(sum / (samples.length || 1));
  }

  // ---------------------------------------------------------------- the conversation (browser)

  const ASR_RATE = 16000;
  const PRE_ROLL_MS = 400;

  // host: { ask(text), stopReply(), transcribe(wav: ArrayBuffer) -> text, synthesize(text) ->
  // ArrayBuffer (a WAV file), setState(state, detail) }. States: off, starting, listening, hearing,
  // transcribing, thinking, speaking, error.
  function create(host) {
    let state = 'off';
    let stream = null;
    let context = null;
    let processor = null;
    let source = null;
    let vad = new Vad();
    let frames = [];        // Float32Arrays of the utterance in progress
    let preRoll = [];       // the last PRE_ROLL_MS before speech started
    let turn = 0;           // bumps when a reply is abandoned, so late audio is dropped
    let epoch = 0;          // bumps when a start is cancelled, so its late microphone is dropped
    let feed = new SentenceFeed();
    let replying = null;    // the reply object being spoken
    let replyDone = false;
    let synthesizing = Promise.resolve();
    let queued = 0;         // sentences asked for and not yet played
    let player = null;      // the playing source, to stop it
    let playing = Promise.resolve();

    const set = (next, detail = '') => { state = next; host.setState(next, detail); };

    function stopPlayback() {
      turn += 1;
      queued = 0;
      synthesizing = Promise.resolve();
      playing = Promise.resolve();
      try { player?.stop(); } catch { /* already finished */ }
      player = null;
    }

    function listen() {
      if (state === 'off') return;
      frames = [];
      preRoll = [];
      vad.reset();
      set('listening');
    }

    // Speaks one sentence: synthesis runs in order, one request at a time (the server speaks
    // one at a time anyway), and playback follows as soon as the earlier sentence ends.
    function speak(sentence) {
      const mine = turn;
      queued += 1;
      const audio = synthesizing.then(() => (mine === turn ? host.synthesize(sentence) : null))
        .then((wav) => (mine === turn && wav ? context.decodeAudioData(wav) : null))
        .catch((error) => { if (mine === turn) set('error', error.message); return null; });
      synthesizing = audio.then(() => {});
      playing = Promise.all([audio, playing]).then(([buffer]) => new Promise((resolve) => {
        if (mine !== turn || !buffer) { resolve(); return; }
        const node = context.createBufferSource();
        node.buffer = buffer;
        node.connect(context.destination);
        node.onended = resolve;
        player = node;
        set('speaking', sentence);
        node.start();
      })).then(() => {
        if (mine !== turn) return;
        queued -= 1;
        if (queued === 0 && replyDone) setTimeout(() => { if (mine === turn) listen(); }, 250);
      });
    }

    function onFrame(samples) {
      if (state !== 'listening' && state !== 'hearing') return;
      const ms = samples.length / context.sampleRate * 1000;
      const verdict = vad.push(rmsOf(samples), ms);
      if (verdict === 'idle') {
        preRoll.push(samples);
        while (preRoll.length * ms > PRE_ROLL_MS) preRoll.shift();
      } else if (verdict === 'start') {
        frames = [...preRoll, samples];
        preRoll = [];
        set('hearing');
      } else if (verdict === 'speech') {
        frames.push(samples);
      } else if (verdict === 'cancel') {
        frames = [];
        set('listening');
      } else if (verdict === 'end') {
        frames.push(samples);
        finishUtterance();
      }
    }

    async function finishUtterance() {
      const total = frames.reduce((n, f) => n + f.length, 0);
      const joined = new Float32Array(total);
      let at = 0;
      for (const f of frames) { joined.set(f, at); at += f.length; }
      frames = [];
      set('transcribing');
      const mine = turn;
      try {
        const text = (await host.transcribe(encodeWav(downsample(joined, context.sampleRate, ASR_RATE), ASR_RATE))).trim();
        if (mine !== turn || state !== 'transcribing') return;
        if (!text) { listen(); return; }
        feed = new SentenceFeed();
        replying = null;
        replyDone = false;
        set('thinking', text);
        host.ask(text);
      } catch (error) {
        if (mine !== turn) return;
        set('error', error.message);
        setTimeout(listen, 1500);
      }
    }

    return {
      get active() { return state !== 'off'; },
      get state() { return state; },
      instruction: VOICE_NOTE,

      // Taking the microphone can wait on a permission prompt; a second tap or stop() meanwhile
      // cancels this start, and whatever it acquires afterwards is released.
      async start() {
        if (state !== 'off') return;
        const mine = ++epoch;
        set('starting');
        let media;
        try {
          media = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
          });
        } catch (error) {
          if (mine === epoch) set('off');
          throw new Error(error.name === 'NotAllowedError' ? 'Microphone access was refused.' : `No microphone: ${error.message}`);
        }
        const release = (audio) => {
          media.getTracks().forEach((track) => track.stop());
          return audio?.close();
        };
        if (mine !== epoch) { release(); return; }
        const audio = new AudioContext();
        try {
          await audio.resume();
        } catch (error) {
          await release(audio);
          if (mine === epoch) set('off');
          throw error;
        }
        if (mine !== epoch) { await release(audio); return; }
        stream = media;
        context = audio;
        source = context.createMediaStreamSource(stream);
        processor = context.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = (event) => onFrame(new Float32Array(event.inputBuffer.getChannelData(0)));
        const mute = context.createGain();
        mute.gain.value = 0;
        source.connect(processor);
        processor.connect(mute);
        mute.connect(context.destination);
        vad = new Vad();
        listen();
      },

      async stop() {
        if (state === 'off') return;
        epoch += 1;  // a start still waiting for the microphone gives it back
        if (state === 'starting') { set('off'); return; }
        stopPlayback();
        host.stopReply();
        processor?.disconnect();
        source?.disconnect();
        stream?.getTracks().forEach((track) => track.stop());
        const closing = context?.close();
        processor = source = stream = context = null;
        set('off');
        await closing;
      },

      // The Talk button during a reply: stop talking and the reply, and listen again.
      interrupt() {
        if (state !== 'thinking' && state !== 'speaking') return false;
        stopPlayback();
        host.stopReply();
        listen();
        return true;
      },

      // The reply being written (chat.js calls this on every repaint and once more when it ends).
      onReply(reply, done) {
        if (state !== 'thinking' && state !== 'speaking') return;
        if (replying !== reply) { replying = reply; feed = new SentenceFeed(); }
        for (const sentence of feed.feed(reply.content, done)) speak(sentence);
        if (done) {
          replyDone = true;
          if (queued === 0) listen();  // nothing speakable came out of it
        }
      },
    };
  }

  window.GufoTalk = { speakable, SentenceFeed, Vad, downsample, encodeWav, rmsOf, VOICE_NOTE, create };
})();
