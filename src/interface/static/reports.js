/* Aidon daily reports page: brief + per-goal detail, each with TTS. */
(function () {
  "use strict";

  function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---- TTS playback (same play/stop toggle as the main app) ----
  let currentAudio = null;
  let currentBtn = null;
  function resetSpeakBtn() {
    if (currentBtn) {
      currentBtn.textContent = "▶";
      currentBtn.classList.remove("playing");
    }
    currentAudio = null;
    currentBtn = null;
  }
  async function speak(text, btn) {
    if (currentBtn === btn && currentAudio) {
      currentAudio.pause();
      resetSpeakBtn();
      return;
    }
    if (currentAudio) { currentAudio.pause(); resetSpeakBtn(); }
    btn.textContent = "…";
    try {
      const r = await fetch("/api/tts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) throw new Error("tts " + r.status);
      const url = URL.createObjectURL(await r.blob());
      const audio = new Audio(url);
      audio.playbackRate = 0.84;
      audio.preservesPitch = true;
      currentAudio = audio;
      currentBtn = btn;
      btn.textContent = "⏹";
      btn.classList.add("playing");
      audio.onended = () => { URL.revokeObjectURL(url); resetSpeakBtn(); };
      audio.onerror = () => { URL.revokeObjectURL(url); resetSpeakBtn(); };
      await audio.play();
    } catch (e) {
      resetSpeakBtn();
    }
  }
  function playButton(text) {
    const b = document.createElement("button");
    b.className = "tts-play";
    b.textContent = "▶";
    b.title = "Play aloud";
    b.addEventListener("click", () => speak(text, b));
    return b;
  }

  const daySel = document.getElementById("report-day");
  const briefEl = document.getElementById("report-brief");
  const briefPlay = document.getElementById("brief-play");
  const goalsEl = document.getElementById("report-goals");
  const statusEl = document.getElementById("report-status");

  async function loadDays() {
    const r = await fetch("/api/reports");
    const payload = await r.json();
    const days = Array.isArray(payload) ? payload : (payload.reports || []);
    daySel.innerHTML = days.map((d) =>
      "<option>" + esc(d) + "</option>").join("");
    if (days.length) loadDay(days[0]);
    else briefEl.textContent = "(no reports yet — generate one below)";
  }

  async function loadDay(day) {
    const r = await fetch("/api/reports/" + encodeURIComponent(day));
    if (!r.ok) { briefEl.textContent = "(report not found)"; return; }
    const data = await r.json();
    briefEl.textContent = data.brief || data.content || "(empty)";
    briefPlay.innerHTML = "";
    if (data.brief || data.content) briefPlay.appendChild(playButton(data.brief || data.content));
    goalsEl.innerHTML = "";
    (data.goals || []).forEach((g) => {
      const sec = document.createElement("section");
      sec.className = "report-goal";
      const h = document.createElement("h3");
      h.textContent = g.name.replace(/-/g, " ");
      h.appendChild(playButton(g.markdown));
      const pre = document.createElement("pre");
      pre.textContent = g.markdown;
      sec.appendChild(h);
      sec.appendChild(pre);
      goalsEl.appendChild(sec);
    });
  }

  daySel.addEventListener("change", () => loadDay(daySel.value));
  document.getElementById("report-generate").addEventListener("click",
    async () => {
      statusEl.textContent = "generating…";
      try {
        const r = await fetch("/api/reports/generate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || r.status);
        statusEl.textContent = "generated: " + data.runs + " runs, " +
          data.goals.length + " goal files.";
        await loadDays();
      } catch (e) {
        statusEl.textContent = "failed: " + e.message;
      }
    });

  // theme toggle mirrors the main app
  const stored = localStorage.getItem("aidon-theme");
  if (stored) document.documentElement.setAttribute("data-theme", stored);
  document.getElementById("theme-toggle").addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const nxt = cur === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", nxt);
    localStorage.setItem("aidon-theme", nxt);
  });

  loadDays();
})();
