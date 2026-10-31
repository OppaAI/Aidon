"""Goal-run tool construction, separated from CLI and service startup."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from ..capabilities.sandbox import Sandbox

_SELF_MODIFY_LOCK = threading.Lock()


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


def _goal_extra_tools(config, workdir, gate=None, researcher=None, question_answer=None, phrase=None, scheduled=False, model_chat=None, governor=None, optional_tools=None,
                      plugin_registry=None, gate_context=None, test_backend_factory=None) -> dict:
    """Extra tools for goal runs, built from existing modules only.

    If ``plugin_registry`` is given (a ``PluginRegistry`` instance that is
    already loaded and live), plugin adoption updates the registry the goal
    runner uses for subsequent tool calls in this workflow.

    The ``self_modify`` tool is available only in the selfmod posture. It
    applies a reviewed Python source replacement only after a forced owner
    approval, and keeps a backup before changing the source tree.

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
    from ..capabilities.tools import ToolResult
    workdir = Path(workdir)
    tools: dict = {}
    if test_backend_factory is None and isinstance(config, Config):
        def test_backend_factory():
            from ..capabilities.execution import make_backend
            sandbox = Sandbox(workdir, require_isolation=True)
            return make_backend(
                config.execution_backend, sandbox, image=config.docker_image,
                allow_network=False, modal_app=config.modal_app,
                modal_cpu=config.modal_cpu, modal_memory_mb=config.modal_memory_mb)

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
        from ..capabilities.webcam import (capture_webcam_photo,
                                           record_webcam_clip)

        def webcam_photo(output="photo.png", device=""):
            # Still-image capture is a host-side action. Each use
            # requires a fresh, explicit approval before the device
            # is opened; the output stays inside this run's workdir.
            ok, why = _approval(
                "record", "Capture webcam photo",
                f"Activate the camera ({device or '/dev/video0'}) and "
                f"capture one photo to {output}?",
                force_ask=True)
            if not ok:
                return f"webcam photo capture rejected: {why}"
            return capture_webcam_photo(str(_confined(output)),
                                        device or "/dev/video0")

        def record_webcam(seconds, output="clip.mp4", device=""):
            # Kind "record", force_ask: camera activation is
            # never pre-approved and never silent. The card
            # names the device, duration, and output file.
            ok, why = _approval(
                "record", "Record webcam video",
                f"Activate the camera ({device or '/dev/video0'}) "
                f"and record {int(seconds)} "
                f"seconds of video to {output}?",
                force_ask=True)
            if not ok:
                return f"webcam recording rejected: {why}"
            return record_webcam_clip(int(seconds), str(_confined(output)),
                                      device or "/dev/video0")

        tools["capture_webcam_photo"] = webcam_photo
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
            mcp_config = {"transport": "stdio",
                          "command": str(command),
                          "args": [str(x) for x in (args or [])],
                          "env": dict(env or {})}

            def _factory(_cfg, _parts=command_parts, _env=dict(env or {})):
                from ..interface.mcp_client import McpClient
                _client = McpClient(_parts, env=_env)
                _client.start()
                return _client
        elif transport in ("http", "streamable-http"):
            if not url:
                return "MCP_REFUSED: url is required for HTTP transport"
            mcp_config = {"transport": transport, "url": str(url),
                          "token": str(token or "")}

            def _factory(_cfg, _url=str(url), _token=str(token or "")):
                from ..interface.mcp_http import McpHttpClient
                _client = McpHttpClient(_url, token=_token or None)
                _client.initialize()
                return _client
        else:
            return f"MCP_REFUSED: unsupported transport {transport!r}"
        # One approval, through the acquisition lifecycle: the
        # lifecycle approves (bound to the exact command/URL),
        # connects, lists, scans descriptions, and registers
        # the server as validated; this tool then keeps the
        # client for the run's namespaced tools. The governor
        # counts once, inside the lifecycle after approval.
        _why = {}

        class _Decision:
            def __init__(self, verdict):
                self.verdict = verdict

        def _acquire_approve(kind, title, body):
            ok, why = _approval(kind, title, body, force_ask=True)
            if not ok:
                _why["why"] = why
            return _Decision("approved" if ok else "rejected")

        from ..capabilities.acquisition_governor import AcquisitionBudgetExceeded
        from ..capabilities.mcp_acquisition import acquire_mcp_server
        try:
            result = acquire_mcp_server(
                config.home, server_name, mcp_config, _acquire_approve,
                client_factory=_factory, governor=governor, keep_client=True)
        except AcquisitionBudgetExceeded:
            raise
        except Exception as exc:
            if "rejected by approval" in str(exc) and _why.get("why"):
                return f"MCP_REFUSED: {_why['why']}"
            return f"MCP_REFUSED: {type(exc).__name__}: {exc}"
        client = result["client"]
        mcp_connections[server_name] = client
        registered = []
        for remote_name in result["tools"]:
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
        scan_note = ""
        if result.get("scan_findings"):
            scan_note = (f"; description scan findings: "
                         f"{len(result['scan_findings'])} (informational, not blocking)")
        return f"MCP_CONNECTED {server_name!r}: {', '.join(registered) or 'no safe tools'}{scan_note}"

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
        from ..capabilities.plugins import (
            PluginRegistry, adoption_gate, ask_plugin_approval,
            commit_plugin, preflight_plugin)

        def adopt_tool_plugin(path):
            # Order matters: preflight and approval cost nothing.
            # The governor counts only the commit, after the
            # owner has said yes — a rejection, whether by the
            # scanner or by the owner, consumes no budget.
            posture = str(getattr(config, "tool_posture", "standard")
                          or "standard").lower()
            if posture == "locked" or getattr(config, "kernel_mode", False):
                return ("adoption refused: tool posture is locked; "
                        "plugin adoption is disabled")
            source = _confined(path)
            if not source.is_dir():
                return f"adoption refused: plugin directory not found: {path}"
            if plugin_registry is None:
                registry = PluginRegistry(config.home)
            else:
                registry = plugin_registry
            preflight = preflight_plugin(source)
            if not preflight.ok:
                return f"adoption refused: {preflight.message}"
            allowed, reason = adoption_gate(
                posture, getattr(config, "kernel_mode", False),
                preflight.plugin.capabilities,
                sandboxed=any(
                    str(t.get("mode", "")).lower() == "sandbox"
                    for t in preflight.plugin.tools))
            if not allowed:
                return f"adoption refused: {reason}"
            if test_backend_factory is None:
                return ("adoption refused: an isolated, network-disabled "
                        "acceptance-test backend is required")
            backend = None
            try:
                from ..capabilities.plugin_acceptance import test_candidate
                backend = test_backend_factory()
                evidence = test_candidate(source, backend)
                if evidence["source_hash"] != preflight.approved_hash:
                    return "adoption refused: candidate changed after preflight"
                preflight.body += (
                    "\nAcceptance validation: "
                    f"{len(evidence['tests'])} test command(s) passed in isolated "
                    f"{evidence['backend']} with network disabled. Evidence is "
                    f"bound to candidate hash {evidence['source_hash']}."
                )
            except Exception as exc:
                return f"adoption refused: acceptance tests failed: {type(exc).__name__}: {exc}"
            finally:
                if backend is not None:
                    backend.cleanup()
            if not ask_plugin_approval(gate, None, preflight):
                return "adoption refused: Adoption rejected by owner"
            if governor is not None:
                governor.acquire("tool")
            approved, _findings, message = commit_plugin(
                registry, source, preflight, test_evidence=evidence)
            # commit_plugin installs the validated plugin into this registry
            # after copying it. Plans already validated cannot name it; a
            # replan after adoption can use the new tool.
            if approved:
                available = ", ".join(preflight.plugin.tool_names()) or "none"
                return (f"{message}; tools available after this adoption step "
                        f"succeeds: {available}")
            return message if approved else f"adoption refused: {message}"

        tools["adopt_tool_plugin"] = adopt_tool_plugin
    except Exception:
        pass

    def adopt_skill(path, name=""):
        """Test a drafted SKILL.md offline, then request separate trust approval."""
        source = _confined(path)
        if not source.is_file() or source.suffix.lower() != ".md":
            return "skill adoption refused: provide a Markdown file in the goal workdir"
        try:
            import hashlib
            import re as _re
            from ..capabilities.skill_library import SkillLibrary, import_skill_file
            from ..capabilities.sandbox import Sandbox as _SkillSandbox

            raw_name = str(name or source.stem).strip()
            clean_name = _re.sub(r"[^A-Za-z0-9_.-]+", "-", raw_name)
            if not clean_name:
                return "skill adoption refused: name is required"
            sandbox = _SkillSandbox(
                workdir, getattr(config, "sandbox_timeout_seconds", 120),
                mem_mb=getattr(config, "sandbox_mem_mb", 1024),
                nproc=getattr(config, "sandbox_nproc", 256),
                fsize_mb=getattr(config, "sandbox_fsize_mb", 512),
                cpu_seconds=getattr(config, "sandbox_cpu_seconds", 0),
                backend=getattr(config, "sandbox_backend", "auto"),
                allow_network=False, require_isolation=True)
            if not sandbox.isolated or sandbox.allow_network:
                return "skill adoption refused: tests need working bubblewrap isolation with network disabled"

            library = SkillLibrary(config.home)
            skill = library.get(clean_name)
            if skill is None:
                skill = import_skill_file(config.home, source, clean_name)
            else:
                stored = Path(config.home) / "skills" / f"{clean_name}.md"
                if not stored.is_file() or stored.read_bytes() != source.read_bytes():
                    return (f"skill adoption refused: {clean_name!r} already exists with different bytes; "
                            "retire it before replacing it")
            if not skill.test_commands:
                return "skill adoption refused: add meaningful commands under a ## Tests section"

            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            report = library.run_tests(skill.name, sandbox)
            if not report.get("passed"):
                return "skill tests failed or are unprovisioned; the draft remains untrusted"
            library.promote(skill.name, report)
            if library.get(skill.name).state == "trusted":
                return f"Skill {skill.name} is already trusted with passing tests"
            stored = Path(config.home) / "skills" / f"{skill.name}.md"
            if (hashlib.sha256(source.read_bytes()).hexdigest() != source_hash
                    or not stored.is_file() or stored.read_bytes() != source.read_bytes()):
                return "skill adoption refused: draft changed during testing; review and test it again"

            ok, why = _approval(
                "skill", f"Trust tested skill {skill.name}?",
                f"Skill: {skill.name}\nDescription: {skill.description or '(none)'}\n"
                f"Passing checks: {report.get('count', 0)}\n"
                f"Tested bytes (SHA-256): {source_hash}\n"
                "Trust makes this procedure available to future plans.",
                action_class="skill_privileged" if skill.privileged else "skill",
                force_ask=True)
            if not ok:
                return f"{skill.name} passed tests and remains validated; trust was not approved ({why})"
            if hashlib.sha256(stored.read_bytes()).hexdigest() != source_hash:
                return "skill adoption refused: validated file changed after approval; run the tests again"
            if governor is not None:
                governor.acquire("skill")
            library.trust(skill.name, approved=True)
            return (f"Trusted skill {skill.name}; its tested procedure is available to the current goal. "
                    "Replan the original goal using it where useful.")
        except Exception as exc:
            return f"skill adoption refused: {type(exc).__name__}: {exc}"

    tools["adopt_skill"] = adopt_skill

    # Tier 3: source changes are a separate, forced install approval.
    # The proposal is prepared in the goal workdir; only the approved,
    # syntax-valid bytes can be copied into Aidon's own src/ tree.
    if (str(getattr(config, "tool_posture", "standard") or "standard")
            .strip().lower() == "selfmod"
            and not getattr(config, "kernel_mode", False)
            and not scheduled):
        def self_modify(target, proposal_file):
            """Apply a staged Python source file after explicit approval."""
            import difflib
            import hashlib
            import ast
            import tempfile
            import stat
            from datetime import datetime, timezone

            if gate is None:
                return "self-modification refused: no approval gate is available"
            raw_target = str(target).replace("\\", "/")
            target_parts = Path(raw_target).parts
            if (Path(raw_target).is_absolute() or ".." in target_parts
                    or not raw_target.startswith("src/")):
                return "self-modification refused: target must be an existing Python file under src/"

            source_root = Path(__file__).resolve().parents[2]
            source_dir = (source_root / "src").resolve()
            destination = (source_root / raw_target).resolve()
            if (destination.suffix != ".py" or destination == source_dir
                    or source_dir not in destination.parents
                    or not destination.is_file()):
                return "self-modification refused: target must be an existing Python file under src/"

            try:
                proposal_path = _confined(proposal_file)
                if not proposal_path.is_file():
                    return "self-modification refused: proposal file was not found in the workdir"
                proposed_bytes = proposal_path.read_bytes()
                old_bytes = destination.read_bytes()
                if len(proposed_bytes) > 256 * 1024 or len(old_bytes) > 256 * 1024:
                    return "self-modification refused: source files are limited to 256 KiB"
                proposed = proposed_bytes.decode("utf-8")
                old_text = old_bytes.decode("utf-8")
                proposed_tree = ast.parse(proposed, filename=raw_target)
                from ..capabilities.plugins import SECRET_RE
                for node in ast.walk(proposed_tree):
                    if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue
                    targets = (node.targets if isinstance(node, ast.Assign)
                               else [node.target])
                    value = node.value
                    if not (isinstance(value, ast.Constant)
                            and isinstance(value.value, str)):
                        continue
                    if any(isinstance(item, ast.Name)
                           and SECRET_RE.search(item.id)
                           for target_node in targets
                           for item in ast.walk(target_node)):
                        return ("self-modification refused: proposed source "
                                "assigns a secret-like name a string literal")
            except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
                return f"self-modification refused: proposal validation failed ({type(exc).__name__})"

            old_hash = hashlib.sha256(old_bytes).hexdigest()
            proposed_hash = hashlib.sha256(proposed_bytes).hexdigest()
            if old_hash == proposed_hash:
                return "self-modification skipped: proposal matches the current source"
            diff = "".join(difflib.unified_diff(
                old_text.splitlines(keepends=True),
                proposed.splitlines(keepends=True),
                fromfile=f"current/{raw_target}",
                tofile=f"proposed/{raw_target}"))
            if len(diff) > 30_000:
                return "self-modification refused: change is too large to review in one approval card"

            body = (f"Target: {raw_target}\n"
                    f"Current SHA-256: {old_hash}\n"
                    f"Proposed SHA-256: {proposed_hash}\n"
                    "Validation: Python syntax parsed successfully\n"
                    "The complete change follows:\n\n" + diff)
            try:
                try:
                    decision = gate.ask(
                        "install", "Approve Aidon source change", body,
                        force_ask=True, context=gate_context)
                except TypeError:
                    # Small gate adapters in embedding applications may not
                    # accept the optional context keyword. Keep force_ask.
                    decision = gate.ask(
                        "install", "Approve Aidon source change", body,
                        force_ask=True)
            except Exception as exc:
                return f"self-modification refused: approval failed ({type(exc).__name__})"
            if getattr(decision, "verdict", "") != "approved":
                return "self-modification refused: owner did not approve the source change"

            # Bind approval to both files, preserve the old version, then
            # atomically replace the destination. Recheck after the card
            # because the source or proposal may have changed while waiting.
            try:
                current_old = destination.read_bytes()
                current_proposal = proposal_path.read_bytes()
                if (hashlib.sha256(current_old).hexdigest() != old_hash
                        or hashlib.sha256(current_proposal).hexdigest() != proposed_hash):
                    return "self-modification aborted: source or proposal changed after approval"
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup = (Path(config.home) / "self_modify_backups" /
                          f"{stamp}_{old_hash[:12]}" / raw_target)
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(old_bytes)
                mode = stat.S_IMODE(destination.stat().st_mode)
                temp_name = None
                try:
                    with tempfile.NamedTemporaryFile(
                            mode="wb", dir=str(destination.parent),
                            prefix=f".{destination.name}.", suffix=".tmp",
                            delete=False) as temp_file:
                        temp_name = temp_file.name
                        temp_file.write(proposed_bytes)
                        temp_file.flush()
                        os.fsync(temp_file.fileno())
                    os.chmod(temp_name, mode)
                    # Serialize the final source check and replacement, so
                    # concurrent approved calls cannot overwrite each other.
                    with _SELF_MODIFY_LOCK:
                        if (hashlib.sha256(destination.read_bytes()).hexdigest()
                                != old_hash):
                            return ("self-modification aborted: source changed "
                                    "before replacement")
                        os.replace(temp_name, destination)
                finally:
                    if temp_name and os.path.exists(temp_name):
                        os.unlink(temp_name)
            except OSError as exc:
                return (f"self-modification failed ({type(exc).__name__}); "
                        "inspect the target and backup before retrying")
            return (f"Applied {raw_target}; backup: {backup}; "
                    "restart Aidon for the change to load")

        tools["self_modify"] = self_modify

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
        # Keep the same dictionary captured by mcp_connect and disconnect:
        # they register and remove tools dynamically during this goal.
        for name in list(tools):
            if name not in CORE_LADDER_TOOLS and name not in ("read_file", "write_file"):
                del tools[name]
    return tools
