"""Static HTML template for the optional 3D factory page."""

from .webui_style import BASE_CSS

FACTORY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Aidon Factory</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/0.160.0/three.min.js"></script>
<style>body{font-family:sans-serif} #factory{width:900px;height:500px;border:1px solid #ccc} .legend{margin-top:8px}</style>
</head>
<body>
<h1>Factory</h1>
<p>Three-dimensional factory, live from trace files, not a replay and not a game. Each station is a run phase: receiving dock is intake, sorting is interpret, archive is research, blueprint table is plan, test bench is sandbox, inspection is verify, foreman desk is approval, assembly line is live, warehouse is learn. A station turns yellow while running, green when done, red when failed. The chat avatar stays in the chat, this factory is a separate view, linked from the chat sidebar.</p>
<div id="factory"></div>
<div class="legend" id="legend"></div>
<div id="runs"></div>
<script>
const stations = [
  {kind: 'interpret', label: 'Sorting', x: -8, z: 0},
  {kind: 'research', label: 'Archive', x: -4, z: 0},
  {kind: 'plan', label: 'Blueprint', x: 0, z: 0},
  {kind: 'sandbox', label: 'Test bench', x: 4, z: 0},
  {kind: 'verify', label: 'Inspection', x: 8, z: 0},
  {kind: 'approval', label: 'Foreman desk', x: 8, z: 4},
  {kind: 'live', label: 'Assembly line', x: 4, z: 4},
  {kind: 'learn', label: 'Warehouse', x: 0, z: 4},
  {kind: 'recall', label: 'Receiving dock', x: -8, z: 4}
];
const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf0f0f0);
const camera = new THREE.PerspectiveCamera(50, 900/500, 0.1, 100);
camera.position.set(0, 12, 14);
camera.lookAt(0, 0, 1);
const renderer = new THREE.WebGLRenderer({antialias: true});
renderer.setSize(900, 500);
document.getElementById('factory').appendChild(renderer.domElement);
const floor = new THREE.Mesh(new THREE.BoxGeometry(24, 0.2, 12), new THREE.MeshStandardMaterial({color: 0xcccccc}));
floor.position.set(0, -0.1, 2);
scene.add(floor);
scene.add(new THREE.AmbientLight(0xffffff, 0.8));
const light = new THREE.DirectionalLight(0xffffff, 0.8);
light.position.set(5, 10, 5);
scene.add(light);
function makeLabel(text) {
  const canvas = document.createElement('canvas');
  canvas.width = 256; canvas.height = 64;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 256, 64);
  ctx.fillStyle = '#000000'; ctx.font = '24px sans-serif'; ctx.textAlign = 'center';
  ctx.fillText(text, 128, 40);
  const texture = new THREE.CanvasTexture(canvas);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({map: texture}));
  sprite.scale.set(4, 1, 1);
  return sprite;
}
const boxes = {};
stations.forEach((station) => {
  const box = new THREE.Mesh(new THREE.BoxGeometry(2.5, 1.5, 2.5), new THREE.MeshStandardMaterial({color: 0x999999}));
  box.position.set(station.x, 0.75, station.z);
  scene.add(box);
  boxes[station.kind] = box;
  const label = makeLabel(station.label);
  label.position.set(station.x, 2.2, station.z);
  scene.add(label);
});
function colourFor(status) {
  if (status === 'done') return 0x2eaa66;
  if (status === 'failed') return 0xcc3333;
  if (status === 'started') return 0xe6b800;
  return 0x999999;
}
async function refresh() {
  try {
    const r = await fetch('/api/factory');
    const data = await r.json();
    const latest = {};
    (data.stages || []).forEach((stage) => { latest[stage.kind] = stage.status; });
    stations.forEach((station) => {
      if (latest[station.kind]) { boxes[station.kind].material.color.setHex(colourFor(latest[station.kind])); }
    });
    document.getElementById('legend').textContent = 'Latest stages: ' + Object.entries(latest).map(([k, v]) => k + '=' + v).join(', ');
    const runs = {};
    (data.stages || []).forEach((stage) => { runs[stage.run] = (runs[stage.run] || 0) + 1; });
    document.getElementById('runs').textContent = 'Trace runs: ' + Object.entries(runs).map(([k, v]) => k + ' (' + v + ' stages)').join(', ');
  } catch (e) {}
  requestAnimationFrame(() => { scene.rotation.y += 0.001; });
}
setInterval(refresh, 2000);
refresh();
function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); }
animate();
</script>
</body></html>
"""

FACTORY_HTML = FACTORY_HTML.replace("</head>", "<link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/favicon.svg\"><style>" + BASE_CSS + "</style></head>", 1)
