"""Tool dispatch and input normalization for GoalRunner."""

from __future__ import annotations

import re
from pathlib import Path


def _normalize_shell_args(args: dict) -> dict:
    """Coerce a shell step's command to the backend's list form.

    The planner emits commands as strings and plan validation accepts
    them (it only rejects empty commands), but the execution-backend
    branch requires a non-empty list — and it ran before the shlex
    normalization further down, so every string command died on
    backend runs while the sandbox path worked. Normalize up front
    so validation, backend, and sandbox all agree on the form.
    """
    if not isinstance(args, dict):
        return args
    out = dict(args)
    if "command" not in out and "cmd" in out:
        out["command"] = out["cmd"]
    if isinstance(out.get("command"), str):
        import shlex
        out["command"] = shlex.split(out["command"])
    return out


class ToolDispatchMixin:
    def _call_tool(self, name: str, args: dict) -> str:
        """Sandbox tools first, then adopted plugin tools, else unknown.

        Shell steps, when an execution backend is set, run
        through that backend, not through the sandbox
        directly, so a Docker run is recorded as Docker,
        and a Docker failure is a step failure, not a
        silent fallback to a host shell.
        """
        if self.kernel_mode:
            from .kernel import kernel_allowed
            if not kernel_allowed(name, True):
                raise ValueError(f"kernel mode: tool {name} is not a seed tool")
        if name == "shell":
            args = _normalize_shell_args(args)
        if name == "shell":
            # Mandatory-approval commands always need a human,
            # even in autonomous mode: recursive delete, piped
            # download-to-shell, sudo, raw disk writes, and the
            # rest of the security gate's list. The kind is
            # "mandatory", not "live": a pre-approval granted for a
            # reversible live run must never cover one of these.
            from ..capabilities.security_gate import command_needs_mandatory_approval
            command_text = " ".join(str(c) for c in args.get("command", [])) if isinstance(args.get("command"), list) else str(args.get("command", ""))
            reason = command_needs_mandatory_approval(command_text)
            if reason:
                decision = self._ask_for_approval(
                    "mandatory", f"Mandatory approval: shell ({reason})",
                    self._approval_body(f"Run shell command: {command_text}?"),
                    force_ask=True,
                )
                if decision.verdict not in ("approved", "auto_approved"):
                    raise ValueError(f"shell command rejected by approval gate ({reason})")
        if name == "shell" and self.execution_backend is not None:
            command = args["command"]
            if not isinstance(command, list) or not command:
                raise ValueError("shell command must be a non-empty list")
            result = self.execution_backend.run(
                [str(c) for c in command], self.sandbox.workdir,
                timeout_seconds=self.sandbox.timeout_seconds)
            if not result.ok:
                raise ValueError(f"shell failed: {result.stderr[:200]}")
            return result.stdout
        # Normalize harmless model naming variations at the execution
        # boundary. Deterministic validation still rejects missing paths or
        # empty shell commands before they reach this point.
        if isinstance(args, dict):
            args = dict(args)
            if name in ("write", "write_file"):
                if not args.get("file"):
                    for alias in ("name", "path", "input_path", "filename"):
                        if args.get(alias):
                            args["file"] = args[alias]
                            break
                if "text" not in args and "content" in args:
                    args["text"] = args["content"]
            elif name == "shell" and isinstance(args.get("command"), str):
                import shlex
                args["command"] = shlex.split(args["command"])
        try:
            if name == "shell" and isinstance(args, dict):
                command_text = " ".join(str(x) for x in
                                        (args.get("command") or []))
                # In kernel mode generate_document is NOT available
                # (seed tools only), so the guard must not point at
                # it: it points at the plugin ladder instead. A
                # misleading "use the available tool" message here
                # sent a real run in circles.
                if self.kernel_mode:
                    _pdf_note = ("Kernel mode: generate_document is not "
                                 "available here (seed tools only). Earn "
                                 "the capability via the plugin ladder: "
                                 "research, draft a plugin or skill, "
                                 "sandbox-test it, adopt it with owner "
                                 "approval.")
                else:
                    _pdf_note = ("use the available generate_document "
                                 "tool instead")
                if ("fpdf" in command_text.lower() and
                        "generate_document" in self.extra_tools):
                    raise ValueError(
                        "Do not use shell/fpdf for PDF output; "
                        f"{_pdf_note}")
                if "generate_document" in self.extra_tools:
                    # The text check above misses the common bypass:
                    # write a script file importing fpdf, then run it
                    # by name (``python3 make_pdf.py`` contains no
                    # "fpdf"). Refuse to run a workdir script that
                    # imports a PDF library when the document tool
                    # already writes PDFs with no install needed.
                    import re as _re
                    workdir = getattr(self.sandbox, "workdir", None)
                    for token in (args.get("command") or []):
                        if not str(token).endswith(".py"):
                            continue
                        try:
                            candidate = Path(str(token))
                            if not candidate.is_absolute():
                                candidate = Path(workdir) / candidate
                            elif workdir is not None:
                                # Only police scripts inside the run's
                                # workdir; host scripts are not the
                                # planner's PDF bypass.
                                resolved = candidate.resolve()
                                root = Path(workdir).resolve()
                                if resolved != root and root not in resolved.parents:
                                    continue
                            source = candidate.read_text(
                                encoding="utf-8", errors="replace")
                        except Exception:
                            continue
                        if _re.search(r"^\s*(?:import|from)\s+(fpdf|reportlab)\b",
                                      source, _re.MULTILINE):
                            raise ValueError(
                                "Do not run a script importing fpdf or "
                                "reportlab for PDF output; "
                                f"{_pdf_note}")
            return self.tools.call(name, args)
        except ValueError as exc:
            if str(exc).startswith("unknown tool:") and self.plugins is not None:
                try:
                    def _approve_plugin_call(plugin, spec, call_args,
                                             attachments):
                        import json as _json

                        def _redact(value, key=""):
                            if re.search(
                                    r"(?:api[_-]?key|token|password|secret|authorization)",
                                    key, re.IGNORECASE):
                                return "[redacted]"
                            if isinstance(value, dict):
                                return {str(k): _redact(v, str(k))
                                        for k, v in value.items()}
                            if isinstance(value, (list, tuple)):
                                return [_redact(item) for item in value]
                            return value
                        try:
                            shown_args = _json.dumps(
                                _redact(call_args), ensure_ascii=False,
                                sort_keys=True)
                        except (TypeError, ValueError):
                            shown_args = str(call_args)
                        capabilities = [str(c) for c in
                                        getattr(plugin, "capabilities", [])]
                        attached = [Path(p).name for p in attachments
                                    if Path(p).is_file()]
                        body = (
                            f"Plugin: {plugin.name} {plugin.version}\n"
                            f"Tool: {name}\n"
                            f"Capabilities: {', '.join(capabilities) or 'none'}\n"
                            f"Arguments: {shown_args[:2400]}\n"
                            f"Attached images passed to the sandbox: "
                            f"{', '.join(attached) if attached else 'none'}\n\n"
                            "The isolated plugin process can access its "
                            "approved secrets and network capability for "
                            "this call. Network services may receive the "
                            "listed arguments and images and may incur charges. "
                            "Approve this specific invocation?")
                        decision = self._ask_for_approval(
                            "live", f"Run sandbox plugin {name}",
                            self._approval_body(body), force_ask=True)
                        return decision.verdict in (
                            "approved", "auto_approved")
                    return self.plugins.call(
                        name, args,
                        context_factory=lambda plugin: self.plugins.context_for(
                            name, self.home,
                            sandbox=getattr(self, "sandbox", None),
                            network_available=self.network_available),
                        sandbox=getattr(self, "sandbox", None),
                        attachments=getattr(self, "image_paths", []),
                        approval_callback=_approve_plugin_call,
                        execution_backend=getattr(self, "execution_backend", None))
                except ValueError as plugin_exc:
                    if str(plugin_exc).startswith("unknown plugin tool:"):
                        pass
                    else:
                        raise ValueError(
                            f"plugin tool {name} failed: {type(plugin_exc).__name__}") from plugin_exc
                except Exception as plugin_exc:
                    raise ValueError(
                        f"plugin tool {name} failed: {type(plugin_exc).__name__}") from plugin_exc
            if str(exc).startswith("unknown tool:") and name in self.extra_tools:
                # Extra tools are outside the confined set, so a
                # post or send tool is approval-gated here, before
                # dispatch, fail-closed on rejection.
                if any(word in name for word in ("post", "send", "publish")):
                    decision = self._ask_for_approval(
                        "live", f"Extra tool {name}",
                        self._approval_body(f"Run extra tool {name} with {args}?"),
                        action_class="publish",   # can graduate after threshold
                    )
                    if decision.verdict not in ("approved", "auto_approved"):
                        raise ValueError(f"extra tool {name} rejected by approval gate")
                from ..capabilities.extra_tools import dispatch_extra_tool
                return dispatch_extra_tool(self.extra_tools, name, args)
            if str(exc).startswith("unknown tool:"):
                # No registered tool covers it: hand the run a
                # binding re-plan instruction instead of a bare
                # error, from the capability ladder (exact tool,
                # then skill, then approved MCP).
                from ..capabilities.capability_lookup import (
                    lookup_capability, recovery_note)
                tool_rows = {t: "" for t in self.tools.names()}
                if self.plugins is not None:
                    try:
                        for t in self.plugins.tool_names():
                            tool_rows.setdefault(t, "")
                    except Exception:
                        pass
                # The ladder's skill and MCP rungs used to be dead
                # here: only tools were passed, so skills and
                # approved MCP servers could never match. Pass all
                # three, from the same live sources triage uses.
                skill_rows = []
                if self.plugins is not None:
                    try:
                        for _name, _text in (self.plugins.skill_texts() or {}).items():
                            skill_rows.append({"name": str(_name),
                                               "description": str(_text or "")[:160]})
                    except Exception:
                        pass
                mcp_rows = []
                try:
                    import json as _mcp_json
                    _mcp_data = _mcp_json.loads(
                        (self.home / "mcp_servers.json").read_text())
                    if isinstance(_mcp_data, dict):
                        mcp_rows = [str(k) for k in _mcp_data]
                    elif isinstance(_mcp_data, list):
                        mcp_rows = [str(i.get("name", i)) if isinstance(i, dict)
                                    else str(i) for i in _mcp_data]
                except Exception:
                    pass
                matches = lookup_capability(name, tools=tool_rows,
                                            skills=skill_rows,
                                            mcp_servers=mcp_rows)
                raise ValueError(f"unknown tool: {name}\n{recovery_note(name, matches)}")
            raise
