# Aidon demo deck

Open `index.html` in a modern browser, or serve this folder locally:

```sh
python -m http.server 8000 --directory demo
```

Then visit <http://localhost:8000>. The nine-slide deck opens on the project title, then presents each of the four README showcase capabilities followed immediately by a more detailed process-flow slide. Aidon’s speech bubbles contain the narration; **Listen** reads the active slide aloud using the browser’s built-in speech voice and animates the robot while it speaks. Each capability overview has a simple flow and matching cartoon; its detail slide expands that process into concrete steps.

Use the arrow buttons or keyboard arrows to move between slides. Press `S` to play narration and `Esc` to stop it. Voice availability and voice selection depend on the browser and operating system. The cartoon illustrations are in `assets/showcase-cartoon-grid.png`. The presentation has no build step or external dependencies.

## Speech file quick check

The accompanying narration is in [`SPEECH.md`](SPEECH.md). To place a test
copy in the allowed local Downloads folder, run this from the repository root:

```sh
cp demo/SPEECH.md ~/Downloads/speech.md
```

Then message Aidon with the full path, for example: “Read
`~/Downloads/speech.md` and tell me its title and final sentence. Do not send
the file anywhere.” This checks local document reading without publishing it.
