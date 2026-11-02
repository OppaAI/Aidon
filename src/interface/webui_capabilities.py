"""Reporting, missions, capability inventory, and memory endpoints."""

import json
from pathlib import Path


_TOOL_DESCRIPTIONS = {
    "ask_user": "Ask you a question when a goal needs your choice or missing information.",
    "declare_blocked": "Report that a goal cannot proceed because a required dependency or decision is unavailable.",
    "schedule_job": "Create a scheduled or recurring Aidon goal.",
    "list_schedule": "List scheduled Aidon jobs.",
    "cancel_schedule": "Cancel a scheduled Aidon job.",
    "web_search": "Search the web for current information and sources.",
    "fetch_url": "Fetch and extract readable content from a web page.",
    "read_document": "Extract text from a local document such as a PDF or office file.",
    "search_document": "Search indexed documents for relevant passages.",
    "search_research_cache": "Search saved research results from earlier work.",
    "youtube_learn": "Find and learn from a YouTube video transcript.",
    "github_search": "Search GitHub repositories and source code.",
    "github_fetch": "Read a GitHub repository file or project details.",
    "make_bar_chart": "Create a bar chart from supplied data.",
    "make_line_chart": "Create a line chart from supplied data.",
    "make_chart": "Create a chart from supplied data.",
    "generate_document": "Create a formatted document in the workspace.",
    "build_ebook": "Build an EPUB ebook from Markdown sources.",
    "make_spreadsheet": "Create a spreadsheet file from supplied data.",
    "make_docx": "Create a Word document.",
    "make_slides": "Create a presentation deck.",
    "make_latex": "Compile a LaTeX document into a deliverable.",
    "record_system_audio": "Record system audio after you approve the recording.",
    "capture_webcam_photo": "Capture a webcam photo after you approve camera access.",
    "record_webcam": "Record a webcam clip after you approve camera access.",
    "tts_speak": "Generate spoken audio from text.",
    "transcribe_audio": "Convert an audio recording to text.",
    "email_make_draft": "Draft an email for your review without sending it.",
    "email_send": "Send an email after the required approval.",
    "slack_send": "Send a message or file to Slack.",
    "whatsapp_send": "Send a WhatsApp message.",
    "threads_publish": "Publish a post to Threads after approval.",
    "threads_post": "Publish a post to Threads after approval.",
    "instagram_publish": "Publish an image to Instagram after approval.",
    "instagram_post": "Publish an image to Instagram after approval.",
    "write_versioned": "Write a new version of a workspace file while preserving earlier versions.",
    "read_file": "Read a file from the current workspace.",
    "write_file": "Write or update a file in the current workspace.",
    "notify": "Add a notification to Aidon's notification outbox.",
    "sam_segment": "Segment objects in an image using the image segmentation model.",
    "uv_venv": "Create an isolated Python virtual environment for the task.",
    "install_python_package": "Install a Python package into the task's isolated environment.",
    "apt_install": "Install a system package after approval.",
    "adopt_tool_plugin": "Test and request approval to add a workspace tool plugin to Aidon.",
    "adopt_skill": "Test and request approval to add a reusable skill to Aidon.",
    "mcp_connect": "Connect to an MCP server, inspect its tools, and register the connection after approval.",
    "mcp_disconnect": "Close an MCP connection for the current run.",
    "self_modify": "Propose and apply an approved change to Aidon's own code.",
    "write": "Write a file inside the isolated task workspace.",
    "read": "Read a file inside the isolated task workspace.",
    "list": "List files inside the isolated task workspace.",
    "shell": "Run a command inside the isolated task sandbox.",
    "calculate": "Evaluate a mathematical expression.",
    "computer_use": "Interact with the computer through approved visual controls.",
}


class WebUICapabilityMixin:
    def handle_reports_list(self) -> dict:
        directory = self._json_dir("reports")
        days = sorted(path.stem for path in directory.glob("*.md"))
        return {"reports": days}

    def handle_report_get(self, day: str):
        """Return {"day", "content"} for a stored report, or None
        when no report exists for that day. The day is sanitised
        with _safe_name, so a traversal cannot escape reports/.
        """
        path = self._json_dir("reports") / f"{self._safe_name(day)}.md"
        if not path.is_file():
            return None
        return {"day": self._safe_name(day),
                "content": path.read_text(encoding="utf-8")}

    def handle_missions_list(self) -> dict:
        """Mission records stored under <home>/missions/<conversation>/
        mission.json. No missions is an empty list, never an error;
        a directory without a mission.json is skipped.
        """
        records = []
        missions_dir = self.home / "missions"
        if missions_dir.is_dir():
            for mission_dir in sorted(missions_dir.iterdir()):
                if not mission_dir.is_dir():
                    continue
                data = self._read_json_file(mission_dir / "mission.json")
                if data is not None:
                    records.append(data)
        return {"missions": records}

    def handle_mission_get(self, mission_id: str):
        from ..capabilities.mission import MissionStore
        try:
            return MissionStore(self.home).get(self._safe_name(mission_id))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return None

    def handle_mission_delete(self, mission_id: str) -> dict:
        import shutil
        safe = self._safe_name(mission_id)
        path = self.home / "missions" / safe
        if not path.is_dir():
            return {"ok": False}
        shutil.rmtree(path)
        return {"ok": True}

    def handle_capabilities(self) -> dict:
        """One view over tools available under the current config,
        plus skill library names and MCP server names. A source
        that is missing or unreadable contributes an empty list,
        never an error, so the route works on a fresh home.
        """
        try:
            tools = self.handle_tools()
            tool_names = list(tools.get("goal_tools", []))
            plugin_names = set(tools.get("plugin_tools", []))
        except Exception:
            tool_names = []
            plugin_names = set()
        side_effect_tools = {
            "schedule_job", "cancel_schedule", "slack_send", "whatsapp_send",
            "threads_publish", "threads_post", "instagram_publish",
            "instagram_post", "email_send", "record_system_audio", "notify",
            "uv_venv", "install_python_package", "apt_install",
            "adopt_tool_plugin", "adopt_skill", "mcp_connect", "mcp_disconnect", "self_modify", "tts_speak",
            "write_file", "write_versioned", "sam_segment",
        }
        tool_rows = []
        plugin_descriptions = {}
        try:
            from ..capabilities.plugins import PluginRegistry
            registry = PluginRegistry(self.home)
            registry.load_all()
            for name in plugin_names:
                plugin, spec = registry.tool_owner(name)
                description = (spec or {}).get("description") or getattr(plugin, "description", "")
                if description:
                    plugin_descriptions[name] = str(description)
        except Exception:
            pass
        for name in tool_names:
            tool_rows.append({
                "name": name,
                "description": (plugin_descriptions.get(name)
                                or _TOOL_DESCRIPTIONS.get(name)
                                or "Aidon tool: " + name.replace("_", " ") + "."),
                "origin": "plugin" if name in plugin_names else "core",
                "side_effect": name in side_effect_tools,
            })
        try:
            from ..capabilities.skill_library import SkillLibrary
            skills = [skill.to_dict() for skill in SkillLibrary(self.home).list(
                states=("candidate", "validated", "trusted", "retired"))]
        except Exception:
            skills = []
        mcp_servers = {}
        try:
            from ..capabilities.capability_registry import CapabilityRegistry
            for entry in CapabilityRegistry(self.home).list(kind="mcp"):
                mcp_servers[str(entry.get("name", ""))] = entry
        except Exception:
            pass
        try:
            data = self._read_json_file(self.home / "mcp_servers.json")
            if isinstance(data, dict):
                legacy = data.items()
            elif isinstance(data, list):
                legacy = ((str(item.get("name", "")), item) for item in data
                          if isinstance(item, dict))
            else:
                legacy = ()
            for name, config in legacy:
                if not name or name in mcp_servers or not isinstance(config, dict):
                    continue
                mcp_servers[name] = {"name": name, "status": "connected",
                                     "config": config, "kind": "mcp"}
        except Exception:
            pass
        for entry in mcp_servers.values():
            config = dict(entry.get("config") or {})
            safe = {key: value for key, value in config.items()
                    if key not in ("env", "token", "headers")}
            if config.get("env"):
                safe["environment_variables"] = sorted(
                    str(key) for key in dict(config["env"]))
            if config.get("token") or config.get("headers"):
                safe["authentication"] = "configured (secret hidden)"
            entry["config"] = safe
        try:
            dags = self.handle_dags_list().get("dags", [])
            dags = [{"name": item.get("id", item.get("name", "workflow")),
                     "goal": item.get("goal", "")}
                    for item in dags if isinstance(item, dict)]
        except Exception:
            dags = []
        return {"tools": tool_rows, "skills": skills,
                "mcp_servers": list(mcp_servers.values()),
                "counts": {"tools": len(tool_rows), "skills": len(skills),
                           "mcp": len(mcp_servers)},
                "dags": {"count": len(dags), "recent": dags[:30]}}

    def handle_capability_code(self, name: str, kind: str = "tool"):
        """Source or safe configuration for a named capability.

        Skills return stored Markdown and MCP entries return connection
        metadata with secrets removed. Tools return adopted plugin modules
        or importable core handlers. Goal-tool closures show their builder
        source because they are not importable directly.
        """
        import importlib
        import inspect
        import pkgutil
        wanted = str(name or "")
        if not wanted:
            return None
        kind = str(kind or "tool").strip().lower()
        if kind == "skill":
            try:
                from ..capabilities.skill_library import SkillLibrary
                skill = SkillLibrary(self.home).get(wanted)
                if skill is None:
                    return None
                path = (Path(self.home) / "skills" / f"{skill.name}.md")
                home = Path(self.home).resolve()
                resolved = path.resolve()
                if resolved.is_relative_to(home) and resolved.is_file():
                    source = resolved.read_text(encoding="utf-8")
                else:
                    source = skill.to_markdown()
                return {"name": wanted, "source": source,
                        "origin": f"skill · {skill.state}",
                        "note": "Stored Markdown instructions; skills are guidance, not executable code."}
            except (OSError, ValueError):
                return None
        if kind == "mcp":
            try:
                from ..capabilities.capability_registry import CapabilityRegistry
                entry = CapabilityRegistry(self.home).get(wanted)
                if not entry:
                    data = self._read_json_file(Path(self.home) / "mcp_servers.json")
                    if isinstance(data, dict):
                        config = data.get(wanted)
                    elif isinstance(data, list):
                        config = next((item for item in data
                                       if isinstance(item, dict)
                                       and str(item.get("name", "")) == wanted), None)
                    else:
                        config = None
                    if isinstance(config, dict):
                        entry = {"config": config, "status": "connected"}
                if not entry:
                    return None
                config = dict(entry.get("config") or {})
                # MCP environment and token values are never returned to the
                # browser. Only variable names make a useful, safe diagnostic.
                safe = {key: value for key, value in config.items()
                        if key not in ("env", "token", "headers")}
                if config.get("env"):
                    safe["environment_variables"] = sorted(
                        str(key) for key in config["env"])
                if config.get("token"):
                    safe["authentication"] = "configured (secret hidden)"
                return {"name": wanted, "source": json.dumps(safe, indent=2),
                        "origin": f"MCP · {entry.get('status', 'registered')}",
                        "note": "Connection details shown with secrets hidden."}
            except Exception:
                return None
        if kind != "tool":
            return None
        # Adopted plugin tools can show their own checked-in module source.
        try:
            from ..capabilities.plugins import PluginRegistry
            registry = PluginRegistry(self.home)
            registry.load_all()
            plugin, spec = registry.tool_owner(wanted)
            if plugin is not None and spec is not None:
                module = (Path(plugin.directory) / str(spec.get("module", ""))).resolve()
                if module.is_relative_to(Path(plugin.directory).resolve()) and module.is_file():
                    return {"name": wanted,
                            "source": module.read_text(encoding="utf-8"),
                            "origin": f"plugin · {plugin.name}",
                            "note": "Source from the adopted plugin module."}
        except (OSError, ValueError, AttributeError):
            pass
        # Goal-run tools are closures assembled by main.py, so they do not
        # exist as importable module-level functions. They are nevertheless
        # real tools; show their builder source instead of returning a
        # misleading 404 from the capabilities studio.
        try:
            from ..capabilities.tool_catalog import all_tool_names
            selfmod = (str(getattr(self.config, "tool_posture", "standard")
                           or "standard").strip().lower() == "selfmod"
                       and not getattr(self.config, "kernel_mode", False))
            if wanted in all_tool_names(selfmod=selfmod):
                if wanted in {"write", "read", "list", "shell", "calculate"}:
                    source_module = importlib.import_module("src.capabilities.tools")
                    source = inspect.getsource(source_module.ToolRegistry)
                    note = "Built-in sandbox tool; implementation is shown from ToolRegistry."
                else:
                    source_module = importlib.import_module("src.system.main")
                    source = inspect.getsource(source_module._goal_extra_tools)
                    note = "Goal tool assembled by _goal_extra_tools; implementation is shown from its builder."
                return {"name": wanted, "code": source, "origin": "core", "note": note}
        except (ImportError, OSError, TypeError):
            pass
        for package in ("src.capabilities", "src.agentic"):
            try:
                pkg = importlib.import_module(package)
            except Exception:
                continue
            for info in pkgutil.iter_modules(pkg.__path__):
                try:
                    module = importlib.import_module(f"{package}.{info.name}")
                except Exception:
                    continue
                candidate = getattr(module, wanted, None)
                if callable(candidate):
                    try:
                        return {"name": wanted,
                                "code": inspect.getsource(candidate)}
                    except (OSError, TypeError):
                        continue
        return None

    def handle_memory_points(self, collection: str, limit: int = 50) -> dict:
        """Points from one memory collection, as JSON-safe dicts.
        limit defaults to 50 and is capped at 200. No memory
        configured is an empty list, never an error.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        if self.memory is None:
            return {"points": []}
        try:
            points = self.memory.list_points(collection, limit=limit)
        except Exception as exc:
            return {"points": [], "total": 0,
                    "error": f"{type(exc).__name__}: {exc}"}
        out = []
        for point in points:
            out.append({"id": getattr(point, "id", ""),
                        "text": getattr(point, "text", ""),
                        "payload": dict(getattr(point, "payload", {}) or {})})
        # Keep the compact empty response compatible with older clients;
        # the studio already derives zero from an empty points list.
        if not out:
            return {"points": []}
        return {"points": out, "total": len(out),
                "mode": type(getattr(self.memory, "store", None)).__name__}

    def handle_memory_point_delete(self, collection: str, point_id: str) -> dict:
        """Forget one point, via the same forget path the studio
        memory handler uses. Unknown points (or no memory) report
        ok False, which the route maps to 404.
        """
        if self.memory is None:
            return {"ok": False}
        try:
            return {"ok": bool(self.memory.forget(collection, point_id))}
        except Exception:
            return {"ok": False}

