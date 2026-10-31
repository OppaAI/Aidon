/* Aidon illustrated robot with an optional owner-provided VRM avatar.
 *
 * The user makes the male VRM himself. Zero-code-change contract:
 *   1. drop the file at aidon/webui/avatar.vrm   (or set AIDON_VRM_PATH)
 *   2. open the chat: the VRM loads automatically when one is present,
 *      otherwise the robot placeholder stays.
 * The chat loads the VRM when one is present, as old Aidon did; without
 * one, the robot placeholder stays.
 *
 * The illustrated robot is the default. A supplied VRM replaces it when
 * one is available.
 */
(function () {
  "use strict";

  const wrap = document.getElementById("avatar-wrap");
  const canvas = document.getElementById("avatar");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let fallbackMood = "idle";
  window.aidonSetMood = function (name) { fallbackMood = name === "working" ? "working" : "idle"; };

  // The SVG fallback gets short arm waves, side steps, and hops. Keyframes are eased
  // between random pauses so it feels lively without being distracting.
  function animateFallbackIdle() {
    if (reduceMotion) return;
    const root = document.querySelector(".robot-body-anim") || document.getElementById("robot");
    if (!root) return;
    const head = document.querySelector(".robot-head-anim") || document.getElementById("robot-head");
    const left = document.querySelector(".robot-arm-left") || document.getElementById("robot-arm-left");
    const right = document.querySelector(".robot-arm-right") || document.getElementById("robot-arm-right");
    const pose = { x: 0, y: 0, yaw: 0, head: 0, left: 0, right: 0 };
    let action = null;
    let nextGesture = performance.now() + 1800;
    let previous = performance.now();

    const actions = [
      { duration: 2300, frames: [
        [0, 0, 0, 0, 0, 0, 0], [.18, 0, -2, 0, -4, -8, 0], [.34, 0, -2, 0, -4, -32, 0],
        [.50, 0, -2, 0, -4, -12, 0], [.66, 0, -2, 0, -4, -32, 0], [.82, 0, -1, 0, -2, -10, 0], [1, 0, 0, 0, 0, 0, 0],
      ] }, // wave
      { duration: 2100, frames: [
        [0, 0, 0, 0, 0, 0, 0], [.2, -7, -3, 0, 0, 14, -12], [.42, 7, -5, 0, 0, -12, 14],
        [.64, -6, -3, 0, 0, 12, -12], [.82, 5, -1, 0, 0, -8, 10], [1, 0, 0, 0, 0, 0, 0],
      ] }, // step side to side
      { duration: 1900, frames: [
        [0, 0, 0, 0, 0, 0, 0], [.2, 0, -7, 0, -2, -24, -24], [.44, 0, -3, 0, -2, -32, -32],
        [.68, 0, -2, 0, -1, -16, -16], [1, 0, 0, 0, 0, 0, 0],
      ] }, // happy hop with arms up
    ];

    function interpolate(frames, p) {
      let i = 0;
      while (i < frames.length - 2 && p > frames[i + 1][0]) i++;
      const a = frames[i], b = frames[i + 1];
      const u = Math.max(0, Math.min(1, (p - a[0]) / (b[0] - a[0])));
      const eased = u * u * (3 - 2 * u);
      return a.slice(1).map((v, j) => v + (b[j + 1] - v) * eased);
    }

    function tick(now) {
      const dt = Math.min(0.05, (now - previous) / 1000);
      previous = now;
      if (!action && now >= nextGesture) {
        action = { ...actions[Math.floor(Math.random() * actions.length)], started: now };
      }
      if (action) {
        const p = Math.min(1, (now - action.started) / action.duration);
        const [x, y, yaw, headAngle, leftAngle, rightAngle] = interpolate(action.frames, p);
        pose.x = x; pose.y = y; pose.yaw = yaw;
        pose.head = headAngle; pose.left = leftAngle; pose.right = rightAngle;
        if (p >= 1) {
          action = null;
          nextGesture = now + (fallbackMood === "working" ? 6500 : 3500) + Math.random() * 7000;
        }
      }
      root.style.transform = `perspective(420px) rotateY(${pose.yaw.toFixed(1)}deg) translate(${pose.x.toFixed(1)}px, ${pose.y.toFixed(1)}px)`;
      if (head) head.style.transform = `rotate(${pose.head.toFixed(1)}deg)`;
      if (left) left.style.transform = `rotate(${pose.left.toFixed(1)}deg)`;
      if (right) right.style.transform = `rotate(${pose.right.toFixed(1)}deg)`;
      requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
  }
  animateFallbackIdle();

  // The CSS/SVG fallback avatar uses the same audio level as the VRM
  // mouth. A loaded model replaces this hook with its morph-target driver.
  window.aidonSetMouthOpen = function (level) {
    const amount = Math.max(0, Math.min(1, Number(level) || 0));
    const smile = document.getElementById("fallback-mouth-smile");
    const opening = document.getElementById("fallback-mouth-open");
    if (smile) smile.setAttribute("opacity", String(1 - Math.min(1, amount * 5)));
    if (opening) {
      opening.setAttribute("rx", String(2 + amount * 5));
      opening.setAttribute("ry", String(1 + amount * 4));
      opening.setAttribute("opacity", String(Math.min(1, amount * 5)));
    }
    // The legacy chat template keeps its original mouth element IDs.
    const legacySmile = document.getElementById("mouth-smile");
    const legacyOpen = document.getElementById("mouth-open");
    if (legacySmile) legacySmile.setAttribute("opacity", String(1 - Math.min(1, amount * 5)));
    if (legacyOpen) {
      legacyOpen.setAttribute("rx", String(2 + amount * 5));
      legacyOpen.setAttribute("ry", String(1 + amount * 4));
      legacyOpen.setAttribute("opacity", String(Math.min(1, amount * 5)));
    }
  };

  // Load the VRM into the chat when one is present, as old Aidon did;
  // without one, the robot placeholder stays.
  async function boot() {
    let status;
    try {
      const response = await fetch("/api/avatar/status");
      status = await response.json();
    } catch (_) {
      return; // The illustrated robot remains available without the API.
    }
    if (!status || !status.present) return;
    try {
      await loadModel();
      if (wrap) wrap.classList.add("has-model");
    } catch (error) {
      console.warn("[avatar] VRM load failed, keeping the illustrated robot:", error);
    }
  }

  async function loadModel() {
    const THREE = await import("three");
    const { GLTFLoader } = await import("three/addons/loaders/GLTFLoader.js");
    const { VRMLoaderPlugin } = await import("@pixiv/three-vrm");

    const cssSize = canvas.clientWidth || 168;
    const size = cssSize * 2; // retina
    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setSize(size, size, false);
    renderer.setPixelRatio(2);
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(28, 1, 0.05, 10);

    scene.add(new THREE.AmbientLight(0xffffff, 0.9));
    const key = new THREE.DirectionalLight(0xffffff, 1.1);
    key.position.set(0.6, 1.2, 1.5);
    scene.add(key);
    const rim = new THREE.DirectionalLight(0x8b7cf0, 0.5);
    rim.position.set(-1, 0.5, -1);
    scene.add(rim);

    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync("/api/avatar.vrm");
    const vrm = gltf.userData.vrm;
    if (!vrm || !vrm.humanoid) throw new Error("no VRM humanoid in file");
    scene.add(vrm.scene);

    // ── headshot framing: auto-fit from model bounds ──
    // Bone offsets proved unreliable across VRM exports, so frame from the
    // actual geometry: the framed band is the top slice of the model's
    // bounding box. At 18.5% of the model height the head fills ~70% of
    // the circle and the band still reaches the collar, so the portrait
    // reads as a headshot with a bit of t-shirt at the neck (the old
    // 22% left the head at ~54% of the circle - too small for a
    // portrait).
    const bbox = new THREE.Box3().setFromObject(vrm.scene);
    const size3 = new THREE.Vector3();
    const center3 = new THREE.Vector3();
    bbox.getSize(size3);
    bbox.getCenter(center3);
    const topY = bbox.max.y;
    const headshotH = size3.y * 0.185;
    const targetY = topY - headshotH * 0.46;
    // Distance so the headshot height fills ~96% of the frame
    const fitDist = (headshotH * 0.5) / Math.tan(THREE.MathUtils.degToRad(camera.fov * 0.5));
    const dist = fitDist * 1.04;
    // Face direction: the head-bone +Z heuristic proved inverted on this
    // model (user reports back-of-head twice), so negate it. If a future
    // model faces the other way, flip the sign below.
    const FACE_FLIP = -1;
    const h = vrm.humanoid;
    const head =
      (h.getNormalizedBoneNode && h.getNormalizedBoneNode("head")) ||
      (h.getRawBoneNode && h.getRawBoneNode("head")) ||
      null;
    let fwd = new THREE.Vector3(0, 0, 1);
    if (head) {
      head.updateWorldMatrix(true, false);
      const q = new THREE.Quaternion();
      head.getWorldQuaternion(q);
      fwd = new THREE.Vector3(0, 0, 1).applyQuaternion(q);
      fwd.y = 0; // keep the camera level
      if (fwd.lengthSq() < 1e-6) fwd.set(0, 0, 1);
      fwd.normalize();
    }
    camera.position.set(
      center3.x + fwd.x * dist * FACE_FLIP,
      // A touch above the band centre: the chin and collar stay in
      // frame (the t-shirt at the neck) without cropping the skull.
      targetY + headshotH * 0.05,
      center3.z + fwd.z * dist * FACE_FLIP
    );
    camera.lookAt(center3.x, targetY, center3.z);

    // ── rest pose: arms down from T-pose ──
    // VRM loads in T-pose; rotate upper arms down so the headshot isn't
    // flanked by raised arms. Sign flipped empirically (first attempt
    // raised them). ~70° keeps a natural slight angle off the torso.
    const REST_ANGLE = THREE.MathUtils.degToRad(70);
    const lUpper = h.getNormalizedBoneNode
      ? h.getNormalizedBoneNode("leftUpperArm") : null;
    const rUpper = h.getNormalizedBoneNode
      ? h.getNormalizedBoneNode("rightUpperArm") : null;
    if (lUpper) lUpper.rotation.z = REST_ANGLE;
    if (rUpper) rUpper.rotation.z = -REST_ANGLE;
    const expr = vrm.expressionManager || null;
    // ---- morph targets: the face this model actually has ---------------
    // The VRM ships 57 morph targets (Fcl_ALL_Joy, Fcl_EYE_Close, the
    // five vowels, ...) but its VRM expression groups are EMPTY, so the
    // VRM expression manager had nothing to drive - the lip sync and the
    // blink never moved the face. Drive the morph targets directly, and
    // keep the expression manager as the fallback for other models.
    const morphMeshes = [];
    vrm.scene.traverse((o) => {
      if (o.isMesh && o.morphTargetDictionary &&
          Object.keys(o.morphTargetDictionary).length) {
        morphMeshes.push(o);
      }
    });
    const hasMorphs = morphMeshes.length > 0;
    const setMorph = (name, weight) => {
      for (const m of morphMeshes) {
        const idx = m.morphTargetDictionary[name];
        if (idx !== undefined) m.morphTargetInfluences[idx] = weight;
      }
    };
    const setMorphs = (names, weight) => {
      for (const n of names) setMorph(n, weight);
    };
    // "Joy" is the happy preset, spread over the face. It comes in
    // parts, and they are driven separately on purpose: Fcl_EYE_Joy is
    // the SQUINT (happy closed eyes), so an idle face that drives it
    // looks like it is dozing. The idle mood smiles with the mouth and
    // brows and leaves the eyes open.
    const JOY_MOUTH = ["Fcl_MTH_Joy"];
    const JOY_BROW = ["Fcl_BRW_Joy"];
    const JOY_EYE = ["Fcl_EYE_Joy"];          // the squint - opt-in only
    const FUN_MOUTH = ["Fcl_MTH_Fun"];
    const FUN_BROW = ["Fcl_BRW_Fun"];
    const FUN_EYE = ["Fcl_EYE_Fun"];

    // ---- mood: the face follows what the agent is doing ---------------
    const MOODS = {
      idle: { mouth: 0.30, brow: 0.50, eye: 0.0, funMouth: 0.45,
              funBrow: 0.20, funEye: 0.0 },   // happy at rest, mouth
              // closed (the Fun smile, not the open Joy grin) and
              // eyes fully open - no squint (user's call)
      working: { mouth: 0.20, brow: 0.10, eye: 0.0, funMouth: 0.0,
                 funBrow: 0.0, funEye: 0.0 },  // focused, not grinning
      happy: { mouth: 0.95, brow: 0.85, eye: 0.65, funMouth: 0.22,
               funBrow: 0.22, funEye: 0.18 },  // a real squinty grin
    };
    const mood = { ...MOODS.idle };
    let moodTarget = MOODS.idle;
    window.aidonSetMood = function (name) {
      moodTarget = MOODS[name] || MOODS.idle;
    };

    // Lip sync hook: app.js drives this from the TTS analyser (0 =
    // closed, 1 = open). It only STORES the level - the frame loop
    // below is the single writer of morph influences, applying them
    // after vrm.update(). Writing them here got them overwritten
    // before the render, which is why the mouth never moved.
    let mouthLevel = 0;
    window.aidonSetMouthOpen = function (level) {
      mouthLevel = Math.max(0, Math.min(1, level));
    };
    let blinkAt = 2.5, blinkT = -1, t = 0;
    let idleNext = 0;
    const idle = { x: 0, y: 0, z: 0 };
    const idleTarget = { x: 0, y: 0, z: 0 };
    const clock = new THREE.Clock();
    const baseRX = head ? head.rotation.x : 0;
    const baseRY = head ? head.rotation.y : 0;

    function frame() {
      requestAnimationFrame(frame);
      const dt = Math.min(clock.getDelta(), 0.1);
      t += dt;
      // subtle sway (incommensurate freqs so it never looks looped)
      if (head) {
        if (reduceMotion) {
          head.rotation.x = baseRX;
          head.rotation.y = baseRY;
          head.rotation.z = 0;
        } else {
          if (t >= idleNext) {
            idleTarget.x = (Math.random() * 2 - 1) * 0.025;
            idleTarget.y = (Math.random() * 2 - 1) * 0.065;
            idleTarget.z = (Math.random() * 2 - 1) * 0.018;
            idleNext = t + 1.4 + Math.random() * 2.4;
          }
          const idleEase = 1 - Math.exp(-dt * 1.3);
          idle.x += (idleTarget.x - idle.x) * idleEase;
          idle.y += (idleTarget.y - idle.y) * idleEase;
          idle.z += (idleTarget.z - idle.z) * idleEase;
          head.rotation.x = baseRX + idle.x;
          head.rotation.y = baseRY + idle.y;
          head.rotation.z = idle.z;
        }
      }
      // mood: ease toward the target so the smile arrives softly
      const k = Math.min(1, dt * 3);
      for (const key of Object.keys(mood)) {
        mood[key] += (moodTarget[key] - mood[key]) * k;
      }
      // blink: 120ms sin curve every 2.5–6s
      let blinkW = 0;
      if (blinkT < 0 && t > blinkAt) blinkT = 0;
      if (blinkT >= 0) {
        blinkT += dt;
        const kb = blinkT / 0.12;
        if (kb >= 1) { blinkT = -1; blinkAt = t + 2.5 + Math.random() * 3.5; }
        else blinkW = Math.sin(Math.PI * kb);
      }
      // update the rig FIRST: vrm.update() rewrites morph influences
      // from the expression manager, discarding anything set earlier
      // in the frame. All face writes happen after it, just before
      // the render - one writer, values that survive to the screen.
      vrm.update(dt);
      if (hasMorphs) {
        setMorphs(JOY_MOUTH, mood.mouth);
        setMorphs(JOY_BROW, mood.brow);
        setMorphs(JOY_EYE, mood.eye);
        setMorphs(FUN_MOUTH, mood.funMouth);
        setMorphs(FUN_BROW, mood.funBrow);
        setMorphs(FUN_EYE, mood.funEye);
        setMorph("Fcl_EYE_Close", blinkW);
        // Lip sync: the jaw shapes are subtle, so they are driven
        // hard - at half strength the mouth barely moves.
        setMorph("Fcl_MTH_Small", mouthLevel * 0.95);
        setMorph("Fcl_MTH_Large", mouthLevel * 0.60);
        setMorph("Fcl_MTH_A", mouthLevel * 0.85);
      } else if (expr) {
        expr.setValue("blink", blinkW);
        expr.setValue("aa", mouthLevel);
      }
      renderer.render(scene, camera);
    }
    frame();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
