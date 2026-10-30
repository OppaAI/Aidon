"""Command line for Aidon.

Usage:
  python -m src.main goal "Plan a dinner for two"
  python -m src.main goal --conversation dinner "Plan a dinner for two"

Configuration comes from the environment, or from a .env
file in the current directory or in the Aidon home, loaded
without overriding real environment variables:
  AIDON_HOME      where Aidon keeps its state (default ~/.aidon)
  AIDON_MODEL     model name at the endpoint (default muse-spark)
  AIDON_API_ENDPOINT  endpoint URL for the model API
  AIDON_API_KEY   API key for that endpoint
  AIDON_EMBED_MODEL  embedding model, default google/embeddinggemma-2
  AIDON_EMBEDDER=hash  forces the test embedder

Memory starts blank, by design: new collections, no import
from previous chats. If the real embedder is not
installed, the CLI falls back to HashEmbedder with a
warning on stderr, so a run still works offline, with
non-semantic recall. The demo and submission run on the
real embedder, not on the fallback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from ..interface.approval import ApprovalGate, Decision
from .config import Config
from ..agentic.loop import GoalRunner, format_run_stats
from .model_client import FakeClient
from ..capabilities.sandbox import Sandbox


def _startup_style(kind: str, text: str) -> None:
    """Print compact, terminal-friendly startup diagnostics."""
    if sys.stderr.isatty():
        colors = {
            "section": "\033[38;5;141m\033[1m",
            "info": "\033[38;5;51m",
            "warn": "\033[38;5;220m",
            "ok": "\033[38;5;78m",
            "error": "\033[38;5;203m",
        }
        prefix = {"section": "◆", "info": "·", "warn": "!", "ok": "✓", "error": "✗"}[kind]
        print(f"{colors[kind]}{prefix} {text}\033[0m", file=sys.stderr)
    else:
        print(text, file=sys.stderr)


def _cli_gate(home, timeout_seconds: int = 900,
              conversation_id: str = "") -> ApprovalGate:
    from ..interface.autonomy import policy_for

    def decide(kind: str, title: str, body: str) -> Decision:
        print(f"\n[{kind}] {title}\n{body}\n")
        answer = input("Approve? [y/N] ").strip().lower()
        return Decision("approved" if answer in ("y", "yes") else "rejected")

    gate = ApprovalGate(
        decide,
        timeout_seconds=timeout_seconds,
        policy=policy_for(home),
    )
    # Pre-approval applies to CLI and scheduled runs too, not only to
    # the WebUI: the store is the same file the WebUI, WhatsApp, and
    # Slack surfaces write, keyed by the run's conversation id.
    try:
        from ..interface.preapproval import PreapprovalStore
        from ..interface.preapproval_gate import PreapprovalGate
        return PreapprovalGate(gate, PreapprovalStore(home), conversation_id)
    except Exception:
        return gate


def _scheduled_gate(sandboxed: bool = False) -> ApprovalGate:
    """Non-blocking gate for unattended scheduled runs.

    A daemon tick loop must never block on stdin: _cli_gate's
    input() either hangs the loop or dies on EOF in a headless
    daemon. This gate decides immediately, with no policy
    auto-approval and no prompt: email tools are approved (the
    email tool itself enforces owner-only delivery for scheduled
    runs, so the gate cannot leak mail to other recipients),
    sandboxed shell commands that pass the practice safety scan
    are approved, and everything else is rejected fail-closed
    and fast. A scheduled goal that needs install, publish, or
    live approval therefore fails with a clear rejection
    instead of hanging the tick loop — it never worked
    unattended before either, it just failed slowly.
    """
    def decide(kind: str, title: str, body: str) -> Decision:
        if kind == "live" and title in ("Extra tool email_send",
                                        "Extra tool email_make_draft"):
            return Decision("approved")
        if kind == "mandatory" and sandboxed:
            from ..interface.practice import _mandatory_safe_for_practice
            if _mandatory_safe_for_practice(body):
                return Decision("approved",
                                reason="scheduled shell command")
        return Decision("rejected")

    return ApprovalGate(decide, policy=None)


def _make_web_researcher(config, conversation_context: str = ""):
    """Snippet researcher from config, shared by interactive and
    scheduled runs. Returns title/snippet strings (no URLs — the
    deep researcher is the citation-carrying path)."""
    def _researcher(goal_text):
        try:
            from ..agentic.research_allow import DomainGrants, load_allowlist
            from ..capabilities.search import search as _s
            search_text = goal_text
            if conversation_context:
                search_text = (f"Conversation topic:\n{conversation_context}\n\n"
                               f"Current request:\n{goal_text}")
            _r = _s(search_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                    allowlist=load_allowlist(config.home, config.research_allowlist),
                    grants=DomainGrants(config.home))
            return [f"{x.get('title', '')}: {x.get('snippet', '')}" for x in _r.get("results", [])]
        except Exception:
            return []
    return _researcher


def _network_available(execution_backend, sandbox) -> bool:
    """True when shell steps can reach the network, for the loop's
    network-on announcement and the sandbox offline hint. A Docker
    backend reports its own flag; a bubblewrap backend is always
    offline; with no backend, shell runs through Sandbox, which is
    offline when bwrap is present and on the host network otherwise."""
    if execution_backend is not None:
        return bool(getattr(execution_backend, "allow_network", False))
    return False if getattr(sandbox, "bwrap", None) else True


def _run_workspace(config: Config, run_id: str) -> Path:
    """Create one timestamped workspace directory for a run."""
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    path = config.workdir / stamp
    path.mkdir(parents=True, exist_ok=False)
    return path


def _run_sandbox(config: Config, run_id: str) -> Sandbox:
    return Sandbox(
        _run_workspace(config, run_id), config.sandbox_timeout_seconds,
        mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
        fsize_mb=config.sandbox_fsize_mb,
        cpu_seconds=config.sandbox_cpu_seconds,
        backend=config.sandbox_backend,
        allow_network=config.sandbox_network,
        require_isolation=config.sandbox_require_isolation)




def _env_int(name, default):
    try:
        return max(1, int(os.environ.get(name, default)))
    except Exception:
        return default


def _deep_search_fns(config):
    """Search + fetch callables for deep research, from config.

    Shared by the deep_research plan tool (extractive, no model
    client) and the loop-side deep researcher (judge + synthesis
    via the run's client). Search returns result dicts with
    title/url/snippet, so citations survive — the researcher
    callable used by web_search returns strings only, no URLs.
    """
    from ..agentic.research_allow import DomainGrants, load_allowlist
    from ..capabilities.documents import read_document as _read_doc
    from ..capabilities.search import search as _search

    def _search_fn(query_text):
        result = _search(query_text, backend=config.search_backend,
                         tavily_key=config.tavily_api_key,
                         searxng_url=config.searxng_url,
                         allowlist=load_allowlist(config.home, config.research_allowlist),
                         grants=DomainGrants(config.home))
        return result.get("results", []) if isinstance(result, dict) else result

    def _fetch_fn(url):
        try:
            return _read_doc(str(url))
        except Exception as exc:
            return f"fetch failed: {type(exc).__name__}: {exc}"

    return _search_fn, _fetch_fn


def make_deep_researcher(config):
    """Loop-side deep researcher: (query, chat) -> report dict.

    chat is the run's model callable (loop._chat with phase
    "research"), so the judge and synthesis run at the run's
    thinking level — max in research mode. Rounds/sources come
    from the deep-research env params.
    """
    from ..agentic.deep_research import deep_research as _deep
    search_fn, fetch_fn = _deep_search_fns(config)

    def deep_researcher(query, chat):
        return _deep(str(query), search_fn, fetch_fn, chat=chat,
                     max_rounds=_env_int("AIDON_DEEP_RESEARCH_ROUNDS", 2),
                     max_sources=_env_int("AIDON_DEEP_RESEARCH_SOURCES", 5))

    return deep_researcher


def _make_governor(config):
    """One acquisition governor per run, from config caps.

    getattr defaults keep old configs working: a config
    without the acq_* fields still gets the starting policy.
    """
    from ..capabilities.acquisition_governor import AcquisitionGovernor
    return AcquisitionGovernor(
        max_tools=getattr(config, "acq_max_tools", 2),
        max_skills=getattr(config, "acq_max_skills", 2),
        max_mcp=getattr(config, "acq_max_mcp", 1),
        max_dependency_sets=getattr(config, "acq_max_dependency_sets", 2),
        max_repair_cycles=getattr(config, "acq_max_repair_cycles", 3),
        max_acquisition_seconds=getattr(config, "acq_max_seconds", 600.0))


def _goal_extra_tools(config, workdir, gate=None, researcher=None, question_answer=None, phrase=None, scheduled=False, model_chat=None, governor=None, optional_tools=None) -> dict:
    """Extra tools for goal runs, built from existing modules only.

    The loop approval-gates any extra tool whose name contains
    post, send, or publish before dispatch, and that gate is not
    weakened here: email send and social publish keep those words
    in their names, so they always go through the gate. Tools that
    write files (charts) are confined to the sandbox workdir.
    No Gmail MCP here: that direction is dropped by the owner,
    Gmail API send already exists via the email draft/sender path.
    The research/messaging/schedule tools below are the old
    default-tool set, rebuilt on the same modules the CLI already
    uses: web_search runs the run's researcher when one is given
    (otherwise it says so plainly), fetch_url and read_document
    go through the documents module's SSRF/allowlist guards,
    search_document uses the home DocStore, search_research_cache
    uses the home research cache, slack_send and whatsapp_send
    use the messaging senders (their "send" names keep them
    approval-gated by the loop, no second gate here), and
    list_schedule/cancel_schedule use the home Scheduler.
    Every one of these returns a readable string on failure and
    never raises out of the tool.
    """
    from pathlib import Path
    from ..capabilities.tools import ToolResult
    workdir = Path(workdir)
    tools: dict = {}

    def _free_path(path: Path) -> Path:
        """First non-existing sibling: stem, stem_2, stem_3, ..."""
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise ValueError(f"no free filename near {path}")

    def _confined(name: str) -> Path:
        relative = str(name).replace("\\", "/")
        # ``workdir/`` is a prompt label, not a real child directory.
        if relative == "workdir":
            relative = ""
        elif relative.startswith("workdir/"):
            relative = relative[len("workdir/"):]
        target = (workdir / relative).resolve()
        root = workdir.resolve()
        if target != root and root not in target.parents:
            raise ValueError(f"path outside workdir: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    try:
        from ..capabilities.charts import bar_chart, line_chart

        def _chart_data(data):
            if isinstance(data, dict):
                data = list(data.items())
            return [(str(label), float(value)) for label, value in data]

        def make_bar_chart(title, data, output="chart.svg"):
            return str(bar_chart(title, _chart_data(data), _confined(output)))

        def make_line_chart(title, data, output="chart.svg"):
            return str(line_chart(title, _chart_data(data), _confined(output)))

        def make_chart(title, data, output="chart.svg", kind="bar"):
            if str(kind).lower() in ("line", "line_chart"):
                return make_line_chart(title, data, output)
            return make_bar_chart(title, data, output)

        tools["make_bar_chart"] = make_bar_chart
        tools["make_line_chart"] = make_line_chart
        tools["make_chart"] = make_chart
    except Exception:
        pass
    try:
        from ..capabilities.docgen import build_ebook, generate_document, write_document_file
        def _generate_document(title="Aidon document", sections=None,
                               content="", body="", filename="", path="",
                               input_path="", output="", output_path="",
                               format="", overwrite=False, **kwargs):
            # Models have historically called the destination ``path`` or
            # requested ``format`` explicitly. Accept those aliases so a
            # valid document plan is not rejected by a naming mismatch.
            destination = str(filename or path or input_path or output or output_path or "")
            if sections is None:
                raw = content or body or kwargs.get("text", "")
                sections = [{"heading": "", "body": str(raw)}]
            elif isinstance(sections, str):
                sections = [{"heading": "", "body": sections}]
            if not destination:
                # Keep generated artifacts discoverable even when the model
                # omits a filename: derive a safe contextual stem and add
                # the run timestamp. The containing directory is already
                # the timestamped workspace for this run.
                stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(title)).strip("_.")
                stem = stem[:80] or "aidon_document"
                destination = f"{stem}_{Path(workdir).name}"
                if format:
                    destination += "." + str(format).lstrip(".")
                else:
                    destination += ".md"
            if destination:
                if format and "." not in Path(destination).name:
                    destination += "." + str(format).lstrip(".")
                target = _confined(destination)
                if target.exists() and not overwrite:
                    # Same dead-end as write_file: a FILE_EXISTS string
                    # is marked as a passed step, then repair skips the
                    # deliverable forever while nothing was written.
                    # Rename instead; the original is untouched.
                    renamed_from = destination
                    target = _free_path(target)
                else:
                    renamed_from = ""
                if target.exists() and overwrite:
                    ok, why = _approval(
                        "live", "Overwrite existing document",
                        f"Replace the existing file {target}?",
                        force_ask=True)
                    if not ok:
                        return f"overwrite rejected for {destination}: {why}"
                result = write_document_file(title, sections, target)
                if renamed_from:
                    result = (f"wrote {target} (renamed: requested "
                              f"{renamed_from} already existed)")
                return ToolResult(result, path=target, kind="document")
            return generate_document(title, sections)
        tools["generate_document"] = _generate_document
        tools["build_ebook"] = build_ebook
    except Exception:
        pass
    try:
        from ..capabilities.github_tools import fetch_file, search_repos

        def github_search(query, token=""):
            return search_repos(query, token=token or getattr(config, "github_token", "") or "")

        def github_fetch(owner, repo, path, ref="main", token=""):
            return fetch_file(owner, repo, path, ref=ref,
                              token=token or getattr(config, "github_token", "") or "")

        tools["github_search"] = github_search
        tools["github_fetch"] = github_fetch
    except Exception:
        pass
    try:
        from ..social.social import publish_instagram, publish_threads

        def threads_publish(text):
            return publish_threads(
                text, getattr(config, "threads_account_id", ""),
                getattr(config, "threads_access_token", ""),
                approve=lambda kind, body: True)

        def instagram_publish(image_url, caption=""):
            return publish_instagram(
                image_url, caption, getattr(config, "ig_user_id", ""),
                getattr(config, "ig_access_token", ""),
                approve=lambda kind, body: True)

        tools["threads_publish"] = threads_publish
        tools["instagram_publish"] = instagram_publish
    except Exception:
        pass
    try:
        from ..social.youtube_upload import (
            build_metadata, refresh_access_token, upload_video)

        def youtube_upload(video, title, description="",
                           privacy="", category=""):
            # Named youtube_upload, not youtube_publish, on
            # purpose: the loop auto-gates post/send/publish
            # names with a generic card, and this tool carries
            # its own richer approval below (title, description,
            # visibility, file, size). One approval, full details.
            source = _confined(video)
            if not source.is_file():
                return f"upload refused: video file not found: {video}"
            meta = build_metadata(
                title, description,
                privacy or getattr(config, "youtube_privacy_status", "private") or "private",
                category or getattr(config, "youtube_category_id", "22") or "22")
            ok, why = _approval(
                "live", "Publish to YouTube",
                f"Upload {source.name} ({source.stat().st_size} bytes) "
                f"to YouTube?\n"
                f"Title: {meta['snippet']['title']}\n"
                f"Description: {meta['snippet']['description']}\n"
                f"Visibility: {meta['status']['privacyStatus']}\n"
                f"Category: {meta['snippet']['categoryId']}",
                force_ask=True)
            if not ok:
                return f"upload rejected: {why}"
            try:
                token = refresh_access_token(
                    getattr(config, "youtube_client_id", ""),
                    getattr(config, "youtube_client_secret", ""),
                    getattr(config, "youtube_refresh_token", ""))
                result = upload_video(source, meta, token)
            except Exception as exc:
                return f"upload failed: {type(exc).__name__}: {exc}"
            video_id = result.get("id", "") if isinstance(result, dict) else ""
            if not video_id:
                return "upload failed: no video id in response"
            return (f"YOUTUBE_UPLOADED {video_id} "
                    f"https://www.youtube.com/watch?v={video_id}")

        tools["youtube_upload"] = youtube_upload
    except Exception:
        pass
    try:
        from ..capabilities.voice import transcribe, tts

        def tts_speak(text, voice_id=""):
            result = tts(text, getattr(config, "fish_api_key", ""),
                         voice_id or getattr(config, "tts_voice_id", ""))
            return str(result.get("ok", False))

        def transcribe_audio(audio, key=""):
            # The old tool accepted a local audio path; the WebUI uses raw
            # bytes. Support both forms so migrated goals keep working.
            if isinstance(audio, (str, Path)):
                path = _confined(str(audio))
                if not path.is_file():
                    return f"TRANSCRIBE_FAILED: no such file: {audio}"
                audio = path.read_bytes()
            result = transcribe(bytes(audio), key or getattr(config, "transcribe_api_key", ""))
            return result.get("text", "")

        tools["tts_speak"] = tts_speak
        tools["transcribe_audio"] = transcribe_audio
    except Exception:
        pass
    try:
        from ..capabilities.email import make_draft, send_draft

        def email_make_draft(to, subject, body):
            return make_draft(config.home, to, subject, body)

        def email_send(draft, owner_email=""):
            # The tool used to pass sender=None, so it could only
            # keep the draft and always reported "no sender". With
            # Gmail keys present it now sends for real. Safety is
            # layered, not repeated: interactive runs already pass
            # the loop's send-gate (a human card) before dispatch,
            # so their approve callback stays permissive; scheduled
            # runs pass the ScheduledGate (email tools only) and
            # then this callback refuses every non-owner recipient
            # — send_draft skips the callback entirely for owner
            # sends, so owner mail is the only mail a scheduled
            # run can ever deliver.
            sender = None
            if (os.environ.get("GMAIL_CLIENT_ID", "")
                    and os.environ.get("GMAIL_CLIENT_SECRET", "")
                    and os.environ.get("GMAIL_REFRESH_TOKEN", "")):
                from ..capabilities.gmail import gmail_sender as _gs
                _real = _gs(os.environ["GMAIL_CLIENT_ID"],
                            os.environ["GMAIL_CLIENT_SECRET"],
                            os.environ["GMAIL_REFRESH_TOKEN"])

                def sender(to, subject, body):
                    result = _real(to, subject, body)
                    if isinstance(result, dict) and result.get("id") \
                            and "ok" not in result:
                        result = dict(result)
                        result["ok"] = True
                    return result
            approve = (lambda kind, body: False) if scheduled \
                else (lambda kind, body: True)
            return send_draft(config.home, draft,
                              owner_email or getattr(config, "owner_email", ""),
                              approve=approve, sender=sender)

        tools["email_make_draft"] = email_make_draft
        tools["email_send"] = email_send
    except Exception:
        pass
    try:
        from ..capabilities.office import (  # type: ignore
            make_docx, make_latex, make_slides, make_spreadsheet)

        # Office builders take an output path: confine it to the
        # workdir like the chart builders, so a model-supplied
        # path cannot escape the sandbox. Each wrapper confines
        # only its own output argument, by position.
        def _office_spreadsheet(sheets, output):
            return make_spreadsheet(sheets, str(_confined(output)))

        def _office_docx(title, blocks, output):
            return make_docx(title, blocks, str(_confined(output)))

        def _office_slides(title, slides, output):
            return make_slides(title, slides, str(_confined(output)))

        def _office_latex(title, sections, output, author="Aidon"):
            return make_latex(title, sections, str(_confined(output)), author=author)

        tools["make_spreadsheet"] = _office_spreadsheet
        tools["make_docx"] = _office_docx
        tools["make_slides"] = _office_slides
        tools["make_latex"] = _office_latex
    except Exception:
        pass  # office.py absent; the guarded import skips it.
    try:
        from ..capabilities.audio import record_system_audio
        def record_audio(seconds, output="recording.wav"):
            return record_system_audio(int(seconds), str(_confined(output)))
        tools["record_system_audio"] = record_audio
    except Exception:
        pass
    try:
        from ..capabilities.webcam import record_webcam_clip

        def record_webcam(seconds, output="clip.mp4", device=""):
            # Kind "record", force_ask: camera activation is
            # never pre-approved and never silent. The card
            # names the duration and the output file first.
            ok, why = _approval(
                "record", "Record webcam video",
                f"Activate the camera and record {int(seconds)} "
                f"seconds of video to {output}?",
                force_ask=True)
            if not ok:
                return f"webcam recording rejected: {why}"
            return record_webcam_clip(int(seconds), str(_confined(output)),
                                      device or "/dev/video0")

        tools["record_webcam"] = record_webcam
    except Exception:
        pass

    # Compatibility names from Aidon.
    if "threads_publish" in tools:
        tools["threads_post"] = tools["threads_publish"]
    if "instagram_publish" in tools:
        tools["instagram_post"] = tools["instagram_publish"]

    def read_file(name="", path="", input_path="", file=""):
        name = str(name or path or input_path or file)
        path = _confined(name)
        if not path.is_file():
            return f"file not found: {name}"
        return path.read_text(encoding="utf-8", errors="replace")

    def write_file(name="", text="", input_path="", path="", file="",
                   content="", overwrite=False):
        name = str(name or input_path or path or file)
        path = _confined(name)
        if path.exists() and not overwrite:
            # Never dead-end the run with a FILE_EXISTS string: the
            # executor marks a returned string as a passed step, then
            # repair skips that deliverable forever while the file
            # was never written. Rename instead; the original is
            # untouched, so no approval is needed for a rename.
            path = _free_path(path)
            renamed_from = name
        else:
            renamed_from = ""
        if path.exists() and overwrite:
            ok, why = _approval("live", "Overwrite existing file",
                                f"Replace the existing file {path}?",
                                force_ask=True)
            if not ok:
                return f"overwrite rejected for {name}: {why}"
        path.write_text(str(text if text != "" else content), encoding="utf-8")
        note = f" (renamed: requested {renamed_from} already existed)" if renamed_from else ""
        return ToolResult(f"wrote {path}{note}", path=path, kind="file")

    tools["read_file"] = read_file
    tools["write_file"] = write_file

    try:
        from urllib.parse import parse_qs, urlparse
        from ..social.youtube import youtube_learn as _youtube_learn

        def youtube_learn(query_or_url, max_chars=12000, visual=False):
            value = str(query_or_url).strip()
            video_id = ""
            if "youtube.com" in value or "youtu.be" in value:
                parsed = urlparse(value)
                video_id = parse_qs(parsed.query).get("v", [""])[0]
                if not video_id and parsed.hostname == "youtu.be":
                    video_id = parsed.path.strip("/").split("/", 1)[0]
            result = _youtube_learn(
                video_id=video_id, query="" if video_id else value,
                api_key=getattr(config, "youtube_api_key", ""),
                visual_request=bool(visual))
            return json.dumps(result, ensure_ascii=False)[:int(max_chars)]

        tools["youtube_learn"] = youtube_learn
    except Exception:
        pass

    def _approval(kind, title, body, action_class=None, force_ask=False):
        if gate is None:
            return False, "no approval gate configured"
        try:
            from ..interface.phrasing import approval_lead_in
            lead = None
            if phrase is not None:
                try:
                    lead = phrase("approval")
                except Exception:
                    lead = None
            if not lead:
                lead = approval_lead_in()
            decision = gate.ask(
                kind, title, f"{lead}\n\n{body}",
                action_class=action_class,
                force_ask=force_ask,
            )
            return decision.verdict in ("approved", "auto_approved"), decision.verdict
        except Exception as exc:
            return False, f"approval failed: {exc}"

    def _acq(kind):
        """Governor context for one acquisition, or a no-op.

        None governor means ungoverned (tests, legacy paths):
        production call sites always pass one.
        """
        if governor is None:
            from contextlib import nullcontext
            return nullcontext()
        return governor.timed(kind)

    def _computer_secrets() -> dict:
        """Secrets the owner authorized for computer use.

        AIDON_COMPUTER_SECRETS lists env names (kept in .env, which
        Config already loads into os.environ); only listed names
        resolve. The model names a handle, never a value, and an
        unlisted handle is refused inside run_computer_use.
        """
        names = os.environ.get("AIDON_COMPUTER_SECRETS", "")
        secrets = {}
        for name in names.split(","):
            name = name.strip()
            if name and name in os.environ:
                secrets[name] = os.environ[name]
        return secrets

    def _guided_computer_task(driver, approve_fn):
        """computer_use_task closure over one driver + approval path.

        Only wired when a model chat is available: the guided
        loop needs vision, and without it the tool would act
        blind. Scheduled runs never pass model_chat, so they
        never get this tool.
        """
        def computer_use_task(description, max_actions=10,
                              screenshot_dir="computer-screenshots"):
            from ..capabilities.computer_use import run_guided_task
            target = _confined(screenshot_dir)
            result = run_guided_task(
                description, model_chat, approve=approve_fn,
                driver=driver, screenshot_dir=target,
                max_actions=max_actions, secrets=_computer_secrets())
            return json.dumps(result, ensure_ascii=False)
        return computer_use_task

    # The WebUI goal runner receives this dictionary as its extra toolset.
    # Keep desktop control here as well as in the CLI tool builder; otherwise
    # the status panel can say x11 while the planner cannot select
    # computer_use for a WebUI goal.
    # Prefer explicit configuration, but also discover an installed X11
    # driver automatically. Discovery is read-only; every actual desktop
    # action remains approval-gated below.
    _computer_requested = getattr(config, "computer_driver", "") == "x11"
    if not getattr(config, "kernel_mode", False) and (_computer_requested or
                                                       os.environ.get("DISPLAY")):
        try:
            from ..capabilities.computer_use import run_computer_use
            from ..capabilities.x11_driver import X11Driver, x11_available
            if not _computer_requested and not x11_available():
                raise RuntimeError("no discoverable X11 driver")
            desktop_driver = X11Driver()

            def _desktop_approve(kind, detail):
                ok, _ = _approval(
                    "live_execute", "Approve desktop action",
                    f"Run X11 computer action {kind}: {detail}",
                    force_ask=True,
                )
                return ok

            def computer_use(actions, screenshot_dir="computer-screenshots"):
                target = _confined(screenshot_dir)
                result = run_computer_use(
                    actions, approve=_desktop_approve, driver=desktop_driver,
                    screenshot_dir=target, secrets=_computer_secrets())
                return json.dumps(result, ensure_ascii=False)

            tools["computer_use"] = computer_use
            if model_chat is not None:
                tools["computer_use_task"] = _guided_computer_task(
                    desktop_driver, _desktop_approve)
        except Exception:
            # A missing xdotool or DISPLAY must not make other tools vanish.
            pass

    # Recording host audio is a sensitive side effect. Keep the old
    # mandatory approval boundary even though the recorder itself lives in
    # capabilities/audio.py.
    if "record_system_audio" in tools:
        _record_system_audio = tools["record_system_audio"]

        def approved_record_system_audio(seconds, output="recording.wav"):
            target = _confined(output)
            ok, why = _approval(
                "record", "Record system audio",
                f"Capture {seconds} seconds of host audio to {target}",
                force_ask=True,
            )
            if not ok:
                return f"recording rejected: {why}"
            try:
                return _record_system_audio(int(seconds), str(target))
            except Exception as exc:
                return f"recording failed: {type(exc).__name__}: {exc}"

        tools["record_system_audio"] = approved_record_system_audio

    try:
        from ..capabilities.install import (apt_install as _apt_install,
            install_python_package as _install_python_package,
            validate_apt, validate_pypi, uv_venv as _uv_venv)

        def uv_venv(path=""):
            target = _confined(path or ".venv")
            ok, why = _approval("install", "Create Python virtual environment",
                                f"Run on host: uv venv {target}",
                                force_ask=True,
                          )
            if not ok:
                return f"install rejected: {why}"
            success, output = _uv_venv(target)
            return f"VENV_CREATED {target}" if success else f"VENV_FAILED: {output}"

        def install_python_package(packages, venv=""):
            names = [str(packages)] if isinstance(packages, str) else [str(x) for x in packages]
            names = [x.strip() for x in names for x in x.split(",") if x.strip()]
            error = validate_pypi(names)
            if error:
                return f"INSTALL_REFUSED: {error}"
            target = _confined(venv or ".venv")
            ok, why = _approval("install", "Install Python packages",
                                f"Run on host: uv pip install --python {target}/bin/python {' '.join(names)}",
                                force_ask=True,
                            )
            if not ok:
                return f"install rejected: {why}"
            with _acq("dependency"):
                success, output = _install_python_package(names, target)
            return f"INSTALLED {' '.join(names)} -> {target}" if success else f"INSTALL_FAILED: {output}"

        def apt_install(packages):
            names = [str(packages)] if isinstance(packages, str) else [str(x) for x in packages]
            names = [x.strip() for x in names for x in x.split(",") if x.strip()]
            error = validate_apt(names)
            if error:
                return f"INSTALL_REFUSED: {error}"
            ok, why = _approval("install", "Install system packages",
                                f"Run on host: sudo apt-get install -y {' '.join(names)}",
                                force_ask=True,
                            )
            if not ok:
                return f"install rejected: {why}"
            with _acq("dependency"):
                success, output = _apt_install(names)
            return f"APT_INSTALLED {' '.join(names)}" if success else f"APT_FAILED: {output}"

        tools.update(uv_venv=uv_venv, install_python_package=install_python_package,
                     apt_install=apt_install)
    except Exception:
        pass
    try:
        from ..agentic.file_versions import write_versioned
        def write_versioned_tool(name, text, expected_hash=""):
            result = write_versioned(workdir, name, text, expected_hash=expected_hash or None)
            return str(result)
        tools["write_versioned"] = write_versioned_tool
    except Exception:
        pass

    try:
        from ..capabilities.sam import sam_segment as _sam_segment

        def sam_segment(image_paths, prompt):
            paths = [image_paths] if isinstance(image_paths, str) else list(image_paths)
            confined = [str(_confined(path)) for path in paths]
            result = _sam_segment(
                confined, str(prompt),
                getattr(config, "sam_api_key", "") or getattr(config, "api_key", ""),
                getattr(config, "sam_base_url", "https://api.meta.ai/v1"),
                getattr(config, "sam_model", "meta-sam"))
            return json.dumps(result, ensure_ascii=False)

        tools["sam_segment"] = sam_segment
    except Exception:
        pass

    # Style/photo edits are host-side: the image API needs the key, the
    # network, and a home-wide output folder, none of which the sandbox
    # exposes. Credits are spent, so every call is approval-gated.
    try:
        from ..capabilities.image_gen import edit_image_files, save_image as _save_image

        def _image_reference(reference) -> Path:
            raw = str(reference or "").strip()
            if not raw:
                raise ValueError("an image path is required")
            path = Path(raw).expanduser()
            uploads = config.home / "library" / "uploads"
            if not path.is_absolute():
                candidates = [workdir / raw, uploads / raw, config.home / raw]
                path = next((p for p in candidates if p.is_file()), candidates[0])
                if not path.is_file() and "/" not in raw.replace("\\", "/"):
                    hits = sorted(uploads.glob(f"*-{raw}"), key=lambda p: p.stat().st_mtime)
                    if hits:
                        path = hits[-1]
            path = path.resolve()
            home_root = config.home.resolve()
            if path != home_root and home_root not in path.parents:
                raise ValueError(f"image path outside the Aidon home: {raw}")
            if not path.is_file():
                raise ValueError(f"image not found: {raw}")
            return path

        def _image_destination(output) -> Path:
            images_dir = (config.home / "images").resolve()
            raw = str(output or "").strip() or (
                f"aidon-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
            relative = raw.replace("\\", "/")
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = (config.home / relative) if relative.startswith("images/") \
                    else (images_dir / relative)
            target = target.resolve()
            home_root = config.home.resolve()
            if target != home_root and home_root not in target.parents:
                raise ValueError("output path must stay inside the Aidon home")
            if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                target = target.with_suffix(".png")
            return target

        def edit_image(prompt, image="", images=None, output="", size="1024x1024"):
            """Restyle or compose a photo. Saves under <home>/images by default."""
            refs = []
            if images is not None:
                refs.extend([images] if isinstance(images, str) else list(images))
            if image:
                refs.insert(0, image)
            if not str(prompt or "").strip():
                return "IMAGE_EDIT_FAILED: a prompt is required"
            if not refs or len(refs) > 10:
                return "IMAGE_EDIT_FAILED: pass 1 to 10 reference image paths"
            try:
                paths = [_image_reference(item) for item in refs]
                target = _image_destination(output)
            except Exception as exc:
                return f"IMAGE_EDIT_FAILED: {exc}"
            key = str(getattr(config, "api_key", "") or "")
            endpoint = str(getattr(config, "api_base", "") or "")
            if not key or not endpoint:
                return ("IMAGE_EDIT_FAILED: image tools need AIDON_API_KEY and "
                        "AIDON_API_ENDPOINT in .env")
            ok, why = _approval(
                "live", "Edit image with Muse Image",
                "Send {n} image(s) to {endpoint} and spend image credits.\n"
                "Prompt: {prompt}\nSave to: {target}".format(
                    n=len(paths), endpoint=endpoint,
                    prompt=str(prompt)[:500], target=target),
                force_ask=True)
            if not ok:
                return f"image edit rejected: {why}"
            try:
                data = edit_image_files(
                    str(prompt), key, endpoint, [str(p) for p in paths],
                    size=str(size or "1024x1024"),
                    model=getattr(config, "image_model", "muse-image-1.0"))
                saved = _save_image(data, target)
                return f"IMAGE_SAVED: {saved}"
            except Exception as exc:
                return f"IMAGE_EDIT_FAILED: {type(exc).__name__}: {exc}"

        tools["edit_image"] = edit_image
    except Exception:
        pass

    def notify(title, body="", recipient="owner"):
        from ..capabilities.outbox import Outbox
        item = Outbox(config.home).queue(str(recipient), str(title), str(body))
        return f"notification queued: {item.get('id')}"

    tools["notify"] = notify

    # Desktop control is opt-in and never a raw shell. Kernel mode keeps it
    # unavailable; the demo must explicitly run with AIDON_KERNEL=0.
    if getattr(config, "computer_driver", "") == "x11" and not getattr(config, "kernel_mode", False):
        try:
            from ..capabilities.computer_use import run_computer_use
            from ..capabilities.x11_driver import X11Driver

            _desktop_driver = X11Driver()

            def _desktop_approve2(kind, detail):
                ok, why = _approval(
                    "live_execute", "Approve desktop action",
                    f"Run X11 computer action {kind}: {detail}",
                    force_ask=True,
                )
                return ok

            def computer_use(actions, screenshot_dir="computer-screenshots"):
                target = _confined(screenshot_dir)
                result = run_computer_use(
                    actions, approve=_desktop_approve2, driver=_desktop_driver,
                    screenshot_dir=target, secrets=_computer_secrets(),
                )
                return json.dumps(result, ensure_ascii=False)

            tools["computer_use"] = computer_use
            if model_chat is not None:
                tools["computer_use_task"] = _guided_computer_task(
                    _desktop_driver, _desktop_approve2)
        except Exception:
            # A missing display or driver leaves the capability unavailable.
            pass

    # MCP client tools from Aidon. Connections live for this goal run;
    # every new stdio connection is approval-gated before third-party code
    # is started. Server tools are exposed with a namespaced name so they
    # cannot collide with Aidon's own tools.
    mcp_connections = {}

    def mcp_connect(name, transport="stdio", command="", args=None,
                    env=None, url="", token=""):
        import shlex
        server_name = str(name).strip()
        if not server_name:
            return "MCP_REFUSED: server name is required"
        if server_name in mcp_connections:
            return f"MCP_ALREADY_CONNECTED {server_name!r}"
        transport = str(transport or "stdio").lower()
        if transport == "stdio":
            command_parts = shlex.split(str(command)) + [str(x) for x in (args or [])]
            if not command_parts:
                return "MCP_REFUSED: command is required for stdio transport"
            description = " ".join(command_parts)
        elif transport in ("http", "streamable-http"):
            if not url:
                return "MCP_REFUSED: url is required for HTTP transport"
            description = str(url)
        else:
            return f"MCP_REFUSED: unsupported transport {transport!r}"
        ok, why = _approval("install", "Connect MCP server",
                            f"Start/connect to MCP server {server_name!r}: {description}",
                            force_ask=True,
                        )
        if not ok:
            return f"MCP_REFUSED: {why}"
        with _acq("mcp"):
            try:
                if transport == "stdio":
                    from ..interface.mcp_client import McpClient
                    client = McpClient(command_parts, env=dict(env or {}))
                    client.start()
                else:
                    from ..interface.mcp_http import McpHttpClient
                    client = McpHttpClient(str(url), token=str(token or "") or None)
                    client.initialize()
                offered = client.list_tools()
                if not offered:
                    client.close()
                    return f"MCP_REFUSED: {server_name!r} offered no tools"
                mcp_connections[server_name] = client
                registered = []
                for info in offered:
                    remote_name = str(info.get("name", "")).strip()
                    if not remote_name or not remote_name.replace("_", "").isalnum():
                        continue
                    local_name = f"mcp_{server_name}_{remote_name}"

                    def call_remote(_args=None, _client=client, _remote=remote_name):
                        return json.dumps(_client.call_tool(_remote, _args or {}), ensure_ascii=False)

                    tools[local_name] = call_remote
                    registered.append(local_name)
                try:
                    from ..capabilities.capability_registry import CapabilityRegistry
                    CapabilityRegistry(config.home).record_use(server_name)
                except Exception:
                    pass
                return f"MCP_CONNECTED {server_name!r}: {', '.join(registered) or 'no safe tools'}"
            except Exception as exc:
                try:
                    client.close()
                except Exception:
                    pass
                return f"MCP_REFUSED: {type(exc).__name__}: {exc}"

    def mcp_disconnect(name):
        server_name = str(name).strip()
        client = mcp_connections.pop(server_name, None)
        if client is None:
            return f"MCP: no open connection named {server_name!r}"
        client.close()
        prefix = f"mcp_{server_name}_"
        for tool_name in list(tools):
            if tool_name.startswith(prefix):
                tools.pop(tool_name, None)
        return f"MCP_DISCONNECTED {server_name!r}"

    tools["mcp_connect"] = mcp_connect
    tools["mcp_disconnect"] = mcp_disconnect

    try:
        from ..capabilities.plugins import PluginRegistry

        def adopt_tool_plugin(path):
            source = _confined(path)
            if not source.is_dir():
                return f"adoption refused: plugin directory not found: {path}"
            registry = PluginRegistry(config.home)
            with _acq("tool"):
                approved, _findings, message = registry.adopt(source, gate=gate)
            return message if approved else f"adoption refused: {message}"

        tools["adopt_tool_plugin"] = adopt_tool_plugin
    except Exception:
        pass

    def ask_user(question):
        # Interview / clarifying questions are chat turns, not approval
        # cards. When the surface provides a question channel (the
        # WebUI waiter), block until the owner answers; questions
        # carry no 180s clock. Without a channel, say so instead of
        # echoing the question back as if it were answered.
        if question_answer is not None:
            try:
                return str(question_answer(question))
            except Exception as exc:
                return f"question failed: {exc}"
        if gate is None:
            return "no gate configured"
        return ("QUESTION_NOT_DELIVERED: no channel exists to ask the user "
                "this question; proceed with your best judgment and note "
                "the assumption.")

    def declare_blocked(reason):
        # Single shape source: direction.blocked_note is the dict form,
        # the tool keeps the exact "[blocked] {reason}" string it always
        # returned, so no caller behaviour changes.
        from ..agentic.direction import blocked_note
        return f"[blocked] {blocked_note(reason)['reason']}"

    def schedule_job(name, kind="", schedule="", goal="", until="", tz="", time_limit=0):
        from ..interface.triggers import Scheduler, SchedulerJob
        scheduler = Scheduler(config.home)
        try:
            scheduler.add_job(SchedulerJob(name=name, schedule=schedule,
                                           goal=goal, kind=kind or "goal",
                                           conversation_id="",
                                           until=until, tz=tz,
                                           time_limit=time_limit))
        except ValueError as exc:
            return f"schedule failed: {exc}"
        detail = f"scheduled {name} ({kind}) {schedule}: {goal}"
        if until:
            detail += f" until {until}"
        if tz:
            detail += f" ({tz})"
        if time_limit:
            detail += f" time limit {time_limit}s"
        return detail

    def web_search(query):
        try:
            if researcher is None:
                return "web search not configured: no researcher available for this run"
            results = researcher(str(query))
            if not results:
                return f"web search for {query!r} returned no results"
            return "\n".join(str(r) for r in results)
        except Exception as exc:
            return f"web search failed: {type(exc).__name__}: {exc}"

    def fetch_url(url):
        try:
            from ..capabilities.documents import read_document as _read_doc
            return _read_doc(str(url))
        except Exception as exc:
            return f"fetch failed: {type(exc).__name__}: {exc}"

    def read_document(path_or_url):
        try:
            from ..capabilities.documents import read_document as _read_doc
            return _read_doc(str(path_or_url))
        except Exception as exc:
            return f"read failed: {type(exc).__name__}: {exc}"

    def search_document(query):
        try:
            from ..memory.docstore import DocStore
            hits = DocStore(home=config.home).search_all(str(query))
            if not hits:
                return f"no documents matched {query!r}"
            lines = []
            for hit in hits[:6]:
                text = str(hit.get("text", hit.get("chunk", "")))[:300]
                lines.append(f"{hit.get('collection', '')}: {text}")
            return "\n".join(lines)
        except Exception as exc:
            return f"document search failed: {type(exc).__name__}: {exc}"

    def search_research_cache(query):
        try:
            from ..agentic.research_cache import ResearchCache
            hits = ResearchCache(config.home).search_research_cache(str(query))
            if not hits:
                return f"no cached research matched {query!r}"
            lines = []
            for hit in hits[:6]:
                lines.append(f"{hit.get('url', '')}: " + " | ".join(hit.get("lines", [])[:3]))
            return "\n".join(lines)
        except Exception as exc:
            return f"research cache search failed: {type(exc).__name__}: {exc}"

    def _msg_config():
        # Config fields (loaded from .env) win; environment fills gaps.
        # MessagingConfig.from_env alone would miss tokens that live
        # only in the Config object, not in os.environ.
        from ..interface.messaging import MessagingConfig
        env_cfg = MessagingConfig.from_env()
        return MessagingConfig(
            whatsapp_token=config.whatsapp_token or env_cfg.whatsapp_token,
            whatsapp_phone_id=config.whatsapp_phone_id or env_cfg.whatsapp_phone_id,
            whatsapp_verify_token=config.whatsapp_verify_token or env_cfg.whatsapp_verify_token,
            whatsapp_app_secret=config.whatsapp_app_secret or env_cfg.whatsapp_app_secret,
            whatsapp_owner=config.whatsapp_owner or env_cfg.whatsapp_owner,
            slack_bot_token=config.slack_bot_token or env_cfg.slack_bot_token,
            slack_signing_secret=config.slack_signing_secret or env_cfg.slack_signing_secret)

    def slack_send(channel, text):
        try:
            from ..interface.messaging import send_slack
            result = send_slack(_msg_config(), str(channel), str(text))
            if result.get("ok"):
                return f"slack message sent to {channel} (ts {result.get('ts', '')})"
            return f"slack send failed: {result.get('error', 'unknown error')}"
        except Exception as exc:
            return f"slack send failed: {type(exc).__name__}: {exc}"

    def whatsapp_send(to, text):
        try:
            from ..interface.messaging import send_whatsapp
            result = send_whatsapp(_msg_config(), str(to), str(text))
            if result.get("ok"):
                return f"whatsapp message sent to {to} (id {result.get('message_id', '')})"
            return f"whatsapp send failed: {result.get('error', 'unknown error')}"
        except Exception as exc:
            return f"whatsapp send failed: {type(exc).__name__}: {exc}"

    def slack_send_file(channel, file, title=""):
        """Upload a workdir file to a Slack channel (bytes, not a localhost link)."""
        try:
            from ..interface.messaging import send_slack_file
            target = _confined(str(file))
            if not target.is_file():
                return f"slack send file failed: not found in workdir: {file}"
            result = send_slack_file(_msg_config(), str(channel), target, str(title))
            if result.get("ok"):
                return f"slack file sent to {channel} (file {result.get('file_id', '')})"
            return f"slack send file failed: {result.get('error', 'unknown error')}"
        except Exception as exc:
            return f"slack send file failed: {type(exc).__name__}: {exc}"

    def whatsapp_send_media(to, file, caption=""):
        """Send a workdir file over WhatsApp via the media API (bytes, not a link)."""
        try:
            from ..interface.messaging import send_whatsapp_media
            target = _confined(str(file))
            if not target.is_file():
                return f"whatsapp send media failed: not found in workdir: {file}"
            result = send_whatsapp_media(_msg_config(), str(to), target, str(caption))
            if result.get("ok"):
                return f"whatsapp media sent to {to} (id {result.get('message_id', '')})"
            return f"whatsapp send media failed: {result.get('error', 'unknown error')}"
        except Exception as exc:
            return f"whatsapp send media failed: {type(exc).__name__}: {exc}"

    def list_schedule():
        try:
            from ..interface.triggers import Scheduler
            jobs = Scheduler(config.home).jobs
            if not jobs:
                return "(no scheduled jobs)"
            return "\n".join(
                f"{job.name}: {job.goal} schedule={job.schedule} chat={job.conversation_id}"
                for job in jobs)
        except Exception as exc:
            return f"schedule list failed: {type(exc).__name__}: {exc}"

    def cancel_schedule(name):
        try:
            from ..interface.triggers import Scheduler
            scheduler = Scheduler(config.home)
            before = len(scheduler.jobs)
            scheduler.jobs = [job for job in scheduler.jobs if job.name != str(name)]
            if len(scheduler.jobs) == before:
                return f"no job named {name}"
            scheduler.save()
            return f"cancelled {name}"
        except Exception as exc:
            return f"schedule cancel failed: {type(exc).__name__}: {exc}"

    tools["ask_user"] = ask_user
    tools["declare_blocked"] = declare_blocked
    tools["schedule_job"] = schedule_job
    tools["web_search"] = web_search
    tools["fetch_url"] = fetch_url

    def fetch_feed(url, state_file=""):
        # Native feed reading with new-item dedupe. The state file
        # defaults to .rss_seen.json in the run workdir; a given
        # name must stay inside the workdir, like other file tools.
        # Scheduled runs share the serve workdir, so their state
        # persists across ticks; interactive runs get a fresh state
        # per run (every current item reads as new) unless a shared
        # state file is named.
        try:
            from ..capabilities.rss import fetch_feed as _fetch_feed
            from pathlib import Path as _FeedPath
            root = _FeedPath(workdir).resolve()
            if state_file:
                candidate = (root / str(state_file)).resolve()
                if not candidate.is_relative_to(root):
                    return "feed failed: state file outside workdir"
                state_path = candidate
            else:
                state_path = root / ".rss_seen.json"
            return _fetch_feed(str(url), state_path)
        except Exception as exc:
            return f"feed failed: {type(exc).__name__}: {exc}"

    tools["fetch_feed"] = fetch_feed
    tools["read_document"] = read_document

    def deep_research_tool(query):
        # Multi-round search + fetch + condense with citations.
        # The plan tool runs without a model client, so the judge
        # is heuristic (top sources) and the summary extractive;
        # the module's judge/synthesis activate when a caller
        # passes a client (the loop-side researcher does).
        try:
            from ..agentic.deep_research import deep_research as _deep
            search_fn, fetch_fn = _deep_search_fns(config)
            out = _deep(str(query), search_fn, fetch_fn, chat=None,
                        max_rounds=_env_int("AIDON_DEEP_RESEARCH_ROUNDS", 2),
                        max_sources=_env_int("AIDON_DEEP_RESEARCH_SOURCES", 5))
            head = (f"Deep research: {out['query']} "
                    f"({out['rounds']} round(s), {len(out['evidence'])} source(s))")
            return head + "\n\n" + out["summary"]
        except Exception as exc:
            return f"deep research failed: {type(exc).__name__}: {exc}"

    tools["deep_research"] = deep_research_tool
    tools["search_document"] = search_document
    tools["search_research_cache"] = search_research_cache
    tools["slack_send"] = slack_send
    tools["whatsapp_send"] = whatsapp_send
    tools["slack_send_file"] = slack_send_file
    tools["whatsapp_send_media"] = whatsapp_send_media
    tools["list_schedule"] = list_schedule
    tools["cancel_schedule"] = cancel_schedule
    if optional_tools is None:
        # Legacy callers (tests, tools without the field) keep the
        # full set; production Config always sets the field, whose
        # default is False (core only).
        optional_tools = bool(getattr(config, "optional_tools", True))
    if not optional_tools:
        from ..agentic.kernel import CORE_LADDER_TOOLS
        tools = {k: v for k, v in tools.items()
                 if k in CORE_LADDER_TOOLS or k in ("read_file", "write_file")}
    return tools


def _make_memory(config: Config):
    """Memory with the real embedder if available, else Hash, with warning."""
    try:
        from ..memory.embedder import make_embedder_cached
        from ..memory.entity_graph import EntityGraph
        from ..memory.memory import Memory
        embedder = make_embedder_cached()
        mem = Memory(config.home, embedder=embedder, entity_graph=EntityGraph(config.home))
        if type(mem.store).__name__ == "SQLiteStore":
            print("warning: Qdrant client not installed, using persistent "
                  "SQLite memory fallback (run ./setup.sh --with-qdrant "
                  "for a Qdrant server)", file=sys.stderr)
        return mem
    except Exception as exc:
        print(f"warning: real embedder unavailable ({exc}), "
              "using HashEmbedder for this run", file=sys.stderr)
    try:
        from ..memory.embedder import HashEmbedder
        from ..memory.memory import Memory
        mem = Memory(config.home, embedder=HashEmbedder())
        if type(mem.store).__name__ == "SQLiteStore":
            print("warning: Qdrant client not installed, using persistent "
                  "SQLite memory fallback (run ./setup.sh --with-qdrant "
                  "for a Qdrant server)", file=sys.stderr)
        return mem
    except Exception as exc:
        # A broken vector-store install must not make every command fail.
        # SQLite keeps memory durable without pretending it is a vector
        # database; Qdrant can still be installed later for larger stores.
        print(f"warning: Qdrant unavailable ({exc}), using persistent "
              "SQLite memory fallback", file=sys.stderr)
        from ..memory.embedder import HashEmbedder
        from ..memory.memory import Memory, SQLiteStore
        return Memory(config.home, embedder=HashEmbedder(),
                      store=SQLiteStore(config.home))


class _LazyMemory:
    """Build memory on first use, not at CLI start.

    Opening Qdrant and loading the 440M embedder costs seconds and
    hundreds of megabytes. Commands such as ``tools list``,
    ``schedule list``, and ``sandbox check`` never touch memory, so
    paying that cost before dispatch is pure startup latency. This
    proxy defers construction until a memory attribute is actually
    read.
    """

    def __init__(self, config: Config):
        self._config = config
        self._memory = None
        self._lock = threading.Lock()

    def _get(self):
        if self._memory is None:
            # The WebUI loads several panels concurrently.  Without a
            # lock, each first request can load the 440M embedder before
            # the others see the cached Memory instance.
            with self._lock:
                if self._memory is None:
                    self._memory = _make_memory(self._config)
        return self._memory

    def __getattr__(self, name):
        return getattr(self._get(), name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aidon")
    parser.add_argument("--cli", action="store_true",
                        help="Open the conversational terminal interface")
    sub = parser.add_subparsers(dest="command", required=False)
    goal_parser = sub.add_parser("goal", aliases=["run"], help="Run a goal")
    goal_parser.add_argument("text", help="Goal in plain language")
    goal_parser.add_argument("--conversation", default="cli",
                             help="Chat id for episodic memory")
    goal_parser.add_argument("--guidance", default="",
                             help="Binding instructions or hints for the run")
    goal_parser.add_argument("--swarm", action="store_true", help="Attempt swarm split, Aidon decides, fallback to single run")
    goal_parser.add_argument("--spec", default=None,
                             help="Attached spec sheet file, skips interview")
    interview_parser = sub.add_parser("interview", help="Interview to a spec")
    interview_parser.add_argument("text", help="Goal in plain language, chat message")
    interview_parser.add_argument("--conversation", default="cli",
                                  help="Chat id for episodic memory")
    interview_parser.add_argument("--run", action="store_true",
                                  help="Run the goal after the spec is approved")
    interview_parser.add_argument("--attach", default=None,
                                  help="Attached spec sheet file, skips interview questions")
    plugin_parser = sub.add_parser("plugin", help="Plugin tools and skills")
    plugin_parser.add_argument("action", choices=["list", "scan", "adopt"],
                               help="List adopted plugins, scan a plugin dir, or adopt it")
    plugin_parser.add_argument("path", nargs="?", default=None,
                               help="Plugin directory, for scan and adopt")
    plugin_parser.add_argument("--conversation", default="cli",
                               help="Chat id for episodic memory")
    trigger_parser = sub.add_parser("trigger", help="Trigger a DAG run")
    trigger_parser.add_argument("action", choices=["list", "ingest", "schedule", "webhook"],
                                help="List sources, scan ingest folder, check due jobs, or fire a webhook trigger")
    trigger_parser.add_argument("name", nargs="?", default=None,
                                help="Webhook trigger name, for webhook action")
    trigger_parser.add_argument("--goal", default="",
                                help="Goal for a webhook trigger")
    trigger_parser.add_argument("--conversation", default="",
                                help="Chat id for episodic memory")
    v1_parser = sub.add_parser("v1", help="v1 API, token auth, MCP")
    v1_parser.add_argument("action", choices=["serve", "keygen", "mcp", "mcp-http", "revoke"],
                           help="Serve the v1 HTTP API, generate a key once, or run the MCP stdio server")
    v1_parser.add_argument("--port", type=int, default=8001,
                           help="Port for v1 serve, default 8001, localhost only")
    v1_parser.add_argument("--name", default="owner",
                           help="Key name for keygen")
    v1_parser.add_argument("--scope", default="owner", choices=["owner", "agent"],
                           help="Key scope for keygen")
    webui_parser = sub.add_parser("webui", help="WebUI server and messaging webhooks")
    webui_parser.add_argument("action", nargs="?", default="serve",
                              choices=["serve"],
                              help="Serve the WebUI, localhost only")
    webui_parser.add_argument("--port", type=int, default=8765,
                              help="Port for WebUI serve, default 8765, localhost only")
    webui_parser.add_argument("--webhook-only", action="store_true",
                              help="Serve only /webhook/whatsapp and /webhook/slack, no chat, settings, or library, for a tunnel front")
    scheduler_parser = sub.add_parser("scheduler", help="Scheduler daemon and pre-approval")
    scheduler_parser.add_argument("action", choices=["serve", "preapprove", "morning",
                                                     "list", "revoke"],
                                  help="Serve due jobs, grant, list, or revoke a pre-approval, "
                                       "or list the morning report")
    scheduler_parser.add_argument("--spec", default="default", help="Spec id for pre-approval")
    scheduler_parser.add_argument("--kind", default=None,
                                  help="Action kind for pre-approval, by name or by the number "
                                       "the menu shows (default sandbox when granting). "
                                       "Omitted on revoke means every kind for that spec")
    scheduler_parser.add_argument("--hours", type=float, default=8.0, help="Pre-approval duration, hours")
    benchmark_parser = sub.add_parser("benchmark", help="Self-learning benchmark, canned, not live")
    benchmark_parser.add_argument("action", choices=["learn"], help="Snake to Worm, fresh vs adapted")
    control_parser = sub.add_parser("control", help="Steer and cancel a running run")
    control_parser.add_argument("action", choices=["list", "cancel", "steer"], help="List runs, cancel a run, or steer a run")
    control_parser.add_argument("run_id", nargs="?", default=None, help="Run id, from the goal reply")
    control_parser.add_argument("note", nargs="?", default=None, help="Steer note, for steer")
    sam_parser = sub.add_parser("sam", help="SAM 3.1 segmentation for video editing, segment and track objects in images or frames")
    sam_parser.add_argument("prompt", help="Noun phrase, for example red toolbox")
    sam_parser.add_argument("images", nargs="+", help="Image or video frame file paths")
    sam_parser.add_argument("--propagation", default="both", choices=["both", "forward", "backward"])
    video_parser = sub.add_parser("video", help="Video pipeline: extract frames, SAM segment, annotate, re-encode with ffmpeg, so Aidon can edit his own demo video")
    video_parser.add_argument("action", choices=["edit", "extract", "encode"], help="edit = full pipeline, extract = frames only, encode = frames to video")
    video_parser.add_argument("input", help="Input video for edit and extract, or frames directory for encode")
    video_parser.add_argument("output", help="Output video for edit and encode, or frames directory for extract")
    video_parser.add_argument("--prompt", default="", help="Noun phrase for SAM segmentation, for edit, for example red toolbox")
    video_parser.add_argument("--fps", type=float, default=1.0, help="Frames per second, default 1")
    image_parser = sub.add_parser("image", help="Muse Image generation and editing, same .env key and endpoint as Spark")
    image_parser.add_argument("action", choices=["generate", "edit"], help="generate = prompt only, edit = prompt plus 1-10 reference images")
    image_parser.add_argument("prompt", help="Prompt, for edit refer to refs as @Image1, @Image2 in input order")
    image_parser.add_argument("--refs", nargs="*", default=[], help="Reference image URLs or file paths, for edit, 1 to 10")
    image_parser.add_argument("--size", default="1024x1024")
    image_parser.add_argument("--output", default="")
    unity_parser = sub.add_parser("unity", help="Unity 3D assets, generated by Aidon")
    unity_parser.add_argument("action", choices=["asset", "build"], help="Generate a Unity-importable asset, or print the Unity batch build command")
    unity_parser.add_argument("name", help="Asset name, for example Worm")
    unity_parser.add_argument("--shape", default="cube", choices=["cube", "sphere", "cylinder"], help="Primitive shape")
    unity_parser.add_argument("--color", default="blue", help="Color name, for the C# material")
    email_parser = sub.add_parser("email", help="Email drafts and Gmail send")
    email_parser.add_argument("action", choices=["drafts", "send"], help="List drafts, or send a draft via Gmail OAuth")
    email_parser.add_argument("--draft", default=None, help="Draft id for send")
    email_parser.add_argument("--to", default=None, help="Recipient for send")
    email_parser.add_argument("--subject", default="", help="Subject for send")
    email_parser.add_argument("--body", default="", help="Body for send")

    memory_parser = sub.add_parser("memory", help="Inspect and prune Aidon memory, Aidon memory commands")
    memory_parser.add_argument("action", choices=["forget", "ingest", "check", "prune", "list"], help="forget = delete matching memories, ingest = ingest a folder, check = list points, prune = delete decayed memories, list = list chats")
    memory_parser.add_argument("query", nargs="?", default="", help="Query for forget, folder for ingest")
    memory_parser.add_argument("--id", default=None, help="Exact point id for forget")
    memory_parser.add_argument("--collection", default="episodic", help="Collection for forget, check, prune")
    memory_parser.add_argument("--dry-run", action="store_true", help="Prune dry run only, do not delete")
    skills_parser = sub.add_parser("skills", help="Inspect the skill library, Aidon skills commands")
    skills_parser.add_argument("action", choices=["list", "show", "check", "rollback"], help="list = list skills, show = show one skill state, check = governor check, rollback = reset a skill to candidate")
    skills_parser.add_argument("name", nargs="?", default="", help="Skill name")
    tools_parser = sub.add_parser("tools", help="Inspect the tool registry")
    tools_parser.add_argument("action", choices=["list", "check"], help="list = list plugin tools, check = check a tool exists")
    tools_parser.add_argument("name", nargs="?", default="", help="Tool name for check")
    capabilities_parser = sub.add_parser("capabilities", help="Inspect registered capabilities (images, MCP servers)")
    capabilities_parser.add_argument("action", choices=["list", "stale"], help="list = list entries, stale = entries idle too long, retirement candidates")
    capabilities_parser.add_argument("--days", type=int, default=90, help="Idle days for stale (default 90)")
    dag_parser = sub.add_parser("dag", help="Inspect saved DAG workflows")
    dag_parser.add_argument("action", choices=["list", "show", "run"], help="list = list saved DAGs, show = show one DAG, run = execute one DAG's tool nodes")
    dag_parser.add_argument("id", nargs="?", default="", help="DAG id for show/run")
    dag_parser.add_argument("--run-id", default=None, help="Run id for dag run checkpoints; reuse it to resume a killed run")
    dag_parser.add_argument("--parallel", action="store_true", help="Run independent DAG nodes concurrently instead of one at a time")
    mission_parser = sub.add_parser("mission", help="Mission lifecycle records")
    mission_parser.add_argument("action", choices=["list", "show", "discard", "create"], help="list = list missions, show = show one, discard = discard a rejected mission, create = create a draft mission")
    mission_parser.add_argument("id", nargs="?", default="", help="Mission id")
    mission_parser.add_argument("--spec", default="{}", help="Spec JSON for create")
    outbox_parser = sub.add_parser("outbox", help="Durable notification outbox")
    outbox_parser.add_argument("action", choices=["list", "drain", "queue"], help="list = list queued, drain = deliver queued with a printed sender, queue = queue a notification")
    outbox_parser.add_argument("--to", default="", help="Recipient for queue")
    outbox_parser.add_argument("--subject", default="", help="Subject for queue")
    outbox_parser.add_argument("--body", default="", help="Body for queue")
    export_parser = sub.add_parser("export", help="Render a read-only markdown view of the registry, and export memory points")
    export_parser.add_argument("--dir", default="", help="Output directory, default <home>/export")
    export_parser.add_argument("--registry", action="store_true", help="Export the full registry to Markdown, in addition to memory points")
    export_parser.add_argument("--full", action="store_true", help="Also export missions, skills, DAGs, and runs as separate Markdown files")
    schedule_parser = sub.add_parser("schedule", help="Cron-like scheduled jobs, Aidon schedule commands, scheduler serve runs them")
    schedule_parser.add_argument("action", choices=["add", "list", "remove", "show"], help="add = add a job, list = list jobs, remove = remove a job, show = show a job")
    schedule_parser.add_argument("--name", default="", help="Job name")
    schedule_parser.add_argument("--goal", default="", help="Goal for add")
    schedule_parser.add_argument("--kind", default="goal",
                                 choices=["goal", "chain", "learn", "report", "brief"],
                                 help="Job kind; chain uses JSON in --goal")
    schedule_parser.add_argument("--schedule", default="0 9 * * *", help="Cron schedule for add, default daily 9am")
    schedule_parser.add_argument("--conversation", default="", help="Chat id for add")
    practice_parser = sub.add_parser("practice", help="Free-time practice windows, same store as the Practice page")
    practice_parser.add_argument("action", choices=["add", "list", "remove", "run"], help="add = add a schedule, list = list schedules, remove = remove one, run = run one now (60 min)")
    practice_parser.add_argument("--name", default="", help="Schedule name")
    practice_parser.add_argument("--date", default="", help="One date YYYY-MM-DD; empty means days/every day")
    practice_parser.add_argument("--days", default="", help="Weekdays, comma separated, e.g. Mon,Wed,Fri; empty means every day")
    practice_parser.add_argument("--start", default="21:00", help="Window start HH:MM")
    practice_parser.add_argument("--end", default="22:00", help="Window end HH:MM")
    practice_parser.add_argument("--goal", action="append", default=[], help="Goal text, repeatable")
    practice_parser.add_argument("--spec", action="append", default=[], help="Spec name per goal, positionally paired with --goal")
    practice_parser.add_argument("--preapprove", dest="preapprove", action="store_true", default=True, help="Pre-approve the schedule (default): reversible steps auto-approve, destructive steps are rejected")
    practice_parser.add_argument("--no-preapprove", dest="preapprove", action="store_false", help="Do not pre-approve: practice stops for approval like any other run")
    sandbox_parser = sub.add_parser("sandbox", help="Sandbox isolation boundary")
    sandbox_parser.add_argument("action", choices=["check"], help="check = report sandbox backend and workdir, fail closed if backend missing")
    chain_parser = sub.add_parser("chain", help="Declarative tool chains, zero-LLM scheduled jobs")
    chain_parser.add_argument("action", choices=["validate", "run"], help="validate = validate a chain spec JSON, run = run a validated chain through sandbox tools")
    chain_parser.add_argument("path", help="Chain spec JSON file")
    chat_parser = sub.add_parser("chat", help="Interactive terminal chat, Aidon chat command, type a goal, empty line to exit")
    computer_parser = sub.add_parser("computer", help="Screenshot and computer use, approval-gated, no GUI action without a driver")
    computer_parser.add_argument("action", choices=["screenshot", "run"], help="screenshot = capture screen to a file, run = run a computer-use action JSON file")
    computer_parser.add_argument("path", help="Output PNG for screenshot, or action JSON file for run")
    computer_parser.add_argument("--approve", action="store_true", help="Approve irreversible actions in this run, otherwise they are refused")

    report_parser = sub.add_parser("report", help="Autonomy and daily reports, Aidon report commands")
    report_parser.add_argument("action", choices=["autonomy", "daily"], help="autonomy = counts and gate status, daily = daily report text")
    demo_parser = sub.add_parser("demo", help="Demo learning and invent-tool, live, not canned")
    demo_parser.add_argument("action", choices=["learn", "invent-tool"], help="learn = real experience counts, invent-tool = draft a plugin skeleton and scan it, adoption still needs the gate")
    demo_parser.add_argument("--name", default="", help="Plugin name for invent-tool")
    demo_parser.add_argument("--description", default="", help="Plugin description for invent-tool")
    slack_parser = sub.add_parser("slack", help="Slack inbound listening, Aidon slack listen")
    slack_parser.add_argument("action", choices=["listen", "status"], help="listen = poll Slack once or for --seconds, status = configured or not")
    slack_parser.add_argument("--channel", default="", help="Slack channel id, or AIDON_SLACK_INBOUND_CHANNEL")
    slack_parser.add_argument("--seconds", type=int, default=0, help="Listen duration in seconds, 0 = one poll only")
    slack_parser.add_argument("--run", action="store_true", help="Run polled messages as goals, otherwise just print them")
    whatsapp_parser = sub.add_parser("whatsapp", help="WhatsApp status and send, Aidon whatsapp commands")
    whatsapp_parser.add_argument("action", choices=["status", "send"], help="status = configured or missing, send = send a real WhatsApp message, fails if not configured, never faked")
    whatsapp_parser.add_argument("--to", default="", help="Recipient number for send")
    whatsapp_parser.add_argument("--text", default="", help="Message text for send")
    tick_parser = sub.add_parser("tick", help="Run due scheduler jobs once, Aidon tick")
    repl_parser = sub.add_parser("repl", help="Full REPL, Aidon repl, /help, /history, /quit, or a goal")
    learn_parser = sub.add_parser("learn", help="Idle learn cycle: re-test candidate skills, promote passers, consolidate validated duplicates")
    skill_lib_parser = sub.add_parser("skill-library", help="Full skill library: list, show, promote, trust, retire, rollback, versions, from-experience")
    skill_lib_parser.add_argument("action", choices=["list", "show", "promote", "trust", "retire", "rollback", "versions", "from-experience", "import"], help="list = list skills, show = show one, promote/trust/retire = lifecycle, rollback = restore a version, versions = version history, from-experience = promote a successful run's workflow to a discoverable skill file, import = add a Markdown skill file as a candidate")
    skill_lib_parser.add_argument("--file", default="", help="Skill file for import")
    skill_lib_parser.add_argument("name", nargs="?", default="", help="Skill name")
    skill_lib_parser.add_argument("--version", type=int, default=0, help="Version for rollback")
    conversations_parser = sub.add_parser("conversations", help="SQLite conversation store: list, history, record, delete")
    conversations_parser.add_argument("action", choices=["list", "history", "record", "delete"], help="list = list conversations, history = show turns, record = record a turn, delete = delete a conversation")
    conversations_parser.add_argument("id", nargs="?", default="", help="Conversation id")
    conversations_parser.add_argument("--role", default="user", help="Role for record")
    conversations_parser.add_argument("--text", default="", help="Text for record")
    registry_parser = sub.add_parser("registry-db", help="SQLite registry: list runs, show a run, list skill versions")
    registry_parser.add_argument("action", choices=["runs", "show", "skill-versions"], help="runs = list run records, show = show one run, skill-versions = list versions for a skill")
    registry_parser.add_argument("id", nargs="?", default="", help="Run id or skill name")
    hub_parser = sub.add_parser("hub", help="Remote hub server and client: serve, list, push, pull")
    hub_parser.add_argument("action", choices=["serve", "list", "push", "pull"], help="serve = start the hub server, list = list DAGs on a hub, push/pull = push or pull a DAG JSON file")
    hub_parser.add_argument("--url", default="http://127.0.0.1:8003", help="Hub URL for list/push/pull")
    hub_parser.add_argument("--port", type=int, default=8003, help="Port for serve")
    hub_parser.add_argument("--path", default="", help="DAG JSON file for push/pull")
    hub_parser.add_argument("--token", default="", help="Hub token, or AIDON_HUB_TOKEN")
    mcp_parser = sub.add_parser("mcp", help="MCP client over HTTP: list tools, call a tool")
    mcp_parser.add_argument("action", choices=["list", "call"], help="list = list tools on the server, call = call one tool")
    mcp_parser.add_argument("--url", default="", help="MCP HTTP endpoint, or AIDON_MCP_URL")
    mcp_parser.add_argument("--name", default="", help="Tool name for call")
    mcp_parser.add_argument("--args", default="{}", help="Tool arguments JSON for call")
    docstore_parser = sub.add_parser("docstore", help="Per-document vector store: ingest a file, search ingested documents, drop a collection")
    docstore_parser.add_argument("action", choices=["ingest", "search", "drop", "list"], help="ingest = index a file, search = search ingested documents, drop = forget a collection, list = list collections")
    docstore_parser.add_argument("arg", nargs="?", default="", help="File path for ingest, query for search, collection for drop")
    docstore_parser.add_argument("--qdrant", action="store_true", help="Use the Qdrant-backed store instead of the JSON index")
    intake_parser = sub.add_parser("intake", help="Intake schema: missing slots and summary for a goal text")
    intake_parser.add_argument("--schema", default="generic", help="Schema name: generic or dinner")
    intake_parser.add_argument("text", nargs="?", default="", help="Goal text to check against the schema")
    ingest_parser = sub.add_parser("ingest", help="Folder ingest into knowledge: ingest, remove, dry-run")
    ingest_parser.add_argument("action", choices=["ingest", "remove", "dry-run"], help="ingest = index a folder, remove = forget a folder, dry-run = count only")
    ingest_parser.add_argument("folder", help="Folder path")
    metrics_parser = sub.add_parser("metrics", help="Autonomy metrics: summary or rendered report")
    metrics_parser.add_argument("--days", type=int, default=30, help="Window in days")
    metrics_parser.add_argument("--render", action="store_true", help="Render as Markdown instead of a dict")
    vrm_parser = sub.add_parser("vrm-check", help="Check VRM morph targets against the lip-sync JS")
    vrm_parser.add_argument("vrm", nargs="?", default="", help="VRM file; default = <home>/avatar.vrm")
    vrm_parser.add_argument("--js", default="", help="JS file to extract morph names from; default = the lip-sync JS")
    resume_parser = sub.add_parser("resume", help="Resume a saved run: list saved runs, or show one")
    resume_parser.add_argument("run_id", nargs="?", default="", help="Run id to show; empty = list")
    plan_guard_parser = sub.add_parser("plan-guard", help="Validate a plan JSON file against available tools")
    plan_guard_parser.add_argument("file", help="Plan JSON file")
    plan_guard_parser.add_argument("--tools", default="write,read,list,shell,calculate", help="Comma-separated available tools")
    inventory_parser = sub.add_parser("inventory", help="Artifact inventory for a workdir")
    inventory_parser.add_argument("workdir", nargs="?", default="", help="Workdir; default = configured workdir")
    delivery_parser = sub.add_parser("delivery", help="Delivery summary from a run report JSON file")
    delivery_parser.add_argument("file", help="Run report JSON file")
    think_parser = sub.add_parser("think", help="Think effort and sampling for a phase")
    think_parser.add_argument("phase", help="Phase: interpret, research, plan, sandbox, verify, approval, live, learn, respond")
    think_parser.add_argument("--level", default="", help="Requested effort level: off, low, medium, high")
    think_parser.add_argument("--escalate", action="store_true", help="Raise one level (verifier rejection only)")
    args = parser.parse_args(argv)
    if args.cli:
        args.command = "chat"
    elif not args.command:
        parser.error("a command is required (or use --cli for conversational mode)")
    # This surface is called `run`; keep the spelling while using
    # the new goal runner and its current state/control implementation.
    if args.command == "run":
        args.command = "goal"

    config = Config.load()
    config.ensure_home()
    from .model_client import client_for_config
    from ..agentic.counting import CountingClient
    try:
        client = client_for_config(config)
    except ValueError as exc:
        _startup_style("error", f"Model backend error: {exc}")
        return 2
    if isinstance(client, FakeClient):
        _startup_style("warn", "No model backend configured; using FakeClient. "
                       "Set AIDON_BACKEND, AIDON_API_ENDPOINT, and "
                       "AIDON_API_KEY in .env for a real run.")
    # Count every model call in the workflow, including planning, research,
    # verification, repairs, learning, and the final response.
    client = CountingClient(client)
    # Fast model, different model name, optionally its own endpoint.
    # This is how a local small model works too: set the endpoint to a local
    # native server, AIDON_MODEL to the strong model, and AIDON_FAST_MODEL
    # to the small local model. No Native-specific branch in the loop.
    main_client = client
    fast_client = None
    model_router = None
    if config.fast_model and config.fast_model != config.model:
        from dataclasses import replace
        from ..agentic.router import RoutedClient
        # The fast model may live on its own endpoint (e.g. main on the
        # Meta API, fast Llama on a local server). When a fast endpoint
        # is set, backend is re-inferred from it; otherwise the main
        # backend, endpoint, and key carry over unchanged.
        _fast_endpoint = (getattr(config, "fast_api_endpoint", "") or "").strip()
        _fast_key = getattr(config, "fast_api_key", "") or ""
        fast_config = replace(
            config, model=config.fast_model,
            model_num_ctx=config.fast_num_ctx or config.model_num_ctx,
            api_base=_fast_endpoint or config.api_base,
            api_key=_fast_key or config.api_key,
            backend="" if _fast_endpoint else config.backend)
        fast_client = CountingClient(client_for_config(fast_config))
        fast_phases = {s.strip() for s in config.router_fast_phases.split(",") if s.strip()}
        client = RoutedClient(fast_client, client, fast_phases=fast_phases)
        # ALSO build the phase router: fast phases go to the fast
        # client, everything else to the main client. If building
        # it fails, the RoutedClient above stays the behaviour.
        try:
            from ..system.model_router import RouterClient
            phase_clients = {phase: fast_client for phase in fast_phases}
            model_router = RouterClient(phase_clients, default=main_client)
        except Exception:
            model_router = None
    sandbox = Sandbox(
        config.workdir, config.sandbox_timeout_seconds,
        mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
        fsize_mb=config.sandbox_fsize_mb,
        cpu_seconds=config.sandbox_cpu_seconds,
        backend=config.sandbox_backend,
        allow_network=config.sandbox_network,
        require_isolation=config.sandbox_require_isolation)
    memory = _LazyMemory(config)

    if args.command == "learn":
        from ..agentic.learn_cycle import run_learn_cycle
        from ..capabilities.skill_lifecycle import LifecycleStore
        summary = run_learn_cycle(
            config.home, sandbox,
            governor_check=LifecycleStore(config.home).governor_check,
            max_per_day=max(0, config.learn_max_skills_per_day))
        print(f"Learn cycle: promoted={summary.get('promoted', [])} "
              f"retired={summary.get('retired_duplicates', [])} "
              f"trusted_for_review={summary.get('trusted_duplicates_for_review', [])} "
              f"skipped={summary.get('skipped', [])}")
        return 0
    if args.command == "resume":
        from ..agentic.resume import list_runs, resume_state
        if args.run_id:
            ok, state, error = resume_state(config.home, args.run_id)
            if not ok:
                print(f"cannot resume: {error}"); return 2
            print(f"Resuming run {args.run_id}: goal={state.get('goal','')} steps={len(state.get('steps',[]))} completed={state.get('completed',[])}")
        else:
            for run_id in list_runs(config.home):
                print(run_id)
        return 0
    if args.command == "plan-guard":
        import json as _json
        from pathlib import Path as _Path
        from ..agentic.plan_guard import plan_first_summary, validate_plan
        plan = _json.loads(_Path(args.file).read_text())
        failures = validate_plan(plan, [t.strip() for t in args.tools.split(",") if t.strip()])
        print(plan_first_summary(plan))
        if failures:
            for failure in failures:
                print(f"FAIL: {failure}")
            return 2
        print("plan guard: pass")
        return 0
    if args.command == "inventory":
        from pathlib import Path as _Path
        from ..agentic.artifact_inventory import inventory_markdown
        workdir = _Path(args.workdir) if args.workdir else config.workdir
        print(inventory_markdown(workdir))
        return 0
    if args.command == "delivery":
        import json as _json
        from pathlib import Path as _Path
        from ..agentic.delivery_summary import delivery_summary
        report = _json.loads(_Path(args.file).read_text())
        print(delivery_summary(report))
        return 0
    if args.command == "think":
        from ..agentic.think_effort import resolve_effort, sampling_for
        level = resolve_effort(args.phase, requested=args.level or None, escalate=args.escalate)
        print(f"phase={args.phase} effort={level} sampling={sampling_for(args.phase)}")
        return 0
    if args.command == "ingest":
        from ..memory.ingest import ingest_folder, remove_folder
        from pathlib import Path as _P
        if args.action == "remove":
            print(f"Removed {remove_folder(config.home, args.folder)} points")
        else:
            print(ingest_folder(config.home, args.folder, dry_run=(args.action == "dry-run")))
        return 0
    if args.command == "metrics":
        from ..agentic.autonomy_metrics import AutonomyMetrics
        metrics = AutonomyMetrics(config.home)
        print(metrics.render(args.days) if args.render else metrics.summary(args.days))
        return 0
    if args.command == "vrm-check":
        from pathlib import Path as _P
        from ..capabilities.vrm_check import check_vrm
        vrm_path = _P(args.vrm) if args.vrm else config.home / "avatar.vrm"
        if args.js:
            js_source = _P(args.js).read_text()
        else:
            from ..capabilities.audio import vrm_lip_sync_js
            js_source = vrm_lip_sync_js()
        result = check_vrm(vrm_path, js_source)
        print(result)
        return 0 if result.get("ok") else 2
    if args.command == "skill-library":
        from ..capabilities.skill_library import SkillLibrary, import_skill_file
        if args.action == "import":
            if not args.file:
                print("skill-library import needs --file"); return 2
            try:
                skill = import_skill_file(config.home, args.file,
                                          args.name or None)
            except (ValueError, PermissionError) as exc:
                print(f"import failed: {exc}"); return 1
            print(f"Imported skill {skill.name} as candidate "
                  f"from {args.file}"); return 0
        lib = SkillLibrary(config.home)
        if args.action == "list":
            for skill in lib.list():
                print(f"{skill.name} v{skill.version} {skill.state} privileged={skill.privileged}")
            return 0
        if not args.name:
            print("skill-library show/promote/trust/retire/rollback/versions/from-experience need a name"); return 2
        if args.action == "from-experience":
            from ..memory.experience import (
                promote_experience_to_skill, recall_experiences)
            memory = _make_memory(config)
            matches = recall_experiences(memory, args.name, limit=1)
            if not matches:
                print(f"No successful experience matches {args.name!r}"); return 2
            try:
                path = promote_experience_to_skill(config.home, matches[0])
            except FileExistsError as exc:
                print(str(exc)); return 1
            print(f"Promoted {matches[0].goal!r} to skill: {path}"); return 0
        if args.action == "show":
            skill = lib.get(args.name)
            print(skill if skill else f"No skill {args.name}"); return 0 if skill else 2
        if args.action == "promote":
            print(lib.promote(args.name, {"passed": True, "evidence": "CLI promote"})); return 0
        if args.action == "trust":
            skill = lib.get(args.name)
            if skill is None:
                print(f"No skill {args.name}"); return 2
            action_class = "skill_privileged" if skill.privileged else "skill"
            gate = _cli_gate(config.home)
            decision = gate.ask(
                "skill",
                f"Trust skill {args.name}?",
                f"Promote {args.name} (v{skill.version}) to trusted.\n"
                f"{skill.description or '(no description)'}",
                action_class=action_class,
            )
            if decision.verdict not in ("approved", "auto_approved", "edited"):
                print(f"trust rejected: {decision.verdict}"); return 1
            print(lib.trust(args.name, approved=True)); return 0
        if args.action == "retire":
            print(lib.retire(args.name, "retired from CLI")); return 0
        if args.action == "rollback":
            print(lib.rollback(args.name, args.version)); return 0
        if args.action == "versions":
            print(lib.version_history(args.name)); return 0
        return 2
    if args.command == "conversations":
        from ..memory.conversations_db import ConversationStoreDB
        store = ConversationStoreDB(config.home)
        if args.action == "list":
            print(store.list_conversations("default")); return 0
        if args.action == "import":
            import json as _json
            from pathlib import Path as _P
            count = 0
            for path in sorted((_P(config.home) / "conversations").glob("*.json")):
                try:
                    data = _json.loads(path.read_text())
                except Exception:
                    continue
                store.import_conversation(str(data.get("id", path.stem)),
                                          list(data.get("turns", [])))
                count += 1
            print(f"Imported {count} legacy conversations"); return 0
        if not args.id:
            print("conversations history/record/delete need an id"); return 2
        if args.action == "history":
            print(store.history(args.id)); return 0
        if args.action == "record":
            print(store.record_exchange(args.id, args.role, args.text)); return 0
        if args.action == "delete":
            print(f"Deleted {args.id}: {store.delete_conversation(args.id)}"); return 0
        return 2
    if args.command == "registry-db":
        from ..capabilities.registry_db import Registry
        reg = Registry(config.home)
        if args.action == "runs":
            print(reg.list_runs()); return 0
        if args.action == "show":
            print(reg.get_run(args.id) if args.id else "registry-db show needs an id"); return 0
        if args.action == "skill-versions":
            print(reg.list_skill_versions(args.id) if args.id else "registry-db skill-versions needs a skill name"); return 0
        return 2
    if args.command == "hub":
        import os as _os
        from ..capabilities.remote_hub_server import HubClient, HubServer
        if args.action == "serve":
            token = args.token or _os.environ.get("AIDON_HUB_TOKEN", "")
            server = HubServer(config.home, token=token)
            print(f"Hub serving on 127.0.0.1:{args.port} (token set: {bool(token)})")
            server.serve(port=args.port)
            return 0
        client = HubClient(args.url, token=args.token or _os.environ.get("AIDON_HUB_TOKEN", ""))
        if args.action == "list":
            print(client.list_dags()); return 0
        if args.action == "push":
            import json as _json
            from pathlib import Path as _P
            print(client.push_dag(_json.loads(_P(args.path).read_text()))); return 0
        if args.action == "pull":
            print(client.pull_dag(args.id)); return 0
        return 2
    if args.command == "mcp":
        import os as _os, json as _json
        from ..interface.mcp_http import McpHttpClient
        url = args.url or _os.environ.get("AIDON_MCP_URL", "")
        if not url:
            print("mcp needs --url or AIDON_MCP_URL"); return 2
        client = McpHttpClient(url, token=_os.environ.get("AIDON_MCP_TOKEN", ""))
        client.initialize()
        try:
            if args.action == "list":
                print(client.list_tools()); return 0
            print(client.call_tool(args.name, _json.loads(args.args))); return 0
        finally:
            client.close()
    if args.command == "docstore":
        from ..memory.docstore import DocStore
        from pathlib import Path as _P
        store = DocStore.from_qdrant(config.home) if getattr(args, "qdrant", False) else DocStore(home=config.home)
        if args.action == "ingest":
            path = _P(args.arg)
            print(store.ingest(path.name, path.read_text(encoding="utf-8", errors="replace"))); return 0
        if args.action == "search":
            print(store.search_all(args.arg)); return 0
        if args.action == "drop":
            print(f"Dropped {args.arg}: {store.drop(args.arg)}"); return 0
        if args.action == "list":
            print(store.collections()); return 0
        return 2
    if args.command == "intake":
        from ..agentic.intake_schema import get_schema, missing_slots, ready, summary_lines
        schema = get_schema(args.schema)
        if schema is None:
            print(f"Unknown intake schema: {args.schema}"); return 2
        spec = {"goal": args.text}
        print(f"Ready: {ready(schema, spec)}")
        print(f"Missing: {missing_slots(schema, spec)}")
        for line in summary_lines(schema, spec):
            print(line)
        return 0
    if args.command == "memory":
        from ..memory.kb import KnowledgeBase
        if args.action == "forget":
            if args.id:
                ok = memory.forget(args.collection, args.id)
                print(f"Forgot {args.id}: {ok}")
                return 0 if ok else 2
            result = memory.forget_search(args.query) if args.query else {"matches": []}
            print(f"Forget matches for {args.query!r}: {result}")
            return 0
        if args.action == "ingest":
            from pathlib import Path as _P
            kb = KnowledgeBase(memory)
            folder = _P(args.query)
            total = 0
            for file_path in sorted(folder.rglob("*")) if folder.is_dir() else [folder]:
                if file_path.is_file():
                    try:
                        total += kb.ingest_file(file_path)
                    except Exception as exc:
                        print(f"Ingest failed {file_path}: {exc}")
            print(f"Ingested {total} chunks from {args.query}")
            return 0
        if args.action == "check":
            points = memory.list_points(args.collection, limit=50)
            print(f"{args.collection}: {len(points)} points")
            for point in points:
                print(f"  {point.id} {point.payload.get('text', '')[:80]}")
            return 0
        if args.action == "prune":
            result = memory.prune(args.collection) if not args.dry_run else {"pruned": 0, "dry_run": True}
            print(f"Prune {args.collection}: {result}")
            return 0
        if args.action == "list":
            from ..memory.conversations_db import ConversationStoreDB
            ids = [r["id"] for r in ConversationStoreDB(config.home).list_all_conversations()]
            print(f"Chats: {', '.join(ids)}")
            return 0
        return 2
    if args.command == "skills":
        from ..capabilities.skill_lifecycle import LifecycleStore
        lifecycle = LifecycleStore(config.home)
        if args.action == "list":
            print(f"Skills: {list(lifecycle._data.keys())}")
            return 0
        if not args.name:
            print("skills show/check/rollback need a name"); return 2
        if args.action == "show":
            print(f"{args.name}: {lifecycle.state(args.name)}")
            return 0
        if args.action == "check":
            print(f"{args.name} governor ok: {lifecycle.governor_check(args.name)}")
            return 0
        if args.action == "rollback":
            lifecycle._data[args.name] = {"state": "candidate", "successes": 0, "failures": 0, "learns": {}}; lifecycle._save()
            print(f"{args.name} rolled back to candidate")
            return 0
        return 2
    if args.command == "tools":
        from ..capabilities.plugins import PluginRegistry
        from ..capabilities.tool_catalog import all_tool_names
        registry = PluginRegistry(config.home); registry.load_all()
        available = all_tool_names(registry.tool_names())
        if args.action == "list":
            print(f"Plugin tools: {', '.join(registry.tool_names())}")
            print("Goal tools: " + ", ".join(available))
            return 0
        if args.action == "check":
            print(f"{args.name} exists: {args.name in available}")
            return 0
        return 2
    if args.command == "capabilities":
        from ..capabilities.capability_registry import CapabilityRegistry
        registry = CapabilityRegistry(config.home)
        if args.action == "list":
            for entry in registry.list():
                print(f"{entry.get('kind')}:{entry.get('name')} {entry.get('status')} uses={entry.get('use_count', 0)} last_used={entry.get('last_used', '') or 'never'}")
            return 0
        if args.action == "stale":
            stale = registry.stale_entries(days=args.days)
            for entry in stale:
                print(f"{entry['kind']}:{entry['name']} {entry['status']} idle={entry['idle_days']}d uses={entry['use_count']}")
            if not stale:
                print("No stale capabilities.")
            return 0
        return 2
    if args.command == "dag":
        from ..agentic.dag_store import DagStore
        store = DagStore(config.home)
        if args.action == "list":
            for dag in store.list_dags():
                print(f"{dag['id']}: {dag['goal']} nodes={dag['nodes']}")
            return 0
        if args.action == "show":
            dag = store.get(args.id)
            print(dag if dag else f"No DAG {args.id}")
            return 0 if dag else 2
        if args.action == "run":
            # Execute a saved DAG's tool nodes via the DAG executor.
            # Tool nodes map to the core ToolRegistry (write/read/list/
            # shell/calculate, confined to the sandbox workdir); fast
            # and strong nodes have no tool mapping, so a DAG containing
            # any is refused before anything runs rather than half-run.
            # Checkpoints live in the Registry under the run id, so a
            # killed run resumes by re-running with the same --run-id.
            from ..agentic.dag import DAG, DAGNode
            from ..agentic.dag_executor import DAGExecutor
            from ..capabilities.registry_db import Registry
            from ..capabilities.tools import ToolRegistry
            import uuid as _dag_uuid
            data = store.get(args.id)
            if data is None:
                print(f"No DAG {args.id}")
                return 2
            try:
                dag = DAG([DAGNode.from_dict(node) for node in data.get("nodes", [])])
                dag.validate_strict()
            except Exception as exc:
                print(f"dag run {args.id} refused: {exc}")
                return 2
            try:
                # Learned absolute paths are made relative to this
                # run's workdir first, the same relativize() the
                # loop applies to adapted DAGs, so a DAG learned
                # in this workdir passes ToolRegistry confinement
                # instead of failing on its own absolute paths.
                dag = dag.relativize(sandbox.workdir)
                non_tool = [node.id for node in dag.nodes.values() if node.executor != "tool"]
                if non_tool:
                    print(f"dag run supports tool nodes only, refusing DAG with non-tool nodes: {', '.join(non_tool)}")
                    return 2
                registry = ToolRegistry(sandbox.workdir, sandbox=sandbox,
                                        network_available=_network_available(None, sandbox))
                def _run_node(node):
                    args_dict = node.args if isinstance(node.args, dict) else {}
                    return registry.call(node.tool, args_dict)
                run_id = args.run_id or _dag_uuid.uuid4().hex[:12]
                executor = DAGExecutor(dag, _run_node, state=Registry(config.home), run_id=run_id)
                result = executor.execute_parallel() if getattr(args, "parallel", False) else executor.execute()
            except Exception as exc:
                print(f"dag run {args.id} failed: {exc}")
                return 1
            print(f"dag run {args.id}: {result['status']}, completed: {', '.join(result['completed'])}")
            if result["failed"]:
                print(f"failed: {result['failed']}")
            return 0 if result["status"] == "done" else 1
        return 2
    if args.command == "mission":
        from ..capabilities.mission import MissionStore
        import json as _json
        store = MissionStore(config.home)
        if args.action == "list":
            missions_dir = config.home / "missions"
            for path in sorted(missions_dir.glob("*/mission.json")) if missions_dir.exists() else []:
                try:
                    data = _json.loads(path.read_text())
                    print(f"{data.get('conversation_id')}: {data.get('status')}")
                except Exception:
                    continue
            return 0
        if args.action == "show":
            try:
                print(store.get(args.id))
            except FileNotFoundError:
                print(f"No mission {args.id}"); return 2
            return 0
        if args.action == "create":
            try:
                spec = _json.loads(args.spec)
            except Exception:
                spec = {}
            print(store.create(args.id, spec))
            return 0
        if args.action == "discard":
            try:
                mission = store.get(args.id)
            except FileNotFoundError:
                print(f"No mission {args.id}"); return 2
            if mission.get("status") != "cancelled" and mission.get("status") != "rejected":
                # Discard is explicit only for cancelled or rejected drafts; otherwise refuse.
                print(f"Discard refused: mission {args.id} status is {mission.get('status')}, not cancelled or rejected"); return 2
            store.set_status(args.id, "cancelled")
            print(f"Discarded {args.id}")
            return 0
        return 2
    if args.command == "outbox":
        from ..capabilities.outbox import Outbox
        outbox = Outbox(config.home)
        if args.action == "list":
            for item in outbox.list():
                print(f"{item.get('id')} to {item.get('to')} subject {item.get('subject')} status {item.get('status')}")
            return 0
        if args.action == "queue":
            print(outbox.queue(args.to, args.subject, args.body))
            return 0
        if args.action == "drain":
            result = outbox.drain(lambda item: print(f"Would send to {item.get('to')}: {item.get('subject')}") or False)
            print(f"Outbox drain, no real sender configured in CLI, sent={result['sent']} failed={result['failed']} remaining={result['remaining']}, items stay queued until a real sender is supplied")
            return 0
        return 2
    if args.command == "export":
        from ..memory.memory_extras import export_points
        from pathlib import Path as _P
        dest = _P(args.dir) if args.dir else config.home / "export"
        dest.mkdir(parents=True, exist_ok=True)
        out_file = dest / "points.jsonl"
        count = export_points(memory, out_file)
        print(f"Exported {count} memory points to {out_file}")
        if args.registry:
            from ..capabilities.registry import export_registry_markdown
            reg_path = export_registry_markdown(config.home, dest / "registry.md")
            print(f"Exported registry to {reg_path}")
        if args.full:
            from ..capabilities.export_full import export_markdown
            print(f"Full export: {export_markdown(config.home, dest / 'full')}")
        return 0
    if args.command == "schedule":
        from ..interface.triggers import Scheduler, SchedulerJob
        scheduler = Scheduler(config.home)
        if args.action == "list":
            for job in scheduler.jobs:
                print(f"{job.name}: {job.goal} schedule={job.schedule} chat={job.conversation_id}")
            return 0
        if args.action == "show":
            for job in scheduler.jobs:
                if job.name == args.name:
                    print(job); return 0
            print(f"No job {args.name}"); return 2
        if args.action == "add":
            if not args.name or not args.goal:
                print("schedule add needs --name and --goal"); return 2
            try:
                scheduler.add_job(SchedulerJob(name=args.name, goal=args.goal,
                                               schedule=args.schedule,
                                               kind=args.kind,
                                               conversation_id=args.conversation))
            except ValueError as exc:
                print(f"schedule add failed: {exc}"); return 2
            print(f"Added job {args.name}")
            return 0
        if args.action == "remove":
            before = len(scheduler.jobs)
            scheduler.jobs = [j for j in scheduler.jobs if j.name != args.name]
            scheduler.save()
            print(f"Removed {args.name}" if len(scheduler.jobs) < before else f"No job {args.name}")
            return 0 if len(scheduler.jobs) < before else 2
        return 2
    if args.command == "practice":
        from ..interface.practice import (add_schedule, get_schedule,
                                          load_schedules, remove_schedule)
        if args.action == "list":
            for s in load_schedules(config.home):
                goals = "; ".join(g["goal"] for g in s.get("goals", []))
                when = s.get("date") or ",".join(
                    ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][d]
                    for d in s.get("days", [])) or "every day"
                print(f"{s['name']}: {when} {s.get('start')}-{s.get('end')} goals=[{goals}]")
            return 0
        if args.action == "add":
            if not args.name or not args.goal:
                print("practice add needs --name and at least one --goal"); return 2
            goals = []
            for i, g in enumerate(args.goal):
                # "goal || spec" splits like the Practice page; an
                # explicit --spec in the same position wins.
                text, _, inline_spec = g.partition("||")
                spec = (args.spec[i] if i < len(args.spec) else ""
                        ) or inline_spec.strip()
                goals.append({"goal": text.strip(), "spec": spec})
            _padd, _perr = add_schedule(config.home, {
                "name": args.name, "date": args.date,
                "days": [d.strip() for d in args.days.split(",") if d.strip()],
                "start": args.start, "end": args.end, "goals": goals,
                "preapprove": args.preapprove})
            if _perr:
                print(f"practice add failed: {_perr}"); return 2
            print(f"Added practice schedule {args.name}")
            return 0
        if args.action == "remove":
            removed = remove_schedule(config.home, args.name)
            print(f"Removed {args.name}" if removed else f"No practice schedule {args.name}")
            return 0 if removed else 2
        if args.action == "run":
            from datetime import datetime, timedelta
            from ..interface.practice import (get_schedule, record_session,
                                              run_practice_session)
            schedule = get_schedule(config.home, args.name)
            if schedule is None:
                print(f"No practice schedule {args.name}"); return 2
            from ..agentic.budgets import Budget as _PB
            from ..agentic.trace import TraceRecorder as _PT
            from ..agentic.verifier_model import ModelVerifier as _PMV
            from ..capabilities.plugins import PluginRegistry as _PPR
            from ..capabilities.sandbox import Sandbox as _PSB
            from ..interface.practice import (PRACTICE_TOOL_NAMES, PracticeGate,
                                              coerce_preapprove, practice_tools)
            import os as _pos
            import re as _pre
            _pdir = config.home / "practice" / _pre.sub(
                r"[^A-Za-z0-9_-]+", "-", schedule["name"]).strip("-")
            _pdir.mkdir(parents=True, exist_ok=True)
            _psandbox = _PSB(_pdir, config.sandbox_timeout_seconds,
                             mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
                             fsize_mb=config.sandbox_fsize_mb,
                             cpu_seconds=config.sandbox_cpu_seconds,
                             backend=config.sandbox_backend,
                             allow_network=config.sandbox_network,
                             require_isolation=config.sandbox_require_isolation)
            _pplugins = _PPR(config.home); _pplugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _pmb
                _pbackend = _pmb(config.execution_backend, _psandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _pbackend = None
            if coerce_preapprove(schedule.get("preapprove", True)):
                _pgate = PracticeGate(_pdir, sandboxed=_pbackend is not None)
            else:
                _pgate = _cli_gate(config.home, config.approval_timeout_seconds)
            from ..interface.messaging import MessagingConfig as _PMC
            _pmcfg = _PMC.from_env()
            _prunner = GoalRunner(client, _psandbox, _pgate, config.home, memory=memory, conversation_id="practice:" + schedule["name"], plugins=_pplugins, execution_backend=_pbackend, budget=_PB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_PT(config.home, conversation_id="practice:" + schedule["name"]), model_verifier=_PMV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_pbackend, _psandbox),
                                  extra_tools=practice_tools(
                                      _pdir, _pmcfg, owner_email=config.owner_email,
                                      gmail_client_id=_pos.environ.get("GMAIL_CLIENT_ID", ""),
                                      gmail_client_secret=_pos.environ.get("GMAIL_CLIENT_SECRET", ""),
                                      gmail_refresh_token=_pos.environ.get("GMAIL_REFRESH_TOKEN", "")),
                                  allowed_side_effect_tools=set(PRACTICE_TOOL_NAMES))

            def _p_run_goal(goal_text, spec):
                _report = _prunner.run(goal_text, spec=spec)
                return {"status": _report.status, "files": list(_report.files or []),
                        "error": _report.error or "",
                        "elapsed_seconds": getattr(_report, "elapsed_seconds", 0),
                        "tokens": _report.model_tokens_estimated,
                        "run_id": getattr(_prunner, "run_id", "") or "",
                        "dag": _report.dag_summary or {}}

            def _p_deliver(text, goal, result):
                print(text)
                try:
                    from ..interface.messaging import (MessagingConfig as _MC,
                                                        send_slack as _ss,
                                                        send_whatsapp as _sw)
                    _mcfg = _MC.from_env()
                    if _mcfg.slack_outbound_channel:
                        _ss(_mcfg, _mcfg.slack_outbound_channel, text)
                    if _mcfg.whatsapp_owner:
                        _sw(_mcfg, _mcfg.whatsapp_owner, text)
                except Exception as exc:
                    print(f"Practice report delivery failed: {exc}")

            summary = run_practice_session(schedule, _p_run_goal,
                                           datetime.now() + timedelta(minutes=60),
                                           home=config.home, on_report=_p_deliver,
                                           on_progress=lambda live: print(
                                               f"Practice live: {live['goal']} "
                                               f"(attempt {live['attempt']})")
                                           if live else None,
                                           preapprove=coerce_preapprove(
                                               schedule.get("preapprove", True)))
            if not summary["passed"]:
                summary["report"] = (
                    f"Practice session {schedule['name']}: no pass in this window "
                    f"({len(summary['attempts'])} attempts).")
            record_session(config.home, schedule["name"], summary)
            print(f"Practice session {schedule['name']}: "
                  f"{'passed: ' + summary['goal'] if summary['passed'] else 'no pass'} "
                  f"({len(summary['attempts'])} attempts)")
            return 0
        return 2
    if args.command == "sandbox":
        from ..capabilities.execution import make_backend
        try:
            backend = make_backend(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network, vm_host=config.vm_host, vm_user=config.vm_user or None, vm_sudo_password=config.vm_sudo_password or None, vm_use_sudo=config.vm_use_sudo, modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                    modal_memory_mb=config.modal_memory_mb)
            print(f"Sandbox backend: {config.execution_backend}, workdir: {sandbox.workdir}, backend: {backend}")
            return 0
        except Exception as exc:
            print(f"Sandbox check failed, fail closed: {exc}"); return 2
    if args.command == "chain":
        from ..capabilities.chain import load_chain_spec, run_chain, validate_chain_spec
        from ..capabilities.tools import ToolRegistry
        try:
            spec = load_chain_spec(args.path)
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"Chain spec error: {exc}")
            return 2
        if args.action == "validate":
            failures = validate_chain_spec(spec)
            print(f"Chain valid: {not failures}, failures: {failures}")
            return 0 if not failures else 2
        if args.action == "run":
            result = run_chain(spec, ToolRegistry(sandbox.workdir))
            print(result)
            return 0 if result.get("ok") else 2
        return 2
    if args.command == "chat":
        from ..interface.repl import Repl
        from .model_client import Message
        from ..agentic.think_effort import sampling_for
        messages = []

        def _chat_handler(text: str) -> str:
            messages.append(Message("user", text))
            try:
                reply = client.chat(messages, sampling=sampling_for("respond"))
            except TypeError:
                reply = client.chat(messages)
            messages.append(Message("assistant", str(reply)))
            return str(reply)

        def _remember(text: str) -> str:
            point = memory.remember(text, source="cli-command")
            return f"Remembered in long-term memory: {text} (id {point.id})"

        Repl(_chat_handler, title="Aidon Chat", on_clear=messages.clear,
             on_remember=_remember).run()
        return 0
    if args.command == "computer":
        from ..capabilities.computer_use import capture_screenshot, run_computer_use
        import json as _json
        if args.action == "screenshot":
            result = capture_screenshot(args.path)
            print(result)
            return 0 if result.get("ok") else 2
        if args.action == "run":
            from pathlib import Path
            actions = _json.loads(Path(args.path).read_text(encoding="utf-8"))
            if not isinstance(actions, list):
                actions = actions.get("actions", [])
            result = run_computer_use(actions, approve=lambda kind, detail: args.approve)
            print(result)
            return 0 if result.get("ok") else 2
        return 2

    if args.command == "report":
        from ..interface.report import autonomy_report, daily_report
        if args.action == "daily":
            print(daily_report(config.home))
        else:
            print(autonomy_report(config.home))
        return 0
    if args.command == "demo":
        if args.action == "learn":
            # Real counts, not canned benchmark numbers: experience points and last run.
            try:
                points = memory.list_points("experience", limit=1000)
                print(f"Experience points: {len(points)}")
            except Exception as exc:
                print(f"Experience points: error {exc}")
            from pathlib import Path as _P
            last = _P(config.home) / "learned" / "last_run.json"
            print(f"Last run file: {last} exists={last.is_file()}")
            if last.is_file():
                print(last.read_text()[:500])
            return 0
        if args.action == "invent-tool":
            if not args.name:
                print("demo invent-tool needs --name"); return 2
            from pathlib import Path as _P
            from ..capabilities.plugins import scan_plugin
            draft_dir = _P(config.home) / "plugins_draft" / args.name
            draft_dir.mkdir(parents=True, exist_ok=True)
            (draft_dir / "tool.py").write_text(f'"""Draft plugin {args.name}, generated by demo invent-tool, not adopted yet."""\n\ndef {args.name}_tool(text: str) -> str:\n    return text\n', encoding="utf-8")
            (draft_dir / "skill.md").write_text(f"# {args.name}\n\n{args.description or 'Draft skill, adoption still needs the gate.'}\n", encoding="utf-8")
            (draft_dir / "manifest.json").write_text(f'{{"name": "{args.name}", "version": "0.1.0", "description": "{args.description or "draft"}"}}', encoding="utf-8")
            findings = scan_plugin(draft_dir)
            print(f"Draft plugin at {draft_dir}, scan findings: {len(findings)}")
            for finding in findings:
                print(f"  {finding.type} {finding.path}:{finding.line} {finding.message}")
            print("Not adopted, adoption still needs the plugin gate and your approval.")
            return 0
        return 2
    if args.command == "slack":
        from ..interface.messaging import MessagingConfig, poll_slack_once
        import os as _os
        cfg = MessagingConfig.from_env()
        if args.action == "status":
            print(f"Slack configured: {cfg.slack_configured()}, channel env set: {bool(_os.environ.get('AIDON_SLACK_INBOUND_CHANNEL'))}")
            return 0
        if args.action == "listen":
            if not cfg.slack_configured():
                print("Slack not configured, no token, nothing polled"); return 2
            channel = args.channel or _os.environ.get("AIDON_SLACK_INBOUND_CHANNEL", "")
            if not channel:
                print("Slack listen needs --channel or AIDON_SLACK_INBOUND_CHANNEL"); return 2
            import time as _time
            last_ts = ""
            end = _time.time() + args.seconds if args.seconds else _time.time()
            while True:
                messages, last_ts = poll_slack_once(cfg, channel, last_ts=last_ts)
                for message in messages:
                    print(f"Slack message: {message.get('text', '')[:200]}")
                    if args.run:
                        from ..agentic.budgets import Budget as _SB
                        from ..agentic.trace import TraceRecorder as _ST
                        from ..agentic.verifier_model import ModelVerifier as _SMV
                        from ..capabilities.plugins import PluginRegistry as _SPR
                        _splugins = _SPR(config.home); _splugins.load_all()
                        try:
                            from ..capabilities.execution import make_backend as _smb
                            _sbackend = _smb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
                        except Exception:
                            _sbackend = None
                        _srunner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id="slack:" + channel, plugins=_splugins, execution_backend=_sbackend, budget=_SB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_ST(config.home, conversation_id="slack:" + channel), model_verifier=_SMV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_sbackend, sandbox))
                        _sreport = _srunner.run(message.get("text", ""))
                        print(f"Slack run: {_sreport.status}, files: {', '.join(_sreport.files)}")
                if not args.seconds or _time.time() >= end:
                    break
                _time.sleep(10)
            return 0
        return 2
    if args.command == "whatsapp":
        from ..interface.messaging import MessagingConfig
        from ..interface.whatsapp import send_whatsapp_message
        cfg = MessagingConfig.from_env()
        if args.action == "status":
            print(f"WhatsApp configured: {cfg.whatsapp_configured()}, missing: {cfg.whatsapp_missing()}")
            return 0
        if args.action == "send":
            if not args.to or not args.text:
                print("whatsapp send needs --to and --text"); return 2
            result = send_whatsapp_message(cfg, args.to, args.text)
            print(result)
            return 0 if result.get("ok") else 2
        return 2
    if args.command == "tick":
        from datetime import datetime
        from ..interface.triggers import Scheduler
        scheduler = Scheduler(config.home)
        due = scheduler.check_due(datetime.now())
        if not due:
            print("Tick: no jobs due")
            return 0
        for job in due:
            print(f"Tick due job: {job.name}: {job.goal}")
            # Run via GoalRunner, same construction as scheduler serve, without semantic router.
            from ..agentic.budgets import Budget as _B
            from ..agentic.trace import TraceRecorder as _T
            from ..agentic.verifier_model import ModelVerifier as _MV
            from ..capabilities.plugins import PluginRegistry as _PR
            _plugins = _PR(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb
                _backend = _mb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            from ..interface.run_control import RunControl as _TickControl
            import uuid as _tick_uuid
            _tick_run_id = _tick_uuid.uuid4().hex[:12]
            _tick_control = _TickControl(config.home)
            _tick_session = getattr(job, "conversation_id", "") or getattr(job, "project_id", "") or "scheduled"
            _tick_control.register(_tick_run_id, job.goal, _tick_session)
            print(f"Tick job {job.name}: run id {_tick_run_id}")
            runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds, _tick_session), config.home, memory=memory, conversation_id=_tick_session, plugins=_plugins, execution_backend=_backend, budget=_B(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T(config.home, conversation_id=_tick_session), model_verifier=_MV(client), kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox), run_id=_tick_run_id, run_control=_tick_control)
            report = runner.run(job.goal)
            print(f"Tick job {job.name}: {report.status}, files: {', '.join(report.files)}")
        return 0
    if args.command == "repl":
        from ..interface.repl import Repl
        _cli_think_state = {"on": False}
        _cli_research_state = {"on": False}
        _cli_mode_state = {"mode": "ask"}

        def _repl_handler(goal: str) -> str:
            from ..agentic.budgets import Budget as _B2
            from ..agentic.trace import TraceRecorder as _T2
            from ..agentic.verifier_model import ModelVerifier as _MV2
            from ..capabilities.plugins import PluginRegistry as _PR2
            _plugins = _PR2(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb2
                _backend = _mb2(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            from ..interface.surfaces import parse_think_command as _cli_think
            from ..interface.surfaces import parse_research_command as _cli_research
            _cli_cmd = _cli_think(goal)
            _cli_once = False
            if _cli_cmd is not None and _cli_cmd["action"] == "once":
                goal = _cli_cmd["goal"]
                _cli_once = True
            _cli_rcmd = _cli_research(goal)
            _cli_research_once = False
            if _cli_rcmd is not None:
                if _cli_rcmd["action"] == "toggle":
                    _cli_research_state["on"] = not _cli_research_state["on"]
                    return (f"Deep-research mode {'on' if _cli_research_state['on'] else 'off'} "
                            "for this REPL session.")
                goal = _cli_rcmd["goal"]
                _cli_research_once = True
            from ..interface.surfaces import parse_mode_command as _cli_mode
            from ..interface.surfaces import parse_proceed_command as _cli_proceed
            _cli_mode_once = None
            _cli_mcmd = _cli_mode(goal)
            if _cli_mcmd is not None:
                if _cli_mcmd["action"] == "set":
                    _cli_mode_state["mode"] = _cli_mcmd["mode"]
                    if _cli_mcmd["mode"] == "execute":
                        return "Execute mode on for this REPL session — goals run directly."
                    return f"{_cli_mcmd['mode'].capitalize()} mode on for this REPL session."
                goal = _cli_mcmd["goal"]
                _cli_mode_once = _cli_mcmd["mode"]
            _cli_resume_plan = None
            _cli_resume_spec = None
            _cli_resume_stop_id = ""
            _cli_pcmd = _cli_proceed(goal)
            if _cli_pcmd is not None:
                from ..interface.mode_stops import load_mode_stop as _cli_load_stop
                from ..interface.mode_stops import latest_mode_stop as _cli_latest_stop
                _stop = (_cli_load_stop(config.home, _cli_pcmd["stop_id"])
                         if _cli_pcmd["stop_id"]
                         else _cli_latest_stop(config.home, "cli"))
                if _stop is None:
                    if _cli_pcmd["stop_id"]:
                        return f"No stopped run with id {_cli_pcmd['stop_id']}."
                    return "No stopped run to proceed with."
                goal = _stop["goal"]
                _cli_mode_once = "execute"
                _cli_resume_stop_id = _stop["stop_id"]
                if _stop["mode"] == "plan" and isinstance(_stop.get("plan"), dict) and _stop["plan"].get("steps"):
                    from ..agentic.loop import Plan as _CliPlan
                    _rp = _stop["plan"]
                    _cli_resume_plan = _CliPlan(
                        steps=_rp.get("steps", []),
                        deliverables=[str(d) for d in _rp.get("deliverables", [])],
                        criteria=[str(c) for c in _rp.get("criteria", [])])
                elif _stop["mode"] == "ask" and isinstance(_stop.get("spec"), dict):
                    from ..agentic.interview import Spec as _CliSpec
                    _cli_resume_spec = _CliSpec.from_dict(_stop["spec"])
            try:
                from ..agentic.clef_router import build_router as _cli_build_router
                _cli_router = _cli_build_router(
                    config, memory.embedder,
                    config.home / "semantic_router_vectors.json")
            except Exception:
                _cli_router = None
            _cli_clef_client = None
            try:
                if getattr(config, "clef_url", ""):
                    from ..agentic.clef_client import ClefClient as _CliClefClient
                    _cli_clef_client = _CliClefClient(
                        config.clef_url,
                        model=getattr(config, "clef_model", "") or "Cloudflare/clef-flash",
                        key=getattr(config, "clef_key", "") or "",
                        secret=getattr(config, "clef_secret", "") or "")
            except Exception:
                _cli_clef_client = None
            runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id="cli", plugins=_plugins, execution_backend=_backend, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, conversation_id="cli"), model_verifier=_MV2(client), kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox), think_max=_cli_once or _cli_think_state["on"], research_mode=_cli_research_once or _cli_research_state["on"], deep_researcher=make_deep_researcher(config), mode=_cli_mode_once or _cli_mode_state["mode"], question_ask=lambda q: input(f"\nAidon: {q}\nYou: "), conversation_key="cli", resume_plan=_cli_resume_plan, semantic_router=_cli_router, clef_client=_cli_clef_client)
            if _cli_resume_spec is not None:
                report = runner.run(goal, spec=_cli_resume_spec)
            else:
                report = runner.run(goal)
            if _cli_resume_stop_id and report.status == "done":
                try:
                    from ..interface.mode_stops import delete_mode_stop as _cli_del_stop
                    _cli_del_stop(config.home, _cli_resume_stop_id)
                except Exception:
                    pass
            if report.status == "chat":
                return report.chat_reply or "Received."
            if report.status == "stopped":
                _sid = (report.mode_stop or {}).get("stop_id", "")
                _smode = (report.mode_stop or {}).get("mode", "") or "run"
                return f"[stopped] {_smode} ready — run /proceed {_sid} to continue."
            return f"[{report.status}] {', '.join(report.files)} {report.error}".strip()
        def _remember(text: str) -> str:
            point = memory.remember(text, source="cli-command")
            return f"Remembered in long-term memory: {text} (id {point.id})"

        def _on_think() -> str:
            _cli_think_state["on"] = not _cli_think_state["on"]
            return ("Maximum thinking on for this REPL session."
                    if _cli_think_state["on"]
                    else "Maximum thinking off for this REPL session.")

        Repl(_repl_handler, on_remember=_remember, on_think=_on_think).run()
        return 0
    if args.command == "webui":
        from ..interface.messaging import MessagingConfig, send_slack, send_whatsapp
        from ..interface.triggers import TriggerDispatcher, TriggerEvent
        from ..interface.v1 import AuthStore
        from ..interface.webui import WebUIServer
        msg = MessagingConfig.from_env()
        # Override from Config, which already read the old names.
        msg.whatsapp_token = config.whatsapp_token or msg.whatsapp_token
        msg.whatsapp_phone_id = config.whatsapp_phone_id or msg.whatsapp_phone_id
        msg.whatsapp_verify_token = config.whatsapp_verify_token or msg.whatsapp_verify_token
        msg.whatsapp_app_secret = config.whatsapp_app_secret or msg.whatsapp_app_secret
        msg.whatsapp_owner = config.whatsapp_owner or msg.whatsapp_owner
        msg.slack_bot_token = config.slack_bot_token or msg.slack_bot_token
        msg.slack_signing_secret = config.slack_signing_secret or msg.slack_signing_secret
        msg.slack_outbound_channel = (config.slack_outbound_channel
                                      or msg.slack_outbound_channel)
        dispatcher = TriggerDispatcher()
        # Goal handler, wired here, not echo: WebUI chat,
        # WhatsApp, and Slack all run the same GoalRunner,
        # with the WebUI approval gate, so a card in the
        # browser, or approve <id> on a message, resolves
        # a run that started on any surface.

        def whatsapp_sender(to: str, text: str):
            result = send_whatsapp(msg, to, text)
            if not result["ok"]:
                print(f"WhatsApp send failed: {result['error']}")
            return result

        def slack_sender(channel: str, text: str, thread_ts=None):
            result = send_slack(msg, channel, text, thread_ts=thread_ts)
            if not result["ok"]:
                print(f"Slack send failed: {result['error']}")
            return result

        from ..interface.messaging import download_whatsapp_media
        from ..interface.run_control import RunControl
        run_control = RunControl(config.home)
        server = WebUIServer(config.home, dispatcher=dispatcher,
                             auth_store=AuthStore(config.home), memory=memory,
                             messaging_config=msg,
                             whatsapp_sender=whatsapp_sender,
                             slack_sender=slack_sender,
                             webhook_only=args.webhook_only,
                             media_downloader=lambda media_id: download_whatsapp_media(msg, media_id),
                             run_control=run_control,
                             webui_token=config.webui_token or None,
                             transcribe_key=config.transcribe_api_key or config.api_key or None,
                             tts_key=config.fish_api_key or config.api_key or None,
                             config=config)
        server.voice_announce = config.voice_announce

        def _goal_handler(event):
            from ..agentic.loop import GoalRunner, format_run_stats
            from ..interface.run_control import new_run_id
            from ..agentic.budgets import Budget
            from ..agentic.trace import TraceRecorder
            from ..agentic.verifier_model import ModelVerifier
            from ..capabilities.plugins import PluginRegistry
            from ..agentic.semantic_router import SemanticRouter
            from ..interface.scheduler_loop import NudgeTracker
            run_id = str(event.payload.get("run_id", "")) or new_run_id()
            run_control.register(run_id, event.goal, event.conversation_id)
            # Each goal gets its own timestamped workspace directory. This
            # prevents unrelated runs from colliding on filenames and keeps
            # the user-visible artifact path contextual instead of exposing
            # a generic ``workdir`` directory.
            run_sandbox = _run_sandbox(config, run_id)
            # Full runner, same as the CLI goal run, not a thin runner:
            # budget, trace, model verifier, plugins, execution backend,
            # semantic router skip, and autonomy plus nudge trackers
            # on the WebUI gate, so a WebUI, WhatsApp, or Slack run
            # is the same run as a CLI run, with the same checks.
            server.webui_gate.nudge_tracker = NudgeTracker(
                nudge_minutes=config.approval_nudge_minutes,
                channel=config.approval_nudge_channel)
            from ..interface.autonomy import policy_for
            server.webui_gate.gate.policy = policy_for(
                config.home,
                threshold=config.autonomy_threshold,
                decay_days=config.autonomy_decay_days,
            )
            server.webui_gate.autonomy_tracker = None
            server.webui_gate.slack_sender = slack_sender
            server.webui_gate.whatsapp_sender = whatsapp_sender
            plugins = PluginRegistry(config.home)
            plugins.load_all()
            budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                            max_seconds=config.budget_seconds)
            trace = TraceRecorder(config.home, conversation_id=event.conversation_id)
            try:
                from ..capabilities.execution import make_backend
                execution_backend = make_backend(
                    config.execution_backend, run_sandbox,
                    image=config.docker_image, allow_network=config.docker_network,
                    vm_host=config.vm_host, vm_user=config.vm_user or None,
                    vm_sudo_password=config.vm_sudo_password or None,
                    vm_use_sudo=config.vm_use_sudo,
                    modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                    modal_memory_mb=config.modal_memory_mb)
            except Exception:
                execution_backend = None
            try:
                from ..agentic.clef_router import build_router as _build_router
                semantic_router = _build_router(
                    config, memory.embedder,
                    config.home / 'semantic_router_vectors.json')
            except Exception:
                semantic_router = None
            # Clef decision client for capability triage in run().
            # None when no URL is configured: triage is skipped and
            # the run behaves exactly as before.
            _clef_client = None
            try:
                if getattr(config, "clef_url", ""):
                    from ..agentic.clef_client import ClefClient as _ClefClient
                    _clef_client = _ClefClient(
                        config.clef_url,
                        model=getattr(config, "clef_model", "") or "Cloudflare/clef-flash",
                        key=getattr(config, "clef_key", "") or "",
                        secret=getattr(config, "clef_secret", "") or "")
            except Exception:
                _clef_client = None
            conversation_context = ""
            conversation_id = str(event.payload.get("conversation_id", "") or "")
            if conversation_id:
                try:
                    history = server._conversations_db().history(conversation_id, limit=8)
                    conversation_context = "\n".join(
                        f"{item.get('role', 'user')}: {str(item.get('text', ''))[:1200]}"
                        for item in history[:-1]
                    )
                except Exception:
                    conversation_context = ""
            try:
                from ..interface.preapproval import PreapprovalStore
                from ..interface.preapproval_gate import PreapprovalGate
                _gate = PreapprovalGate(server.webui_gate.gate, PreapprovalStore(config.home), event.conversation_id)
            except Exception:
                _gate = server.webui_gate.gate
            _web_researcher = _make_web_researcher(config, conversation_context)
            # Slack/WhatsApp runs have no WebUI waiter callback in the
            # payload, so build one: send the question straight to the
            # surface (not via the reply list, which is only flushed
            # after the run ends), then block in the shared waiter
            # until the owner's next message in that channel arrives
            # as the answer. Without a channel/sender key there is no
            # waiter, and ask_user says so honestly.
            _question_callback = None
            _qkey = ""
            if event.source == "slack":
                _qkey = "slack:" + str(event.payload.get("channel", ""))
            elif event.source == "whatsapp":
                _qkey = "whatsapp:" + str(event.payload.get("from", ""))
            if _qkey and not _qkey.endswith(":"):
                def _question_callback(question, _key=_qkey):
                    from ..interface.surfaces import waiter_keys_for_slack
                    also: list[str] = []
                    if event.source == "slack":
                        if slack_sender is not None:
                            try:
                                slack_sender(
                                    str(event.payload.get("channel", "")),
                                    str(question),
                                    event.payload.get("thread_ts"))
                            except Exception:
                                pass
                        # The thread is visible in WebUI under
                        # slack:<channel>, question included.
                        try:
                            server._conversations_db().record_exchange(
                                "slack:" + str(event.payload.get("channel", "")),
                                "assistant", str(question))
                        except Exception:
                            pass
                        also = waiter_keys_for_slack(
                            config.home, str(event.payload.get("channel", "")),
                            str(msg.whatsapp_owner or ""))
                        # A linked WebUI conversation shows the
                        # question, so the owner can answer there.
                        for key in also:
                            if key.startswith("whatsapp:"):
                                continue
                            try:
                                server._conversations_db().record_exchange(
                                    key, "assistant", str(question))
                            except Exception:
                                pass
                        for key in also:
                            if key.startswith("whatsapp:") and whatsapp_sender is not None:
                                try:
                                    whatsapp_sender(key[len("whatsapp:"):], str(question))
                                except Exception:
                                    pass
                    elif event.source == "whatsapp" and whatsapp_sender is not None:
                        try:
                            whatsapp_sender(
                                str(event.payload.get("from", "")),
                                str(question))
                        except Exception:
                            pass
                    return server.ask_question(run_id, _key, question, also_keys=tuple(also))
            def _say(prompt):
                from ..system.model_client import Message as _Message
                messages = [_Message("user", prompt)]
                if model_router is not None:
                    try:
                        return model_router.chat("respond", messages)
                    except TypeError:
                        return model_router.chat(messages)
                return client.chat(messages)

            def _phrase(kind):
                from ..interface.phrasing import phrase_with_model as _pwm
                return _pwm(kind, _say)

            # Approval cards carry this run's context so the
            # WebUI gate can deliver a copy to WhatsApp when
            # the run warrants one: a run that started on
            # WhatsApp, or a WebUI/Slack conversation the
            # owner mirrored to WhatsApp. Mirroring is the
            # opt-in; an unmirrored run gets no WhatsApp card.
            _whatsapp_to = None
            if event.source == "whatsapp":
                _whatsapp_to = str(event.payload.get("from", "") or "")
            elif msg.whatsapp_owner:
                from ..interface.surfaces import (
                    whatsapp_mirrored_for_conversation,
                    whatsapp_mirrored_for_slack)
                if event.source == "webui" and conversation_id and \
                        whatsapp_mirrored_for_conversation(
                            config.home, conversation_id):
                    _whatsapp_to = msg.whatsapp_owner
                elif event.source == "slack" and \
                        whatsapp_mirrored_for_slack(
                            config.home,
                            str(event.payload.get("channel", ""))):
                    _whatsapp_to = msg.whatsapp_owner
            _gate_context = {"run_id": run_id,
                             "conversation_id": conversation_id,
                             "source": event.source}
            if _whatsapp_to:
                _gate_context["whatsapp_to"] = _whatsapp_to

            # Mode resume + conversation key for the stop record.
            _conversation_key = ""
            if event.source == "webui":
                _conversation_key = str(event.payload.get("conversation_id", "") or "")
            elif event.source == "slack":
                _conversation_key = "slack:" + str(event.payload.get("channel", "") or "")
            elif event.source == "whatsapp":
                _conversation_key = "whatsapp:" + str(event.payload.get("from", "") or "")
            _resume_stop_id = str(event.payload.get("resume_stop_id", "") or "")
            _resume_plan = None
            _resume_spec = None
            if isinstance(event.payload.get("resume_plan"), dict):
                _rp = event.payload["resume_plan"]
                if _rp.get("steps"):
                    from ..agentic.loop import Plan as _ResumePlan
                    _resume_plan = _ResumePlan(
                        steps=_rp.get("steps", []),
                        deliverables=[str(d) for d in _rp.get("deliverables", [])],
                        criteria=[str(c) for c in _rp.get("criteria", [])])
            if isinstance(event.payload.get("resume_spec"), dict):
                from ..agentic.interview import Spec as _ResumeSpec
                _resume_spec = _ResumeSpec.from_dict(event.payload["resume_spec"])
            _governor = _make_governor(config)
            runner = GoalRunner(client, run_sandbox, _gate,
                                config.home, memory=memory,
                                conversation_id=event.conversation_id, plugins=plugins,
                                execution_backend=execution_backend, budget=budget,
                                trace=trace, model_verifier=ModelVerifier(client),
                                kernel_mode=config.kernel_mode,
                                semantic_router=semantic_router,
                                clef_client=_clef_client,
                                use_semantic_router_skip=True,
                                run_id=run_id, run_control=run_control,
                                researcher=_web_researcher, extra_tools=_goal_extra_tools(config, run_sandbox.workdir, gate=_gate, researcher=_web_researcher, question_answer=event.payload.get("_question_callback") or _question_callback, phrase=_phrase, model_chat=lambda messages: client.chat(messages), governor=_governor), router=model_router, versioned_writes=True, network_available=_network_available(execution_backend, run_sandbox), image_paths=event.payload.get("image_paths") or [], force_web=bool(event.payload.get("force_web")), think_max=bool(event.payload.get("think_max")), research_mode=bool(event.payload.get("research_mode")), deep_researcher=make_deep_researcher(config), stream_callback=event.payload.get("_stream_callback"), conversation_context=conversation_context, route_callback=event.payload.get("_route_callback"), trace_callback=event.payload.get("_trace_callback"), gate_context=_gate_context, mode=event.payload.get("mode"), question_ask=event.payload.get("_question_callback") or _question_callback, conversation_key=_conversation_key, resume_plan=_resume_plan, acquisition_governor=_governor)
            def _deliver_files(files, target):
                """Upload run deliverables as bytes to a Slack/WhatsApp target.

                target is ("slack", channel) or ("whatsapp", number).
                Slack and WhatsApp cannot open localhost file links,
                so a run's files reach those surfaces as uploads,
                not links. Paths are confined to the run workdir;
                anything outside it, or not a file, is skipped.
                Failures are reported as a reply, never silent.
                """
                if not files or not target:
                    return
                from pathlib import Path as _P
                root = _P(run_sandbox.workdir).resolve()
                kind, recipient = target
                if not recipient:
                    return
                for name in files:
                    candidate = (_P(name) if _P(name).is_absolute()
                                 else root / str(name))
                    try:
                        resolved = candidate.resolve()
                    except Exception:
                        continue
                    if resolved != root and root not in resolved.parents:
                        continue
                    if not resolved.is_file():
                        continue
                    try:
                        from ..interface.messaging import (
                            send_slack_file as _ssf, send_whatsapp_media as _swm)
                        if kind == "slack":
                            result = _ssf(msg, recipient, resolved)
                        else:
                            result = _swm(msg, recipient, resolved)
                        if not result.get("ok"):
                            event.send_reply(
                                f"File delivery failed for {resolved.name}: "
                                f"{result.get('error', 'unknown error')}")
                    except Exception as exc:
                        event.send_reply(
                            f"File delivery failed for {resolved.name}: {exc}")

            def _surface_reply(text, files=None):
                # The WebUI already displays and polls the run ID. Keep the
                # ID in Slack/WhatsApp replies where it is needed for
                # cross-surface control commands, but do not show it in chat.
                rendered = str(text) if event.source == "webui" \
                    else f"[{run_id}] {text}"
                event.send_reply(rendered)
                # Unified surfaces: a linked surface gets the same
                # result, so WebUI and Slack stay in sync no matter
                # where the run started. WhatsApp only when mirrored
                # (or specified via result delivery, below).
                try:
                    from ..interface.surfaces import (
                        conversation_for_slack, slack_for_conversation,
                        whatsapp_mirrored_for_conversation,
                        whatsapp_mirrored_for_slack)
                    _cid = str(event.payload.get("conversation_id", "") or "")
                    _slack_ch = (slack_for_conversation(config.home, _cid)
                                 if _cid else None)
                    if _slack_ch and event.source != "slack" and slack_sender is not None:
                        slack_sender(_slack_ch, rendered)
                    if event.source == "slack":
                        _linked = conversation_for_slack(
                            config.home, str(event.payload.get("channel", "")))
                        if _linked:
                            try:
                                server._conversations_db().record_exchange(
                                    _linked, "assistant", rendered)
                            except Exception:
                                pass
                    _wa_mirrored = False
                    if _cid:
                        _wa_mirrored = whatsapp_mirrored_for_conversation(config.home, _cid)
                    elif event.source == "slack":
                        _wa_mirrored = whatsapp_mirrored_for_slack(
                            config.home, str(event.payload.get("channel", "")))
                    if (_wa_mirrored and event.source != "whatsapp"
                            and whatsapp_sender is not None and msg.whatsapp_owner):
                        whatsapp_sender(msg.whatsapp_owner, rendered)
                except Exception:
                    pass
                delivery = event.payload.get("result_delivery") or {}
                channel = str(delivery.get("channel", "")).lower()
                if not channel or event.source != "webui":
                    return
                if channel == "whatsapp":
                    recipient = msg.whatsapp_owner
                    result = whatsapp_sender(recipient, rendered) if recipient else {
                        "ok": False, "error": "AIDON_WHATSAPP_OWNER is not configured"}
                elif channel == "slack":
                    recipient = msg.slack_outbound_channel
                    result = slack_sender(recipient, rendered) if recipient else {
                        "ok": False, "error": "AIDON_SLACK_OUTBOUND_CHANNEL is not configured"}
                else:
                    result = {"ok": False, "error": "unsupported result channel"}
                if not result.get("ok"):
                    event.send_reply(f"Result delivery to {channel} failed: "
                                     f"{result.get('error', 'unknown error')}")
                else:
                    # A delivered result carries its files as bytes
                    # too — the phone cannot open a localhost link.
                    _deliver_files(files, (channel, recipient))
            # WebUI goals run once. Swarm remains an explicit CLI option;
            # automatically splitting ordinary chat made conversational
            # questions appear multiple times and multiplied model calls.
            if _resume_spec is not None:
                report = runner.run(event.goal, spec=_resume_spec)
            else:
                report = runner.run(event.goal)
            route_callback = event.payload.get("_route_callback")
            if callable(route_callback):
                route_callback(runner.last_route)
            run_control.set_status(run_id, report.status)
            # A stopped run hands the frontend its Proceed button
            # through the run record (the poll returns the record),
            # and a finished proceed consumes the stop record so a
            # failed proceed can still be retried.
            if report.status == "stopped" and report.mode_stop:
                try:
                    from ..interface.mode_stops import enrich_mode_stop
                    enriched = enrich_mode_stop(config.home, report.mode_stop)
                except Exception:
                    enriched = dict(report.mode_stop)
                try:
                    if run_id in server._web_runs:
                        server._web_runs[run_id]["mode_stop"] = enriched
                except Exception:
                    pass
            if _resume_stop_id and report.status == "done":
                try:
                    from ..interface.mode_stops import delete_mode_stop
                    delete_mode_stop(config.home, _resume_stop_id)
                except Exception:
                    pass
            if report.status == "stopped":
                _stopped = report.mode_stop or {}
                _sid = str(_stopped.get("stop_id", "") or "")
                if _stopped.get("mode") == "ask":
                    _shead = "Spec ready — the interview is done."
                else:
                    _delivs = _stopped.get("deliverables") or []
                    _shead = ("Plan ready" + (f" ({len(_delivs)} deliverable(s): "
                                              + ", ".join(str(d) for d in _delivs[:5])
                                              + ")" if _delivs else "") + ".")
                if event.source == "webui":
                    _saction = "Tap Proceed below to continue."
                elif event.source in ("slack", "whatsapp"):
                    _saction = f"Reply proceed {_sid} to continue."
                else:
                    _saction = f"Run /proceed {_sid} to continue."
                _surface_reply(f"{_shead}\n{_saction}\n" + format_run_stats(report))
            elif report.status == "chat":
                _surface_reply((report.chat_reply or "Received.") + "\n" + format_run_stats(report))
            else:
                from ..interface.phrasing import (
                    phrase_with_model as _pwm, report_text as _report_text)
                _phrase = (_pwm("report-" + report.status, _say)
                           if report.status in ("done", "failed", "rejected",
                                                "cancelled") else None)
                _surface_reply(_report_text(
                    report.status, report.files, report.error, run_id,
                    format_run_stats(report), phrase=_phrase,
                    blocked=report.blocked), files=report.files)
            # A run that started on Slack or WhatsApp gets its
            # deliverables as uploads on that same surface, after
            # the text report above — localhost links are dead
            # from a phone.
            if report.status == "done" and report.files:
                if event.source == "slack":
                    _deliver_files(report.files,
                                   ("slack", str(event.payload.get("channel", ""))))
                elif event.source == "whatsapp":
                    _deliver_files(report.files,
                                   ("whatsapp", str(event.payload.get("from", ""))))

        dispatcher.register("webui", _goal_handler)
        dispatcher.register("whatsapp", _goal_handler)
        dispatcher.register("slack", _goal_handler)
        # Preserve the old ``launch.sh --both`` behavior: the full WebUI
        # can poll Slack when explicitly enabled, while the separate
        # webhook-only process does not start a duplicate poller.
        import os as _slack_os
        if (_slack_os.environ.get("AIDON_SLACK_AUTOSTART", "0") == "1"
                and msg.slack_bot_token):
            from ..interface.messaging import poll_slack_once as _poll_slack_once
            _channels = [c.strip() for c in _slack_os.environ.get(
                "AIDON_SLACK_INBOUND_CHANNEL", "").split(",") if c.strip()]
            if _channels:
                _poll_seconds = max(3, int(_slack_os.environ.get(
                    "AIDON_SLACK_POLL_SECONDS", "10")))
                _ack_after = max(0, int(_slack_os.environ.get(
                    "AIDON_SLACK_ACK_AFTER_SECONDS", "15")))
                _cursor_path = config.home / "slack_poll_cursors.json"

                def _slack_poll_loop():
                    import json as _slack_json
                    import time as _slack_time
                    try:
                        _cursors = _slack_json.loads(_cursor_path.read_text()) \
                            if _cursor_path.is_file() else {}
                    except (OSError, ValueError):
                        _cursors = {}
                    while True:
                        for _channel in _channels:
                            try:
                                _messages, _last = _poll_slack_once(
                                    msg, _channel,
                                    last_ts=str(_cursors.get(_channel, "")))
                                if _last:
                                    _cursors[_channel] = _last
                                    _cursor_path.write_text(
                                        _slack_json.dumps(_cursors, indent=2))
                                for _message in _messages:
                                    _text = _message.get("text", "")
                                    _replies = []
                                    _decision = server._resolve_decision(_text)
                                    if _decision is None:
                                        _decision = server._resolve_control(_text)
                                    if _decision is not None:
                                        _replies.append(_decision)
                                    else:
                                        _event = TriggerEvent(
                                            source="slack", kind="message",
                                            goal=_text, conversation_id="slack:" + _channel,
                                            payload={"channel": _channel},
                                            reply=_replies.append)
                                        _run_thread = threading.Thread(
                                            target=dispatcher.dispatch,
                                            args=(_event,), daemon=True)
                                        _run_thread.start()
                                        _run_thread.join(_ack_after)
                                        if _run_thread.is_alive():
                                            slack_sender(
                                                _channel, "Working on it…",
                                                _message.get("ts"))
                                        _run_thread.join()
                                    for _reply in _replies:
                                        slack_sender(_channel, _reply,
                                                     _message.get("ts"))
                            except Exception as _exc:
                                print(f"Slack poll error: {_exc}")
                        _slack_time.sleep(_poll_seconds)

                _slack_thread = threading.Thread(
                    target=_slack_poll_loop, daemon=True, name="slack-inbound")
                _slack_thread.start()
                print("Slack inbound polling auto-started")
        missing = msg.whatsapp_missing()
        _startup_style("section", "Messaging")
        if missing:
            _startup_style("warn", f"WhatsApp incomplete; missing: {', '.join(missing)}")
        if not msg.whatsapp_owner:
            _startup_style("warn", "WhatsApp inbound disabled: AIDON_WHATSAPP_OWNER is not set (owner-only, fail-closed).")
        if not msg.whatsapp_app_secret:
            _startup_style("warn", "WhatsApp signature verification is off: AIDON_WHATSAPP_APP_SECRET is not set.")
        if not msg.slack_bot_token:
            _startup_style("warn", "Slack sending is not configured: SLACK_BOT_TOKEN is not set.")
        if not msg.slack_signing_secret:
            _startup_style("warn", "Slack webhook requests are rejected: AIDON_SLACK_SIGNING_SECRET is not set (fail-closed).")
        try:
            import importlib.util
            qdrant_available = importlib.util.find_spec("qdrant_client") is not None
        except Exception:
            qdrant_available = False
        if not qdrant_available:
            _startup_style("warn", "Qdrant client is not installed; using persistent SQLite memory fallback.")
        elif os.environ.get("AIDON_QDRANT_URL", "").strip():
            _startup_style("info", "Qdrant remote store configured; connection checked when memory is first used.")
        else:
            _startup_style("info", "Qdrant local store available under the Aidon home.")
        _startup_style("section", "WebUI")
        if args.webhook_only:
            _startup_style("ok", f"Webhook listener: http://127.0.0.1:{args.port}/ (WhatsApp and Slack only)")
        else:
            _startup_style("ok", f"WebUI: http://127.0.0.1:{args.port}/ (localhost only)")
            _startup_style("info", "Webhooks: /webhook/whatsapp and /webhook/slack")
            _startup_style("info", "External services need a tunnel; localhost is not publicly reachable.")
        server.serve(port=args.port)
        return 0
    if args.command == "v1":
        from ..interface.v1 import AuthStore, GoalStore, handle
        # Live v1 and MCP execute callback, wired here, not 501:
        # a v1 goal runs through the same GoalRunner as a CLI goal,
        # with the same plugins, budget, trace, verifier, and backend.
        # Approval is asked on the server console, via _cli_gate,
        # so a v1 run does not auto-approve live execution.
        def _v1_execute(goal_record):
            from ..agentic.budgets import Budget as _VB
            from ..agentic.trace import TraceRecorder as _VT
            from ..agentic.verifier_model import ModelVerifier as _VM
            from ..capabilities.plugins import PluginRegistry as _VP
            _plugins = _VP(config.home); _plugins.load_all()
            try:
                from ..capabilities.execution import make_backend as _vmb
                _backend = _vmb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _backend = None
            _runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home,
                                 memory=memory, conversation_id=goal_record.conversation_id, plugins=_plugins,
                                 execution_backend=_backend, budget=_VB(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                 trace=_VT(config.home, conversation_id=goal_record.conversation_id), model_verifier=_VM(client),
                                 kernel_mode=config.kernel_mode, network_available=_network_available(_backend, sandbox))
            _report = _runner.run(goal_record.goal)
            return {"status": _report.status, "files": _report.files, "error": _report.error}
        auth = AuthStore(config.home)
        goals = GoalStore(config.home)
        if args.action == "revoke":
            ok = auth.revoke(args.name)
            print(f"Revoked {args.name}" if ok else f"No such key {args.name}")
            return 0 if ok else 2
        if args.action == "keygen":
            key, record = auth.create_key(args.name, args.scope)
            print("Save this key now, it is shown once, only its hash is stored:")
            print(key)
            print(f"Name: {record.name}, scope: {record.scope}, hash prefix: {record.key_hash[:12]}")
            return 0
        if args.action == "mcp":
            from ..interface.mcp_server import MCPServer
            MCPServer(auth, goals, execute=_v1_execute).run_stdio()
            return 0
        if args.action == "mcp-http":
            # Streamable HTTP MCP: JSON-RPC over HTTP at /mcp,
            # Bearer token, same store and scopes as stdio MCP.
            from ..interface.mcp_server import MCPServer
            import json as _json2
            from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
            mcp_server = MCPServer(auth, goals, execute=_v1_execute)

            class _McpHandler(BaseHTTPRequestHandler):
                def do_POST(self):
                    if self.path != "/mcp":
                        self.send_response(404); self.end_headers(); return
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    raw = self.rfile.read(length) if length else b""
                    try:
                        body = _json2.loads(raw) if raw else {}
                    except Exception:
                        body = {}
                    auth_header = self.headers.get("Authorization", "")
                    token = auth_header[7:] if auth_header.startswith("Bearer ") else None
                    payload = mcp_server.handle(body, token)
                    data = _json2.dumps(payload).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

                def log_message(self, *args):
                    pass

            print(f"Streamable HTTP MCP on http://127.0.0.1:{args.port}/mcp, localhost only, Bearer <redacted> required except initialize and ping")
            ThreadingHTTPServer(("127.0.0.1", args.port), _McpHandler).serve_forever()
            return 0
        # serve: localhost only, stdlib HTTP, v1 handler only.
        import json as _json
        from http.server import BaseHTTPRequestHandler, HTTPServer

        class _Handler(BaseHTTPRequestHandler):
            def _do(self, method):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = _json.loads(raw) if raw else {}
                except Exception:
                    body = {}
                status, payload = handle(auth, goals, method, self.path,
                                         dict(self.headers), body, execute=_v1_execute)
                data = _json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def log_message(self, *args):
                pass

        print(f"v1 API on http://127.0.0.1:{args.port}/api/v1/, localhost only, Bearer token required except health")
        HTTPServer(("127.0.0.1", args.port), _Handler).serve_forever()
        return 0
    if args.command == "trigger":
        from datetime import datetime
        from ..interface.triggers import (ALLOWED_SOURCES, IngestWatcher, Scheduler,
                               TriggerDispatcher, handle_webhook)
        if args.action == "list":
            print("Trigger sources, all start a DAG run, none bypass approval:")
            for source in ALLOWED_SOURCES:
                print(f"  {source}")
            print("  No Telegram source, by owner decision.")
            return 0
        if args.action == "ingest":
            watcher = IngestWatcher(config.ingest_path, config.home)
            for event in watcher.events(conversation_id=args.conversation):
                print(f"ingest trigger: {event.payload['path']}")
            return 0
        if args.action == "schedule":
            scheduler = Scheduler(config.home)
            for job in scheduler.check_due(datetime.now()):
                print(f"scheduled trigger: {job.name}: {job.goal}")
            return 0
        if args.action == "webhook":
            if not args.name:
                print("trigger webhook needs a name")
                return 2
            event = handle_webhook(args.name, {"goal": args.goal},
                                   conversation_id=args.conversation)
            dispatcher = TriggerDispatcher()
            dispatcher.register("webhook", lambda e: print(f"webhook trigger: {e.kind}: {e.goal}"))
            dispatcher.dispatch(event)
            return 0
        return 2
    if args.command == "unity":
        from ..capabilities.unity import UnityAssetBuilder
        builder = UnityAssetBuilder(config.home / "unity")
        if args.action == "asset":
            result = builder.generate_asset(args.name, shape=args.shape, color=args.color)
            print(f"Unity asset: {result['name']}, shape {result['shape']}")
            print(f"OBJ: {result['obj']}")
            print(f"C# script: {result['script']}")
            print(f"Import: open the folder in Unity, the OBJ imports as a mesh, the C# script is a MonoBehaviour that colours it {args.color}.")
            print(f"Build command, if Unity is installed: {builder.build_command(result['project_dir'])}")
            return 0
        print(builder.build_command(config.home / "unity" / args.name))
        return 0
    if args.command == "image":
        from ..capabilities.image_gen import generate_image, edit_image, save_image
        from pathlib import Path as _Path
        if not config.api_key or not config.api_base:
            print("Image generation needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark, no separate image key.")
            return 2
        out = _Path(args.output) if args.output else config.home / "library" / "files" / "image.png"
        if args.action == "generate":
            data = generate_image(args.prompt, config.api_key, config.api_base, size=args.size, model=config.image_model)
        else:
            data = edit_image(args.prompt, config.api_key, config.api_base, reference_images=args.refs or [], size=args.size, model=config.image_model)
        path = save_image(data, out)
        print(f"Image saved: {path}, model {config.image_model}")
        return 0
    if args.command == "video":
        from ..capabilities.video import edit_video, extract_frames, encode_frames, ffmpeg_available
        if not ffmpeg_available():
            print("ffmpeg not found on PATH, install ffmpeg to run the video pipeline, no fake video is produced.")
            return 2
        if args.action == "extract":
            frames = extract_frames(args.input, args.output, fps=args.fps)
            print(f"Extracted {len(frames)} frames to {args.output}")
            return 0
        if args.action == "encode":
            from pathlib import Path as _P
            frames = sorted(_P(args.input).glob("frame*.jpg"))
            out = encode_frames(frames, args.output, fps=args.fps)
            print(f"Encoded video: {out}")
            return 0
        if not args.prompt:
            print("video edit needs --prompt, a noun phrase for SAM, for example --prompt 'red toolbox'")
            return 2
        if not config.api_key or not config.api_base:
            print("video edit needs SAM via AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient
        sam_client = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = edit_video(args.input, args.output, args.prompt, sam_client, fps=args.fps)
        if "error" in result:
            print(f"Video pipeline error: {result['error']}")
            return 1
        print(f"Video edited: {result['output']}, frames: {len(result['frames'])}, prompt: {result['prompt']}")
        print("Edited video uses SAM box overlays, not pixel-mask compositing, mask decoding needs the optional meta-sam-parser package, see src/video.py.")
        return 0
    if args.command == "sam":
        if not config.api_key or not config.api_base:
            print("SAM needs AIDON_API_KEY and AIDON_API_ENDPOINT in .env, same key and endpoint as Spark.")
            return 2
        from ..capabilities.sam import SAMClient, build_object_prompt
        client_sam = SAMClient(config.api_base, config.api_key, config.sam_model)
        result = client_sam.segment(args.images, build_object_prompt(args.prompt), propagation=args.propagation)
        if "error" in result:
            print(f"SAM error: {result['error']}")
            return 1
        for seg in result["segments"]:
            print(f"frame {seg['frame']} id {seg['object_id']} box {seg['box']} confidence {seg['confidence']}")
        print("SAM is the segment and track step only. Frame extraction and video re-encoding need ffmpeg, not built here.")
        return 0
    if args.command == "control":
        from ..interface.run_control import RunControl
        control = RunControl(config.home)
        if args.action == "list":
            for run in control.list_runs():
                print(f"{run['run_id']} {run['status']} {run['goal']} steer_notes={len(run.get('steer_notes', []))}")
            return 0
        if args.action == "cancel":
            ok = control.cancel(args.run_id) if args.run_id else False
            print(f"Cancelled {args.run_id}" if ok else f"No such run {args.run_id}")
            return 0 if ok else 2
        if args.action == "steer":
            ok = control.steer(args.run_id, args.note or "") if args.run_id else False
            print(f"Steered {args.run_id}: {args.note}" if ok else f"No such run {args.run_id}")
            return 0 if ok else 2
    if args.command == "scheduler":
        from ..interface.preapproval import (DEFAULT_HOURS, HELP_TEXT,
                                             IRREVERSIBLE, MAX_HOURS,
                                             PreapprovalStore, grantable_kind,
                                             kind_from_token)
        import time as _time
        store = PreapprovalStore(config.home)
        if args.action == "preapprove":
            # Same kinds and same refusal as the WebUI, WhatsApp, and
            # Slack surfaces: by name or by the menu number.
            raw_kind = args.kind or "sandbox"
            kind = kind_from_token(raw_kind)
            if kind is None:
                print(f"Unknown kind {raw_kind!r}.\n\n{HELP_TEXT}")
                return 2
            if kind not in IRREVERSIBLE and not grantable_kind(kind):
                print(f"Unknown kind {kind!r}.\n\n{HELP_TEXT}")
                return 2
            hours = args.hours if args.hours and args.hours > 0 else DEFAULT_HOURS
            hours = min(hours, MAX_HOURS)
            ok = store.grant(args.spec, kind,
                             _time.time() + hours * 3600, reversible=True)
            if ok:
                print(f"Pre-approved {kind} for {args.spec}, for the next {hours:g}h.")
                print("Irreversible steps (send, publish, spend, delete, install) still ask.")
                return 0
            print(f"Refused: {kind} can never be pre-approved, it is irreversible "
                  f"or leaves the machine.")
            return 2
        if args.action == "list":
            rows = store.active()
            if not rows:
                print("No pre-approvals are active.")
                return 0
            now = _time.time()
            for row in rows:
                left = max(0.0, (float(row["until"]) - now) / 3600.0)
                spec_id = row["spec_id"]
                kind_name = row["action_kind"]
                print(f"{spec_id} {kind_name} {left:.1f}h left")
            return 0
        if args.action == "revoke":
            kind = None
            if args.kind and args.kind.lower() not in ("all", "*"):
                kind = kind_from_token(args.kind)
                if kind is None:
                    print(f"Unknown kind {args.kind!r}.\n\n{HELP_TEXT}")
                    return 2
            removed = store.revoke(args.spec, kind)
            print(f"Revoked {removed} pre-approval(s) for {args.spec}."
                  if removed else f"Nothing was pre-approved for {args.spec}.")
            return 0 if removed else 2
        if args.action == "morning":
            for item in store.morning_report():
                print(f"{item['spec_id']} {item['action_kind']}: undo: {item['undo']}")
            return 0
        # serve: poll due jobs every 60 seconds, run each via GoalRunner.
        # KNOWN BUG, documented, not fixed (decision 2026-10-28: keep
        # the live timer/WebUI path, fix this post-hackathon): this
        # branch references server and msg, which are only assigned
        # in the webui serve branch, so they are unbound locals
        # here. The branch crashes at startup on
        # server.practice_session_runner, and the practice-runner
        # and practice-delivery uses would crash or silently drop
        # after it. Do not run schedule serve until the WebUI
        # construction is shared with this branch (see ROADMAP).
        from datetime import datetime
        from ..interface.triggers import Scheduler
        print("Scheduler daemon, polling every 60 seconds, Ctrl-C to stop.")
        print("Scheduler daemon also watches ingest and polls Slack, when configured, same loop.")
        def _full_scheduler_runner(session: str = ""):
            from ..agentic.budgets import Budget as _B2
            from ..agentic.trace import TraceRecorder as _T2
            from ..agentic.verifier_model import ModelVerifier as _M2
            from ..capabilities.plugins import PluginRegistry as _P2
            _pl = _P2(config.home); _pl.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb2
                _be = _mb2(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _be = None
            return GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home, memory=memory, conversation_id=session, plugins=_pl, execution_backend=_be, budget=_B2(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds), trace=_T2(config.home, conversation_id=session), model_verifier=_M2(client), kernel_mode=config.kernel_mode, network_available=_network_available(_be, sandbox))

        import os as _os
        _slack_last_ts = ""
        _slack_ts_path = config.home / "slack_last_ts.txt"
        if _slack_ts_path.is_file():
            _slack_last_ts = _slack_ts_path.read_text().strip()
        # Practice sessions: the serve daemon owns the window tick
        # and the report delivery. Each schedule gets its own
        # practice folder in the sandbox, and a runner built for
        # it: core tools live in that folder, the gate is the
        # PracticeGate for pre-approved schedules (the WebUI
        # gate otherwise, so it stops for approval), and the
        # only extra tools are the owner-only practice tools.
        def _practice_runner(schedule):
            from ..agentic.budgets import Budget as _B3
            from ..agentic.trace import TraceRecorder as _T3
            from ..agentic.verifier_model import ModelVerifier as _M3
            from ..capabilities.plugins import PluginRegistry as _P3
            from ..capabilities.sandbox import Sandbox as _S3
            from ..interface.practice import (PRACTICE_TOOL_NAMES, PracticeGate,
                                              coerce_preapprove, practice_tools)
            import os as _os3
            practice_dir = config.home / "practice" / server._safe_name(schedule["name"])
            practice_dir.mkdir(parents=True, exist_ok=True)
            _psandbox = _S3(practice_dir, config.sandbox_timeout_seconds,
                            mem_mb=config.sandbox_mem_mb, nproc=config.sandbox_nproc,
                            fsize_mb=config.sandbox_fsize_mb,
                            cpu_seconds=config.sandbox_cpu_seconds,
                            backend=config.sandbox_backend,
                            allow_network=config.sandbox_network,
                            require_isolation=config.sandbox_require_isolation)
            _pl = _P3(config.home); _pl.load_all()
            try:
                from ..capabilities.execution import make_backend as _mb3
                _be = _mb3(config.execution_backend, _psandbox, image=config.docker_image, allow_network=config.docker_network)
            except Exception:
                _be = None
            if coerce_preapprove(schedule.get("preapprove", True)):
                _gate = PracticeGate(practice_dir, sandboxed=_be is not None)
            else:
                _gate = server.webui_gate.gate
            return GoalRunner(client, _psandbox, _gate, config.home, memory=memory,
                              conversation_id="practice:" + schedule["name"], plugins=_pl, execution_backend=_be,
                              budget=_B3(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                              trace=_T3(config.home, conversation_id="practice:" + schedule["name"]), model_verifier=_M3(client),
                              kernel_mode=config.kernel_mode,
                              network_available=_network_available(_be, _psandbox),
                              extra_tools=practice_tools(
                                  practice_dir, msg, owner_email=config.owner_email,
                                  gmail_client_id=_os3.environ.get("GMAIL_CLIENT_ID", ""),
                                  gmail_client_secret=_os3.environ.get("GMAIL_CLIENT_SECRET", ""),
                                  gmail_refresh_token=_os3.environ.get("GMAIL_REFRESH_TOKEN", "")),
                              allowed_side_effect_tools=set(PRACTICE_TOOL_NAMES))

        def _practice_deliver(text, goal, result):
            # Practice reports are isolated to the Practice
            # page (the session record) plus the owner's
            # specified surfaces below. They are deliberately
            # NOT recorded into any chat conversation, so a
            # practice run never shows up among real runs.
            try:
                if msg.slack_outbound_channel:
                    slack_sender(msg.slack_outbound_channel, text)
            except Exception:
                pass
            # Practice reports are explicitly specified outputs,
            # so WhatsApp delivery here follows the owner's rule.
            try:
                if msg.whatsapp_owner:
                    whatsapp_sender(msg.whatsapp_owner, text)
            except Exception:
                pass

        def _practice_session_sync(schedule, end):
            from ..interface.practice import (coerce_preapprove,
                                              run_practice_session as _rps,
                                              record_session as _rec,
                                              update_live as _live)

            def _run_goal(goal_text, spec):
                _runner = _practice_runner(schedule)
                _report = _runner.run(goal_text, spec=spec)
                return {"status": _report.status, "files": list(_report.files or []),
                        "error": _report.error or "",
                        "elapsed_seconds": getattr(_report, "elapsed_seconds", 0),
                        "tokens": _report.model_tokens_estimated,
                        "run_id": getattr(_runner, "run_id", "") or "",
                        "dag": _report.dag_summary or {}}

            def _on_progress(live):
                try:
                    _live(config.home, schedule["name"], live)
                except Exception:
                    pass

            summary = _rps(schedule, _run_goal, end, home=config.home,
                           on_report=_practice_deliver, on_progress=_on_progress,
                           preapprove=coerce_preapprove(schedule.get("preapprove", True)))
            if not summary["passed"]:
                # Window closed with no pass: the no-pass note
                # lives in the session record on the Practice
                # page, so a silent failure never looks like
                # a pass — and never lands in a chat conversation.
                summary["report"] = (
                    f"Practice session {schedule['name']}: no pass in this window "
                    f"({len(summary['attempts'])} attempts).")
            _rec(config.home, schedule["name"], summary)
            print(f"Practice session {schedule['name']}: "
                  f"{'passed' if summary['passed'] else 'no pass'}")
        server.practice_session_runner = _practice_session_sync
        while True:
            scheduler = Scheduler(config.home)
            # Tick wiring: SchedulerLoop carries the outbox drain
            # and the scheduled-run notify hook for this daemon.
            # Sending is not configured here, so the outbox sender
            # prints the notification and reports it sent; the
            # on_due hook queues a "Scheduled run due" notice
            # before each due job runs. Job execution below is
            # unchanged: the loop object is not asked to dispatch,
            # it only notifies and drains once per tick.
            from ..capabilities.outbox import Outbox as _ServeOutbox
            from ..interface.scheduler_loop import SchedulerLoop as _ServeLoop
            _serve_outbox = _ServeOutbox(config.home)
            def _serve_sender(item):
                print(f"Outbox notification to {item.get('to', '')}: {item.get('subject', '')}: {item.get('body', '')}")
                return True
            def _serve_on_due(event):
                _serve_outbox.queue("", "Scheduled run due",
                                    f"Scheduled run due: {getattr(event, 'goal', event)}")
            _serve_loop = _ServeLoop(scheduler, dispatcher=None, outbox=_serve_outbox,
                                     outbox_sender=_serve_sender, on_due=_serve_on_due)
            for job in scheduler.check_due(datetime.now()):
                _serve_on_due(job)
                print(f"Due job: {job.name}: {job.goal}")
                from ..agentic.budgets import Budget as _Budget
                from ..agentic.trace import TraceRecorder as _Trace
                from ..agentic.verifier_model import ModelVerifier as _MV
                from ..capabilities.plugins import PluginRegistry as _PR
                _plugins = _PR(config.home); _plugins.load_all()
                try:
                    from ..capabilities.execution import make_backend as _mb
                    _backend = _mb(config.execution_backend, sandbox, image=config.docker_image, allow_network=config.docker_network)
                except Exception:
                    _backend = None
                try:
                    from ..agentic.semantic_router import SemanticRouter as _SR2
                    _router2 = _SR2(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
                except Exception:
                    _router2 = None
                from ..interface.run_control import RunControl as _ServeControl
                import uuid as _serve_uuid
                _serve_run_id = _serve_uuid.uuid4().hex[:12]
                _serve_control = _ServeControl(config.home)
                _serve_session = getattr(job, "conversation_id", "") or getattr(job, "project_id", "") or "scheduled"
                _serve_control.register(_serve_run_id, job.goal, _serve_session)
                print(f"Job {job.name}: run id {_serve_run_id}")

                # Scheduled non-goal jobs retain the Aidon scheduler
                # semantics. They do not need a model GoalRunner.
                _handled = False
                if job.kind == "learn":
                    from ..agentic.learn_cycle import run_learn_cycle as _learn
                    from ..capabilities.skill_lifecycle import LifecycleStore
                    _summary = _learn(
                        config.home, sandbox,
                        governor_check=LifecycleStore(config.home).governor_check,
                        max_per_day=max(0, config.learn_max_skills_per_day))
                    print(f"Job {job.name}: learn cycle {_summary}")
                    _handled = True
                elif job.kind == "report":
                    from ..interface.report import daily_report as _daily
                    print(f"Job {job.name}: report\n{_daily(config.home)}")
                    _handled = True
                elif job.kind == "chain":
                    from ..capabilities.chain import (
                        run_chain as _run_chain,
                        run_conditional_chain as _run_cond,
                        validate_chain_spec as _validate_chain,
                        validate_conditional_chain as _validate_cond,
                    )
                    from ..capabilities.tools import ToolRegistry as _ChainTools
                    import json as _chain_json
                    _chain = _chain_json.loads(job.goal)
                    _tools = _ChainTools(
                        sandbox.workdir, sandbox=sandbox,
                        network_available=_network_available(_backend, sandbox))
                    _chain_failures = (
                        _validate_cond(_chain, allowed_tools=tuple(_tools.names()))
                        if isinstance(_chain, list)
                        else _validate_chain(_chain))
                    if _chain_failures:
                        print(f"Job {job.name}: chain rejected: {_chain_failures}")
                        _handled = True
                        continue
                    _chain_decision = _cli_gate(
                        config.home, config.approval_timeout_seconds,
                        _serve_session).ask(
                            "live", f"Approve scheduled chain {job.name}?",
                            json.dumps(_chain, sort_keys=True), force_ask=True)
                    if _chain_decision.verdict not in ("approved", "auto_approved"):
                        print(f"Job {job.name}: chain rejected by approval")
                        _handled = True
                        continue
                    if isinstance(_chain, list):
                        def _invoke(name, args):
                            return _tools.call(name, args)
                        _chain_result = _run_cond(
                            _chain, _invoke,
                            allowed_tools=tuple(_tools.names()))
                    else:
                        _chain_result = _run_chain(_chain, _tools)
                    print(f"Job {job.name}: chain {_chain_result}")
                    _handled = True
                elif job.kind == "brief":
                    from ..interface.report import build_nightly_brief as _brief
                    from ..interface.messaging import MessagingConfig, send_whatsapp
                    _brief_result = _brief(config.home, client)
                    _msg = MessagingConfig(
                        whatsapp_token=config.whatsapp_token,
                        whatsapp_phone_id=config.whatsapp_phone_id,
                        whatsapp_verify_token=config.whatsapp_verify_token,
                        whatsapp_app_secret=config.whatsapp_app_secret,
                        whatsapp_owner=config.whatsapp_owner)
                    _sent = send_whatsapp(_msg, config.whatsapp_owner,
                                          _brief_result["text"])
                    print(f"Job {job.name}: brief delivery {_sent}")
                    _handled = True
                if not _handled:
                    # Scheduled runs get the same extra tools as
                    # interactive runs (fetch, email, web search),
                    # but behind the scheduled gate: email to the
                    # owner goes through, everything else fails
                    # fast instead of blocking the tick on stdin.
                    # Questions have no channel here, so ask_user
                    # tells the model to proceed with its best
                    # judgment and note the assumption. The runner
                    # itself keeps researcher=None on purpose: a
                    # researcher would newly let scheduled goals
                    # route to webchat replies instead of the
                    # agentic pipeline; the web_search plan tool
                    # carries its own researcher.
                    _job_researcher = _make_web_researcher(config)
                    # Sandboxed shell for the gate: a real backend
                    # (bwrap/Docker) isolates, and a host Sandbox
                    # with bwrap present is offline; a host Sandbox
                    # without bwrap is not isolated, so its shell
                    # approvals stay rejected.
                    _job_sandboxed = _backend is not None or bool(
                        getattr(sandbox, "bwrap", False))
                    _job_gate = _scheduled_gate(sandboxed=_job_sandboxed)
                    _job_governor = _make_governor(config)
                    _job_tools = _goal_extra_tools(config, sandbox.workdir, gate=_job_gate, researcher=_job_researcher, question_answer=None, scheduled=True, governor=_job_governor)
                    try:
                        _job_deadline = float(job.time_limit or 0) or None
                    except (TypeError, ValueError):
                        _job_deadline = None
                    runner = GoalRunner(client, sandbox, _job_gate, config.home,
                                        memory=memory, conversation_id=_serve_session, plugins=_plugins,
                                        execution_backend=_backend, budget=_Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                        trace=_Trace(config.home, conversation_id=_serve_session), model_verifier=_MV(client),
                                        kernel_mode=config.kernel_mode, semantic_router=_router2,
                                        use_semantic_router_skip=True, network_available=_network_available(_backend, sandbox), run_id=_serve_run_id, run_control=_serve_control,
                                        extra_tools=_job_tools,
                                        acquisition_governor=_job_governor,
                                        deadline_seconds=_job_deadline)
                    report = runner.run(job.goal)
                    print(f"Job {job.name}: {report.status}, files: {', '.join(report.files)}")
                    # Failure notification for goal jobs, mirroring
                    # the brief kind's WhatsApp send. Failed and
                    # partial (time-box cut) both notify: either
                    # way the job did not complete. Unconfigured
                    # messaging fails soft, like everything else
                    # in this loop — the print above is the floor.
                    if report.status in ("failed", "partial"):
                        try:
                            from ..interface.messaging import MessagingConfig, send_whatsapp
                            _notify_cfg = MessagingConfig(
                                whatsapp_token=config.whatsapp_token,
                                whatsapp_phone_id=config.whatsapp_phone_id,
                                whatsapp_verify_token=config.whatsapp_verify_token,
                                whatsapp_app_secret=config.whatsapp_app_secret,
                                whatsapp_owner=config.whatsapp_owner)
                            _detail = (report.error or "")[:500]
                            _text = (f"Scheduled job {job.name} {report.status}: "
                                     f"{_detail or ', '.join(report.files) or 'no output'}")
                            _sent = send_whatsapp(_notify_cfg, config.whatsapp_owner, _text)
                            print(f"Job {job.name}: failure notification {_sent}")
                        except Exception as exc:
                            print(f"Job {job.name}: failure notification failed: {exc}")
            # Tick drain: once per tick, due notifications only,
            # cadence-spaced by the Outbox itself.
            _serve_loop.drain_outbox()
            # Ingest watcher, same loop, same GoalRunner, no separate daemon.
            try:
                from ..interface.triggers import IngestWatcher
                watcher = IngestWatcher(config.ingest_path, config.home)
                for ingest_event in watcher.events(conversation_id=""):
                    print(f"Ingest trigger: {ingest_event.payload.get('path', '')}")
                    ingest_runner = _full_scheduler_runner()
                    ingest_report = ingest_runner.run(ingest_event.goal)
                    print(f"Ingest job: {ingest_report.status}")
            except Exception as exc:
                print(f"Ingest watch error: {exc}")
            # Slack polling daemon, same loop, channel from AIDON_SLACK_INBOUND_CHANNEL.
            try:
                _channel = _os.environ.get("AIDON_SLACK_INBOUND_CHANNEL", "")
                if config.slack_bot_token and _channel:
                    from ..interface.messaging import MessagingConfig, poll_slack_once
                    _msg_cfg = MessagingConfig(slack_bot_token=config.slack_bot_token)
                    _messages, _slack_last_ts = poll_slack_once(_msg_cfg, _channel, last_ts=_slack_last_ts)
                    _slack_ts_path.write_text(_slack_last_ts)
                    for _message in _messages:
                        print(f"Slack poll message: {_message['text'][:100]}")
                        _poll_runner = _full_scheduler_runner("slack:" + _channel)
                        _poll_report = _poll_runner.run(_message["text"])
                        print(f"Slack poll job: {_poll_report.status}")
            except Exception as exc:
                print(f"Slack poll error: {exc}")
            # Practice windows, same loop: start at most one session
            # per schedule per day while its window is open.
            try:
                from ..interface.practice import (due_for_session, load_schedules,
                                                  save_schedules, window_end)
                from datetime import datetime as _dt
                _now = _dt.now()
                _schedules = load_schedules(config.home)
                _changed = False
                for _sched in _schedules:
                    if due_for_session(_sched, _now):
                        _sched["last_session_date"] = _now.strftime("%Y-%m-%d")
                        _changed = True
                        _end = window_end(_sched, _now)
                        import threading as _th
                        _th.Thread(target=_practice_session_sync, args=(_sched, _end),
                                   daemon=True, name=f"practice-{_sched.get('name')}").start()
                        print(f"Practice session started: {_sched.get('name')}")
                if _changed:
                    save_schedules(config.home, _schedules)
            except Exception as exc:
                print(f"Practice tick error: {exc}")
            import time as _sleep
            _sleep.sleep(60)
    if args.command == "benchmark":
        # Canned Snake to Worm benchmark, for the self-learning claim.
        # Fresh: a long plan JSON, adapted: a short changed-node JSON.
        # Tokens are estimated at one per four characters, as in budgets.
        from ..agentic.benchmark import benchmark_learn
        from ..agentic.counting import CountingClient
        from ..agentic.loop import RunReport, Plan

        class _PlanClient:
            def __init__(self, reply):
                self.reply = reply
                self.calls = 0
            def chat(self, messages, phase="chat"):
                self.calls += 1
                return self.reply
            def chat_phase(self, phase, messages):
                return self.chat(messages, phase=phase)

        long_plan = '{"steps": [{"tool": "write", "args": {"file": "worm.py", "text": "Worm game loop, input handling, rendering, scoring, tests, and a long description of every step in detail."}, "deliverable": "worm.py"}, {"tool": "write", "args": {"file": "worm_test.py", "text": "Tests for the Worm game loop, input, rendering, and scoring, in detail."}, "deliverable": "worm_test.py"}], "deliverables": ["worm.py", "worm_test.py"], "criteria": ["worm.py exists", "worm.py contains Worm"]}'
        short_adapt = '{"adapted": [{"index": 0, "args": {"file": "worm.py", "text": "Worm"}, "deliverable": "worm.py"}]}'
        fresh_client = CountingClient(_PlanClient(long_plan))
        adapted_client = CountingClient(_PlanClient(short_adapt))
        # Fresh and adapted reports, from the counting clients directly,
        # so the benchmark measures model calls, not sandbox execution.
        fresh_client.chat([], phase="plan")
        adapted_client.chat([], phase="adapt")
        fresh_report = RunReport("done", "Write a Worm game", Plan([], ["worm.py"], []), True, True,
                                 files=["worm.py"], model_calls=fresh_client.calls,
                                 model_tokens_estimated=fresh_client.tokens_estimated)
        adapted_report = RunReport("done", "Write a Worm game", Plan([], ["worm.py"], []), True, True,
                                   files=["worm.py"], model_calls=adapted_client.calls,
                                   model_tokens_estimated=adapted_client.tokens_estimated)
        result = benchmark_learn(lambda: (fresh_report, {"worm.py": "Worm game"}),
                                 lambda: (adapted_report, {"worm.py": "Worm game"}),
                                 "Worm", "Snake")
        print(f"Fresh: {result['fresh']['model_calls']} calls, {result['fresh']['model_tokens_estimated']} estimated tokens, quality_ok={result['fresh']['quality_ok']}")
        print(f"Adapted: {result['adapted']['model_calls']} calls, {result['adapted']['model_tokens_estimated']} estimated tokens, quality_ok={result['adapted']['quality_ok']}")
        print(f"Tokens saved, estimated: {result['tokens_saved_estimated']}, saving_counts={result['saving_counts']}")
        print("Canned benchmark, not a live model run. Live benchmark is demo and testing, after work.")
        return 0
    if args.command == "email":
        import os as _os
        if args.action == "drafts":
            drafts_dir = config.home / "email" / "drafts"
            if drafts_dir.exists():
                for draft in sorted(drafts_dir.glob("*.md")):
                    print(draft.name)
            return 0
        # send: Gmail OAuth, only if the keys are present, else drafts only.
        client_id = _os.environ.get("GMAIL_CLIENT_ID", "")
        client_secret = _os.environ.get("GMAIL_CLIENT_SECRET", "")
        refresh_token = _os.environ.get("GMAIL_REFRESH_TOKEN", "")
        if not (client_id and client_secret and refresh_token):
            print("Gmail OAuth keys are missing, email stays as drafts. Fill GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN in .env, then run again.")
            return 2
        from ..capabilities.gmail import gmail_sender
        sender = gmail_sender(client_id, client_secret, refresh_token)
        result = sender(args.to or "", args.subject, args.body)
        print(f"Gmail send result: {result.get('id', 'sent')}")
        return 0
    from ..capabilities.execution import make_backend
    try:
        execution_backend = make_backend(
            config.execution_backend, sandbox,
            image=config.docker_image, allow_network=config.docker_network,
            vm_host=config.vm_host, vm_user=config.vm_user or None,
            vm_sudo_password=config.vm_sudo_password or None,
            vm_use_sudo=config.vm_use_sudo,
            modal_app=config.modal_app, modal_cpu=config.modal_cpu,
                    modal_memory_mb=config.modal_memory_mb)
    except Exception as exc:
        print(f"execution backend error: {exc}")
        return 2
    if args.command == "plugin":
        from pathlib import Path
        from ..capabilities.plugins import PluginRegistry, scan_plugin
        registry = PluginRegistry(config.home)
        if args.action == "list":
            plugins = registry.load_all()
            for plugin in plugins:
                print(f"{plugin.name} {plugin.version}: {', '.join(plugin.tool_names()) or 'no tools'}")
            return 0
        if args.path is None:
            print("plugin scan and adopt need a directory path")
            return 2
        if args.action == "scan":
            findings = scan_plugin(Path(args.path))
            for finding in findings:
                print(f"{finding.path}:{finding.line} {finding.type}")
            print(f"findings: {len(findings)}")
            return 0
        ok, findings, message = registry.adopt(
            Path(args.path), gate=_cli_gate(config.home, config.approval_timeout_seconds), memory=memory,
            conversation_id=args.conversation)
        print(message)
        for finding in findings:
            print(f"{finding.path}:{finding.line} {finding.type}")
        return 0 if ok else 2
    if args.command == "interview":
        # Semantic routing, interview surface too: a chat goal does not
        # start an interview or a spec, it gets a direct chat answer.
        try:
            from ..agentic.semantic_router import SemanticRouter
            _router = SemanticRouter(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
            _route, _conf = _router.classify(args.text)
            if _route == "chat" and _conf >= 0.15 and not any(w in args.text.lower() for w in ("write", "save", ".md", ".py")):
                from .model_client import Message as _Msg
                print(client.chat([_Msg("user", args.text)]))
                return 0
        except Exception:
            pass
        from ..agentic.interview import InterviewRunner
        runner = InterviewRunner(client, config.home, memory=memory,
                                 gate=_cli_gate(config.home, config.approval_timeout_seconds), conversation_id=args.conversation)
        print(f"Spec template: {runner.ensure_template()}  "
              f"(copy it, fill it in, attach it to skip the interview; "
              f"docs: README, Spec sheet section)")
        if args.attach:
            # Attached spec sheet: skip the interview questions.
            from pathlib import Path
            result = runner.load_attached_spec(
                Path(args.attach).read_text(encoding="utf-8"))
        else:
            # Questions are chat, no clock, no card: input() in CLI.
            result = runner.start_from_message(
                args.text, ask=lambda q: input(f"\nAidon: {q}\nYou: "))
        print(f"\nSpec verified: {result.verified}")
        if result.failures:
            print(f"Spec failures: {'; '.join(result.failures)}")
        print(f"Spec approved: {result.approved}")
        print(f"Spec file: {config.home}/specs/{args.conversation}.json")
        print(f"Spec sheet: {config.home}/specs/{args.conversation}.md")
        if not result.approved:
            return 2
        if not args.run:
            return 0
        from ..capabilities.plugins import PluginRegistry
        plugins = PluginRegistry(config.home)
        plugins.load_all()
        def _interview_researcher(goal_text):
            try:
                from ..agentic.research_allow import DomainGrants, load_allowlist
                from ..capabilities.search import search as _search
                result = _search(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                                 allowlist=load_allowlist(config.home, config.research_allowlist),
                                 grants=DomainGrants(config.home))
                return [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in result.get("results", [])]
            except Exception:
                return []
        # Full runner, same as the CLI goal run: budget, trace, model
        # verification, semantic routing, and run control. The interview
        # branch previously built a thin runner with none of these, so
        # caps, verification, and steer/cancel silently did not apply.
        from ..agentic.budgets import Budget as _IBudget
        from ..agentic.trace import TraceRecorder as _ITrace
        from ..agentic.verifier_model import ModelVerifier as _IModelVerifier
        from ..interface.run_control import RunControl as _IRunControl
        try:
            from ..agentic.semantic_router import SemanticRouter as _ISemanticRouter
            _i_router = _ISemanticRouter(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
        except Exception:
            _i_router = None
        import uuid as _i_uuid
        _i_run_id = _i_uuid.uuid4().hex[:12]
        _i_control = _IRunControl(config.home)
        _i_control.register(_i_run_id, args.text, args.conversation)
        print(f"interview run id: {_i_run_id}")
        _i_governor = _make_governor(config)
        goal_runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds), config.home,
                                 memory=memory,
                                 conversation_id=args.conversation, plugins=plugins, execution_backend=execution_backend, researcher=_interview_researcher, extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=_cli_gate(config.home, config.approval_timeout_seconds), researcher=_interview_researcher, governor=_i_governor), router=model_router, versioned_writes=True,
                                 budget=_IBudget(max_tokens=config.budget_tokens, max_steps=config.max_steps, max_seconds=config.budget_seconds),
                                 trace=_ITrace(config.home, conversation_id=args.conversation),
                                 model_verifier=_IModelVerifier(client),
                                 semantic_router=_i_router,
                                 use_semantic_router_skip=True,
                                 run_control=_i_control, run_id=_i_run_id, network_available=_network_available(execution_backend, sandbox), acquisition_governor=_i_governor)
        report = goal_runner.run(args.text, spec=result.spec)
        print(f"status: {report.status}")
        print(f"verified: {report.verified}")
        print(f"approved: {report.approved}")
        print(f"files: {', '.join(report.files)}")
        print(f"attempts: {report.attempts}")
        print(format_run_stats(report))
        if report.error:
            print(f"error: {report.error}")
            return 1
        return 0 if report.status == "done" else 2
    from ..capabilities.plugins import PluginRegistry
    plugins = PluginRegistry(config.home)
    plugins.load_all()
    from ..agentic.budgets import Budget
    from ..agentic.trace import TraceRecorder
    budget = Budget(max_tokens=config.budget_tokens, max_steps=config.max_steps,
                    max_seconds=config.budget_seconds)
    trace = TraceRecorder(config.home, conversation_id=args.conversation)
    from ..agentic.verifier_model import ModelVerifier
    model_verifier = ModelVerifier(client)
    try:
        from ..agentic.semantic_router import SemanticRouter as _SR
        _cli_router = _SR(memory.embedder, cache_path=config.home / 'semantic_router_vectors.json')
    except Exception:
        _cli_router = None
    def _researcher(goal_text):
        try:
            from ..agentic.research_allow import DomainGrants, load_allowlist
            from ..capabilities.search import search as _search
            result = _search(goal_text, backend=config.search_backend, tavily_key=config.tavily_api_key, searxng_url=config.searxng_url,
                             allowlist=load_allowlist(config.home, config.research_allowlist),
                             grants=DomainGrants(config.home))
            return [f"{r.get('title', '')}: {r.get('snippet', '')}" for r in result.get("results", [])]
        except Exception:
            return []
    import uuid as _uuid
    _goal_run_id = _uuid.uuid4().hex[:12]
    from ..interface.run_control import RunControl as _GoalControl
    _goal_control = _GoalControl(config.home)
    _goal_control.register(_goal_run_id, args.text, args.conversation)
    print(f"run id: {_goal_run_id}")
    _governor = _make_governor(config)
    runner = GoalRunner(client, sandbox, _cli_gate(config.home, config.approval_timeout_seconds, args.conversation), config.home,
                        memory=memory,
                        conversation_id=args.conversation, plugins=plugins, execution_backend=execution_backend,
                        budget=budget, trace=trace, model_verifier=model_verifier,
                        kernel_mode=config.kernel_mode, semantic_router=_cli_router,
                        use_semantic_router_skip=True, researcher=_researcher,
                        extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=_cli_gate(config.home, config.approval_timeout_seconds, args.conversation), researcher=_researcher, model_chat=lambda messages: client.chat(messages), governor=_governor),
                        router=model_router, versioned_writes=True,
                        run_id=_goal_run_id, run_control=_goal_control,
                        network_available=_network_available(execution_backend, sandbox),
                        guidance=getattr(args, "guidance", ""), acquisition_governor=_governor)
    spec = None
    if getattr(args, "spec", None):
        from pathlib import Path
        from ..agentic.interview import InterviewRunner, parse_spec_sheet
        spec = parse_spec_sheet(Path(args.spec).read_text(encoding="utf-8"))
        verified, failures = spec.verify()
        if not verified:
            print(f"Attached spec is not verified: {'; '.join(failures)}")
            return 2
    if getattr(args, "swarm", False):
        try:
            from ..agentic.swarm import run_swarm
            def _swarm_runner(subgoal):
                # Per-subgoal runner, not the shared main runner: each
                # sub-run gets its own run_id (never the parent's), and
                # the research + routing the main runner uses. Sub-runs
                # register under their own ids, so control list shows
                # them and cancel/steer can target a sub-run directly.
                _sub_id = f"{runner.run_id}-{_uuid.uuid4().hex[:8]}"
                _goal_control.register(_sub_id, subgoal, args.conversation)
                sub_runner = GoalRunner(client, sandbox, runner.gate,
                                        config.home, memory=memory,
                                        conversation_id=args.conversation, plugins=plugins,
                                        execution_backend=execution_backend,
                                        budget=budget, trace=trace,
                                        model_verifier=model_verifier,
                                        kernel_mode=config.kernel_mode,
                                        semantic_router=_cli_router,
                                        use_semantic_router_skip=True,
                                        researcher=_researcher,
                                        extra_tools=_goal_extra_tools(config, sandbox.workdir, gate=runner.gate, researcher=_researcher, governor=getattr(runner, "acquisition_governor", None)),
                                        router=model_router, versioned_writes=True,
                                        run_id=_sub_id, run_control=_goal_control, network_available=_network_available(execution_backend, sandbox), acquisition_governor=getattr(runner, "acquisition_governor", None))
                sub_report = sub_runner.run(subgoal, spec=spec)
                return f"[{sub_report.status}] {', '.join(sub_report.files)} {sub_report.error}".strip()
            swarm_result = run_swarm(args.text, client, _swarm_runner)
            if swarm_result.get("swarmed"):
                print(f"Swarm, subgoals: {', '.join(swarm_result['subgoals'])}")
                print(swarm_result["merged"])
                print("status: swarm")
                return 0
        except Exception as exc:
            print(f"Swarm failed, falling back to single run: {exc}")
    report = runner.run(args.text, spec=spec)
    print(f"status: {report.status}")
    print(f"verified: {report.verified}")
    print(f"approved: {report.approved}")
    print(f"files: {', '.join(report.files)}")
    print(f"attempts: {report.attempts}")
    print(format_run_stats(report))
    if report.error:
        print(f"error: {report.error}")
        return 1
    return 0 if report.status == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
