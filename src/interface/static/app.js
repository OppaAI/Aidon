/* Aidon WebUI chat client. Polling, no websockets for v1. */
(function () {
  "use strict";
  const STAGES = ["interpret", "research", "recall", "plan", "sandbox",
                  "approval", "live", "learn", "done"];

  const thread = document.getElementById("thread");
  const input = document.getElementById("goal-input");
  const guidanceInput = document.getElementById("guidance-input");
  const sendBtn = document.getElementById("send");
  const steerRow = document.getElementById("steer-row");
  const steerInput = document.getElementById("steer-input");
  const steerBtn = document.getElementById("steer-send");
  const cancelBtn = document.getElementById("cancel-run");
  const stagesEl = document.getElementById("stages");
  const approvalSlot = document.getElementById("approval-slot");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");

  function loadDashboardStatus() {
    fetch("/api/status").then((r) => r.json()).then((data) => {
      document.querySelectorAll("[data-status]").forEach((node) => {
        const key = node.getAttribute("data-status");
        let value = data[key] || "Unavailable";
        if (key === "model" && data.backend) value += " · " + data.backend;
        node.textContent = value;
        node.classList.toggle("status-ready", ["Online", "Ready", "bubblewrap", "docker", "Semantic memory (on demand)", "EmbeddingGemma2Embedder"].includes(value));
      });
    }).catch(() => {
      document.querySelectorAll("[data-status]").forEach((node) => { node.textContent = "Unavailable"; });
    });
  }
  loadDashboardStatus();

  let currentRun = null;
  let pollTimer = null;
  let lastEventCount = 0;
  let lastTraceCount = 0;
  let lastTracePhase = null;
  let cardShownFor = null;

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // Minimal markdown -> HTML. Input is ESCAPED first, so the output can
  // never introduce markup of its own (no raw HTML passthrough). Supports
  // what Aidon actually emits: fenced code, headings, lists, blockquotes,
  // bold/italic/strike, inline code, links and pipe tables.
  function md(src) {
    let t = esc(src == null ? "" : src);
    const blocks = [];
    // fenced code first: its content must not be touched by inline rules
    t = t.replace(/```([a-zA-Z0-9_+#.-]*)[ \t]*\n?([\s\S]*?)```/g, (m, lang, code) => {
      blocks.push(code.replace(/\n$/, ""));
      return "\x00CODE" + (blocks.length - 1) + "\x00";
    });
    t = t.replace(/`([^`\n]+)`/g, (m, c) => "<code>" + c + "</code>");
    t = t.replace(/^###\s+(.+)$/gm, "<h4>$1</h4>")
         .replace(/^##\s+(.+)$/gm, "<h3>$1</h3>")
         .replace(/^#\s+(.+)$/gm, "<h2>$1</h2>");
    t = t.replace(/^\s*[-*]\s+(.+)$/gm, "<li>$1</li>")
         .replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, "<ul>$1</ul>");
    t = t.replace(/^\s*&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");
    t = t.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
         .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s.,!?)\]])/g, "$1<em>$2</em>")
         .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s.,!?)\]])/g, "$1<em>$2</em>")
         .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
    t = t.replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|file:)\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    // A file:// link is not clickable from an http page in Chrome, so
    // the WebUI serves the file itself: same-origin, always clickable.
    t = t.replace(/href="file:\/\/([^"]+)"/g,
        (m, p) => 'href="/api/files/' +
          p.replace(/^\/+/, "").split("/").map(encodeURIComponent).join("/") +
          '"');
    // Markdown tables: a header row, a |---|---| separator, then body
    // rows. Without this they render as raw pipe text - the model
    // writes proper tables, the renderer just did not know them.
    // Line-scanned rather than one regex: the last row often has no
    // trailing newline, and prose above the table may itself contain a
    // pipe (inline code), which must not swallow the header.
    const tlines = t.split("\n");
    const kept = [];
    const cellsOf = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "")
      .split("|").map((c) => c.trim());
    const isSep = (row) => row !== undefined && row.indexOf("|") !== -1 &&
      /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(row);
    for (let i = 0; i < tlines.length; i++) {
      if (tlines[i].indexOf("|") === -1 || !isSep(tlines[i + 1])) {
        kept.push(tlines[i]);
        continue;
      }
      let html = '<table class="md-table"><thead><tr>';
      cellsOf(tlines[i]).forEach((c) => { html += "<th>" + c + "</th>"; });
      html += "</tr></thead><tbody>";
      i += 2;
      while (i < tlines.length && tlines[i].trim() !== "" &&
             tlines[i].indexOf("|") !== -1) {
        html += "<tr>";
        cellsOf(tlines[i]).forEach((c) => { html += "<td>" + c + "</td>"; });
        html += "</tr>";
        i++;
      }
      html += "</tbody></table>";
      blocks.push(html);
      kept.push("\u0000CODE" + (blocks.length - 1) + "\u0000");
      i--;  // the for-increment moves past the consumed rows
    }
    t = kept.join("\n");
    t = t.replace(/\n{2,}/g, "</p><p>").replace(/\n/g, "<br>");
    t = "\x01<p>" + t + "</p>\x01";
    // restore code blocks (outside <p>, which cannot contain <pre>)
    t = t.replace(/\x00CODE(\d+)\x00/g,
        (m, i) => { const b = blocks[Number(i)];
          return b.indexOf("<table") === 0 ? "</p>" + b + "<p>"
            : "</p><pre><code>" + b + "</code></pre><p>"; });
    return t.replace(/^<p>|<\/p>$/g, "") + "";
  }

  function addMsg(kind, text, t) {
    const d = document.createElement("div");
    d.className = "msg " + kind;
    d.innerHTML = (t ? '<div class="t">' + esc(t) + "</div>" : "") + md(text);
    thread.appendChild(d);
    thread.scrollTop = thread.scrollHeight;
    return d;
  }

  function finalMessageParts(text) {
    const value = String(text || "");
    const lines = value.split("\n");
    const index = lines.findIndex((line) => /^\s*⏱️?\s/.test(line));
    if (index < 0) return { body: value, stats: "" };
    return {
      body: lines.slice(0, index).join("\n").replace(/\s+$/, ""),
      stats: lines.slice(index).join(" ").replace(/^\s*⏱️?\s*/, "").trim(),
    };
  }

  function renderFinalMessage(text) {
    const parts = finalMessageParts(text);
    let html = md(parts.body);
    if (parts.stats) {
      html += '<div class="run-stats"><span class="run-stats-icon" aria-hidden="true">◷</span>' +
        '<span>' + esc(parts.stats) + "</span></div>";
    }
    return html;
  }

  // Typewriter reveal: streams text into the message char-by-char,
  // karaoke style. Returns the element; callers can append buttons after.
  function addMsgTyped(kind, text, t, final = false) {
    const d = document.createElement("div");
    d.className = "msg " + kind;
    if (t) {
      const td = document.createElement("div");
      td.className = "t";
      td.textContent = t;
      d.appendChild(td);
    }
    const body = document.createElement("div");
    d.appendChild(body);
    thread.appendChild(d);
    const full = text || "";
    let i = 0;
    const step = Math.max(1, Math.floor(full.length / 120)); // ~120 frames
    // The content element and a "typing finished" promise: the karaoke
    // highlight needs the FINAL text (the typewriter re-renders the
    // body every frame, which would wipe any spans we added), and the
    // voice should start when the text is fully revealed.
    d._body = body;
    d._typing = new Promise((resolve) => {
      (function tick() {
        i = Math.min(full.length, i + step);
        // markdown as it streams: unfinished fences/asterisks are harmless
        // (they simply render literally until the closing token arrives)
        body.innerHTML = md(full.slice(0, i));
        thread.scrollTop = thread.scrollHeight;
        if (i < full.length) requestAnimationFrame(tick);
        else {
          body.innerHTML = final ? renderFinalMessage(full) : md(full);
          resolve();
        }
      })();
    });
    return d;
  }

  // ---- karaoke: highlight the words as they are spoken -----------------
  // Wrap every word in the rendered text in a span, remembering its
  // character offsets in the plain text. Offsets let us map the audio's
  // playhead back onto the words: position = currentTime / duration.
  function karaokeWords(root) {
    if (!root) return [];
    const words = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const nodes = [];
    let n;
    while ((n = walker.nextNode())) {
      if (n.parentElement && n.parentElement.closest(".run-stats")) continue;
      nodes.push(n);
    }
    let offset = 0;
    let pastClock = false;   // the clock line is never spoken
    for (const node of nodes) {
      const value = node.nodeValue || "";
      if (!value) continue;
      if (!value.trim()) { offset += value.length; continue; }
      const frag = document.createDocumentFragment();
      for (const part of value.split(/(\s+)/)) {
        if (!part) continue;
        if (/^\s+$/.test(part)) {
          frag.appendChild(document.createTextNode(part));
          offset += part.length;
          continue;
        }
        if (part.indexOf("\u23f1") >= 0) pastClock = true;
        const span = document.createElement("span");
        span.className = "kw";
        span.textContent = part;
        frag.appendChild(span);
        offset += part.length;
        // A word takes roughly its length in speech time, plus the
        // pause the voice takes after it. Weighting by that (instead of
        // spreading the words evenly over the audio) is what keeps the
        // highlight on the word being spoken.
        const tail = part.match(/[.,!?;:\u2014-]+$/);
        let weight = part.length;
        if (tail) weight += /[.!?;:]/.test(tail[0]) ? 4 : 2;
        words.push({ node: span, start: offset - part.length, end: offset,
                     weight: weight, spoken: !pastClock });
      }
      node.parentNode.replaceChild(frag, node);
    }
    return words;
  }

  function karaokeSync(words, audio) {
    // Light the words the voice has passed; the current one glows.
    // The timeline is weighted (see karaokeWords): the voice takes
    // longer over long words and pauses at punctuation, so spreading
    // the words evenly over the audio would drift visibly.
    let raf = 0;
    const spoken = words.filter((w) => w.spoken);
    const totalW = spoken.reduce((s, w) => s + w.weight, 0) || 1;
    const tick = () => {
      const dur = (audio.duration && isFinite(audio.duration)) ? audio.duration : 0;
      if (dur > 0) {
        const pos = (audio.currentTime / dur) * totalW;
        let acc = 0;
        for (const w of words) {
          if (!w.spoken) { acc += w.weight; continue; }
          const next = acc + w.weight;
          const done = pos >= next;
          const now = !done && pos >= acc;
          w.node.classList.toggle("kw-done", done);
          w.node.classList.toggle("kw-now", now);
          acc = next;
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      for (const w of words) {
        w.node.classList.remove("kw-done", "kw-now");
      }
    };
  }

  // ---- attached images: rendered as <img>, not typed text --------------
  const IMG_MARKER = "![attached image](";
  function addMsgImage(kind, dataUrl, t) {
    const d = document.createElement("div");
    d.className = "msg " + kind;
    if (t) {
      const td = document.createElement("div");
      td.className = "t";
      td.textContent = t;
      d.appendChild(td);
    }
    const img = document.createElement("img");
    img.src = dataUrl;
    img.className = "chat-image";
    img.alt = "attached image";
    d.appendChild(img);
    thread.appendChild(d);
    thread.scrollTop = thread.scrollHeight;
    return d;
  }
  function renderMaybeImage(kind, text, t) {
    if (text && text.indexOf(IMG_MARKER) !== -1) {
      const parts = text.split(IMG_MARKER);
      if (parts[0].trim()) addMsg(kind, parts[0].trim(), t);
      for (let i = 1; i < parts.length; i++) {
        const end = parts[i].indexOf(")");
        if (end === -1) { addMsg(kind, IMG_MARKER + parts[i], null); continue; }
        addMsgImage(kind, parts[i].slice(0, end), i === 1 ? t : null);
        const rest = parts[i].slice(end + 1).trim();
        if (rest) addMsg(kind, rest, null);
      }
      return true;
    }
    return false;
  }

  // ---- TTS playback: play button toggles to stop while playing --------
  // Audio routes through a Web Audio analyser that drives the avatar's
  // lip sync (mouth open from RMS level) and the waveform bars under
  // the avatar circle — same pattern as the rest of this WebUI.
  let currentAudio = null;
  let currentBtn = null;
  let voiceAnnounce = false;
  let ttsAvailable = false;
  let ttsCtx = null, ttsAnalyser = null, ttsAnalyserData = null;
  let ttsMouthLevel = 0, ttsMouthLoop = false, ttsPlaying = false;
  let micCtx = null, micAnalyser = null, micAnalyserData = null;
  const waveCvs = document.getElementById("voice-wave");
  const WAVE_BARS = 26;
  const waveLevels = new Array(WAVE_BARS).fill(0);
  let waveFreq = null;

  function getTtsAnalyser() {
    if (!ttsCtx) {
      ttsCtx = new (window.AudioContext || window.webkitAudioContext)();
      ttsAnalyser = ttsCtx.createAnalyser();
      ttsAnalyser.fftSize = 256;
      ttsAnalyser.connect(ttsCtx.destination);
      ttsAnalyserData = new Float32Array(ttsAnalyser.fftSize);
    }
    if (ttsCtx.state === "suspended") ttsCtx.resume().catch(() => {});
    return ttsAnalyser;
  }

  function drawWave() {
    if (!waveCvs) return;
    const ctx = waveCvs.getContext("2d");
    const W = waveCvs.width, H = waveCvs.height;
    ctx.clearRect(0, 0, W, H);
    let freq = null;
    let micTime = null;
    if (recorder && micAnalyser) {
      if (!micAnalyserData || micAnalyserData.length !== micAnalyser.fftSize)
        micAnalyserData = new Uint8Array(micAnalyser.fftSize);
      micAnalyser.getByteTimeDomainData(micAnalyserData);
      micTime = micAnalyserData;
    } else if (ttsPlaying && ttsAnalyser) {
      if (!waveFreq || waveFreq.length !== ttsAnalyser.frequencyBinCount)
        waveFreq = new Uint8Array(ttsAnalyser.frequencyBinCount);
      ttsAnalyser.getByteFrequencyData(waveFreq);
      freq = waveFreq;
    }
    const mid = H / 2, bw = W / WAVE_BARS;
    const lt = document.documentElement.dataset.theme === "light";
    for (let i = 0; i < WAVE_BARS; i++) {
      let target = 0.06; // idle floor
      if (micTime) {
        const stride = Math.max(1, Math.floor(micTime.length / WAVE_BARS));
        let peak = 0;
        for (let j = i * stride; j < Math.min(micTime.length, (i + 1) * stride); j++)
          peak = Math.max(peak, Math.abs(micTime[j] - 128) / 128);
        target = 0.08 + Math.min(1, peak * 2.4);
      } else if (freq) {
        const b0 = 2 + Math.floor(i * 44 / WAVE_BARS);
        const v = ((freq[b0] || 0) + (freq[b0 + 1] || 0)) / 2 / 255;
        target = 0.06 + Math.min(1, v * 1.7);
      }
      const lv = waveLevels[i];
      waveLevels[i] = lv + (target - lv) * (target > lv ? 0.55 : 0.3);
      const h = Math.max(1.5, waveLevels[i] * (H - 2));
      const grad = ctx.createLinearGradient(0, mid - h / 2, 0, mid + h / 2);
      grad.addColorStop(0, lt ? "#4d9fff" : "#7de9ff");
      grad.addColorStop(1, lt ? "#0064e0" : "#1a9ec4");
      ctx.fillStyle = grad;
      ctx.fillRect(i * bw + bw * 0.22, mid - h / 2, Math.max(1, bw * 0.56), h);
    }
    if (recorder) requestAnimationFrame(drawWave);
  }

  function mouthLoop() {
    if (!ttsMouthLoop) return;
    let target = 0;
    if (ttsPlaying && ttsAnalyser && ttsAnalyserData) {
      ttsAnalyser.getFloatTimeDomainData(ttsAnalyserData);
      let sum = 0;
      for (let i = 0; i < ttsAnalyserData.length; i++) {
        const v = ttsAnalyserData[i];
        sum += v * v;
      }
      const rms = Math.sqrt(sum / ttsAnalyserData.length);
      target = Math.max(0, Math.min(1, (rms - 0.012) * 9.5));
    }
    const coeff = target > ttsMouthLevel ? 0.65 : 0.28;
    ttsMouthLevel += (target - ttsMouthLevel) * coeff;
    if (window.aidonSetMouthOpen) window.aidonSetMouthOpen(ttsMouthLevel);
    drawWave();
    if (!ttsPlaying && ttsMouthLevel < 0.01) {
      ttsMouthLoop = false;
      ttsMouthLevel = 0;
      if (window.aidonSetMouthOpen) window.aidonSetMouthOpen(0);
      drawWave();
      return;
    }
    requestAnimationFrame(mouthLoop);
  }

  function startMouthLoop() {
    if (ttsMouthLoop) return;
    ttsMouthLoop = true;
    requestAnimationFrame(mouthLoop);
  }
  fetch("/api/voice").then((r) => r.json()).then((v) => {
    voiceAnnounce = !!v.announce;
    ttsAvailable = !!v.tts_available;
  }).catch(() => {});

  let stopKaraokeActive = null;  // cancels the rAF loop of the current voice
  let currentAudioUrl = null;    // blob URL to revoke when playback ends
  let currentAudioCancel = null;
  function resetSpeakBtn() {
    // Every stop path (ended, error, toggled off, replaced by a newer
    // message) must land here: pause() fires no "ended", so the rAF
    // loop used to keep running at 60 fps for the rest of the session.
    if (stopKaraokeActive) {
      try { stopKaraokeActive(); } catch (e) { /* already gone */ }
      stopKaraokeActive = null;
    }
    if (currentAudioUrl) {
      try { URL.revokeObjectURL(currentAudioUrl); } catch (e) { /* noop */ }
      currentAudioUrl = null;
    }
    if (currentAudioCancel) {
      try { currentAudioCancel(); } catch (e) { /* noop */ }
      currentAudioCancel = null;
    }
    if (currentBtn) {
      currentBtn.textContent = "▶";
      currentBtn.classList.remove("playing");
      currentBtn.title = "Play aloud";
    }
    currentAudio = null;
    currentBtn = null;
    ttsPlaying = false;
  }

  let speakGen = 0;   // playback generation: only the newest wins

  // Fish can send MP3 bytes progressively. MediaSource lets the browser
  // start playback after the first audio chunk while the rest is arriving;
  // unsupported browsers use the buffered endpoint below.
  async function progressiveAudio(response) {
    const mime = "audio/mpeg";
    if (!window.MediaSource || !response.body ||
        !MediaSource.isTypeSupported(mime)) {
      try { if (response.body) await response.body.cancel(); } catch (e) {}
      return null;
    }
    const mediaSource = new MediaSource();
    const url = URL.createObjectURL(mediaSource);
    const audio = new Audio(url);
    const reader = response.body.getReader();
    const queue = [];
    let sourceBuffer = null;
    let ended = false;
    let firstChunk = null;
    const firstAudio = new Promise((resolve, reject) => {
      firstChunk = { resolve, reject };
    });
    const pump = () => {
      if (!sourceBuffer || sourceBuffer.updating || !queue.length) {
        if (ended && sourceBuffer && !sourceBuffer.updating && !queue.length &&
            mediaSource.readyState === "open") {
          try { mediaSource.endOfStream(); } catch (e) {}
        }
        return;
      }
      try { sourceBuffer.appendBuffer(queue.shift()); }
      catch (e) { if (firstChunk) firstChunk.reject(e); }
    };
    mediaSource.addEventListener("sourceopen", () => {
      try {
        sourceBuffer = mediaSource.addSourceBuffer(mime);
        sourceBuffer.addEventListener("updateend", () => {
          if (firstChunk) {
            firstChunk.resolve({ audio, url, cancel: () => reader.cancel() });
            firstChunk = null;
          }
          pump();
        });
        pump();
      } catch (e) { if (firstChunk) firstChunk.reject(e); }
    }, { once: true });
    (async () => {
      try {
        while (true) {
          const item = await reader.read();
          if (item.done) break;
          if (item.value && item.value.byteLength) {
            queue.push(item.value.slice().buffer);
            pump();
          }
        }
        ended = true;
        pump();
        if (firstChunk && !queue.length) {
          firstChunk.reject(new Error("empty TTS stream"));
          firstChunk = null;
        }
      } catch (e) {
        if (firstChunk) firstChunk.reject(e);
      }
    })();
    try { return await firstAudio; }
    catch (e) {
      try { reader.cancel(); } catch (ignored) {}
      URL.revokeObjectURL(url);
      return null;
    }
  }

  async function speak(el, text, btn) {
    const gen = ++speakGen;
    if (currentBtn === btn && currentAudio) {  // toggle: stop
      currentAudio.pause();
      resetSpeakBtn();
      return;
    }
    if (currentAudio) {  // only one voice at a time
      currentAudio.pause();
      resetSpeakBtn();
    }
    btn.textContent = "…";
    try {
      // Wait for the typewriter to finish before touching the DOM: it
      // re-renders the body every frame, which would wipe the karaoke
      // spans. The voice then starts exactly when the text is complete.
      const body = (el && el._body) || null;
      if (el && el._typing) { try { await el._typing; } catch (e) {} }
      if (gen !== speakGen) return;
      // Speak what is ON SCREEN, not the markdown source: no "**" and
      // no link syntax, and the words match the highlight exactly.
      // The clock line is stripped - it is for the eye.
      const spoken = spokenText(body ? body.textContent : (text || ""));
      const request = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: spoken }),
      };
      const streamResponse = await fetch("/api/tts/stream", request);
      if (!streamResponse.ok) throw new Error("tts " + streamResponse.status);
      const progressive = await progressiveAudio(streamResponse);
      let audio;
      let url;
      if (progressive) {
        audio = progressive.audio;
        url = progressive.url;
        currentAudioCancel = progressive.cancel;
      } else {
        // Safari, older browsers, and unusual MP3 implementations use the
        // complete-response path without changing user behavior.
        const r = await fetch("/api/tts", request);
        if (!r.ok) throw new Error("tts " + r.status);
        url = URL.createObjectURL(await r.blob());
        audio = new Audio(url);
        currentAudioCancel = null;
      }
      // A newer playback started while this one was downloading: drop
      // it, or the two voices talk over each other.
      if (gen !== speakGen) {
        if (currentAudioCancel) {
          try { currentAudioCancel(); } catch (e) { /* noop */ }
          currentAudioCancel = null;
        }
        URL.revokeObjectURL(url);
        return;
      }
      // Route through the analyser for lip sync + waveform (no-op if
      // Web Audio is unavailable — falls back to plain playback).
      try {
        const analyser = getTtsAnalyser();
        const src = ttsCtx.createMediaElementSource(audio);
        src.connect(analyser);
      } catch (e) { /* plain playback */ }
      currentAudio = audio;
      currentAudioUrl = url;
      currentBtn = btn;
      btn.textContent = "⏹";
      btn.classList.add("playing");
      btn.title = "Stop";
      ttsPlaying = true;
      startMouthLoop();
      // Karaoke: the words light up as the voice reaches them.
      // resetSpeakBtn() owns cancellation and the blob URL, so a
      // stopped/replaced playback cannot leave either behind.
      if (body) {
        const words = karaokeWords(body);
        stopKaraokeActive = karaokeSync(words, audio);
      }
      audio.onended = () => resetSpeakBtn();
      audio.onerror = () => resetSpeakBtn();
      await audio.play();  // may reject before first user gesture; fine
    } catch (e) {
      resetSpeakBtn();
    }
  }

  function playButton(text, el) {
    const b = document.createElement("button");
    b.className = "tts-play";
    b.textContent = "▶";
    b.title = "Play aloud";
    b.addEventListener("click", () => speak(el, text, b));
    return b;
  }

  function maybeAnnounce(text, btn) {
    // Running commentary: no element, so no karaoke highlight - it is
    // spoken as plain text.
    if (voiceAnnounce && ttsAvailable) speak(null, text, btn).catch(() => {});
  }

  // The elapsed marker is for the eye, not the ear: "17m 26s" read
  // aloud is noise. Strip it from whatever we speak.
  // The avatar wears what the agent is doing: focused while it works,
  // smiling when the run is over.
  function setAvatarMood(mood) {
    if (window.aidonSetMood) {
      try { window.aidonSetMood(mood); } catch (e) {}
    }
  }

  function spokenText(text) {
    // The metrics line is always the LAST line ("⏱️ 2m 4s" or the
    // compact WebUI "◷ 2m 4s", optionally
    // with "(plus 3s waiting for you)"), so cut from the marker to the
    // end of that line.
    const value = String(text || "");
    const clock = Math.max(value.lastIndexOf("⏱"), value.lastIndexOf("◷"));
    return (clock >= 0 ? value.slice(0, clock) : value).trim();
  }

  // Speak the two moments that MATTER: a decision is waiting for you,
  // and the run is over. Everything in between stays silent unless
  // AIDON_VOICE_ANNOUNCE=1 asks for the running commentary.
  function announceImportant(el, text, btn) {
    if (!ttsAvailable) return;
    const say = spokenText(text);
    if (say) speak(el, say, btn).catch(() => {});
  }

  function renderStages(stage) {
    stagesEl.innerHTML = "";
    const idx = STAGES.indexOf(stage);
    const icons = {
      interpret: "⌁", research: "⌕", recall: "◈", plan: "◇",
      sandbox: "□", approval: "◌", live: "▶", learn: "✦", done: "✓",
    };
    STAGES.forEach((s, i) => {
      const p = document.createElement("span");
      p.className = "stage" + (i === idx ? " active" : "") + (i < idx ? " done" : "");
      const icon = document.createElement("span");
      icon.className = "stage-ico";
      icon.textContent = icons[s] || "·";
      p.appendChild(icon);
      p.appendChild(document.createTextNode(s));
      stagesEl.appendChild(p);
    });
  }

  const STAGE_LABELS = {
    interpret: "Understanding your request…",
    research: "Researching the web…",
    recall: "Recalling past experience…",
    plan: "Building a plan…",
    sandbox: "Testing in sandbox…",
    approval: "Waiting for your approval…",
    live: "Executing…",
    learn: "Learning from this run…",
    done: "Done",
    respond: "Responding…",
  };

  function stageLabel(stage) {
    return STAGE_LABELS[stage] || ("Working · " + stage);
  }

  function setStatus(mode, text) {
    statusDot.className = mode;
    statusText.textContent = text;
  }

  function renderTrace(items) {
    if (!items || !items.length) return;
    const list = document.getElementById("thinking-list");
    items.forEach((e) => {
      if (e.phase !== lastTracePhase) {
        lastTracePhase = e.phase;
        const h = document.createElement("div");
        h.className = "think-phase";
        h.textContent = e.phase;
        list.appendChild(h);
      }
      const item = document.createElement("div");
      item.className = "think-item " + (e.kind || "note");
      const meta = e.seconds + "s · " + e.tokens_in + "→" + e.tokens_out + " tok";
      // Expandable: whole header row toggles; title + token count always visible
      item.innerHTML =
        '<div class="think-head-row" title="' + (e.thinking ? "expand thinking" : "") + '">' +
        (e.thinking
          ? '<button class="think-toggle" title="expand">›</button>'
          : '<span class="think-toggle" style="visibility:hidden">›</span>') +
        '<div class="think-title">' + esc(e.title) + "</div>" +
        '<div class="think-meta">' + esc(meta) + "</div></div>" +
        (e.thinking
          ? '<div class="think-body"><div class="think-detail">' + esc(e.title) +
            '</div><pre class="think-thinking">' + esc(e.thinking) + "</pre></div>"
          : "");
      if (e.thinking) {
        item.querySelector(".think-head-row").addEventListener("click", () => {
          item.classList.toggle("open");
        });
      }
      list.appendChild(item);
    });
    const count = document.getElementById("thinking-count");
    if (count) count.textContent = "(" + (lastTraceCount + items.length) + ")";
    list.scrollTop = list.scrollHeight;
  }

  document.getElementById("thinking-toggle-all").addEventListener("click", () => {
    const items = document.querySelectorAll(".think-item");
    const anyClosed = [...items].some((i) => !i.classList.contains("open"));
    items.forEach((i) => i.classList.toggle("open", anyClosed));
  });

  function renderReplay(replay) {
    if (!replay || !replay.length) return "<p>No tool calls recorded.</p>";
    return replay.map((r) => {
      const args = Object.entries(r.args || {}).map(([k, v]) =>
        k + "=" + String(v).slice(0, 120)).join(" ");
      return '<div class="replay-item"><span class="tool">' + esc(r.tool) +
        "</span> " + esc(args) + "<pre>" + esc(r.result || "") + "</pre></div>";
    }).join("");
  }

  function showApprovalCard(run, pending) {
    if (cardShownFor === run.run_id + pending.title) return;
    cardShownFor = run.run_id + pending.title;
    approvalSlot.innerHTML = ""; // only one active card per run — replace, don't stack
    const card = document.createElement("div");
    card.className = "card approval-card";
    // Domain-grant cards get three explicit one-click choices —
    // temporary, permanent, reject — instead of the Edit dance
    // (the permanent button sends the edited/PERMANENT decision the
    // gate already understands; no typing, no second click).
    const isDomain = pending.kind === "domain";
    card.innerHTML =
      '<div class="kind">approval required — ' + esc(pending.kind) + "</div>" +
      "<h3>" + esc(pending.title) + "</h3>" +
      "<pre>" + esc(pending.body) + "</pre>" +
      (isDomain ? "" :
        '<textarea id="edit-text" placeholder="Edited text — press Submit to send…"></textarea>' +
        '<div id="edit-submit-row" class="row" style="display:none; justify-content:flex-end">' +
        '<button data-d="submit-edited" style="flex:none; padding:9px 18px">Submit</button></div>') +
      '<details><summary>Run replay — what Aidon did (' + run.replay.length +
      ' tool calls)</summary>' + renderReplay(run.replay) + "</details>" +
      '<div class="row">' +
      (isDomain
        ? '<button class="approve" data-d="approved">Approve Temporary</button>' +
          '<button data-d="permanent">Approve Permanently</button>' +
          '<button class="reject" data-d="rejected">Reject</button>'
        : '<button class="approve" data-d="approved">Approve</button>' +
          '<button data-d="edited">Edit</button>' +
          '<button class="reject" data-d="rejected">Reject</button>') +
      "</div>";
    approvalSlot.appendChild(card);
    const speakBtn = playButton(pending.title + ". " + pending.body, null);
    card.querySelector(".row").appendChild(speakBtn);
    // A waiting decision is worth hearing about - always, not only
    // when the running commentary is switched on.
    announceImportant(null, pending.title + ". " + pending.body, speakBtn);
    const ta = card.querySelector("#edit-text");
    const submitRow = card.querySelector("#edit-submit-row");
    const editBtn = card.querySelector('[data-d="edited"]');
    if (editBtn) editBtn.addEventListener("click", () => {
      ta.style.display = ta.style.display === "block" ? "none" : "block";
      if (submitRow) submitRow.style.display = ta.style.display;
      if (ta.style.display === "block") return; // box open: Submit sends
      decide(run.run_id, "edited", ta.value); // second Edit click still sends
    });
    const submitBtn = card.querySelector('[data-d="submit-edited"]');
    if (submitBtn) submitBtn.addEventListener("click", () =>
      decide(run.run_id, "edited", ta.value));
    const permBtn = card.querySelector('[data-d="permanent"]');
    if (permBtn) permBtn.addEventListener("click", () =>
      decide(run.run_id, "edited", "PERMANENT"));
    card.querySelector('[data-d="approved"]').addEventListener("click", () =>
      decide(run.run_id, "approved", ""));
    card.querySelector('[data-d="rejected"]').addEventListener("click", () =>
      decide(run.run_id, "rejected", ""));
  }

  async function decide(runId, decision, editedText) {
    try {
      const r = await fetch("/api/runs/" + runId + "/approval", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision, edited_text: editedText || "" }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        addMsg("stage-line",
          "approval failed: " + (data.detail || data.error || r.status));
        if (r.status === 409) {
          // Server has no pending approval: this card is dead (the gate
          // failed closed on timeout, or it was already decided). Remove
          // it instead of leaving tappable buttons that all fail again.
          cardShownFor = null;
          approvalSlot.innerHTML = "";
        }
        return;
      }
    } catch (e) {
      addMsg("stage-line", "approval failed: " + e.message);
      return;
    }
    // Decision accepted — drop the card and reset the shown-card tracking so
    // a repeated gate of the same kind renders fresh instead of hanging.
    cardShownFor = null;
    approvalSlot.innerHTML = "";
  }

  async function poll() {
    if (!currentRun) return;
    try {
      const r = await fetch("/api/runs/" + currentRun +
        "?since=" + lastEventCount + "&trace_since=" + lastTraceCount);
      if (!r.ok) return;
      const run = await r.json();
      renderStages(run.stage);
      setStatus(run.status === "running" ? "busy" : "ok",
                run.status === "running" ? stageLabel(run.stage) : run.status);
      setAvatarMood(run.status === "running" ? "working" : "idle");
      // append new events (the server returns only the unseen tail)
      let sawDecision = false;
      run.events.forEach((e) => {
        if (e.kind === "user") {
          if (!renderMaybeImage("user", e.text, e.t))
            addMsg("user", e.text, e.t);
        }
        else if (e.kind === "aidon" || e.kind === "aidon-final") {
          const m = addMsgTyped("aidon", e.text, e.t, e.kind === "aidon-final");
          const btn = playButton(e.text || "", m);
          m.appendChild(btn);
          if (e.kind === "aidon-final") {
            // The run is over: the avatar smiles, the answer is read
            // (never the elapsed clock - that is for the eye), with the
            // words lighting up as they are spoken.
            setAvatarMood("happy");
            setTimeout(() => setAvatarMood("idle"), 7000);
            announceImportant(m, e.text, btn);
          } else if (e.text) {
            // Running commentary: only when AIDON_VOICE_ANNOUNCE=1.
            maybeAnnounce(e.text, btn);
          }
        }
        // stage events are NOT rendered as chat bubbles — the stages bar
        // + status line already show "working · interpret" etc. (one
        // status line, not a bubble per phase)
        else if (e.kind === "approval-request") {
          const m = addMsg("stage-line", "approval requested: " + e.text);
          m.appendChild(playButton("Approval requested. " + e.text));
        }
        else if (e.kind === "approval-decision") {
          sawDecision = true;
          addMsg("stage-line", e.text);
        }
      });
      lastEventCount = (typeof run.event_total === "number")
        ? run.event_total : lastEventCount + run.events.length;
      if (run.trace && run.trace.length) {
        // Skip interpret/respond phases in the thinking panel — that's
        // routing noise for casual chat, not real thinking. Pipeline runs
        // (research, plan, ...) still show their trace.
        const items = run.trace.filter((e) =>
          e.phase !== "interpret" && e.phase !== "respond");
        if (items.length) renderTrace(items);
      }
      if (typeof run.trace_total === "number")
        lastTraceCount = run.trace_total;
      if (run.pending) {
        showApprovalCard(run, run.pending);
      } else if (cardShownFor !== null && run.status === "running") {
        // Approval resolved without a UI decision in this tab: the gate
        // failed closed on timeout and the server cleared pending, but
        // the slot was never cleared mid-run, leaving a dead card whose
        // buttons all fail with 409. Remove the card. Resetting
        // cardShownFor first keeps this one-shot: later polls stay
        // silent. A decision event in this same batch means the card
        // was decided elsewhere, so skip the timeout line in that case.
        cardShownFor = null;
        approvalSlot.innerHTML = "";
        if (!sawDecision) {
          addMsg("stage-line",
            "approval timed out, treated as rejected — card removed");
        }
      }
      if (run.status === "running") {
        // Chat-first questions: while the run waits for a question
        // answer, the composer must stay usable, or the server's
        // pending-question path (POST /api/goals returns the same
        // run id) is unreachable and the run deadlocks waiting for
        // an answer the user cannot send. While running and NOT
        // waiting, the composer stays disabled so a new goal cannot
        // queue behind the active run. Guidance stays disabled
        // while answering: an answer is a chat message, not a new
        // goal with guidance.
        const waiting = !!run.waiting_question;
        sendBtn.disabled = !waiting;
        input.disabled = !waiting;
        guidanceInput.disabled = true;
        input.placeholder = waiting
          ? "Answer Aidon's question…"
          : "Chat or give Aidon a goal…";
      }
      if (run.status !== "running") {
        clearInterval(pollTimer);
        pollTimer = null;
        sendBtn.disabled = false;
        input.disabled = false;
        guidanceInput.disabled = false;
        input.placeholder = "Chat or give Aidon a goal…";
        steerRow.hidden = true;
        approvalSlot.innerHTML = ""; // no stale card survives completion
        // loadHistory lives in the sidebar IIFE below (exposed on window)
        if (window.aidonLoadHistory) window.aidonLoadHistory();
        // no "Finished" chat line — the stage bar + status already show it
      }
    } catch (e) {
      console.error("poll failed:", e);
    }
  }

  // ---- attachments (upload button + camera) ---------------------------
  // One import batch holds at most 3 files (images + documents
  // combined); the server keeps at most 5 active per conversation.
  let pendingImages = [];  // image File objects
  let pendingFiles = [];  // document File objects
  const imgPreview = document.getElementById("img-preview");
  function refreshPreview() {
    imgPreview.innerHTML = "";
    imgPreview.hidden = pendingImages.length === 0 && pendingFiles.length === 0;
    pendingImages.forEach((f, i) => {
      const wrap = document.createElement("span");
      wrap.className = "img-thumb-wrap";
      const img = document.createElement("img");
      img.className = "img-thumb";
      img.src = URL.createObjectURL(f);
      const x = document.createElement("button");
      x.className = "img-thumb-x";
      x.textContent = "×";
      x.title = "Remove";
      x.addEventListener("click", () => {
        pendingImages.splice(i, 1);
        refreshPreview();
      });
      wrap.appendChild(img);
      wrap.appendChild(x);
      imgPreview.appendChild(wrap);
    });
    pendingFiles.forEach((f, i) => {
      const wrap = document.createElement("span");
      wrap.className = "img-thumb-wrap";
      const label = document.createElement("span");
      label.className = "img-thumb";
      label.textContent = "📎 " + f.name;
      label.title = f.name;
      const x = document.createElement("button");
      x.className = "img-thumb-x";
      x.textContent = "×";
      x.title = "Remove";
      x.addEventListener("click", () => {
        pendingFiles.splice(i, 1);
        refreshPreview();
      });
      wrap.appendChild(label);
      wrap.appendChild(x);
      imgPreview.appendChild(wrap);
    });
  }
  function pickImages() {
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = "image/*,.pdf,.doc,.docx,.xls,.xlsx,.csv,.tsv,.ppt,.pptx,.txt,.md,.json,.html,.htm,.epub,.rtf,.odt,.ods,.py,.js,.ts,.tsx,.jsx,.mjs,.java,.c,.h,.cpp,.hpp,.go,.rs,.rb,.php,.sh,.yaml,.yml,.toml,.xml,.zip";
    inp.multiple = true;
    inp.addEventListener("change", () => {
      for (const f of inp.files) {
        if (pendingImages.length + pendingFiles.length >= 3) break;
        if (f.type.startsWith("image/")) {
          pendingImages.push(f);
        } else if (f.size <= 25 * 1024 * 1024) {
          pendingFiles.push(f);
        }
      }
      refreshPreview();
    });
    inp.click();
  }
  document.getElementById("attach-btn").addEventListener("click",
    () => pickImages());

  // ---- take photo: real webcam capture (getUserMedia) -------------------
  // The camera button is disabled up front when the device has no webcam
  // (no mediaDevices API, no videoinput device, or a denied/busy camera)
  // so the button never silently opens a file picker instead.
  const camBtn = document.getElementById("camera-btn");
  let camStream = null;
  let camVideo = null;

  function stopCam() {
    if (camStream) {
      camStream.getTracks().forEach((t) => t.stop());
      camStream = null;
    }
    if (camVideo) camVideo.srcObject = null;
  }

  function cameraUnavailable(reason) {
    camBtn.disabled = true;
    camBtn.title = reason;
    camBtn.setAttribute("aria-disabled", "true");
  }

  async function cameraState() {
    // getUserMedia only exists in secure contexts (https or localhost).
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      return "no-api";
    }
    try {
      const devices = await navigator.mediaDevices.enumerateDevices();
      if (!devices.some((d) => d.kind === "videoinput")) return "no-device";
    } catch (e) {
      // enumeration can reject before any permission grant; the
      // click-time getUserMedia error handling is the reliable backstop.
    }
    return "ok";
  }

  function ensureCamModal() {
    let overlay = document.getElementById("cam-overlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "cam-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="cam-panel" role="dialog" aria-label="Take a photo">' +
      '<div class="cam-title">Take a photo</div>' +
      '<video id="cam-video" autoplay playsinline muted></video>' +
      '<div class="cam-actions">' +
      '<button id="cam-cancel" type="button">Cancel</button>' +
      '<button id="cam-capture" type="button">Capture</button>' +
      "</div></div>";
    document.body.appendChild(overlay);
    camVideo = overlay.querySelector("#cam-video");
    overlay.querySelector("#cam-cancel").addEventListener("click", closeCam);
    overlay.querySelector("#cam-capture").addEventListener("click", shootCam);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeCam();
    });
    return overlay;
  }

  function closeCam() {
    stopCam();
    const o = document.getElementById("cam-overlay");
    if (o) o.hidden = true;
  }

  async function openCamera() {
    if (camBtn.disabled) return;
    const state = await cameraState();
    if (state === "no-api") {
      cameraUnavailable("Camera needs https or localhost");
      return;
    }
    if (state === "no-device") {
      cameraUnavailable("No webcam detected");
      return;
    }
    const overlay = ensureCamModal();
    try {
      // rear camera first (phones), then any camera (desktops)
      camStream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" }, audio: false,
      });
    } catch (first) {
      try {
        camStream = await navigator.mediaDevices.getUserMedia({
          video: true, audio: false,
        });
      } catch (err) {
        stopCam();
        const reasons = {
          NotAllowedError: "Camera blocked - allow access, then reload",
          NotFoundError: "No webcam detected",
          NotReadableError: "Webcam busy (another app is using it)",
          SecurityError: "Camera needs https or localhost",
        };
        cameraUnavailable(reasons[err.name] || "Camera unavailable");
        return;
      }
    }
    camVideo.srcObject = camStream;
    overlay.hidden = false;
  }

  function shootCam() {
    if (!camStream || !camVideo) return;
    if (pendingImages.length + pendingFiles.length >= 3) { closeCam(); return; }
    const canvas = document.createElement("canvas");
    canvas.width = camVideo.videoWidth || 1280;
    canvas.height = camVideo.videoHeight || 720;
    canvas.getContext("2d").drawImage(
      camVideo, 0, 0, canvas.width, canvas.height);
    canvas.toBlob((blob) => {
      if (!blob) { closeCam(); return; }
      pendingImages.push(new File(
        [blob], "camera-" + Date.now() + ".jpg", { type: "image/jpeg" }));
      refreshPreview();
      closeCam();
    }, "image/jpeg", 0.92);
  }

  camBtn.addEventListener("click", openCamera);
  cameraState().then((state) => {
    if (state === "no-api") cameraUnavailable("Camera needs https or localhost");
    if (state === "no-device") cameraUnavailable("No webcam detected");
  });

  async function startRun() {
    const goal = input.value.trim();
    if (!goal || sendBtn.disabled) return;
    const previousRun = currentRun;
    const guidance = guidanceInput.value.trim();
    const imgs = pendingImages.slice();
    const fls = pendingFiles.slice();
    const files = imgs.concat(fls);
    pendingImages = [];
    pendingFiles = [];
    refreshPreview();
    input.value = "";
    guidanceInput.value = "";
    sendBtn.disabled = true;
    input.disabled = true;
    guidanceInput.disabled = true;
    // Cursor / thread resets happen only for a genuinely new run,
    // after POST succeeds (below). For an answer to a waiting
    // question the server returns the SAME run id, and resetting
    // here would re-append the whole thread from event 0.
    // Continue the open conversation (if any) in the selected
    // project; the server creates a persistent conversation when
    // this is the first message of a new chat.
    const convCtx = window.aidonConversation
      ? window.aidonConversation() : {};
    let data;
    try {
      if (files.length) {
        const fd = new FormData();
        fd.append("goal", goal);
        fd.append("guidance", guidance);
        if (convCtx.conversation_id)
          fd.append("conversation_id", convCtx.conversation_id);
        if (convCtx.project_id) fd.append("project_id", convCtx.project_id);
        for (const f of files) fd.append("files", f, f.name);
        const r = await fetch("/api/goals/with-images", {
          method: "POST",
          body: fd,
        });
        data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      } else {
        const r = await fetch("/api/goals", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            goal, guidance,
            conversation_id: convCtx.conversation_id || "",
            project_id: convCtx.project_id || "",
          }),
        });
        data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.detail || ("HTTP " + r.status));
      }
      if (!data || !data.run_id)
        throw new Error((data && data.detail) || "the server returned no run");
    } catch (err) {
      // No failure path used to exist: the composer stayed disabled and
      // poll() bailed on the missing run id, so the page was dead until
      // a reload. Restore everything the user had.
      addMsg("stage-line", "could not start the run: " +
             (err && err.message ? err.message : err));
      pendingImages = imgs;
      pendingFiles = fls;
      refreshPreview();
      if (previousRun) {
        // Answering a waiting question failed: the run is still
        // waiting, so keep polling it and leave the composer usable
        // for a retry — do not stop polling or hide the steer row.
        sendBtn.disabled = false;
        input.disabled = false;
        guidanceInput.disabled = true;
        return;
      }
      if (window.aidonStopPolling) window.aidonStopPolling();
      sendBtn.disabled = false;
      input.disabled = false;
      guidanceInput.disabled = false;
      setStatus("idle", "ready");
      steerRow.hidden = true;
      return;
    }
    if (data.conversation_id && window.aidonSetConversation)
      window.aidonSetConversation(data.conversation_id);
    if (previousRun && data.run_id === previousRun) {
      // Answer to a waiting question, not a new run: the server
      // delivered the message to the waiter and returned the same
      // run id. Keep the poll cursors, the thread, and the poll
      // interval — poll() will re-enable the composer if the next
      // question is already waiting, or keep it disabled while
      // the run proceeds.
      currentRun = data.run_id;
      setStatus("busy", "working");
      poll();
      return;
    }
    lastEventCount = 0;
    lastTraceCount = 0;
    lastTracePhase = null;
    // keep previous messages in the thread — just scroll to the latest
    document.getElementById("thinking-list").innerHTML = "";
    document.getElementById("thinking-count").textContent = "";
    cardShownFor = null;
    approvalSlot.innerHTML = "";
    currentRun = data.run_id;
    setStatus("busy", "working");
    renderStages("interpret");
    steerRow.hidden = false;
    // never orphan a poll interval if a previous run's timer is still alive
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    pollTimer = setInterval(poll, 2000);
    poll();
  }

  async function sendSteer() {
    const text = steerInput.value.trim();
    if (!text || !currentRun) return;
    steerInput.value = "";
    steerBtn.disabled = true;
    try {
      const r = await fetch("/api/runs/" + currentRun + "/steer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        addMsg("stage-line", "steer rejected: " + (data.detail || r.status));
      }
    } catch (e) {
      addMsg("stage-line", "steer failed: " + e.message);
    }
    steerBtn.disabled = false;
    steerInput.focus();
  }

  async function sendCancel() {
    if (!currentRun) return;
    cancelBtn.disabled = true;
    try {
      const r = await fetch("/api/runs/" + currentRun + "/cancel", {
        method: "POST",
      });
      if (!r.ok) {
        const data = await r.json().catch(() => ({}));
        addMsg("stage-line", "cancel rejected: " +
               (data.detail || r.status));
      } else {
        // The next poll sees status "cancelled" (not "running") and
        // re-enables the composer itself; this line is the immediate
        // acknowledgement, not the re-enable.
        addMsg("stage-line", "Run cancelled — chat re-enabled");
      }
    } catch (e) {
      addMsg("stage-line", "cancel failed: " + e.message);
    }
    cancelBtn.disabled = false;
  }

  sendBtn.addEventListener("click", startRun);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      startRun();
    }
  });
  const preapproveBtn = document.getElementById("preapprove-btn");
  if (preapproveBtn) preapproveBtn.addEventListener("click", async () => {
    const project = window.aidonConversation ? window.aidonConversation() : {};
    const specId = project.project_id || "default";
    if (!window.confirm("Preapprove reversible live actions for " + specId + " for 8 hours?")) return;
    preapproveBtn.disabled = true;
    try {
      const r = await fetch("/api/preapprovals", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ spec_id: specId, kind: "live", hours: 8 }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data.ok) throw new Error(data.error || ("HTTP " + r.status));
      preapproveBtn.classList.add("active");
      preapproveBtn.title = "Live actions preapproved for 8 hours";
      addMsg("stage-line", "Reversible live actions preapproved for " + specId + " for 8 hours.");
    } catch (e) {
      addMsg("stage-line", "Preapproval failed: " + e.message);
    } finally {
      preapproveBtn.disabled = false;
    }
  });
  steerBtn.addEventListener("click", sendSteer);
  if (cancelBtn) cancelBtn.addEventListener("click", sendCancel);
  steerInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendSteer();
  });

  // Opening a stored conversation (sidebar) must stop live polling,
  // or the running poll would keep appending into the opened thread.
  window.aidonStopPolling = () => {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    currentRun = null;
    steerRow.hidden = true;
    sendBtn.disabled = false;
    input.disabled = false;
    guidanceInput.disabled = false;
  };

  document.getElementById("new-chat").addEventListener("click", () => {
    // never orphan a poll interval or strand the composer mid-run
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    currentRun = null;
    // a new chat starts a fresh persistent conversation on first send
    if (window.aidonSetConversation) window.aidonSetConversation("");
    approvalSlot.innerHTML = "";
    cardShownFor = null;
    thread.innerHTML = "";
    document.getElementById("thinking-list").innerHTML = "";
    document.getElementById("thinking-count").textContent = "";
    document.querySelectorAll(".hist-item").forEach((x) => x.classList.remove("active"));
    sendBtn.disabled = false;
    input.disabled = false;
    guidanceInput.disabled = false;
    steerRow.hidden = true;
    input.focus();
  });

  renderStages("interpret");

  // ── theme: dark / light chooser, persisted ──────────────────────────
  const rootEl = document.documentElement;
  function setTheme(t) {
    rootEl.dataset.theme = t;
    try { localStorage.setItem("aidon-theme", t); } catch (e) {}
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = t === "dark" ? "◐" : "◑";
  }
  setTheme(localStorage.getItem("aidon-theme") || "dark");
  document.getElementById("theme-toggle").addEventListener("click", () => {
    setTheme(rootEl.dataset.theme === "dark" ? "light" : "dark");
  });

  // ── mic: browser MediaRecorder → /api/transcribe (Muse Voice
  // Transcribe) → text lands in the goal box. 503 = no key configured.
  const micBtn = document.getElementById("mic");
  let recorder = null;
  micBtn.addEventListener("click", async () => {
    if (recorder) { recorder.stop(); return; } // second click = stop
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      addMsg("stage-line", "mic unavailable: " + (e.message || e));
      return;
    }
    const chunks = [];
    const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
    const mimeType = mimeTypes.find((m) => window.MediaRecorder.isTypeSupported(m)) || "";
    try {
      micCtx = new (window.AudioContext || window.webkitAudioContext)();
      const source = micCtx.createMediaStreamSource(stream);
      micAnalyser = micCtx.createAnalyser();
      micAnalyser.fftSize = 256;
      source.connect(micAnalyser);
      micAnalyserData = new Uint8Array(micAnalyser.fftSize);
    } catch (e) {
      micCtx = null; micAnalyser = null; micAnalyserData = null;
    }
    recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
    recorder.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
      micBtn.classList.remove("recording");
      stream.getTracks().forEach((t) => t.stop());
      if (micCtx) micCtx.close().catch(() => {});
      micCtx = null; micAnalyser = null; micAnalyserData = null;
      recorder = null;
      const audioType = mimeType || "audio/webm";
      const blob = new Blob(chunks, { type: audioType });
      try {
        const r = await fetch("/api/transcribe", {
          method: "POST",
          headers: { "X-Audio-Name": audioType.includes("mp4") ? "mic.mp4" : "mic.webm",
                     "Content-Type": audioType },
          body: blob,
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || data.detail || ("HTTP " + r.status));
        if (data.text) {
          input.value = (input.value ? input.value + " " : "") + data.text;
          input.focus();
        } else {
          addMsg("stage-line", data.error || data.detail || "transcription failed");
        }
      } catch (e) {
        addMsg("stage-line", "transcription request failed: " + e.message);
      }
    };
    recorder.start();
    micBtn.classList.add("recording");
    drawWave();
  });

  drawWave(); // idle waveform frame on load
})();

/* ── left sidebar: projects, history, search, ideas, goals, library ── */
(function () {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // The chat IIFE owns live-run polling; opening a stored
  // conversation renders its saved exchanges straight into the
  // thread here, and the next send continues that conversation.
  const thread = document.getElementById("thread");
  const input = document.getElementById("goal-input");

  let currentProjectId = localStorage.getItem("aidon-project") || "";
  let currentConversationId = "";
  window.aidonConversation = () => ({
    conversation_id: currentConversationId,
    project_id: currentProjectId,
  });
  window.aidonSetConversation = (id) => {
    currentConversationId = id || "";
    markActiveConversation();
  };

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d) ? "" : d.toLocaleDateString(undefined,
      { month: "short", day: "numeric" });
  }
  function fmtSize(n) {
    if (!n && n !== 0) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return Math.round(n / 1024) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  // ---- view switching ----
  const navBtns = document.querySelectorAll(".nav-btn[data-view]");
  navBtns.forEach((b) => b.addEventListener("click", () => {
    navBtns.forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    document.querySelectorAll(".left-view").forEach(
      (v) => v.classList.remove("active"));
    document.getElementById("view-" + b.dataset.view).classList.add("active");
    if (b.dataset.view === "goals") loadGoals();
    if (b.dataset.view === "chat") loadHistory();
    if (b.dataset.view === "library") loadLibrary();
    if (b.dataset.view === "ideas") loadIdeas();
  }));

  // ---- projects ----
  async function deleteWithConfirm(url, confirmText) {
    if (!window.confirm(confirmText)) return false;
    try {
      const r = await fetch(url, { method: "DELETE" });
      if (!r.ok) {
        let msg = "Delete failed (HTTP " + r.status + ")";
        try { msg = (await r.json()).detail || msg; } catch (e) {}
        window.alert(msg);
        return false;
      }
      return true;
    } catch (e) { return false; }
  }
  async function loadProjects() {
    const list = document.getElementById("project-list");
    let projects = [];
    try {
      const data = await (await fetch("/api/projects")).json();
      projects = Array.isArray(data) ? data : (data.projects || []);
      projects = projects.map((p) => typeof p === "string"
        ? { id: p, name: p, conversation_count: 0 } : p);
    } catch (e) { projects = []; }
    if (!projects.length) { list.innerHTML = ""; return; }
    if (!projects.some((p) => p.id === currentProjectId))
      currentProjectId = projects[0].id;
    localStorage.setItem("aidon-project", currentProjectId);
    list.innerHTML = "";
    projects.forEach((p) => {
      const d = document.createElement("div");
      d.className = "project-item" +
        (p.id === currentProjectId ? " active" : "");
      d.innerHTML = "<span>📁</span><span>" + esc(p.name) + "</span>" +
        '<span class="proj-count">' + (p.conversation_count || 0) +
        "</span>" +
        '<button class="hist-del brief-btn" title="Project brief — ' +
        'standing instructions for this project">📋</button>' +
        // Distinct class for the delete button: it used to share
        // "hist-del" with the brief button, and querySelector
        // (".hist-del") returned the brief button first — so the X
        // got NO handler at all and clicking it did nothing, while
        // the brief icon fired both handlers.
        '<button class="hist-del proj-del" title="Delete project">' +
        "✕</button>";
      d.querySelector(".brief-btn").addEventListener("click",
        (ev) => {
          ev.stopPropagation();
          openBrief(p);
        });
      d.querySelector(".proj-del").addEventListener("click",
        async (ev) => {
          ev.stopPropagation();
          if (await deleteWithConfirm(
              "/api/projects/" + encodeURIComponent(p.id),
              "Delete the project '" + p.name + "'? This deletes " +
              "its chats and ideas. Runs in it are cancelled first.")) {
            if (p.id === currentProjectId) {
              currentProjectId = "";
              currentConversationId = "";
              thread.innerHTML = "";
            }
            loadProjects(); loadHistory(); loadIdeas(); loadLibrary();
          }
        });
      d.addEventListener("click", () => selectProject(p.id));
      list.appendChild(d);
    });
  }
  function selectProject(id) {
    if (id === currentProjectId) return;
    currentProjectId = id;
    localStorage.setItem("aidon-project", id);
    currentConversationId = "";
    thread.innerHTML = "";
    loadProjects();
    loadHistory();
    loadIdeas();
    loadLibrary();
  }
  document.getElementById("new-project").addEventListener("click",
    async () => {
      const name = prompt("New project name:");
      if (!name || !name.trim()) return;
      try {
        const p = await (await fetch("/api/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: name.trim() }),
        })).json();
        currentProjectId = p.id;
        localStorage.setItem("aidon-project", p.id);
        currentConversationId = "";
        thread.innerHTML = "";
        loadProjects(); loadHistory(); loadIdeas(); loadLibrary();
      } catch (e) { /* the list simply stays as it was */ }
    });

  // ---- project brief: standing instructions, loaded into every
  // run in the project as binding guidance ----
  let briefProjectId = "";
  function ensureBriefModal() {
    let overlay = document.getElementById("brief-overlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.id = "brief-overlay";
    overlay.hidden = true;
    overlay.innerHTML =
      '<div class="cam-panel brief-panel" role="dialog" ' +
      'aria-label="Project brief">' +
      '<div class="cam-title" id="brief-title">Project brief</div>' +
      '<div class="brief-hint">Standing instructions for this ' +
      'project. Every chat and run opened here starts from this ' +
      'brief. Leave it empty to clear it.</div>' +
      '<textarea id="brief-text" rows="14" spellcheck="false" ' +
      'placeholder="e.g. Hard budget cap: CAD $100 for the shopping ' +
      'basket. Ledger: basket.json in the workspace — totals come ' +
      'from the file."></textarea>' +
      '<div class="cam-actions">' +
      '<button id="brief-cancel" type="button">Cancel</button>' +
      '<button id="brief-save" type="button">Save brief</button>' +
      "</div></div>";
    document.body.appendChild(overlay);
    overlay.querySelector("#brief-cancel").addEventListener(
      "click", closeBrief);
    overlay.querySelector("#brief-save").addEventListener(
      "click", saveBrief);
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) closeBrief();
    });
    return overlay;
  }
  function closeBrief() {
    const o = document.getElementById("brief-overlay");
    if (o) o.hidden = true;
  }
  async function openBrief(p) {
    briefProjectId = p.id;
    const overlay = ensureBriefModal();
    overlay.querySelector("#brief-title").textContent =
      "Project brief — " + p.name;
    const ta = overlay.querySelector("#brief-text");
    ta.value = "";
    ta.placeholder = "Loading…";
    overlay.hidden = false;
    try {
      const r = await (await fetch(
        "/api/projects/" + encodeURIComponent(p.id) + "/brief")).json();
      ta.value = r.brief || "";
    } catch (e) { ta.value = ""; }
    ta.focus();
  }
  async function saveBrief() {
    const ta = document.getElementById("brief-text");
    try {
      await fetch(
        "/api/projects/" + encodeURIComponent(briefProjectId) +
        "/brief", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ brief: ta.value }),
        });
    } catch (e) { /* the modal simply stays as it was */ }
    closeBrief();
  }

  // ---- history: persistent conversations in this project ----
  function conversationItem(conv) {
    const d = document.createElement("div");
    d.className = "hist-item" +
      (conv.id === currentConversationId ? " active" : "");
    d.dataset.conversationId = conv.id;
    const meta = [conv.surface || "webui",
      (conv.exchange_count || 0) + " exchanges",
      fmtDate(conv.updated_at)].filter(Boolean).join(" · ");
    d.innerHTML = '<div class="hist-goal">' +
      esc(conv.title || "New chat") + "</div>" +
      '<div class="hist-meta">' + esc(meta) + "</div>" +
      '<button class="hist-del" title="Delete this chat">✕</button>';
    d.querySelector(".hist-del").addEventListener("click", (ev) => {
      ev.stopPropagation();
      deleteConversation(conv.id);
    });
    d.addEventListener("click", () => openConversation(conv.id));
    return d;
  }
  async function deleteConversation(id) {
    if (!window.confirm("Delete this chat and its messages?")) return;
    try {
      const r = await fetch("/api/conversations/" +
        encodeURIComponent(id), { method: "DELETE" });
      if (!r.ok) return;
    } catch (e) { return; }
    if (id === currentConversationId) {
      currentConversationId = "";
      thread.innerHTML = "";
    }
    loadHistory();
  }
  function markActiveConversation() {
    document.querySelectorAll("#history-list .hist-item").forEach((el) => {
      el.classList.toggle("active",
        el.dataset.conversationId === currentConversationId);
    });
  }
  async function loadHistory() {
    const list = document.getElementById("history-list");
    let convs = [];
    try {
      const r = await fetch("/api/conversations?limit=30&project_id=" +
        encodeURIComponent(currentProjectId || ""));
      const data = await r.json();
      convs = Array.isArray(data) ? data : (data.conversations || []);
    } catch (e) { convs = []; }
    if (!convs.length) {
      list.innerHTML = '<div class="empty-note">No chats yet.<br>' +
        "Give Aidon a goal to start.</div>";
      return;
    }
    list.innerHTML = "";
    convs.forEach((c) => list.appendChild(conversationItem(c)));
  }
  // poll() in the chat IIFE calls this when a run completes.
  window.aidonLoadHistory = () => { loadHistory(); loadProjects(); };

  async function openConversation(id) {
    let conv;
    try {
      const r = await fetch("/api/conversations/" +
        encodeURIComponent(id));
      if (!r.ok) return;
      conv = await r.json();
    } catch (e) { return; }
    currentConversationId = conv.id;
    if (window.aidonStopPolling) window.aidonStopPolling();
    if (conv.project_id) {
      currentProjectId = conv.project_id;
      localStorage.setItem("aidon-project", conv.project_id);
      loadProjects();
    }
    markActiveConversation();
    thread.innerHTML = "";
    const atts = conv.attachments || [];
    const byImport = {};
    const outputsByRun = {};
    atts.forEach((a) => {
      if (a.kind === "output") {
        (outputsByRun[a.run_id] = outputsByRun[a.run_id] || []).push(a);
      } else {
        (byImport[a.imported_exchange] =
          byImport[a.imported_exchange] || []).push(a);
      }
    });
    const renderAtt = (a) => {
      const d = document.createElement("div");
      d.className = "msg user";
      const url = "/api/library/" + a.id + "/file";
      if (a.kind === "image") {
        const img = document.createElement("img");
        img.src = url;
        img.className = "chat-image";
        img.alt = a.name;
        d.appendChild(img);
      } else {
        d.innerHTML = '📎 <a href="' + url +
          '" target="_blank" rel="noopener">' + esc(a.name) + "</a>" +
          (a.memory_id ? ' <span class="hist-meta">· linked to memory</span>' : "");
      }
      thread.appendChild(d);
    };
    (conv.exchanges || []).forEach((ex) => {
      if (ex.user_text) {
        const u = document.createElement("div");
        u.className = "msg user";
        u.textContent = ex.user_text;
        thread.appendChild(u);
      }
      (byImport[ex.exchange_no] || []).forEach(renderAtt);
      if (ex.assistant_text) {
        const a = document.createElement("div");
        a.className = "msg aidon";
        a.textContent = ex.assistant_text;
        thread.appendChild(a);
      }
      (outputsByRun[ex.run_id] || []).forEach(renderAtt);
    });
    thread.scrollTop = thread.scrollHeight;
    input.disabled = false;
    input.focus();
  }

  // (the New Chat button is handled in the chat IIFE above: it stops
  // polling, resets the composer, and clears the conversation id via
  // window.aidonSetConversation.)

  // ---- search: across persistent conversations ----
  const searchInput = document.getElementById("search-input");
  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = searchInput.value.trim();
      const box = document.getElementById("search-results");
      if (!q) { box.innerHTML = ""; return; }
      let hits = [];
      try {
        hits = await (await fetch("/api/conversations?limit=30&q=" +
          encodeURIComponent(q))).json();
      } catch (e) { hits = []; }
      if (!hits.length) {
        box.innerHTML = '<div class="empty-note">No matches.</div>';
        return;
      }
      box.innerHTML = "";
      hits.forEach((c) => box.appendChild(conversationItem(c)));
    }, 250);
  });

  // ---- ideas (server-backed, per project) ----
  async function loadIdeas() {
    const list = document.getElementById("ideas-list");
    let ideas = [];
    try {
      const data = await (await fetch("/api/ideas?project_id=" +
        encodeURIComponent(currentProjectId || ""))).json();
      ideas = Array.isArray(data) ? data : (data.ideas || []);
    } catch (e) { ideas = []; }
    if (!ideas.length) {
      list.innerHTML = '<div class="empty-note">No ideas yet.<br>' +
        "Hit ＋ to jot one down.</div>";
      return;
    }
    list.innerHTML = "";
    ideas.forEach((idea) => {
      const d = document.createElement("div");
      d.className = "idea-item";
      d.innerHTML = '<button class="mini-btn idea-del" title="Delete">' +
        '×</button><div class="idea-text">' + esc(idea.text) + "</div>";
      d.querySelector(".idea-del").addEventListener("click",
        async (ev) => {
          ev.stopPropagation();
          await fetch("/api/ideas/" + encodeURIComponent(idea.id),
            { method: "DELETE" });
          loadIdeas();
        });
      d.addEventListener("click", () => {
        input.value = idea.text;
        input.focus();
      });
      list.appendChild(d);
    });
  }
  document.getElementById("new-idea").addEventListener("click",
    async () => {
      const text = prompt("New idea:");
      if (!text || !text.trim()) return;
      await fetch("/api/ideas", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_id: currentProjectId,
                               text: text.trim() }),
      });
      loadIdeas();
    });

  // ---- goals: active runs in this project + durable missions ----
  async function loadGoals() {
    const list = document.getElementById("goals-list");
    let runs = [];
    let missions = [];
    try {
      const data = await (await fetch("/api/runs")).json();
      runs = Array.isArray(data) ? data : (data.runs || []);
    } catch (e) { runs = []; }
    try {
      const data = await (await fetch("/api/missions")).json();
      missions = Array.isArray(data) ? data : (data.missions || []);
    } catch (e) { missions = []; }
    const active = runs.filter((x) =>
      (!currentProjectId || x.project_id === currentProjectId) &&
      !/done|complete|answered|error|failed/i.test(x.status || ""));
    list.innerHTML = "";
    if (!active.length && !missions.length) {
      list.innerHTML = '<div class="empty-note">No active goals.</div>';
      return;
    }
    active.forEach((g) => {
      const d = document.createElement("div");
      d.className = "goal-item";
      d.innerHTML = '<div class="goal-name">' +
        esc(g.goal || "(no goal)") + "</div>" +
        '<div class="goal-desc">' + esc(g.stage || "") + " · " +
        esc(g.status || "") + "</div>";
      d.addEventListener("click", () => {
        if (g.conversation_id) openConversation(g.conversation_id);
      });
      list.appendChild(d);
    });
    missions.forEach((m) => {
      const d = document.createElement("div");
      d.className = "goal-item";
      d.innerHTML = '<div class="goal-name">' +
        esc(m.goal || "(mission)") + "</div>" +
        '<div class="goal-desc">mission · ' + esc(m.state || "") +
        "</div>" +
        '<button class="hist-del" title="Delete this mission record">' +
        "✕</button>";
      d.querySelector(".hist-del").addEventListener("click",
        async (ev) => {
          ev.stopPropagation();
          if (await deleteWithConfirm(
              "/api/missions/" + encodeURIComponent(m.id),
              "Delete this mission record? This erases the run's " +
              "review history.")) loadGoals();
        });
      list.appendChild(d);
    });
  }

  // ---- library: every imported file + run output in this project ----
  async function loadLibrary() {
    const list = document.getElementById("library-list");
    let items = [];
    try {
      const data = await (await fetch("/api/library?project_id=" +
        encodeURIComponent(currentProjectId || ""))).json();
      items = Array.isArray(data) ? data : (data.files || []);
    } catch (e) { items = []; }
    if (!items.length) {
      list.innerHTML = '<div class="empty-note">Nothing here yet.<br>' +
        "Attach a file or ask Aidon to make one.</div>";
      return;
    }
    list.innerHTML = "";
    items.forEach((a) => {
      const d = document.createElement("div");
      d.className = "lib-item";
      const meta = [a.kind, fmtSize(a.size),
        a.conversation_title || "",
        a.memory_linked ? "🔗 memory" : "",
        fmtDate(a.created_at)].filter(Boolean).join(" · ");
      const visual = a.kind === "image"
        ? '<img class="lib-thumb" src="' + a.url + '" alt="">'
        : '<span class="lib-ico">' +
          (a.kind === "output" ? "📄" : "📎") + "</span>";
      d.innerHTML = visual + "<div><div class=\"lib-name\">" +
        esc(a.name) + '</div><div class="lib-meta">' + esc(meta) +
        "</div></div>" +
        '<button class="hist-del" title="Delete this file">✕</button>';
      d.querySelector(".hist-del").addEventListener("click",
        async (ev) => {
          ev.stopPropagation();
          if (await deleteWithConfirm(
              "/api/library/" + encodeURIComponent(a.id),
              "Delete '" + a.name + "' from the library?")) {
            loadLibrary();
          }
        });
      d.addEventListener("click", () =>
        window.open(a.url, "_blank", "noopener"));
      list.appendChild(d);
    });
  }

  // initial load
  loadProjects().then(() => {
    loadHistory();
    loadIdeas();
    loadLibrary();
  });
})();
