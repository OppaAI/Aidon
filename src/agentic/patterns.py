"""Task patterns, Aidon patterns concept, rebuilt.

A pattern is a named reusable plan shape: keywords, steps, and deliverables. match_pattern(goal)
returns the first pattern whose keywords appear in the goal, or None, so a goal can be planned
from a pattern without a model call, and the pattern can be adapted by the DAG adapter.
Patterns are data, not model output, and adding one is adding a dict, not retraining anything.
"""

from __future__ import annotations

PATTERNS = [
    {"name": "write_file", "keywords": ["write", "create", "make"], "steps": [{"tool": "write", "deliverable": "output"}], "deliverables": ["output"]},
    {"name": "research_write", "keywords": ["research", "report", "summary"], "steps": [{"tool": "write", "deliverable": "report"}], "deliverables": ["report"]},
    {"name": "plan_only", "keywords": ["plan", "outline"], "steps": [{"tool": "write", "deliverable": "plan"}], "deliverables": ["plan"]},
]


def match_pattern(goal: str, patterns: list[dict] | None = None) -> dict | None:
    text = (goal or "").lower()
    for pattern in patterns or PATTERNS:
        if any(keyword in text for keyword in pattern.get("keywords", [])):
            return pattern
    return None


# -- deeper pattern machinery (added alongside the dict patterns above) -----

import re
from dataclasses import dataclass, field

TEXT_KINDS = [
    ("press release", "press release"),
    ("product description", "product description"),
    ("cover letter", "cover letter"),
    ("blog post", "blog post"),
    ("newsletter", "newsletter"),
    ("screenplay", "screenplay"),
    ("announcement", "announcement"),
    ("action plan", "action plan"),
    ("study guide", "study guide"),
    ("itinerary", "itinerary"),
    ("social media post", "social media post"),
    ("summary", "summary"),
    ("checklist", "checklist"),
    ("tutorial", "tutorial"),
    ("guide", "guide"),
    ("outline", "outline"),
    ("report", "report"),
    ("review", "review"),
    ("analysis", "analysis"),
    ("proposal", "proposal"),
    ("essay", "essay"),
    ("article", "article"),
    ("poem", "poem"),
    ("haiku", "haiku"),
    ("limerick", "limerick"),
    ("sonnet", "sonnet"),
    ("song", "song"),
    ("story", "story"),
    ("recipe", "recipe"),
    ("schedule", "schedule"),
    ("menu", "menu"),
    ("speech", "speech"),
    ("letter", "letter"),
    ("notes", "notes"),
    ("plan", "plan"),
    ("script", "script"),
    ("text", "text"),
]

_VOICE_CUES = re.compile(
    r"\b(read (?:it |this |them )?(?:out|aloud)|read me|say (?:it |this )?"
    r"|speak(?: it| this)?|narrat\w*|aloud|voice ?over|audio|tts|speech)\b",
    re.I)
_FILE_RE = re.compile(
    r"(/[\w.-]+(?:/[\w.-]+)*\.(?:md|txt|py|json|ya?ml|csv|html|pdf|docx|xlsx|pptx|sh|sql|ipynb)"
    r"|[\w][\w./-]*\.(?:md|txt|py|json|ya?ml|csv|html|pdf|docx|xlsx|pptx|sh|sql|ipynb))\b",
    re.I)
_FILE_CLAUSE_RE = re.compile(
    r"\s*(?:,|and)?\s*(?:please\s+)?(?:write|save|put|store|place|add|"
    r"output|export)?\s*(?:it|them|this|that)?\s*"
    r"(?:to|in|into|as|at|onto)\s+(?:a\s+)?(?:new\s+)?(?:file\s+)?"
    r"(/[\w.-]+(?:/[\w.-]+)*\.\w{1,5}|[\w][\w./-]*\.\w{1,5})", re.I)
_LEAD_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|i want you to\s+|"
    r"i need you to\s+|i'd like you to\s+)?"
    r"(?:write|create|make|save|produce|generate|compose|draft|put)\s+",
    re.I)
_TRAILING_RE = re.compile(
    r"\s+(?:as|in|to|into)\s+(?:a\s+)?(?:markdown|md|plain\s+text|json|"
    r"python|html|csv)\s*(?:file)?\s*\.?\s*$", re.I)
_DANGLING_RE = re.compile(
    r"\s*(?:,|and)?\s*(?:and\s+)?(?:write|save|put|store|place|add|"
    r"output|export)\s*(?:it|them|this)?\s*$", re.I)
_STOPWORDS = {"a", "an", "the", "to", "in", "of", "for", "and", "as", "me",
              "please", "about", "with", "is", "it", "that", "this", "my"}
_QUESTION_START_RE = re.compile(
    r"^\s*(?:hey\s+|ok\s+|okay\s+)?(?:can you|could you|would you|will you|"
    r"please\s+)?(?:who|what|when|where|which|why|how|is|are|was|were|do|"
    r"does|did|has|have|will|should|tell me|any)\b", re.I)


def extract_text_kind(goal: str) -> str | None:
    """The kind of text the goal asks for, longest phrase first."""
    low = (goal or "").lower()
    for phrase, kind in TEXT_KINDS:
        if re.search(r"\b" + re.escape(phrase) + r"\b", low):
            return kind
    return None


def extract_destinations(goal: str) -> list[str]:
    """Where the result should go: any of file, voice, screen."""
    out = []
    if _VOICE_CUES.search(goal or ""):
        out.append("voice")
    if extract_slot("file", goal):
        out.append("file")
    if not out:
        out.append("screen")
    return out


def extract_destination(goal: str) -> str:
    return ", ".join(extract_destinations(goal))


def _keywords(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _intent_keywords(goal: str) -> set[str]:
    """The goal's keywords minus its file names: a deliverable's
    name is not the intent."""
    text = goal or ""
    for name in re.findall(r"[\w./-]+\.\w{1,5}", text):
        text = text.replace(name, " ")
    return _keywords(text)


def extract_slot(kind: str, goal: str) -> str | None:
    """Pull one slot's value out of a plain-language goal, by rules
    only — no model call, so a pattern is never slower than planning."""
    goal = goal or ""
    if kind == "file":
        m = re.search(r"\b(?:to|in|into|as|at)\s+"
                      r"(/[\w.-]+(?:/[\w.-]+)*\.\w{1,5}|[\w][\w./-]*\.\w{1,5})",
                      goal, re.I)
        if m:
            return m.group(1)
        m = _FILE_RE.search(goal)
        return m.group(1) if m else None
    if kind == "kind":
        return extract_text_kind(goal) or "text"
    if kind == "destination":
        return extract_destination(goal)
    if kind == "topic":
        text = _FILE_CLAUSE_RE.sub("", goal)
        if text.strip() == goal.strip():
            text = _FILE_RE.sub("", text)
        text = _TRAILING_RE.sub("", text)
        text = _DANGLING_RE.sub("", text)
        text = _LEAD_VERB_RE.sub("", text)
        kind_word = extract_text_kind(text)
        if kind_word:
            text = re.sub(r"^(?:a\s+|an\s+|the\s+)?" + re.escape(kind_word)
                          + r"\s+(?:about\s+|on\s+)?", "", text, flags=re.I)
        text = re.sub(r"\s{2,}", " ", text).strip(" .,:;-")
        return text if len(text) >= 3 else None
    if kind == "goal":
        return goal.strip() or None
    if kind == "request":
        text = _FILE_CLAUSE_RE.sub("", goal)
        if text.strip() == goal.strip():
            text = _FILE_RE.sub("", text)
        text = _TRAILING_RE.sub("", text)
        text = _DANGLING_RE.sub("", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" .,:;-")
        return text or goal.strip() or None
    return None


@dataclass
class Pattern:
    name: str
    description: str
    steps: list[str] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)
    match: list[str] = field(default_factory=list)
    requires_question: bool = False

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "steps": list(self.steps), "slots": list(self.slots),
                "match": list(self.match),
                "requires_question": self.requires_question}


def looks_like_question(goal: str) -> bool:
    text = (goal or "").strip()
    if text.endswith("?"):
        return True
    return bool(_QUESTION_START_RE.match(text))


def fill_slots(pattern: Pattern, goal: str) -> dict | None:
    """Values for every slot of a pattern, or None if a required
    slot cannot be read from the goal."""
    values: dict[str, str] = {}
    for slot in pattern.slots:
        name = slot.get("name", "")
        kind = slot.get("kind", "goal")
        value = extract_slot(kind, goal)
        if value is None:
            if slot.get("required", True):
                return None
            continue
        values[name] = value
    return values


def _fill_template(obj, values: dict):
    if isinstance(obj, str):
        out = obj
        for key, value in values.items():
            out = out.replace("{vars." + key + "}", str(value))
        return out
    if isinstance(obj, dict):
        return {k: _fill_template(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill_template(v, values) for v in obj]
    return obj


def instantiate(pattern: Pattern, values: dict) -> dict:
    """A runnable node list for this pattern, with the goal's values
    filled in. Steps are plain dicts, not DAG objects, so the caller
    can compile them into whatever graph it uses."""
    nodes: list[dict] = []
    search_ids: list[str] = []
    if "research" in pattern.steps:
        for suffix, label in (("", "Research the subject"),
                              (" comparison review", "Research another angle")):
            node_id = f"search{len(nodes) + 1}"
            nodes.append({"id": node_id, "name": label, "tool": "web_search",
                          "args": {"query": "{vars.topic}" + suffix}, "deps": []})
            search_ids.append(node_id)
    if "generate_text" in pattern.steps:
        prompt = ("Write a {vars.kind} about {vars.topic}.\n\n"
                  "The user's request: {vars.request}\n\n"
                  "Follow the request exactly — its length, format, tone,"
                  " and any extra requirements. Return ONLY the text.")
        if search_ids:
            prompt += ("\n\nResearch results:\n"
                       + "\n".join("{nodes." + n + "}" for n in search_ids)
                       + "\n\nUse the concrete facts the results give you"
                         " rather than generalities.")
        nodes.append({"id": "draft", "name": "Write the text", "tool": "llm",
                      "args": {"prompt": prompt}, "deps": list(search_ids),
                      "executor": "strong"})
    if "answer" in pattern.steps:
        prompt = ("Answer the user's question using the research results.\n\n"
                  "Question: {vars.goal}\n\nResearch results:\n"
                  + "\n".join("{nodes." + n + "}" for n in search_ids)
                  + "\n\nBe direct and specific. No preamble.")
        nodes.append({"id": "answer", "name": "Answer", "tool": "llm",
                      "args": {"prompt": prompt}, "deps": list(search_ids),
                      "executor": "strong"})
    destinations = [d.strip() for d in
                    str(values.get("destination", "screen")).split(",")
                    if d.strip()] or ["screen"]
    if "deliver" in pattern.steps:
        if "file" in destinations:
            fname = values.get("file", "output.md")
            nodes.append({"id": "write", "name": "Write the file",
                          "tool": "write", "args": {"file": fname,
                                                    "text": "{nodes.draft}"},
                          "deps": ["draft"] if any(n["id"] == "draft" for n in nodes) else []})
            nodes.append({"id": "verify", "name": "Read it back",
                          "tool": "read", "args": {"file": fname},
                          "deps": ["write"]})
        if "voice" in destinations:
            nodes.append({"id": "speak", "name": "Read it aloud",
                          "tool": "tts", "args": {"text": "{nodes.draft}"},
                          "deps": ["draft"] if any(n["id"] == "draft" for n in nodes) else []})
    reply_node = "draft" if "generate_text" in pattern.steps else "answer"
    dag = {"name": pattern.name, "goal": "", "nodes": nodes,
           "reply_node": reply_node}
    if "file" in destinations:
        dag["acceptance_criteria"] = [
            f"a file exists at {values.get('file', 'output.md')}",
            f"the file contains the {values.get('kind', 'text')} the user asked for",
        ]
    elif "voice" in destinations:
        dag["acceptance_criteria"] = ["the text was synthesized to speech without error"]
    else:
        dag["acceptance_criteria"] = [
            f"the reply contains the {values.get('kind', 'text')} the user asked for"]
    return _fill_template(dag, values)


def score_pattern(pattern: Pattern, goal: str) -> float:
    """How well a pattern fits a goal, 0..1: intent words first,
    then whether the goal actually states the values the pattern
    needs (a text kind, a topic, a file)."""
    goal_keywords = _intent_keywords(goal)
    if not goal_keywords:
        return 0.0
    hits = [w for w in pattern.match if w in goal_keywords]
    score = min(1.0, len(hits) / 2.0)
    score += 0.05 * max(0, len(hits) - 2)
    kinds = {s.get("kind") for s in pattern.slots}
    if "kind" in kinds and extract_text_kind(goal):
        score += 0.20
    if "topic" in kinds and extract_slot("topic", goal):
        score += 0.10
    if "file" in kinds and extract_slot("file", goal):
        score += 0.15
    return min(1.0, score)


def default_patterns() -> list[Pattern]:
    """The general procedures Aidon knows how to run."""
    text_slots = [{"name": "goal", "kind": "goal", "required": True},
                  {"name": "request", "kind": "request", "required": True},
                  {"name": "kind", "kind": "kind", "required": False},
                  {"name": "topic", "kind": "topic", "required": True},
                  {"name": "destination", "kind": "destination", "required": False},
                  {"name": "file", "kind": "file", "required": False}]
    return [
        Pattern(name="text",
                description="Write a piece of text of the requested kind and deliver it.",
                steps=["generate_text", "deliver"], slots=text_slots,
                match=["write", "create", "produce", "generate", "draft",
                       "compose", "make", "poem", "haiku", "article", "essay",
                       "story", "recipe", "guide", "letter", "outline", "speech"]),
        Pattern(name="research_text",
                description="Research a subject, write the requested text about it, and deliver it.",
                steps=["research", "generate_text", "deliver"], slots=text_slots,
                match=["research", "compare", "comparison", "investigate",
                       "gather", "survey", "report", "analysis", "benchmark",
                       "versus", "sources", "latest", "current"]),
        Pattern(name="question",
                description="Look a question up and answer it.",
                steps=["research", "answer"],
                slots=[{"name": "goal", "kind": "goal", "required": True},
                       {"name": "topic", "kind": "topic", "required": True}],
                match=["who", "what", "when", "where", "how", "which", "why",
                       "weather", "price", "cost", "score", "news", "today",
                       "tomorrow", "yesterday"],
                requires_question=True),
        Pattern(name="summarise",
                description="Condense a named text or topic into a short summary.",
                steps=["research", "generate_text", "deliver"],
                slots=[{"name": "goal", "kind": "goal", "required": True},
                       {"name": "request", "kind": "request", "required": True},
                       {"name": "kind", "kind": "kind", "required": False},
                       {"name": "topic", "kind": "topic", "required": True},
                       {"name": "destination", "kind": "destination", "required": False}],
                match=["summarise", "summarize", "condense", "shorten", "tl;dr", "digest"]),
        Pattern(name="plan_outline",
                description="Produce a plan or outline for a goal, without executing it.",
                steps=["generate_text", "deliver"],
                slots=[{"name": "goal", "kind": "goal", "required": True},
                       {"name": "request", "kind": "request", "required": True},
                       {"name": "kind", "kind": "kind", "required": False},
                       {"name": "topic", "kind": "topic", "required": True},
                       {"name": "destination", "kind": "destination", "required": False}],
                match=["plan", "outline", "roadmap", "steps", "strategy"]),
        Pattern(name="build_script",
                description="Write a small script to a file and verify it reads back.",
                steps=["generate_text", "deliver"],
                slots=[{"name": "goal", "kind": "goal", "required": True},
                       {"name": "request", "kind": "request", "required": True},
                       {"name": "kind", "kind": "kind", "required": False},
                       {"name": "topic", "kind": "topic", "required": True},
                       {"name": "destination", "kind": "destination", "required": False},
                       {"name": "file", "kind": "file", "required": True}],
                match=["script", "program", "code", "tool", "utility", "automation"]),
    ]
