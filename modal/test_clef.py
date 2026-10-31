"""Decision test for Cloudflare/clef-flash via the SystemOne endpoint (/v1/systemone).

Usage:
    python test_clef.py

Request shape (from the server's OpenAPI spec):
    {"state": ..., "model": ..., "questions": {name: {"type": "choice"|"score"|"noul", ...}}}
    - choice: "criteria" is {option_key: description}
    - score:  "criteria" is a list of 1-10 things to rate
    - noul:   optional "criteria" {"true": ..., "false": ...}

The response format isn't in the spec, so the script prints the raw response for the
first call of each question type, then tries to pull the chosen option out of it.
If accuracy looks wrong, paste the raw output and the extractor can be fixed.

Standard library only.
"""
import json
import time
import urllib.error
import urllib.request

BASE_URL = "https://oppa-ai-org--aidon-clef-flash-serve.us-east.modal.run"
MODEL = "Cloudflare/clef-flash"

# (situation, [options], index of correct option)
CASES = [
    ("Phone battery is 8%, home is 5 km away, a charger is 200 m away.",
     ["Keep walking home", "Go to the charger", "Turn off the phone"], 1),
    ("A pot of oil on the stove has caught fire.",
     ["Pour water on it", "Cover it with a lid and turn off the heat", "Carry it outside"], 1),
    ("You hear a smoke alarm at 3 a.m. and smell smoke.",
     ["Go back to sleep", "Open all windows and wait", "Get everyone out and call emergency services"], 2),
    ("A robot's left wheel motor reports a stall current of 3x normal.",
     ["Increase power to push through", "Stop the motor and flag a fault", "Ignore it, readings are noisy"], 1),
    ("Raw chicken has been sitting on the counter for 6 hours at room temperature.",
     ["Cook it thoroughly and serve", "Throw it away", "Rinse it and refrigerate"], 1),
    ("You find an unknown mushroom while hiking and cannot identify it.",
     ["Eat a small piece to test it", "Do not eat it", "Boil it and eat it"], 1),
    ("A server's disk is 98% full and a log folder holds 40 GB of old logs.",
     ["Reboot the server", "Archive or delete the old logs", "Buy a bigger server first"], 1),
    ("An API call fails with HTTP 429 (rate limited) and a Retry-After of 30 seconds.",
     ["Retry immediately in a tight loop", "Wait 30 seconds, then retry", "Give up permanently"], 1),
]

KEYS = "ABCDEFGH"
_raw_shown = set()


def post(state, questions):
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/v1/systemone",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:  # long timeout for cold start
            return json.load(r)
    except urllib.error.HTTPError as e:
        print(f"\nHTTP {e.code} from server:\n{e.read().decode(errors='replace')}\n")
        print("Request body that was sent:\n" + body.decode() + "\n")
        raise SystemExit(1)


def show_raw_once(label, resp):
    if label not in _raw_shown:
        _raw_shown.add(label)
        print(f"--- raw response ({label}) ---")
        print(json.dumps(resp, indent=2)[:2000])
        print("---\n")


def find(obj, name):
    """Find the answer for question `name` anywhere in the response."""
    if isinstance(obj, dict):
        if name in obj:
            return obj[name]
        for v in obj.values():
            hit = find(v, name)
            if hit is not None:
                return hit
    elif isinstance(obj, list):
        for v in obj:
            hit = find(v, name)
            if hit is not None:
                return hit
    return None


def extract_choice(ans, keys):
    """Best-effort: turn an unknown answer shape into one of the option keys."""
    if isinstance(ans, str):
        return ans if ans in keys else None
    if isinstance(ans, dict):
        for k in ("choice", "answer", "selected", "decision", "value", "result", "best", "top"):
            if k in ans:
                got = extract_choice(ans[k], keys)
                if got:
                    return got
        nums = {k: v for k, v in ans.items() if k in keys and isinstance(v, (int, float))}
        if nums:
            return max(nums, key=nums.get)  # highest-scoring candidate
    if isinstance(ans, list) and ans:
        return extract_choice(ans[0], keys)
    return None


def ask_choice(situation, options):
    criteria = {KEYS[i]: o for i, o in enumerate(options)}
    resp = post(
        situation,
        {"decision": {"type": "choice", "instructions": "Pick the best action.", "criteria": criteria}},
    )
    show_raw_once("choice", resp)
    key = extract_choice(find(resp, "decision"), criteria.keys())
    return (criteria[key] if key else None), criteria


def probe_other_types():
    """Print raw responses for score and noul questions so you can see their shape."""
    resp = post(
        "Phone battery is 8%, home is 5 km away, a charger is 200 m away.",
        {
            "urgency": {"type": "score", "instructions": "Rate the situation.",
                        "criteria": ["How urgent is it to act?", "How risky is doing nothing?"]},
            "is_low": {"type": "noul", "instructions": "Is the battery critically low?",
                       "criteria": {"true": "Battery is critically low", "false": "Battery is fine"}},
        },
    )
    print("--- raw response (score + noul) ---")
    print(json.dumps(resp, indent=2)[:2000])
    print("---\n")


def main():
    print("Warming up (first call may take a while on a cold start)...")
    t0 = time.time()
    ask_choice(*CASES[0][:2])
    print(f"Warm-up done in {time.time() - t0:.1f}s\n")

    probe_other_types()

    total = ok_fwd = ok_rev = unreadable = 0
    for situation, options, right in CASES:
        total += 1
        right_text = options[right]

        got, _ = ask_choice(situation, options)
        unreadable += got is None
        fwd = got == right_text
        ok_fwd += fwd

        got_r, _ = ask_choice(situation, options[::-1])  # reversed order: position bias check
        unreadable += got_r is None
        rev = got_r == right_text
        ok_rev += rev

        print(f"- {situation}")
        print(f"  forward:  {'OK ' if fwd else 'BAD'} -> {got}")
        print(f"  reversed: {'OK ' if rev else 'BAD'} -> {got_r}\n")

    print("=" * 50)
    print(f"Accuracy (original order): {ok_fwd}/{total}")
    print(f"Accuracy (reversed order): {ok_rev}/{total}")
    print(f"Answers I couldn't read:   {unreadable}")
    if unreadable:
        print("Paste the raw response above and I'll fix the extractor.")
    elif ok_fwd != ok_rev:
        print("Accuracy differs by option order, which suggests position bias.")


if __name__ == "__main__":
    main()
