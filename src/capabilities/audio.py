"""Audio lip sync, old Aidon audio concept, rebuilt for VRM, browser-side, honestly scoped.

New Aidon uses a robot SVG with speechSynthesis, not a VRM with Web Audio, so this module
does NOT claim the SVG lip sync is phoneme-accurate. What it does provide is the real
Web Audio lip sync JavaScript for a VRM avatar, the way old Aidon did it: an AnalyserNode
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
