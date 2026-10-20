// Live play: the running play-test at full speed with sound, over a WebSocket (see live.py).
// const live = PaeLive.attach(canvas, { onStatus(text), onFrames(n) }); live.start(); live.key('A', true) …
(function () {
  const KEYS = { A: 1, B: 2, SELECT: 4, START: 8, RIGHT: 16, LEFT: 32, UP: 64, DOWN: 128, R: 256, L: 512 };
  const RATE = 32768;
  function attach(canvas, opts = {}) {
    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = false;
    let ws = null, mask = 0, frames = 0, sounds = 0, speed = 1, wantSound = false, audio = null, node = null, queue = [], queued = 0;
    const send = (m) => { if (ws && ws.readyState === 1) ws.send(JSON.stringify(m)); };
    function startAudio() {
      if (audio) return;
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      try { audio = new AC({ sampleRate: RATE }); } catch (e) { audio = new AC(); }
      node = audio.createScriptProcessor(2048, 0, 2);
      const step = RATE / audio.sampleRate;  // when the browser won't use our rate, resample (nearest)
      let pos = 0;
      node.onaudioprocess = (e) => {
        const L = e.outputBuffer.getChannelData(0), R = e.outputBuffer.getChannelData(1);
        for (let i = 0; i < L.length; i++) {
          while (queue.length && pos >= queue[0].length / 2) { pos -= queue[0].length / 2; queued -= queue[0].length / 2; queue.shift(); }
          if (!queue.length) { L[i] = R[i] = 0; continue; }
          const buf = queue[0], j = Math.floor(pos) * 2;
          L[i] = buf[j] / 32768; R[i] = buf[j + 1] / 32768;
          pos += step;
        }
      };
      node.connect(audio.destination);
    }
    function onMessage(ev) {
      if (typeof ev.data === 'string') {
        try { const s = JSON.parse(ev.data); if (opts.onStatus) opts.onStatus(s); } catch (e) { /* ignore */ }
        return;
      }
      const data = new Uint8Array(ev.data);
      if (data[0] === 1) {
        createImageBitmap(new Blob([data.subarray(1)], { type: 'image/png' })).then(bmp => {
          ctx.drawImage(bmp, 0, 0, canvas.width, canvas.height); bmp.close && bmp.close();
        });
        frames += speed;
        if (opts.onFrames) opts.onFrames(frames);
      } else if (data[0] === 2 && audio) {
        sounds++;
        const samples = new Int16Array(data.buffer.slice(data.byteOffset + 1));
        if (queued > RATE / 4) { queue = []; queued = 0; }  // more than 1/4 s behind: drop, to keep it in time
        queue.push(samples); queued += samples.length / 2;
      }
    }
    return {
      KEYS,
      get frames() { return frames; },
      get sounds() { return sounds; },
      get running() { return !!ws && ws.readyState <= 1; },
      start() {
        if (ws && ws.readyState <= 1) return;
        ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/play/live');
        ws.binaryType = 'arraybuffer';
        ws.onmessage = onMessage;
        ws.onopen = () => { send({ keys: mask, speed, sound: wantSound }); };
        ws.onclose = () => { if (opts.onClose) opts.onClose(); };
      },
      stop() { if (ws) { send({ keys: 0 }); ws.close(); ws = null; } mask = 0; },
      key(name, down) {
        const bit = KEYS[name]; if (!bit) return;
        const m = down ? mask | bit : mask & ~bit;
        if (m !== mask) { mask = m; send({ keys: mask }); }
      },
      release() { mask = 0; send({ keys: 0 }); },
      speed(n) { speed = n; send({ speed: n }); },
      pause(p) { send({ pause: !!p }); },
      sound(on) {
        wantSound = on;
        if (on) { startAudio(); if (audio && audio.state === 'suspended') audio.resume(); }
        else if (audio) { queue = []; queued = 0; }
        send({ sound: on });
      },
    };
  }
  window.PaeLive = { attach, KEYS };
})();
