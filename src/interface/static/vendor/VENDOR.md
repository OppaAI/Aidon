# Vendored browser libraries

Served locally so the WebUI avatar works offline and the page makes no
third-party requests at runtime. Do not edit these files in place;
upgrade by replacing them with the matching release artifacts.

| Path | Package | Version | Source |
| --- | --- | --- | --- |
| `three/build/three.module.js` | three | 0.160.0 | unpkg `three@0.160.0/build/three.module.js` |
| `three/examples/jsm/loaders/GLTFLoader.js` | three | 0.160.0 | unpkg `three@0.160.0/examples/jsm/loaders/GLTFLoader.js` |
| `three/examples/jsm/utils/BufferGeometryUtils.js` | three | 0.160.0 | unpkg `three@0.160.0/examples/jsm/utils/BufferGeometryUtils.js` |
| `@pixiv/three-vrm/lib/three-vrm.module.js` | @pixiv/three-vrm | 2.0.7 | unpkg `@pixiv/three-vrm@2.0.7/lib/three-vrm.module.js` |

Both packages are MIT-licensed; license headers are embedded in the
files themselves.

## Fonts (served from `../fonts/`)

| Files | Font | License |
| --- | --- | --- |
| `JetBrainsMono-*.woff2` | JetBrains Mono | SIL OFL 1.1 — `../fonts/OFL-JetBrainsMono.txt` |
| `Rajdhani-*.woff2` | Rajdhani | SIL OFL 1.1 — `../fonts/OFL-Rajdhani.txt` |

Both fonts are free and open source; the OFL texts are vendored next to
the font files as the license requires.
