import time
import modal
from sandbox_deploy import AidonSandbox

# If the volume check fails without a delay, try 3 to test for a commit race.
SETTLE_SECONDS = 0

app = modal.App.lookup("aidon-sandbox", create_if_missing=True)
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    line = f"[{'PASS' if ok else 'FAIL'}] {name}"
    if not ok and detail != "":
        line += f"  -> {detail}"
    print(line, flush=True)


box = AidonSandbox(app, timeout=300, block_network=True)
try:
    # --- Basics ---
    r = box.run("echo", "hi")
    check("exec works", r["stdout"].strip() == "hi" and r["returncode"] == 0, r)

    r = box.run("sh", "-c", "exit 3")
    check("nonzero exit code is reported", r["returncode"] == 3, r)

    r = box.run("whoami")
    check("runs as root (no sudo needed)", r["stdout"].strip() == "root", r)

    # --- File I/O ---
    tricky = 'quotes " \' $HOME `backticks` unicode: こんにちは\nline2'
    box.write_file("/tmp/tricky.txt", tricky)
    check("write/read round-trip with special chars",
          box.read_file("/tmp/tricky.txt") == tricky)

    # --- Network lockdown ---
    r = box.run("curl", "-sS", "-m", "5", "https://example.com")
    check("DNS/HTTP blocked", r["returncode"] != 0, r)

    r = box.run("curl", "-sS", "-m", "5", "http://1.1.1.1")
    check("raw IP blocked", r["returncode"] != 0, r)

    r = box.run("python", "-m", "pip", "install", "cowsay", timeout=40)
    check("pip install blocked at runtime", r["returncode"] != 0, r)

    # --- Isolation from host and secrets ---
    r = box.run("env")
    check("no Modal tokens in environment",
          "MODAL_TOKEN" not in r["stdout"] and "SECRET" not in r["stdout"].upper(),
          r["stdout"][:300])

    r = box.run("sh", "-c",
                "test -e /root/.modal.toml && echo FOUND || echo none; ls /home")
    check("no host files visible",
          "FOUND" not in r["stdout"] and "Jonathan" not in r["stdout"], r)

    # --- Output truncation ---
    r = box.run("python", "-c", "print('x' * 200000)", max_chars=4000)
    check("output truncated to max_chars", len(r["stdout"]) <= 4000, len(r["stdout"]))

    # --- Command timeout ---
    t0 = time.time()
    try:
        r = box.run("sleep", "60", timeout=5)
    except Exception as e:
        r = {"returncode": "exception", "stderr": repr(e)}
    elapsed = time.time() - t0
    check("command timeout enforced (<20s)", elapsed < 20, f"took {elapsed:.1f}s, {r}")

    # --- State within one sandbox ---
    box.write_file("/tmp/state.txt", "still here")
    check("files persist between execs in the same sandbox",
          box.read_file("/tmp/state.txt") == "still here")

    # --- Volume ---
    box.write_file("/data/suite_test.txt", "vol ok")
    box.flush()
    check("volume write/read", box.read_file("/data/suite_test.txt") == "vol ok")
finally:
    box.close()

time.sleep(SETTLE_SECONDS)

# --- Wipe check: a NEW sandbox should not see /tmp from the old one ---
box2 = AidonSandbox(app, timeout=120, block_network=True)
try:
    r = box2.run("ls", "/tmp/state.txt")
    check("/tmp wiped in a new sandbox", r["returncode"] != 0, r)

    try:
        ok = box2.read_file("/data/suite_test.txt") == "vol ok"
        detail = ""
    except FileNotFoundError as e:
        ok, detail = False, str(e)
    check("volume data survives into the new sandbox", ok, detail)
finally:
    box2.close()

print(f"\n{sum(results)}/{len(results)} checks passed")
