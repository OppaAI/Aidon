"""
Serve Cloudflare Clef Flash (9B) on Modal using SGLang.

Dev (temporary URL, hot reload):   modal serve clef_modal.py
One-off test request:              modal run clef_modal.py
Deploy (stable URL):               modal deploy clef_modal.py

Clef is a decision model, not a chat model: it only answers /v1/systemone
requests (typed questions in, probability per option out). It refuses
chat/generation requests.
"""
import json
import subprocess
import urllib.request

import modal

MODEL = "Cloudflare/clef-flash"  # use "Cloudflare/clef" for the 27B
PORT = 30000
GPU = "L40S"  # 48 GB. BF16 9B is ~18 GB of weights, so L4/A10G (24 GB) is tight.
# Must be an empty path: the SGLang image already has files in ~/.cache/huggingface,
# and Modal can't mount a volume over a non-empty directory. HF_HOME points there.
HF_CACHE_DIR = "/hf-cache"

# SGLang's Clef support landed after v0.5.21, so use the dev-clef image
# that SGLang built from the merge commit.
image = (
    modal.Image.from_registry("lmsysorg/sglang:dev-clef")
    .entrypoint([])  # drop the image's default entrypoint
    # The SGLang image's Python lacks Modal's client dependencies (grpclib, etc.),
    # which makes the container crash on startup. Installing modal pulls them in.
    .pip_install("modal")
    .env({"HF_HOME": HF_CACHE_DIR})
)

hf_cache = modal.Volume.from_name("hf-cache", create_if_missing=True)
app = modal.App("aidon-clef-flash", image=image)


@app.function(
    gpu=GPU,
    volumes={HF_CACHE_DIR: hf_cache},
    scaledown_window=300,  # keep warm 5 min after last request
    timeout=60 * 60,
)
@modal.concurrent(max_inputs=32)
@modal.web_server(port=PORT, startup_timeout=15 * 60)
# Public by default. For anything beyond a demo, add requires_proxy_auth=True
# to @modal.web_server and send Modal-Key / Modal-Secret headers.
def serve():
    subprocess.Popen(
        [
            "python", "-m", "sglang.launch_server",
            "--model-path", MODEL,
            "--host", "0.0.0.0",
            "--port", str(PORT),
        ]
    )


def decide(base_url: str, state, questions: dict, images: list | None = None) -> dict:
    """POST a decision request to /v1/systemone and return the answers dict."""
    payload = {"model": MODEL, "state": state, "questions": questions}
    if images:
        payload["images"] = images  # data URLs (keep the data:image/...;base64, prefix) or http(s) URLs
    req = urllib.request.Request(
        base_url.rstrip("/") + "/v1/systemone",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    # First call can take minutes while the container boots and loads weights.
    with urllib.request.urlopen(req, timeout=900) as resp:
        return json.load(resp)


@app.local_entrypoint()
def test():
    url = serve.get_web_url()
    print("Endpoint:", url)

    # Example: Aidon verifier gate after a generated tool was tested.
    state = {
        "task": "Create an MCP tool that returns open tickets from the mock Jira server",
        "pytest_exit_code": 0,
        "tests": "5 passed, 0 failed",
        "sample_response": {"tickets": [{"id": "PROJ-1", "status": "Open"}]},
    }
    questions = {
        "tests_passed": {
            "type": "noul",
            "instructions": "Do the results show the tool works as the task requires?",
        },
        "next_action": {
            "type": "choice",
            "instructions": "What should the agent do next?",
            "criteria": {
                "accept": "The result is complete and correct",
                "retry": "The result has fixable problems",
                "escalate": "A human should review or decide",
            },
        },
    }

    result = decide(url, state, questions)
    print(json.dumps(result, indent=2))
    print("Next action:", result["answers"]["next_action"]["choice"])
