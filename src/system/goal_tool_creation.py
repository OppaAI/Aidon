"""File, document, media, and office tool registrations for goal runs."""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path

from ..capabilities.tools import ToolResult


def register_creation_tools(config, workdir, tools, approve, confined,
                            free_path, acq, scheduled=False):
    """Register output-producing tools with shared path and approval gates."""
    workdir = Path(workdir)
    _approval = approve
    _confined = confined
    _free_path = free_path
    _acq = acq
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

