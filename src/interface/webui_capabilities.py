"""Reporting, missions, capability inventory, and memory endpoints."""

import json


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
        for name in tool_names:
            tool_rows.append({
                "name": name,
                "description": ("Adopted plugin tool." if name in plugin_names
                                 else "Goal-run tool."),
                "origin": "plugin" if name in plugin_names else "core",
                "side_effect": name in side_effect_tools,
            })
        try:
            from ..capabilities.skill_library import SkillLibrary
            skill_names = [skill.name for skill in SkillLibrary(self.home).list()]
        except Exception:
            skill_names = []
        mcp_servers: list[str] = []
        try:
            data = self._read_json_file(self.home / "mcp_servers.json")
            if isinstance(data, dict):
                mcp_servers = [str(key) for key in data]
            elif isinstance(data, list):
                mcp_servers = [str(item.get("name", item)) if isinstance(item, dict)
                               else str(item) for item in data]
        except Exception:
            mcp_servers = []
        return {"tools": tool_rows, "skills": skill_names,
                "mcp_servers": mcp_servers}

    def handle_capability_code(self, name: str):
        """Python source for a named capability handler, or None
        when no importable handler carries that name.

        The search covers module-level callables in the
        capabilities and agentic packages (the handlers a plan
        step can actually name). Extra-tool closures built
        inside main._goal_extra_tools are not importable from
        the server — it holds no config — so they are not
        listed here; a name that only exists as such a closure
        is a 404, not a wrong source.
        """
        import importlib
        import inspect
        import pkgutil
        wanted = str(name or "")
        if not wanted:
            return None
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

