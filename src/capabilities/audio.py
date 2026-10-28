"""Audio lip sync, Aidon audio concept, rebuilt for VRM, browser-side, honestly scoped.

The default interface uses a robot SVG with speechSynthesis, not a VRM with Web Audio, so this module
does NOT claim the SVG lip sync is phoneme-accurate. What it does provide is the real
Web Audio lip sync JavaScript for a VRM avatar, the way Aidon did it: an AnalyserNode
on the spoken audio, RMS volume driving Fcl_MTH morphs, and a blink timer driving
Fcl_EYE_Close, so the VRM page can use it when a VRM file is present. The Python side
returns that JavaScript as a string, with the morph names in it, and a test checks for
AnalyserNode, Fcl_MTH, and Fcl_EYE_Close, so a future edit cannot silently drop the sync.
"""

from __future__ import annotations

VRM_LIP_SYNC_JS = """
// VRM lip sync, Web Audio AnalyserNode, RMS to mouth morphs, blink timer to eye morphs.
function startVrmLipSync(vrm, audioElement) {
  const ctx = new AudioContext();
  const source = ctx.createMediaElementSource(audioElement);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);
  analyser.connect(ctx.destination);
  const data = new Uint8Array(analyser.frequencyBinCount);
  let blinkTimer = null;
  function tick() {
    analyser.getByteTimeDomainData(data);
    let sum = 0;
    for (const v of data) { const x = (v - 128) / 128; sum += x * x; }
    const rms = Math.sqrt(sum / data.length);
    const mouth = Math.min(1, rms * 4);
    if (vrm && vrm.expressionManager) {
      vrm.expressionManager.setValue('Fcl_MTH_Small', mouth * 0.5);
      vrm.expressionManager.setValue('Fcl_MTH_Large', mouth);
    }
    requestAnimationFrame(tick);
  }
  tick();
  blinkTimer = setInterval(() => {
    if (vrm && vrm.expressionManager) {
      vrm.expressionManager.setValue('Fcl_EYE_Close', 1);
      setTimeout(() => vrm.expressionManager.setValue('Fcl_EYE_Close', 0), 120);
    }
  }, 4000);
  return blinkTimer;
}
"""


def vrm_lip_sync_js() -> str:
    return VRM_LIP_SYNC_JS


def record_system_audio(seconds: int, out_path) -> str:
    """Capture host system audio into a WAV file.

    Host-side, not sandboxed, and approval-gated at the tool
    layer (the caller asks the human first; this function only
    records). Tries ffmpeg, then arecord, then sox, in that
    order, with a timeout. If none of them is present, raises
    RuntimeError naming all three — it never writes silence
    and calls it a recording. A recorder that exits non-zero,
    or leaves no non-empty file, also raises RuntimeError.
    """
    import shutil
    import subprocess
    from pathlib import Path

    if not isinstance(seconds, int) or isinstance(seconds, bool):
        raise ValueError("seconds must be an integer")
    if seconds < 1 or seconds > 4 * 3600:
        raise ValueError("seconds must be 1..14400")
    if not str(out_path).strip():
        raise ValueError("out_path is required")
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    recorders = [
        ("ffmpeg", ["ffmpeg", "-y", "-f", "pulse", "-i", "default",
                    "-t", str(seconds), str(path)]),
        ("arecord", ["arecord", "-f", "cd", "-t", "wav",
                     "-d", str(seconds), str(path)]),
        ("sox", ["sox", "-d", str(path), "trim", "0", str(seconds)]),
    ]
    available = [(name, argv) for name, argv in recorders
                 if shutil.which(name) is not None]
    if not available:
        raise RuntimeError(
            "no audio recorder found: install ffmpeg, arecord, or sox")
    errors = []
    for name, argv in available:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=seconds + 60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if proc.returncode == 0 and path.is_file() and path.stat().st_size > 0:
            return str(path)
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        errors.append(f"{name}: exit {proc.returncode}"
                      + (f" ({detail[-1][:120]})" if detail else ""))
    raise RuntimeError("audio recording failed: " + "; ".join(errors))
