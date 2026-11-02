/* Browser-controlled demo capture; the browser asks what the user shares. */
(function () {
  "use strict";
  const button = document.getElementById("demo-record");
  const download = document.getElementById("demo-record-download");
  const status = document.getElementById("demo-record-status");
  if (!button || !download || !status) return;
  let recorder = null, stream = null, objectUrl = "", chunks = [];

  function stopTracks() {
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
  }

  button.addEventListener("click", async () => {
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
      button.disabled = true;
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia || !window.MediaRecorder) {
      status.textContent = "Screen recording is unavailable in this browser.";
      return;
    }
    try {
      button.disabled = true;
      stream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: true });
      const type = ["video/webm;codecs=vp9,opus", "video/webm;codecs=vp8,opus", "video/webm"]
        .find((candidate) => MediaRecorder.isTypeSupported(candidate));
      recorder = type ? new MediaRecorder(stream, { mimeType: type }) : new MediaRecorder(stream);
      chunks = [];
      recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
      recorder.onerror = () => { status.textContent = "Recording failed."; stopTracks(); button.disabled = false; };
      recorder.onstop = () => {
        stopTracks();
        button.disabled = false;
        button.classList.remove("recording");
        button.textContent = "Record demo";
        if (!chunks.length) { status.textContent = "No recording was produced."; return; }
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        const blob = new Blob(chunks, { type: recorder.mimeType || "video/webm" });
        objectUrl = URL.createObjectURL(blob);
        download.href = objectUrl;
        download.download = "aidon-demo-" + new Date().toISOString().replace(/[:.]/g, "-") + ".webm";
        download.hidden = false;
        status.textContent = "Recording ready.";
      };
      stream.getVideoTracks()[0].addEventListener("ended", () => {
        if (recorder && recorder.state !== "inactive") recorder.stop();
      });
      recorder.start(1000);
      button.classList.add("recording");
      button.textContent = "Stop recording";
      status.textContent = stream.getAudioTracks().length ? "Recording screen and shared audio." : "Recording screen; audio was not shared.";
    } catch (error) {
      stopTracks();
      status.textContent = error.name === "NotAllowedError" ? "Screen sharing cancelled." : "Could not start screen recording.";
    } finally {
      button.disabled = false;
    }
  });
})();
