"""Schema-driven intake: slot-filling interviews before the pipeline.

A user should be able to state a goal the way they would say it
out loud and have Aidon gather what it needs by talking, one
question at a time, instead of needing a perfectly briefed prompt.

Division of labour, and it matters: the model side proposes
(the question wording, the extraction from an answer) and code
disposes. Python owns the checklist — which slot is asked next,
whether a required slot is filled, type checks, defaults, and
the gate that decides the interview is complete. No model output
ever votes on whether enough information has been gathered.

Schemas are plain data, so a new task type is a new dict, not
new code. Two ship here: a generic goal schema and a dinner
schema. A slot's fields: name, label, kind (text, int, money,
money_cents, choice, bool, list), required, choices for choice
slots, a default, a confirm flag, and the fallback question text.

Grounded extraction: a value enters the spec only if it passes
coercion and, when source text is given, that text actually
contains it. An extraction that invents a choice the user never
named is dropped, never silently accepted. Confirm slots (the
dinner allergies slot) count as filled only when the user
explicitly answered that slot's question or approved the final
summary that displayed the value — a prefilled value alone
never fills them, and a default never applies to them.
"""

from __future__ import annotations

import copy
import re

_GENERIC_SCHEMA = {
    "name": "generic",
    "title": "your goal",
    "slots": [
        {"name": "goal", "label": "Goal", "kind": "text", "required": True,
         "ask": "What should I do for you?"},
        {"name": "audience", "label": "Who it is for", "kind": "text",
         "required": False,
         "ask": "Who is this for?"},
        {"name": "notes", "label": "Notes", "kind": "list", "required": False,
         "default": [],
         "ask": "Anything else I should know?"},
    ],
}

_DINNER_SCHEMA = {
    "name": "dinner",
    "title": "your dinner",
    "slots": [
        {"name": "main", "label": "Main dish", "kind": "text", "required": True,
         "ask": "What should the main dish be?"},
        {"name": "guests", "label": "Guests", "kind": "int", "required": True,
         "ask": "How many people are eating, including yourself?"},
        {"name": "budget_cents", "label": "Budget (cents)", "kind": "money_cents",
         "required": True,
         "ask": "What is the total budget for the shopping basket, in cents?"},
        {"name": "allergies", "label": "Allergies", "kind": "list",
         "required": True, "confirm": True,
         "ask": "Are there any food allergies I must plan around?"
                " Please name them, or say 'none'."},
        {"name": "store", "label": "Store", "kind": "text", "required": True,
         "ask": "Which store should I price this at?"},
        {"name": "style", "label": "Style", "kind": "choice", "required": False,
         "choices": ["fine_dining", "family_style", "casual"],
         "default": "family_style",
         "ask": "How fancy should it feel — fine-dining, family-style,"
                " or casual?"},
    ],
}

SCHEMAS = {"generic": _GENERIC_SCHEMA, "dinner": _DINNER_SCHEMA}


def get_schema(name: str) -> dict | None:
    """A fresh copy of a named schema, or None for an unknown name."""
    schema = SCHEMAS.get((name or "").strip().lower())
    return copy.deepcopy(schema) if schema else None


def _slot(schema: dict, name: str) -> dict | None:
    for slot in schema["slots"]:
        if slot["name"] == name:
            return slot
    return None


def slot_value(spec: dict, name: str):
    return spec.get(name)


def is_filled(schema: dict, spec: dict, slot: dict) -> bool:
    """One slot's gate condition. A confirm slot needs its explicit
    confirmation flag as well as a value; a prefilled value alone
    does not fill it."""
    value = spec.get(slot["name"])
    if slot.get("confirm"):
        return bool(spec.get("_confirmed_" + slot["name"])) \
            and value is not None
    if value is None:
        return False
    if slot["kind"] == "list":
        return isinstance(value, list)
    if slot["kind"] == "text":
        return isinstance(value, str) and bool(value.strip())
    return True


def missing_slots(schema: dict, spec: dict,
                  include_optional: bool = False) -> list[dict]:
    """Unfilled slots in schema (asking) order."""
    out = []
    for slot in schema["slots"]:
        if not include_optional and not slot.get("required"):
            continue
        if not is_filled(schema, spec, slot):
            out.append(slot)
    return out


def ready(schema: dict, spec: dict) -> bool:
    return not missing_slots(schema, spec)


# -- coercion ---------------------------------------------------------------

def _digits(text: str) -> str:
    return "".join(c for c in text if c.isdigit())


def coerce_value(slot: dict, value):
    """Return (ok, coerced). Wrong types and out-of-set choices are
    rejected; nothing is guessed."""
    kind = slot["kind"]
    if value is None:
        return False, None
    if kind == "int":
        try:
            number = int(value)
        except (TypeError, ValueError):
            if isinstance(value, str) and _digits(value):
                number = int(_digits(value))
            else:
                return False, None
        return (number > 0), (number if number > 0 else None)
    if kind == "money":
        try:
            amount = float(value)
        except (TypeError, ValueError):
            if isinstance(value, str):
                try:
                    amount = float(value.replace("$", "").replace(",", "")
                                      .strip())
                except ValueError:
                    return False, None
            else:
                return False, None
        return (amount > 0), (amount if amount > 0 else None)
    if kind == "money_cents":
        # A bare integer is already cents; a "$100" or "100.00"
        # string is dollars and is converted to cents.
        if isinstance(value, int) and not isinstance(value, bool):
            return (value > 0), (value if value > 0 else None)
        if isinstance(value, float):
            cents = int(round(value * 100))
            return (cents > 0), (cents if cents > 0 else None)
        if isinstance(value, str):
            cleaned = value.replace("$", "").replace(",", "").strip()
            try:
                if "." in cleaned:
                    cents = int(round(float(cleaned) * 100))
                else:
                    cents = int(cleaned)
            except ValueError:
                return False, None
            return (cents > 0), (cents if cents > 0 else None)
        return False, None
    if kind == "bool":
        if isinstance(value, bool):
            return True, value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("yes", "true", "1", "y"):
                return True, True
            if low in ("no", "false", "0", "n"):
                return True, False
        return False, None
    if kind == "choice":
        text = str(value).strip().lower().replace(" ", "_").replace("-", "_")
        return (text in slot.get("choices", [])), \
            (text if text in slot.get("choices", []) else None)
    if kind == "list":
        if isinstance(value, str):
            items = [p.strip() for p in re.split(r",|\band\b", value)
                     if p.strip()]
            return True, items
        if isinstance(value, (list, tuple)):
            items = [str(v).strip() for v in value if str(v).strip()]
            return True, items
        return False, None
    text = str(value).strip()
    return bool(text), (text if text else None)


def _grounded(slot: dict, value, source_text: str) -> bool:
    """Whether the user's own words contain this value. Numbers
    match on their digits; choices match on any word of the value;
    text and list items match on the substring, case-insensitive.
    A negated mention ("no slides", "not family style") within
    three words before the match does not count."""
    low = source_text.lower()
    if slot["kind"] in ("int", "money", "money_cents"):
        return _digits(str(value)) in _digits(low)
    if slot["kind"] == "choice":
        words = re.findall(r"[a-z0-9]+", str(value).lower())
        return any(_word_mentioned(w, low) for w in words)
    if slot["kind"] == "list":
        items = value if isinstance(value, list) else [value]
        return all(str(item).lower() in low for item in items)
    return str(value).lower() in low


_NEGATIONS = ("not", "never", "don't", "dont", "isn't", "isnt", "without", "no")


def _word_mentioned(word: str, low: str) -> bool:
    for clause in re.split(r"[.!?;,]|\bbut\b", low):
        tokens = re.findall(r"[a-z0-9']+", clause)
        for i, token in enumerate(tokens):
            if token == word:
                window = tokens[max(0, i - 3):i]
                if not any(n in window for n in _NEGATIONS):
                    return True
    return False


def apply_extraction(schema: dict, spec: dict, extracted: dict,
                     source_text: str | None = None) -> list[str]:
    """Merge an extraction into the spec. Only known slots, only
    values that pass coercion, and only values grounded in the
    source text when one is given. Confirm flags are never set
    here — extraction is a claim, the user's explicit answer or
    summary approval is the confirmation. Returns newly set names."""
    filled = []
    if not isinstance(extracted, dict):
        return filled
    for slot in schema["slots"]:
        name = slot["name"]
        if name not in extracted:
            continue
        ok, value = coerce_value(slot, extracted[name])
        if not ok:
            continue
        if source_text is not None and not _grounded(slot, value, source_text):
            continue
        if spec.get(name) != value:
            spec[name] = value
            filled.append(name)
    return filled


def apply_default(spec: dict, slot: dict) -> bool:
    """Fill a slot with its declared default, recorded as assumed.
    Confirm slots never get a default."""
    if "default" not in slot or slot.get("confirm"):
        return False
    spec[slot["name"]] = slot["default"]
    assumed = spec.setdefault("_assumed", [])
    if slot["name"] not in assumed:
        assumed.append(slot["name"])
    return True


def public_spec(schema: dict, spec: dict) -> dict:
    """The spec as it travels on: slot values plus the list of
    defaults that were assumed (visible, never silent). Private
    bookkeeping keys stay out."""
    out = {}
    for slot in schema["slots"]:
        if slot["name"] in spec:
            out[slot["name"]] = spec[slot["name"]]
    if spec.get("_assumed"):
        out["assumed_defaults"] = list(spec["_assumed"])
    return out


def summary_lines(schema: dict, spec: dict) -> list[str]:
    lines = []
    assumed = set(spec.get("_assumed", []))
    for slot in schema["slots"]:
        name = slot["name"]
        if name not in spec:
            continue
        value = spec[name]
        if isinstance(value, list):
            shown = ", ".join(str(v) for v in value) if value else "none"
        else:
            shown = str(value).replace("_", " ")
        suffix = " (assumed — say if you'd change it)" \
            if name in assumed else ""
        lines.append(f"- {slot['label']}: {shown}{suffix}")
    return lines


# -- the interview loop ------------------------------------------------------

_SKIP_ONE = ("you decide", "up to you", "whatever", "don't care",
             "dont care", "skip")
_SKIP_ALL = ("just go", "just plan", "that's enough", "thats enough",
             "enough questions", "go ahead and plan")
_MAX_TURNS = 14


def _matches(text: str, phrases) -> bool:
    low = (text or "").strip().lower()
    return any(p in low for p in phrases)


def _ask_result(ask, question):
    """Normalise an ask callback's return: a triple
    (decision, text, decided_by), a pair, or plain reply text."""
    result = ask(question)
    if isinstance(result, str):
        return "approved", result, "human"
    if isinstance(result, tuple) and len(result) == 3:
        return result
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], result[1], "human"
    return "approved", "", "human"


def run_intake(schema: dict, seed_text: str, *, ask, extract, word,
               max_turns: int = _MAX_TURNS, record=None) -> dict | None:
    """Drive the interview with the caller's callbacks:

    ask(question) -> (decision, text, decided_by) — the chat turn;
    extract(transcript) -> dict of slot values the user has stated;
    word(slot, spec, last_user_text) -> the next question, phrased.

    Returns the public spec, or None when the interview is bailed
    out of (rejection, no human present, turn cap) — the caller
    then proceeds with the plain goal, as before intake existed.
    """
    spec: dict = {}
    transcript = seed_text or ""
    user_texts = [seed_text or ""]
    # Prefill from the seed goal, grounded in the seed's own words.
    apply_extraction(schema, spec, extract(transcript), source_text=transcript)

    turns = 0
    last_user = ""
    while turns < max_turns:
        missing = missing_slots(schema, spec)
        if not missing:
            break
        slot = missing[0]
        question = word(slot, spec, last_user)
        decision, text, decided_by = _ask_result(ask, question)
        turns += 1
        if decided_by != "human" or decision == "rejected":
            return None
        text = (text or "").strip()
        last_user = text
        if record is not None and text:
            record(question, text)
        if _matches(text, _SKIP_ALL):
            for rest in missing_slots(schema, spec):
                apply_default(spec, rest)
            break
        if text:
            transcript += f"\nQ: {question}\nA: {text}"
            user_texts.append(text)
            apply_extraction(schema, spec, extract(transcript),
                             source_text="\n".join(user_texts))
            if slot.get("confirm") and spec.get(slot["name"]) is not None:
                spec["_confirmed_" + slot["name"]] = True
            elif not is_filled(schema, spec, slot) \
                    and _matches(text, _SKIP_ONE):
                apply_default(spec, slot)
            continue
        # Empty reply: default this slot if it has one, else bail
        # rather than trapping the user in a question loop.
        if apply_default(spec, slot):
            continue
        return None
    for slot in missing_slots(schema, spec):
        if spec.get(slot["name"]) is None:
            return None

    body = ("Here's what I've got — tell me if anything's off:\n\n"
            + "\n".join(summary_lines(schema, spec))
            + "\n\nShall I go ahead and plan it with this?")
    decision, text, decided_by = _ask_result(ask, body)
    if decided_by != "human" or decision == "rejected":
        return None
    for slot in schema["slots"]:
        if slot.get("confirm"):
            spec["_confirmed_" + slot["name"]] = True
    if decision == "edited" and (text or "").strip():
        user_texts.append(text.strip())
        apply_extraction(schema, spec, extract(transcript + f"\nCorrection: {text}"),
                         source_text="\n".join(user_texts))
        if not ready(schema, spec):
            return None
    return public_spec(schema, spec)
