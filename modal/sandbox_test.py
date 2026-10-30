import modal
from sandbox_deploy import AidonSandbox, BASE_IMAGE, build_image_with

app = modal.App.lookup("aidon-sandbox", create_if_missing=True)

def show(label, r):
    print(f"--- {label} (returncode={r['returncode']})")
    print((r["stdout"] or r["stderr"])[-300:].strip())

# 1. Locked sandbox: installs should FAIL
box = AidonSandbox(app, timeout=300, block_network=True)
try:
    show("locked: apt-get update", box.run("apt-get", "update", timeout=60))
    show("locked: pip install cowsay", box.run("python", "-m", "pip", "install", "cowsay", timeout=40))
    show("sudo present?", box.run("sh", "-c", "which sudo || echo 'no sudo installed'"))
    show("plain root command (no sudo needed)", box.run("sh", "-c", "id && touch /etc/aidon_test && echo wrote to /etc"))
finally:
    box.close()

# 2. Networked sandbox: installs should WORK (test only, nothing sensitive in here)
box = AidonSandbox(app, timeout=600, block_network=False)
try:
    show("open: apt install tree", box.run("sh", "-c",
         "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cowsay", timeout=300))
    show("open: pip install cowsay", box.run("python", "-m", "pip", "install", "-q", "cowsay", timeout=120))
    show("open: cowsay works", box.run("python", "-c", "import cowsay; cowsay.cow('hi')"))
finally:
    box.close()

# 3. Approved install through the two-phase path
image = build_image_with(app, BASE_IMAGE, pip=["scipy"], apt=["jq"])
box = AidonSandbox(app, image=image, block_network=True)
try:
    show("snapshot: scipy in locked sandbox", box.run("python", "-c", "import scipy; print(scipy.__version__)"))
finally:
    box.close()
