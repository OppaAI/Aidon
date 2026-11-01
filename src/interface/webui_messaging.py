"""WhatsApp and Slack transport handlers for the WebUI server."""

from __future__ import annotations

import hashlib
import hmac
import time

from .messaging import normalize_number, verify_whatsapp_signature
from .triggers import TriggerEvent


class WebUIMessagingMixin:
    def handle_whatsapp_get(self, query: dict) -> tuple[int, str]:
        mode = query.get("hub.mode", [""])[0]
        token = query.get("hub.verify_token", [""])[0]
        challenge = query.get("hub.challenge", [""])[0]
        if mode == "subscribe" and token and token == self.whatsapp_verify_token:
            return 200, challenge
        return 403, "forbidden"

    def handle_whatsapp_post(self, body: dict, raw: bytes = b"",
                             headers: dict | None = None) -> tuple[int, dict]:
        # Signature first, when an app secret is configured.
        headers = headers or {}
        if not verify_whatsapp_signature(
                raw, self.messaging_config.whatsapp_app_secret,
                headers.get("x-hub-signature-256", "")):
            # Do not echo the signature, request body, or credentials. This
            # marker distinguishes Meta delivery/signing failures from a
            # webhook URL or subscription that receives no POSTs at all.
            print("WhatsApp webhook rejected: signature verification failed")
            return 401, {"error": "bad signature"}
        # Owner gate, fail closed: only the owner number
        # becomes a goal, empty owner means inbound disabled.
        owner = normalize_number(self.messaging_config.whatsapp_owner)
        replies: list[str] = []
        try:
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    for message in change.get("value", {}).get("messages", []):
                        text = message.get("text", {}).get("body", "")
                        if not text and message.get("type") == "image":
                            caption = message.get("image", {}).get("caption", "")
                            media_id = message.get("image", {}).get("id", "")
                            text = caption
                            if self.media_downloader is not None and media_id:
                                try:
                                    data = self.media_downloader(media_id)
                                    from ..memory.library import Library
                                    tmp = self.home / "library" / "whatsapp-image.png"
                                    tmp.parent.mkdir(parents=True, exist_ok=True)
                                    tmp.write_bytes(data)
                                    library = Library(self.home)
                                    saved = library.add_file(tmp, kind="image", description="WhatsApp image")
                                    try:
                                        from ..capabilities.whatsapp_vision import prepare_vision_message
                                        vision = prepare_vision_message(str(tmp), caption)
                                        text = f"{caption} [image library_id={saved['id']} vision_image={vision['image_path']}]".strip()
                                    except Exception:
                                        text = f"{caption} [image library_id={saved['id']}]".strip()
                                except Exception as exc:
                                    text = caption or f"image received, could not save: {exc}"
                        elif not text and message.get("type") in ("document", "video", "audio"):
                            # Same download path as images (the Graph
                            # media-download API, two steps), no vision
                            # step: the file lands in the library and
                            # the goal text carries its library id.
                            mtype = message["type"]
                            media = message.get(mtype, {}) or {}
                            media_id = media.get("id", "")
                            caption = media.get("caption", "")
                            filename = media.get("filename", "") or f"whatsapp-{mtype}"
                            text = caption
                            if self.media_downloader is not None and media_id:
                                try:
                                    data = self.media_downloader(media_id)
                                    from ..memory.library import Library
                                    safe = self._safe_name(filename)
                                    tmp = self.home / "library" / f"whatsapp-{media_id}-{safe}"
                                    tmp.parent.mkdir(parents=True, exist_ok=True)
                                    tmp.write_bytes(data)
                                    library = Library(self.home)
                                    saved = library.add_file(tmp, kind="attachment",
                                                             description=f"WhatsApp {mtype}")
                                    text = (f"{caption} [file library_id={saved['id']} "
                                            f"kind={mtype}]").strip()
                                except Exception as exc:
                                    text = caption or f"{mtype} received, could not save: {exc}"
                        sender = message.get("from", "")
                        if not owner:
                            print("WhatsApp inbound ignored: owner is not configured")
                            continue
                        if normalize_number(sender) != owner:
                            print("WhatsApp inbound ignored: sender failed owner check")
                            continue
                        print("WhatsApp inbound owner message accepted")
                        decision_reply = self._resolve_decision(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_control(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_preapproval(text)
                        if decision_reply is None:
                            decision_reply = self._resolve_memory_command(text)
                        # An answer to a waiting question is not a new
                        # goal: deliver it to the waiter, ack in natural
                        # language, and skip dispatching a new run.
                        if decision_reply is None:
                            answered = self.answer_question(
                                "whatsapp:" + str(sender), text)
                            if answered is not None:
                                from .phrasing import question_ack as _qack
                                ack = _qack()
                                replies.append(ack)
                                if self.whatsapp_sender is not None:
                                    self.whatsapp_sender(sender, ack)
                                continue
                        from .surfaces import (HELP_TEXT as _WA_HELP,
                                                mode_enabled as _wa_mode_enabled,
                                                parse_mode_command as _wa_mode_cmd,
                                                parse_proceed_command as _wa_proceed,
                                                parse_research_command as _wa_research,
                                                parse_think_command as _wa_think,
                                                research_enabled as _wa_research_enabled,
                                                set_mode as _wa_set_mode,
                                                think_enabled as _wa_enabled,
                                                toggle_research as _wa_research_toggle,
                                                toggle_think as _wa_toggle)
                        from .mode_stops import (latest_mode_stop as _wa_latest_stop,
                                                 load_mode_stop as _wa_load_stop)
                        wa_key = "whatsapp:" + str(sender)
                        wa_think_max = False
                        wa_research_mode = False
                        wa_mode = None
                        wa_resume_stop_id = ""
                        wa_resume_spec = None
                        wa_resume_plan = None
                        if decision_reply is None:
                            wa_cmd = _wa_think(text)
                            if wa_cmd is not None:
                                if wa_cmd["action"] == "toggle":
                                    state = _wa_toggle(self.home, wa_key)
                                    decision_reply = (
                                        f"Maximum thinking {'on' if state else 'off'} "
                                        "for this chat.")
                                else:
                                    text = wa_cmd["goal"]
                                    wa_think_max = True
                            else:
                                wa_rcmd = _wa_research(text)
                                if wa_rcmd is not None:
                                    if wa_rcmd["action"] == "toggle":
                                        state = _wa_research_toggle(self.home, wa_key)
                                        decision_reply = (
                                            f"Deep-research mode {'on' if state else 'off'} "
                                            "for this chat.")
                                    else:
                                        text = wa_rcmd["goal"]
                                        wa_research_mode = True
                                elif text.strip() == "/help":
                                    decision_reply = _WA_HELP
                            if decision_reply is None:
                                wa_mcmd = _wa_mode_cmd(text)
                                if wa_mcmd is not None:
                                    if wa_mcmd["action"] == "set":
                                        _wa_set_mode(self.home, wa_key, wa_mcmd["mode"])
                                        if wa_mcmd["mode"] == "execute":
                                            decision_reply = "Execute mode on for this chat — goals run directly."
                                        else:
                                            decision_reply = (f"{wa_mcmd['mode'].capitalize()} mode on "
                                                              "for this chat.")
                                    else:
                                        text = wa_mcmd["goal"]
                                        wa_mode = wa_mcmd["mode"]
                                else:
                                    wa_pcmd = _wa_proceed(text)
                                    if wa_pcmd is not None:
                                        _stop = (_wa_load_stop(self.home, wa_pcmd["stop_id"])
                                                 if wa_pcmd["stop_id"]
                                                 else _wa_latest_stop(self.home, wa_key))
                                        if _stop is None:
                                            if wa_pcmd["stop_id"]:
                                                decision_reply = (
                                                    "I couldn't find that stopped plan. Reply proceed "
                                                    "without an ID to continue the latest "
                                                    "stopped plan in this chat.")
                                            else:
                                                decision_reply = "No stopped run to proceed with."
                                        else:
                                            text = _stop["goal"]
                                            wa_mode = "execute"
                                            wa_resume_stop_id = _stop["stop_id"]
                                            wa_resume_spec = _stop.get("spec")
                                            wa_resume_plan = _stop.get("plan")
                        if decision_reply is None:
                            text, force_web = self._force_web_prefix(text)
                            if not text:
                                decision_reply = "Usage: /web <question>"
                            else:
                                decision_reply = None
                        if decision_reply is not None:
                            replies.append(decision_reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, decision_reply)
                            continue
                        local_replies: list[str] = []
                        _wa_payload = {"from": sender, "conversation_id": wa_key,
                                       "force_web": force_web,
                                       "think_max": wa_think_max or _wa_enabled(self.home, wa_key),
                                       "research_mode": wa_research_mode or _wa_research_enabled(self.home, wa_key),
                                       "mode": wa_mode or _wa_mode_enabled(self.home, wa_key) or "plan"}
                        if wa_resume_stop_id:
                            _wa_payload["resume_stop_id"] = wa_resume_stop_id
                        if isinstance(wa_resume_spec, dict):
                            _wa_payload["resume_spec"] = wa_resume_spec
                        if isinstance(wa_resume_plan, dict):
                            _wa_payload["resume_plan"] = wa_resume_plan
                        event = TriggerEvent(source="whatsapp", kind="message",
                                             goal=text, conversation_id=wa_key, payload=_wa_payload,
                                             reply=local_replies.append)
                        self._record_surface_exchange(wa_key, "user", text)
                        try:
                            self.dispatcher.dispatch(event)
                        except Exception as exc:
                            # Meta's webhook response is not delivered to the
                            # sender as a chat message. Surface shared goal
                            # handler failures on the WhatsApp conversation.
                            print(f"WhatsApp goal handler failed: "
                                  f"{type(exc).__name__}")
                            error_reply = (
                                "Aidon couldn't process that message because its "
                                "run encountered an internal error. Check the "
                                "Aidon logs, then send it again.")
                            replies.append(error_reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, error_reply)
                            continue
                        for reply in local_replies:
                            replies.append(reply)
                            self._record_surface_exchange(wa_key, "assistant", reply)
                            if self.whatsapp_sender is not None:
                                self.whatsapp_sender(sender, reply)
        except Exception as exc:
            return 200, {"error": str(exc)}
        return 200, {"replies": replies}

    def _record_surface_exchange(self, conversation_id: str, role: str, text: str) -> None:
        """Best-effort conversation-store record for a non-WebUI surface."""
        try:
            self._conversations_db().record_exchange(conversation_id, role, text)
        except Exception:
            pass

    def _simple_command_response(self, conversation_id: str, goal: str,
                               text: str) -> dict:
        """Reply to a chat command without running a goal.

        Same shape as a mirror-command reply: the assistant text is
        recorded in the conversation and a done run carries it to the
        frontend, so /think and /help behave like any other answer.
        """
        try:
            self._conversations_db().record_exchange(conversation_id, "assistant", text)
        except Exception:
            pass
        if self.memory is not None:
            try:
                self.memory.record_stm("assistant", text,
                                       conversation_id=conversation_id)
            except Exception:
                pass
        from .run_control import new_run_id
        run_id = new_run_id()
        self._web_runs[run_id] = {"run_id": run_id, "goal": goal,
                                  "conversation_id": conversation_id, "status": "done",
                                  "events": [{"kind": "aidon-final", "text": text,
                                              "stream_id": ""}], "trace": []}
        return {"run_id": run_id, "conversation_id": conversation_id, "goal": goal}

    def _slack_mirror_command(self, channel: str, text: str) -> str | None:
        """Handle a /mirror command from Slack; None when not one."""
        from .surfaces import (conversation_for_slack, link_slack, unlink_slack,
                               mirror_status_text, parse_mirror_command,
                               set_whatsapp_mirror)
        command = parse_mirror_command(text)
        if command is None:
            return None
        action = command["action"]
        if action == "link_webui":
            link_slack(self.home, command["conversation"], channel)
            return (f"Linked this channel to WebUI conversation "
                    f"{command['conversation']}.")
        if action == "unlink_slack":
            conversation = conversation_for_slack(self.home, channel)
            if conversation and unlink_slack(self.home, conversation):
                return "Slack link removed."
            return "No Slack link to remove."
        if action == "whatsapp":
            conversation = (conversation_for_slack(self.home, channel)
                            or ("slack:" + channel))
            set_whatsapp_mirror(self.home, conversation, command["enabled"])
            return ("WhatsApp mirror on for this channel."
                    if command["enabled"]
                    else "WhatsApp mirror off for this channel.")
        if action == "link_slack":
            return "From Slack, link with /mirror webui <conversation-id>."
        return mirror_status_text(self.home, channel=channel)

    def handle_slack_post(self, raw: bytes, headers: dict, body: dict) -> tuple[int, dict]:
        # Verify signature before parsing any event as a goal.
        # Fail closed: without a configured signing secret no
        # Slack request is verified, so none is dispatched.
        if not self.slack_signing_secret:
            return 401, {"error": "slack signing secret not configured"}
        if self.slack_signing_secret:
            timestamp = headers.get("x-slack-request-timestamp", "")
            signature = headers.get("x-slack-signature", "")
            try:
                if abs(time.time() - int(timestamp)) > 300:
                    return 401, {"error": "stale timestamp"}
            except ValueError:
                return 401, {"error": "bad timestamp"}
            base = f"v0:{timestamp}:".encode() + raw
            expected = "v0=" + hmac.new(
                self.slack_signing_secret.encode(), base, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, signature):
                return 401, {"error": "bad signature"}
        if body.get("type") == "url_verification":
            return 200, {"challenge": body.get("challenge", "")}
        return self.handle_slack_message(
            body, dispatch=getattr(self, "slack_message_dispatch", None))

    def handle_slack_message(self, body: dict, dispatch=None) -> tuple[int, dict]:
        """Handle a message already authenticated by webhook or bot polling.

        A polling caller supplies durable background dispatch so approvals,
        questions, and mode commands remain reachable while a goal runs.
        """
        event_body = body.get("event", {})
        if (event_body.get("type") in ("message", "app_mention")
                and not event_body.get("bot_id")
                and event_body.get("subtype") in (None, "", "file_share", "thread_broadcast")):
            text = str(event_body.get("text", ""))
            channel = str(event_body.get("channel", ""))
            thread_ts = event_body.get("thread_ts") or event_body.get("ts")
            # DMs remain conversational. In channels, require the exact bot
            # mention before parsing commands, answering questions, or
            # accepting attachments. Never fall back to another user's
            # mention or to a filename-only post.
            bot_user_id = ""
            if not channel.startswith("D"):
                try:
                    from .messaging import (slack_bot_user_id as _bot_id,
                                            slack_message_for_bot as _addressed,
                                            strip_slack_bot_mention as _strip_mention)
                    bot_user_id = _bot_id(self.messaging_config)
                    if (event_body.get("type") != "app_mention"
                            and not _addressed(text, channel, bot_user_id)):
                        return 200, {"ok": True, "ignored": "not_mentioned"}
                    text = _strip_mention(text, bot_user_id)
                except Exception:
                    return 200, {"ok": True, "ignored": "not_mentioned"}
            decision_reply = self._resolve_decision(text)
            if decision_reply is None:
                decision_reply = self._resolve_control(text)
            if decision_reply is None:
                decision_reply = self._resolve_preapproval(text)
            if decision_reply is None:
                decision_reply = self._resolve_memory_command(text)
            mirror_reply = self._slack_mirror_command(channel, text)
            if mirror_reply is not None:
                if self.slack_sender is not None:
                    self.slack_sender(channel, mirror_reply, thread_ts)
                self._record_surface_exchange("slack:" + channel, "user", text)
                self._record_surface_exchange("slack:" + channel, "assistant", mirror_reply)
                return 200, {"replies": [mirror_reply]}
            from .surfaces import (HELP_TEXT as _SL_HELP,
                                    mode_enabled as _sl_mode_enabled,
                                    parse_mode_command as _sl_mode_cmd,
                                    parse_proceed_command as _sl_proceed,
                                    parse_research_command as _sl_research,
                                    parse_think_command as _sl_think,
                                    research_enabled as _sl_research_enabled,
                                    set_mode as _sl_set_mode,
                                    think_enabled as _sl_enabled,
                                    toggle_research as _sl_research_toggle,
                                    toggle_think as _sl_toggle)
            from .mode_stops import (latest_mode_stop as _sl_latest_stop,
                                     load_mode_stop as _sl_load_stop)
            sl_key = "slack:" + channel
            sl_think_max = False
            sl_research_mode = False
            sl_mode = None
            sl_resume_stop_id = ""
            sl_resume_spec = None
            sl_resume_plan = None
            if decision_reply is None:
                sl_cmd = _sl_think(text)
                if sl_cmd is not None:
                    if sl_cmd["action"] == "toggle":
                        state = _sl_toggle(self.home, sl_key)
                        decision_reply = (f"Maximum thinking {'on' if state else 'off'} "
                                          "for this channel.")
                    else:
                        text = sl_cmd["goal"]
                        sl_think_max = True
                else:
                    sl_rcmd = _sl_research(text)
                    if sl_rcmd is not None:
                        if sl_rcmd["action"] == "toggle":
                            state = _sl_research_toggle(self.home, sl_key)
                            decision_reply = (f"Deep-research mode {'on' if state else 'off'} "
                                              "for this channel.")
                        else:
                            text = sl_rcmd["goal"]
                            sl_research_mode = True
                    elif text.strip() == "/help":
                        decision_reply = _SL_HELP
                if decision_reply is None:
                    sl_mcmd = _sl_mode_cmd(text)
                    if sl_mcmd is not None:
                        if sl_mcmd["action"] == "set":
                            _sl_set_mode(self.home, sl_key, sl_mcmd["mode"])
                            if sl_mcmd["mode"] == "execute":
                                decision_reply = "Execute mode on for this channel — goals run directly."
                            else:
                                decision_reply = (f"{sl_mcmd['mode'].capitalize()} mode on "
                                                  "for this channel.")
                        else:
                            text = sl_mcmd["goal"]
                            sl_mode = sl_mcmd["mode"]
                    else:
                        sl_pcmd = _sl_proceed(text)
                        if sl_pcmd is not None:
                            _stop = (_sl_load_stop(self.home, sl_pcmd["stop_id"])
                                     if sl_pcmd["stop_id"]
                                     else _sl_latest_stop(self.home, sl_key))
                            if _stop is None:
                                if sl_pcmd["stop_id"]:
                                    decision_reply = (
                                        "I couldn't find that stopped plan. Reply proceed "
                                        "without an ID to continue the latest "
                                        "stopped plan in this channel.")
                                else:
                                    decision_reply = "No stopped run to proceed with."
                            else:
                                text = _stop["goal"]
                                sl_mode = "execute"
                                sl_resume_stop_id = _stop["stop_id"]
                                sl_resume_spec = _stop.get("spec")
                                sl_resume_plan = _stop.get("plan")
            force_web = False
            if decision_reply is None:
                text, force_web = self._force_web_prefix(text)
                if not text:
                    decision_reply = "Usage: /web <question>"
            if decision_reply is None:
                # An answer to a waiting question is not a new goal.
                answered = self.answer_question("slack:" + channel, text)
                if answered is not None:
                    from .phrasing import question_ack as _qack
                    ack = _qack()
                    if self.slack_sender is not None:
                        self.slack_sender(channel, ack, thread_ts)
                    self._record_surface_exchange("slack:" + channel, "user", text)
                    self._record_surface_exchange("slack:" + channel, "assistant", ack)
                    return 200, {"replies": [ack]}
            if decision_reply is not None:
                if self.slack_sender is not None:
                    self.slack_sender(channel, decision_reply, thread_ts)
                return 200, {"replies": [decision_reply]}
            caption = text
            replies: list[str] = []
            # Image intake, same shape as parse_slack_event: image
            # files ride with the goal text as [image ...] markers,
            # other files as [file ...] markers. Decision replies
            # above already ran on the raw text, so replacing the
            # goal text here cannot change an approval verdict.
            # When the server's messaging config carries a Slack
            # bot token, the file bytes are downloaded here
            # (mirroring the WhatsApp path: library save + vision
            # message for images, library save for documents);
            # without a token, markers + metadata only.
            payload: dict = {}
            try:
                from .slack_inbound import parse_slack_event as _parse_slack
                _parsed = _parse_slack(body, bot_user_id=bot_user_id)
                if _parsed is not None and (_parsed.payload.get("images")
                                           or _parsed.payload.get("files")):
                    text = _parsed.goal
                    payload = _parsed.payload
            except Exception:
                pass
            if payload.get("images") and self.messaging_config.slack_bot_token:
                from .messaging import download_slack_file as _dl_slack
                image_paths: list[str] = []
                for image in payload["images"]:
                    url = image.get("url_private", "")
                    if not url:
                        continue
                    try:
                        data = _dl_slack(self.messaging_config.slack_bot_token, url)
                    except Exception:
                        continue
                    try:
                        from ..memory.library import Library
                        ext = {"image/png": "png", "image/jpeg": "jpg",
                               "image/gif": "gif", "image/webp": "webp"}.get(
                            image.get("mimetype", ""), "png")
                        tmp = self.home / "library" / f"slack-{image['id']}.{ext}"
                        tmp.parent.mkdir(parents=True, exist_ok=True)
                        tmp.write_bytes(data)
                        # The downloaded file must be passed as a real vision
                        # attachment to GoalRunner. A library/vision marker in
                        # goal text alone neither sends image bytes to the model
                        # nor activates the runner's image-context isolation.
                        image_paths.append(str(tmp))
                        library = Library(self.home)
                        saved = library.add_file(tmp, kind="image", description="Slack image")
                        marker = (f"[image slack_file_id={image['id']} "
                                  f"mimetype={image['mimetype']} name={image['name']}]")
                        try:
                            from ..capabilities.whatsapp_vision import prepare_vision_message
                            vision = prepare_vision_message(
                                str(tmp), caption or "Analyze the attached image.")
                            replacement = (f"[image library_id={saved['id']} "
                                           f"vision_image={vision['image_path']}]")
                        except Exception:
                            replacement = f"[image library_id={saved['id']}]"
                        text = text.replace(marker, replacement)
                        image["downloaded"] = True
                    except Exception:
                        continue
                if image_paths:
                    payload["image_paths"] = image_paths
            # Non-image files: same download, no vision step — the
            # document lands in the library and the marker carries
            # its library id, so the run can read it.
            if payload.get("files") and self.messaging_config.slack_bot_token:
                from .messaging import download_slack_file as _dl_slack_file
                for shared in payload["files"]:
                    url = shared.get("url_private", "")
                    if not url:
                        continue
                    try:
                        data = _dl_slack_file(self.messaging_config.slack_bot_token, url)
                    except Exception:
                        continue
                    try:
                        from ..memory.library import Library
                        safe = self._safe_name(str(shared.get("name", "slack-file")))
                        tmp = self.home / "library" / f"slack-{shared['id']}-{safe}"
                        tmp.parent.mkdir(parents=True, exist_ok=True)
                        tmp.write_bytes(data)
                        library = Library(self.home)
                        saved = library.add_file(tmp, kind="attachment",
                                                 description="Slack file")
                        marker = (f"[file slack_file_id={shared['id']} "
                                  f"mimetype={shared['mimetype']} name={shared['name']}]")
                        text = text.replace(
                            marker, f"[file library_id={saved['id']}]")
                        shared["downloaded"] = True
                    except Exception:
                        continue
            payload["force_web"] = force_web
            payload["think_max"] = sl_think_max or _sl_enabled(self.home, sl_key)
            payload["research_mode"] = sl_research_mode or _sl_research_enabled(self.home, sl_key)
            payload["mode"] = sl_mode or _sl_mode_enabled(self.home, sl_key) or "plan"
            if sl_resume_stop_id:
                payload["resume_stop_id"] = sl_resume_stop_id
            if isinstance(sl_resume_spec, dict):
                payload["resume_spec"] = sl_resume_spec
            if isinstance(sl_resume_plan, dict):
                payload["resume_plan"] = sl_resume_plan
            payload["channel"] = channel
            payload["thread_ts"] = thread_ts
            payload["slack_ts"] = str(event_body.get("ts", ""))
            payload["conversation_id"] = "slack:" + channel
            event = TriggerEvent(source="slack", kind="message", goal=text,
                                 conversation_id="slack:" + channel,
                                 payload=payload,
                                 reply=replies.append)
            if dispatch is not None:
                dispatch(event)
                return 200, {"queued": True}
            try:
                self.dispatcher.dispatch(event)
            except Exception as exc:
                return 200, {"error": str(exc)}
            if self.slack_sender is not None:
                for reply in replies:
                    self.slack_sender(channel, reply, thread_ts)
            self._record_surface_exchange("slack:" + channel, "user", text)
            for reply in replies:
                self._record_surface_exchange("slack:" + channel, "assistant", reply)
            return 200, {"replies": replies}
        return 200, {"ok": True}
