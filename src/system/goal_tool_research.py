"""Research and communication tools available during goal runs."""

from __future__ import annotations

import os


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


def register_research_tools(config, workdir, researcher, confined):
    """Build research, document, messaging, feed, and schedule tools.

    Dependencies are supplied by the goal tool factory so path checks and
    per-run research configuration stay shared with the rest of the toolset.
    """
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
            target = confined(str(file))
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
            target = confined(str(file))
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

    return {
        "web_search": web_search,
        "fetch_url": fetch_url,
        "read_document": read_document,
        "search_document": search_document,
        "search_research_cache": search_research_cache,
        "deep_research": deep_research_tool,
        "fetch_feed": fetch_feed,
        "slack_send": slack_send,
        "whatsapp_send": whatsapp_send,
        "slack_send_file": slack_send_file,
        "whatsapp_send_media": whatsapp_send_media,
        "list_schedule": list_schedule,
        "cancel_schedule": cancel_schedule,
    }
