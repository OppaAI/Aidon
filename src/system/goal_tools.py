"""Goal-run tool construction, separated from CLI and service startup."""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from .config import Config
from .goal_tool_research import _deep_search_fns, _env_int, register_research_tools
from .goal_tool_creation import register_creation_tools
from .goal_tool_computer import register_computer_tools
from .goal_tool_mcp import register_mcp_tools
from ..capabilities.sandbox import Sandbox

_SELF_MODIFY_LOCK = threading.Lock()

# AIDON_OPTIONAL_TOOLS controls convenience built-ins. These tools expose
# specialized formats, model-backed media, indexed knowledge, or external
# services that core read/write/shell tools cannot provide alone. Their
# individual approval, credential, and publication boundaries still apply.
CAPABILITY_TOOLS_WHEN_OPTIONAL_DISABLED = frozenset({
    "edit_image", "sam_segment", "read_document", "generate_document",
    "build_ebook", "make_docx", "make_latex", "make_slides",
    "make_spreadsheet", "transcribe_audio", "tts_speak",
    "email_make_draft", "email_send", "slack_send", "slack_send_file",
    "whatsapp_send", "whatsapp_send_media", "threads_publish",
    "threads_post", "instagram_publish", "instagram_post",
    "youtube_learn", "youtube_upload", "fetch_feed", "search_document",
    "search_research_cache", "schedule_job", "list_schedule",
    "cancel_schedule", "self_modify",
})


def _goal_extra_tools(config, workdir, gate=None, researcher=None, question_answer=None, phrase=None, scheduled=False, model_chat=None, governor=None, optional_tools=None,
                      plugin_registry=None, gate_context=None, test_backend_factory=None,
                      allow_skill_adoption=False) -> dict:
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

    register_creation_tools(config, workdir, tools, _approval, _confined,
                            _free_path, _acq, scheduled=scheduled)
    # Compatibility names from Aidon.
    if "threads_publish" in tools:
        tools["threads_post"] = tools["threads_publish"]
    if "instagram_publish" in tools:
        tools["instagram_post"] = tools["instagram_publish"]

    register_computer_tools(config, tools, _approval, _confined, model_chat)

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
            workspace = workdir.resolve()
            raw = str(output or "").strip() or (
                f"aidon-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
            target = Path(raw).expanduser()
            if not target.is_absolute():
                target = workspace / raw.replace("\\", "/")
            target = target.resolve()
            if target != workspace and workspace not in target.parents:
                raise ValueError("output path must stay inside the goal workspace")
            if target.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                target = target.with_suffix(".png")
            return target

        def edit_image(prompt, image="", images=None, output="", size="1024x1024"):
            """Restyle or compose a photo. Saves in the goal workspace."""
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

    register_mcp_tools(config, tools, _approval, governor)

    try:
        from ..capabilities.plugins import (
            PluginRegistry, _existing_approved_plugin, adoption_gate, ask_plugin_approval,
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
            existing = _existing_approved_plugin(
                registry, preflight.plugin.name, preflight.approved_hash)
            if existing is not None:
                registry.plugins[existing.name] = existing
                return (f"Plugin {existing.name} is already adopted at these "
                        "approved bytes; continue the original goal")
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
                    f"{len(evidence['tests'])} test command(s) and "
                    f"{len(evidence.get('mutation_checks', []))} tool mutation check(s) "
                    f"passed in isolated {evidence['backend']} with network disabled. "
                    "Evidence is "
                    f"bound to candidate hash {evidence['source_hash']}.\n"
                    "Test quality: each declared tool must be called inside a result "
                    "assertion and pass a mutation check. This does not prove complete "
                    "coverage or safety; review the candidate test source before approval."
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

    if allow_skill_adoption:
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

    tools["ask_user"] = ask_user
    tools["declare_blocked"] = declare_blocked
    tools["schedule_job"] = schedule_job
    tools.update(register_research_tools(config, workdir, researcher, _confined))
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
            if (name not in CORE_LADDER_TOOLS
                    and name not in ("read_file", "write_file")
                    and name not in CAPABILITY_TOOLS_WHEN_OPTIONAL_DISABLED):
                del tools[name]
    return tools
