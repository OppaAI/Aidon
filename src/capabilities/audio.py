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
// Follows old Aidon's chat loop: float time-domain RMS, a noise floor,
// asymmetric smoothing, and morph targets driven directly. This model's
// VRM expression groups are empty, so the expression manager alone moves
// nothing; it is only the fallback for models that have groups.
function startVrmLipSync(vrm, audioElement) {
  const ctx = new AudioContext();
  const source = ctx.createMediaElementSource(audioElement);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 256;
  source.connect(analyser);
  analyser.connect(ctx.destination);
  const data = new Float32Array(analyser.fftSize);
  const morphs = [];
  if (vrm && vrm.scene) {
    vrm.scene.traverse((o) => {
      if (o.morphTargetDictionary && o.morphTargetInfluences) {
        morphs.push(o);
      }
    });
  }
  function setMorph(name, value) {
    let hit = false;
    for (const mesh of morphs) {
      const idx = mesh.morphTargetDictionary[name];
      if (idx !== undefined) { mesh.morphTargetInfluences[idx] = value; hit = true; }
    }
    return hit;
  }
  let level = 0;
  let stopped = false;
  let t = 0, blinkAt = 2.5, blinkT = -1, last = performance.now();
  function tick(now) {
    if (stopped) return;
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    t += dt;
    analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (const v of data) { sum += v * v; }
    const rms = Math.sqrt(sum / data.length);
    const target = Math.max(0, Math.min(1, (rms - 0.012) * 9.5));
    const coeff = target > level ? 0.65 : 0.28;
    level += (target - level) * coeff;
    // Blink: 120ms sin curve every 2.5-6s.
    let blinkW = 0;
    if (blinkT < 0 && t > blinkAt) blinkT = 0;
    if (blinkT >= 0) {
      blinkT += dt;
      const kb = blinkT / 0.12;
      if (kb >= 1) { blinkT = -1; blinkAt = t + 2.5 + Math.random() * 3.5; }
      else blinkW = Math.sin(Math.PI * kb);
    }
    let direct = false;
    direct = setMorph('Fcl_MTH_Small', level * 0.95) || direct;
    direct = setMorph('Fcl_MTH_Large', level * 0.60) || direct;
    direct = setMorph('Fcl_MTH_A', level * 0.85) || direct;
    direct = setMorph('Fcl_EYE_Close', blinkW) || direct;
    if (!direct && vrm && vrm.expressionManager) {
      try { vrm.expressionManager.setValue('aa', level); } catch (e) {}
      try { vrm.expressionManager.setValue('blink', blinkW); } catch (e) {}
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
  return function stop() { stopped = true; };
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
