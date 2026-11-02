"""HTTP request handler factory for the WebUI server."""

def make_handler(server):
    # Imported lazily to avoid a module cycle during WebUI initialization.
    from . import webui as _webui
    BaseHTTPRequestHandler = _webui.BaseHTTPRequestHandler
    DAG_HTML = _webui.DAG_HTML
    FACTORY_HTML = _webui.FACTORY_HTML
    REPLAY_HTML = _webui.REPLAY_HTML
    STATIC_DIR = _webui.STATIC_DIR
    VRM_HTML = _webui.VRM_HTML
    _api_key_row = _webui._api_key_row
    _app_version = _webui._app_version
    _page_html = _webui._page_html
    import html, json, mimetypes
    from pathlib import Path
    from urllib.parse import parse_qs, quote, unquote, urlparse


    class Handler(BaseHTTPRequestHandler):

        def _send_json(self, status: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_html(self, status: int, html: str):
            if "</head>" in html and "/static/theme.js" not in html:
                html = html.replace(
                    "</head>", '<script src="/static/theme.js"></script></head>', 1)
            data = html.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send_bytes(self, status: int, content_type: str, data: bytes, extra_headers: dict | None=None):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_static(self, relative: str):
            """Serve only files from the bundled WebUI assets."""
            candidate = (STATIC_DIR / relative).resolve()
            root = STATIC_DIR.resolve()
            if root not in candidate.parents or not candidate.is_file():
                self._send_json(404, {'error': 'not found'})
                return
            data = candidate.read_bytes()
            if candidate.suffix.lower() == '.html':
                asset_times = [candidate.stat().st_mtime]
                for asset_name in ('style.css', 'theme.js', 'app.js', 'avatar.js'):
                    asset = STATIC_DIR / asset_name
                    if asset.is_file():
                        asset_times.append(asset.stat().st_mtime)
                version = str(int(max(asset_times))).encode()
                data = data.replace(b'__ASSET_V__', version)
                data = data.replace(
                    b'</head>', b'<script src="/static/theme.js?v=' + version + b'"></script></head>', 1)
            data = data.replace(b'__AIDON_VERSION__', _app_version().encode())
            content_type = mimetypes.guess_type(str(candidate))[0] or 'application/octet-stream'
            self._send_bytes(200, content_type, data)

        def _read_body(self) -> bytes:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) > 1 or self.headers.get('Transfer-Encoding'):
                raise ValueError('invalid request framing')
            raw_length = lengths[0] if lengths else '0'
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise ValueError('invalid content length')
            length = int(raw_length)
            if length > 80 * 1024 * 1024:
                raise OverflowError('request body too large')
            self.connection.settimeout(30)
            body = self.rfile.read(length) if length else b''
            if len(body) != length:
                raise ValueError('incomplete request body')
            return body

        def _bounded_body(self):
            try:
                return self._read_body()
            except OverflowError:
                self.close_connection = True
                self._send_json(413, {'error': 'request body too large'})
            except (ValueError, TimeoutError, OSError):
                self.close_connection = True
                self._send_json(400, {'error': 'invalid or incomplete request body'})
            return None

        def do_GET(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith(('/webhook/', '/webhooks/')):
                from .webui_auth import is_allowed_host, is_authorized
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not is_authorized(headers, server.webui_token, self.client_address[0]):
                    self._send_json(401, {'error': 'unauthorized'})
                    return
                if not is_allowed_host(self.headers.get('Host', ''), server.webui_allowed_hosts):
                    self._send_json(403, {'error': 'foreign host'})
                    return
            if server.webhook_only and parsed.path not in ('/webhook/whatsapp', '/webhooks/whatsapp'):
                self._send_json(404, {'error': 'webhook only'})
            elif parsed.path == '/static' or parsed.path.startswith('/static/'):
                relative = parsed.path[len('/static/'):] if parsed.path.startswith('/static/') else ''
                self._send_static(relative)
            elif parsed.path == '/':
                self._send_static('index.html')
            elif parsed.path == '/documents':
                self._send_static('docs.html')
            elif parsed.path in ('/dag', '/capabilities', '/labs', '/memory', '/knowledge', '/reports', '/practice', '/graph'):
                self._send_static(parsed.path.lstrip('/') + '.html')
            elif parsed.path == '/settings':
                from .webui_settings_page import render_settings_page
                self._send_html(200, render_settings_page(
                    server, _api_key_row, _page_html))
            elif parsed.path == '/library':
                files = server._library_files()
                html = '<h1>Library</h1>'
                for item in files:
                    html += f"<p>{item['path']} {item['size']} bytes</p>"
                self._send_html(200, _page_html('Library', html))
            elif parsed.path.startswith('/api/library/') and parsed.path.endswith('/file'):
                from ..memory.library import Library
                item_id = parsed.path[len('/api/library/'):-len('/file')].strip('/')
                item = Library(server.home).get_item(item_id)
                if item is None:
                    self._send_json(404, {'error': 'library item not found'})
                else:
                    data = Path(item['absolute_path']).read_bytes()
                    content_type = item.get('mime') or 'application/octet-stream'
                    self._send_bytes(200, content_type, data)
            elif parsed.path == '/api/library':
                from ..memory.library import Library
                query = parse_qs(parsed.query)
                items = Library(server.home).list_items()
                if not items:
                    items = server._library_files()
                self._send_json(200, {'files': items})
            elif parsed.path == '/api/approvals':
                self._send_json(200, {'cards': server.webui_gate.pending_cards()})
            elif parsed.path == '/api/preapprovals':
                self._send_json(200, server.handle_preapprovals())
            elif parsed.path == '/factory':
                self._send_html(200, FACTORY_HTML)
            elif parsed.path == '/api/factory':
                self._send_json(200, {'stages': server.factory_stages()})
            elif parsed.path in ('/dag', '/studio/dag'):
                self._send_html(200, DAG_HTML)
            elif parsed.path == '/api/dags':
                self._send_json(200, server.handle_dags_list())
            elif parsed.path.startswith('/api/dags/'):
                dag_id = parsed.path[len('/api/dags/'):]
                data = server.handle_dag_get(dag_id)
                self._send_json(200 if 'error' not in data else 404, data)
            elif parsed.path in ('/capabilities', '/studio/capabilities'):
                from ..capabilities.plugins import PluginRegistry as _PR
                _reg = _PR(server.home)
                _reg.load_all()
                html = '<h1>Capabilities</h1><p>What Aidon can do, what it has learned, and the evidence.</p>'
                html += f"<p>Sandbox tools: write, read, list, shell, calculate</p><p>Plugin tools: {', '.join(_reg.tool_names())}</p>"
                html += '<p>Computer use: screenshot and click, type, scroll, key, approval-gated, no driver configured by default, no GUI action without a driver.</p>'
                html += "<h2>Capabilities studio</h2><p>Record a skill success or failure, or probe a plugin dir, or edit a skill's description, via POST /api/studio/capabilities with JSON {action: success|failure|probe|edit, name, path, description}.</p>"
                html += '<form onsubmit="event.preventDefault(); fetch(\'/api/studio/capabilities\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({action:document.getElementById(\'cap_action\').value,name:document.getElementById(\'cap_name\').value,path:document.getElementById(\'cap_path\').value,description:document.getElementById(\'cap_desc\').value})}).then(r=>r.json()).then(d=>document.getElementById(\'cap_result\').textContent=JSON.stringify(d));"><select id="cap_action"><option>success</option><option>failure</option><option>probe</option></select> <input id="cap_name" placeholder="skill name"> <input id="cap_path" placeholder="plugin dir for probe"> <button>Run</button></form><pre id="cap_result"></pre>'
                self._send_html(200, _page_html('Capabilities', html))
            elif parsed.path in ('/memory', '/studio/memory'):
                html = '<h1>Memory</h1><p>Episodes Aidon remembers from past work, by collection, no secret values.</p>'
                if server.memory is not None:
                    for coll in ('working', 'episodic', 'semantic', 'experience', 'knowledge'):
                        try:
                            points = server.memory.list_points(coll, limit=20)
                            html += f'<h2>{coll}: {len(points)} points</h2>'
                            for point in points:
                                html += f"<p>{point.id} {point.payload.get('text', '')[:120]}</p>"
                        except Exception:
                            continue
                html += "<h2>Memory studio</h2><p>Forget a point, prune a collection, or promote an episodic point, or edit a point's text, via POST /api/studio/memory with JSON {action: forget|prune|promote|edit, collection, point_id, text}.</p>"
                html += '<form onsubmit="event.preventDefault(); fetch(\'/api/studio/memory\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({action:document.getElementById(\'mem_action\').value,collection:document.getElementById(\'mem_coll\').value,point_id:document.getElementById(\'mem_id\').value,text:document.getElementById(\'mem_text\').value})}).then(r=>r.json()).then(d=>document.getElementById(\'mem_result\').textContent=JSON.stringify(d));"><select id="mem_action"><option>forget</option><option>prune</option><option>promote</option></select> <input id="mem_coll" value="episodic"> <input id="mem_id" placeholder="point id"> <button>Run</button></form><pre id="mem_result"></pre>'
                self._send_html(200, _page_html('Memory', html))
            elif parsed.path in ('/knowledge', '/studio/knowledge'):
                html = '<h1>Knowledge</h1><p>Documents and facts Aidon has ingested, from the knowledge collection and library.</p>'
                if server.memory is not None:
                    try:
                        for point in server.memory.list_points('knowledge', limit=50):
                            html += f"<p>{point.id} {point.payload.get('text', '')[:120]}</p>"
                    except Exception:
                        pass
                for item in server._library_files():
                    html += f"<p>Library: {item['path']}</p>"
                html += '<h2>Knowledge studio</h2><p>Recall, record, edit, or delete knowledge, via POST /api/studio/knowledge with JSON {action: recall|ingest|edit|delete, query, text, point_id}.</p>'
                html += '<form onsubmit="event.preventDefault(); fetch(\'/api/studio/knowledge\',{method:\'POST\',headers:{\'Content-Type\':\'application/json\'},body:JSON.stringify({action:document.getElementById(\'kb_action\').value,query:document.getElementById(\'kb_query\').value,text:document.getElementById(\'kb_text\').value})}).then(r=>r.json()).then(d=>document.getElementById(\'kb_result\').textContent=JSON.stringify(d));"><select id="kb_action"><option>recall</option><option>ingest</option></select> <input id="kb_query" placeholder="query"> <input id="kb_text" placeholder="text to ingest"> <button>Run</button></form><pre id="kb_result"></pre>'
                self._send_html(200, _page_html('Knowledge', html))
            elif parsed.path in ('/reports', '/studio/reports'):
                from ..interface.scheduler_loop import daily_report as _daily
                html = '<h1>Reports</h1><p>Daily reports, from learned last run and trace files, never invented.</p>'
                html += f'<pre>{_daily(server.home)}</pre>'
                self._send_html(200, _page_html('Reports', html))
            elif parsed.path == '/replay':
                self._send_html(200, REPLAY_HTML)
            elif parsed.path == '/avatar-vrm':
                self._send_html(200, VRM_HTML)
            elif parsed.path == '/api/replay':
                query = parse_qs(parsed.query)
                run = query.get('run', [''])[0]
                if run:
                    self._send_json(200, {'run': run, 'events': server.replay_events(run)})
                else:
                    self._send_json(200, {'runs': server.replay_runs()})
            elif parsed.path == '/api/settings/catalog':
                self._send_json(200, server.settings_catalog())
            elif parsed.path == '/api/keys':
                self._send_json(200, server.handle_keys())
            elif parsed.path == '/api/settings':
                self._send_json(200, server.settings_get())
            elif parsed.path == '/api/avatar.vrm':
                vrm_path = server._avatar_path()
                if vrm_path is None:
                    self._send_json(404, {'error': 'no VRM file'})
                else:
                    data = vrm_path.read_bytes()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
            elif parsed.path == '/api/memory-dags':
                dags = []
                if server.memory is not None:
                    try:
                        from ..memory.experience import recall_experiences
                        for exp in recall_experiences(server.memory, '', limit=20):
                            if exp.dag_nodes:
                                dags.append({'goal': exp.goal, 'nodes': exp.dag_nodes})
                    except Exception:
                        pass
                self._send_json(200, {'dags': dags})
            elif parsed.path == '/api/entities':
                query = parse_qs(parsed.query)
                entity = query.get('entity', [''])[0]
                self._send_json(200, server.handle_entities(entity))
            elif parsed.path == '/api/memory/graph':
                query = parse_qs(parsed.query)
                collections = query.get('collection', []) or None
                try:
                    limit = int(query.get('limit', ['500'])[0])
                except ValueError:
                    limit = 500
                self._send_json(200, server.handle_memory_graph(collections, limit))
            elif parsed.path == '/api/tools':
                self._send_json(200, server.handle_tools())
            elif parsed.path.startswith('/api/runs/') and '/' not in parsed.path[len('/api/runs/'):]:
                run_id = parsed.path[len('/api/runs/'):]
                query = parse_qs(parsed.query)
                try:
                    since = max(0, int(query.get('since', ['0'])[0]))
                except (TypeError, ValueError):
                    since = 0
                try:
                    trace_since = max(0, int(query.get('trace_since', ['0'])[0]))
                except (TypeError, ValueError):
                    trace_since = 0
                data = server.run_snapshot(run_id, since=since, trace_since=trace_since)
                self._send_json(200 if data is not None else 404, data or {'error': 'run not found'})
            elif parsed.path == '/api/think':
                from .surfaces import think_enabled as _get_think
                from urllib.parse import parse_qs as _parse_qs_think
                _think_query = _parse_qs_think(parsed.query)
                conv = str(_think_query.get('conversation_id', [''])[0])
                self._send_json(200, {'think_max': _get_think(server.home, conv)})
            elif parsed.path == '/api/research':
                from .surfaces import research_enabled as _get_research
                from urllib.parse import parse_qs as _parse_qs_research
                _research_query = _parse_qs_research(parsed.query)
                conv = str(_research_query.get('conversation_id', [''])[0])
                self._send_json(200, {'research_mode': _get_research(server.home, conv)})
            elif parsed.path == '/api/practice':
                self._send_json(200, server.handle_practice_list())
            elif parsed.path == '/api/lab-session':
                from .interview_store import InterviewStore
                try:
                    kind = parse_qs(parsed.query).get('kind', [''])[0]
                    conversation_id = parse_qs(parsed.query).get(
                        'conversation_id', [''])[0]
                    self._send_json(200, InterviewStore(server.home).load_lab(
                        kind, conversation_id))
                except ValueError as exc:
                    self._send_json(400, {'error': str(exc)})
            elif parsed.path == '/api/conversations':
                self._send_json(200, server.handle_conversations_list())
            elif (parsed.path.startswith('/api/conversation-attachments/')
                  and parsed.path.endswith('/file')):
                attachment_id = parsed.path[
                    len('/api/conversation-attachments/'):-len('/file')].strip('/')
                path = server.handle_conversation_attachment(attachment_id)
                if path is None:
                    self._send_json(404, {'error': 'conversation attachment not found'})
                else:
                    content_type = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    self._send_bytes(200, content_type, path.read_bytes())
            elif parsed.path == '/api/skill-source':
                query = parse_qs(parsed.query)
                source = server.handle_skill_source(
                    query.get('run_id', [''])[0],
                    query.get('conversation_id', [''])[0])
                self._send_json(200 if source else 404,
                                source or {'error': 'no verified successful workflow for this conversation'})
            elif parsed.path.startswith('/api/conversations/') and parsed.path.endswith('/brief'):
                conversation_id = parsed.path[len('/api/conversations/'):-len('/brief')].strip('/')
                data = server.handle_brief_get(conversation_id)
                self._send_json(200 if data is not None else 404, data or {'error': 'not found'})
            elif parsed.path.startswith('/api/conversations/'):
                conv_id = parsed.path[len('/api/conversations/'):]
                data = server.handle_conversation_get(conv_id)
                self._send_json(200 if data is not None else 404, data or {'error': 'not found'})
            elif parsed.path == '/api/mode-stops/latest':
                query = parse_qs(parsed.query)
                conv = query.get('conversation_id', [''])[0]
                data = server.handle_mode_stop_latest(conv)
                self._send_json(200 if data is not None else 404, data or {'error': 'not found'})
            elif parsed.path == '/api/ideas':
                self._send_json(200, server.handle_ideas_list())
            elif parsed.path.startswith('/api/files/'):
                from urllib.parse import unquote
                requested = unquote(parsed.path[len('/api/files/'):])
                status, content_type, data = server.handle_file(requested)
                headers = {}
                if status == 200 and requested.lower().endswith('.html'):
                    from urllib.parse import quote
                    _filename = Path(requested).name
                    headers = {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(_filename, safe=''), 'X-Content-Type-Options': 'nosniff'}
                self._send_bytes(status, content_type, data, headers)
            elif parsed.path == '/api/files':
                query = parse_qs(parsed.query)
                requested = query.get('path', [''])[0]
                status, content_type, data = server.handle_file(requested)
                headers = {}
                if status == 200 and requested.lower().endswith('.html'):
                    from urllib.parse import quote
                    _filename = Path(requested).name
                    headers = {'Content-Disposition': "attachment; filename*=UTF-8''" + quote(_filename, safe=''), 'X-Content-Type-Options': 'nosniff'}
                self._send_bytes(status, content_type, data, headers)
            elif parsed.path == '/api/voice':
                self._send_json(200, server.handle_voice())
            elif parsed.path == '/api/status':
                self._send_json(200, server.handle_status())
            elif parsed.path == '/api/health':
                runs = server.run_control.list_runs() if server.run_control else []
                _status = server.handle_status()
                self._send_json(200, {'ok': True, 'runs': len(runs), 'memory': _status.get('memory'), 'memory_warning': _status.get('memory_warning', '')})
            elif parsed.path == '/api/avatar/status':
                vrm_path = server._avatar_path()
                self._send_json(200, {'present': vrm_path is not None, 'source': str(vrm_path) if vrm_path else None, 'default': 'robot', 'active': 'robot'})
            elif parsed.path == '/api/reports':
                self._send_json(200, server.handle_reports_list())
            elif parsed.path.startswith('/api/reports/'):
                day = parsed.path[len('/api/reports/'):].strip('/')
                data = server.handle_report_get(day)
                self._send_json(200 if data is not None else 404, data or {'error': 'not found'})
            elif parsed.path == '/api/missions':
                self._send_json(200, server.handle_missions_list())
            elif parsed.path.startswith('/api/missions/'):
                mission_id = parsed.path[len('/api/missions/'):].strip('/')
                data = server.handle_mission_get(mission_id)
                self._send_json(200 if data is not None else 404, data or {'error': 'mission not found'})
            elif parsed.path == '/api/capabilities':
                self._send_json(200, server.handle_capabilities())
            elif parsed.path == '/api/capabilities/code':
                query = parse_qs(parsed.query)
                data = server.handle_capability_code(query.get('name', [''])[0])
                self._send_json(200 if data is not None else 404, data or {'error': 'capability not found'})
            elif parsed.path in ('/api/memory/episodes', '/api/memory/knowledge'):
                query = parse_qs(parsed.query)
                collection = parsed.path.rsplit('/', 1)[-1]
                try:
                    limit = int(query.get('limit', ['50'])[0])
                except ValueError:
                    limit = 50
                try:
                    offset = max(0, int(query.get('offset', ['0'])[0]))
                except ValueError:
                    offset = 0
                needle = query.get('q', [''])[0].strip().lower()
                raw_points = server.handle_memory_points(collection, 200).get('points', [])
                items = []
                for point in raw_points:
                    item = dict(point.get('payload', {}) or {})
                    item['id'] = point.get('id', '')
                    item.setdefault('text', point.get('text', ''))
                    items.append(item)
                if needle:
                    items = [item for item in items if needle in json.dumps(item).lower()]
                matched = len(items)
                page = items[offset:offset + max(1, min(limit, 200))]
                self._send_json(200, {'items': page, 'points': page, 'total': len(raw_points), 'matched': matched, 'mode': 'Aidon memory'})
            elif parsed.path in ('/webhook/whatsapp', '/webhooks/whatsapp'):
                query = parse_qs(parsed.query)
                status, text = server.handle_whatsapp_get(query)
                data = text.encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self._send_json(404, {'error': 'not found'})

        def do_POST(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith(('/webhook/', '/webhooks/')):
                from .webui_auth import is_allowed_host, is_allowed_origin, is_authorized
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not is_authorized(headers, server.webui_token, self.client_address[0]):
                    self._send_json(401, {'error': 'unauthorized'})
                    return
                if not is_allowed_host(self.headers.get('Host', ''), server.webui_allowed_hosts):
                    self._send_json(403, {'error': 'foreign host'})
                    return
                if not is_allowed_origin(headers, self.headers.get('Host', '')):
                    self._send_json(403, {'error': 'foreign origin'})
                    return
            if server.webhook_only and parsed.path not in ('/webhook/whatsapp', '/webhooks/whatsapp', '/webhook/slack', '/webhooks/slack'):
                self._send_json(404, {'error': 'webhook only'})
                return
            raw = self._bounded_body()
            if raw is None:
                return
            content_type = self.headers.get('Content-Type', '').lower()
            if content_type.startswith('audio/'):
                body = {}
            else:
                try:
                    body = json.loads(raw) if raw else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    body = {}
            if parsed.path == '/api/goals/with-images' and content_type.startswith('multipart/form-data'):
                try:
                    fields, files = server.parse_goal_multipart(raw, content_type)
                    result = server.handle_goal_with_files(fields, files)
                    self._send_json(200 if 'run_id' in result else 400, result)
                except (ValueError, OSError) as exc:
                    self._send_json(400, {'error': str(exc)})
                return
            if parsed.path == '/api/library/upload':
                import base64 as _b64
                name = str(body.get('name', 'upload.bin'))
                safe = ''.join((c for c in name if c.isalnum() or c in ('-', '_', '.'))) or 'upload.bin'
                try:
                    data = _b64.b64decode(str(body.get('content_b64', '')))
                except Exception:
                    self._send_json(400, {'error': 'bad base64'})
                    return
                tmp = server.home / 'library' / 'uploads' / safe
                tmp.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(data)
                from ..memory.library import Library
                saved = Library(server.home).add_file(tmp, kind='file', description='WebUI upload')
                self._send_json(200, {'id': saved['id'], 'name': safe})
            elif parsed.path == '/api/settings':
                if isinstance(body.get('settings'), dict):
                    updated = []
                    errors = []
                    for key, value in body['settings'].items():
                        result = server.settings_set_env(str(key), value)
                        if result.get('ok'):
                            updated.append(str(key))
                        else:
                            errors.append(result.get('error', str(key)))
                    self._send_json(200 if not errors else 400, {'updated': updated, 'errors': errors})
                else:
                    key = str(body.get('key', ''))
                    if server.settings_set(key, body.get('value')):
                        self._send_json(200, {'ok': True})
                    else:
                        result = server.settings_set_env(key, body.get('value', ''))
                        self._send_json(200 if result.get('ok') else 400, result)
            elif parsed.path == '/api/settings/env':
                result = server.settings_set_env(str(body.get('key', '')), body.get('value', ''))
                self._send_json(200 if result.get('ok') else 400, result)
            elif parsed.path == '/api/think':
                from .surfaces import toggle_think as _post_think
                conv = str(body.get('conversation_id', ''))
                state = _post_think(server.home, conv)
                self._send_json(200, {'think_max': state})
            elif parsed.path == '/api/research':
                from .surfaces import toggle_research as _post_research
                conv = str(body.get('conversation_id', ''))
                state = _post_research(server.home, conv)
                self._send_json(200, {'research_mode': state})
            elif parsed.path == '/api/transcribe_stream/start':
                if not server.transcribe_key:
                    self._send_json(501, {'error': 'transcribe key not configured'})
                    return
                try:
                    import uuid as _uuidt
                    from ..capabilities.voice import RealtimeTranscriber as _RT
                    sid = _uuidt.uuid4().hex
                    rt = _RT(server.transcribe_key, session_id=sid)
                    rt.connect()
                    server.realtime_sessions[sid] = rt
                    self._send_json(200, {'session_id': sid})
                except Exception as exc:
                    self._send_json(502, {'error': str(exc)})
            elif parsed.path == '/api/transcribe_stream/audio':
                import base64 as _b64a
                rt = server.realtime_sessions.get(str(body.get('session_id', '')))
                if rt is None:
                    self._send_json(404, {'error': 'unknown session'})
                    return
                try:
                    audio = _b64a.b64decode(str(body.get('audio_b64', '')))
                except Exception:
                    self._send_json(400, {'error': 'bad base64'})
                    return
                if audio and (not rt.send_audio(audio)):
                    detail = rt.error or ''
                    self._send_json(410, {'error': 'session closed' + (': ' + detail if detail else '')})
                    return
                self._send_json(200, {'transcript': rt.transcript, 'final': rt.final})
            elif parsed.path == '/api/transcribe_stream/end':
                rt = server.realtime_sessions.pop(str(body.get('session_id', '')), None)
                if rt is None:
                    self._send_json(404, {'error': 'unknown session'})
                    return
                try:
                    result = rt.end()
                finally:
                    rt.close()
                self._send_json(200, result)
            elif parsed.path == '/api/transcribe':
                if not server.transcribe_key:
                    self._send_json(501, {'error': 'transcribe key not configured'})
                    return
                import base64 as _b64t
                try:
                    if raw and self.headers.get('Content-Type', '').lower().startswith('audio/'):
                        audio = raw
                    else:
                        audio = _b64t.b64decode(str(body.get('audio_b64', '')))
                    from ..capabilities.voice import transcribe as _transcribe
                    content_type = self.headers.get('Content-Type', 'audio/wav')
                    result = _transcribe(audio, server.transcribe_key, content_type=content_type)
                    self._send_json(200, result)
                except Exception as exc:
                    self._send_json(500, {'error': str(exc)})
            elif parsed.path == '/api/goals':
                result = server.start_goal(body)
                self._send_json(200 if 'run_id' in result else 400, result)
            elif parsed.path.startswith('/api/mode-stops/') and parsed.path.endswith('/spec'):
                stop_id = parsed.path[len('/api/mode-stops/'):-len('/spec')].strip('/')
                result = server.handle_mode_stop_spec_save(stop_id, body)
                self._send_json(200 if result.get('ok') else 400, result)
            elif parsed.path == '/api/proceed':
                stop_id = str(body.get('stop_id', '') or '')
                result = server.start_goal({'goal': f'/proceed {stop_id}'.strip(), 'conversation_id': str(body.get('conversation_id', '') or '')})
                self._send_json(200 if 'run_id' in result else 400, result)
            elif parsed.path.startswith('/api/runs/') and parsed.path.endswith('/approval'):
                run_id = parsed.path[len('/api/runs/'):-len('/approval')].strip('/')
                pending = server.webui_gate.pending_cards()
                if not pending:
                    self._send_json(409, {'error': 'no pending approval'})
                else:
                    decision = str(body.get('decision', body.get('verdict', 'rejected')))
                    ok = server.webui_gate.approve(pending[0]['id'], decision, str(body.get('edited_text', '')))
                    self._send_json(200 if ok else 409, {'ok': ok, 'run_id': run_id})
            elif parsed.path.startswith('/api/runs/') and parsed.path.endswith('/steer'):
                run_id = parsed.path[len('/api/runs/'):-len('/steer')].strip('/')
                ok = server.run_control.steer(run_id, str(body.get('note', ''))) if server.run_control else False
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path.startswith('/api/runs/') and parsed.path.endswith('/cancel'):
                run_id = parsed.path[len('/api/runs/'):-len('/cancel')].strip('/')
                ok = server.run_control.cancel(run_id, gate=server.webui_gate) if server.run_control else False
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path.startswith('/api/runs/') and parsed.path.endswith('/cancel-queued'):
                run_id = parsed.path[len('/api/runs/'):-len('/cancel-queued')].strip('/')
                record = server.run_control.get(run_id) if server.run_control else None
                if record is None:
                    self._send_json(404, {'error': 'run not found'})
                else:
                    self._send_json(409, {'error': 'run is not queued'})
            elif parsed.path == '/api/lab-session':
                from .interview_store import InterviewStore
                try:
                    revision = InterviewStore(server.home).save_lab(
                        body.get('kind'), body.get('state'),
                        int(body.get('revision', 0)),
                        body.get('conversation_id'))
                    self._send_json(200, {'revision': revision})
                except (ValueError, TypeError) as exc:
                    self._send_json(409, {'error': str(exc)})
            elif parsed.path == '/api/chat':
                self._send_json(200, server.handle_chat(body))
            elif parsed.path == '/api/labs/message':
                status, payload = server.handle_lab_message(body)
                self._send_json(status, payload)
            elif parsed.path == '/api/labs/start':
                status, payload = server.handle_lab_start(body)
                self._send_json(status, payload)
            elif parsed.path == '/api/dags':
                status, payload = server.handle_dag_save(body)
                self._send_json(status, payload)
            elif parsed.path == '/api/dags/layout':
                status, payload = server.handle_dag_layout(body)
                self._send_json(status, payload)
            elif parsed.path == '/api/runs':
                runs = server.run_control.list_runs() if server.run_control else []
                self._send_json(200, {'runs': runs})
            elif parsed.path == '/api/steer':
                run_id = str(body.get('run_id', ''))
                note = str(body.get('note', ''))
                ok = server.run_control.steer(run_id, note) if server.run_control else False
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path == '/api/cancel':
                run_id = str(body.get('run_id', ''))
                ok = server.run_control.cancel(run_id, gate=server.webui_gate) if server.run_control else False
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path == '/api/approve':
                card_id = str(body.get('id', ''))
                verdict = str(body.get('verdict', 'rejected'))
                ok = server.webui_gate.approve(card_id, verdict, str(body.get('edited_text', '')))
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path == '/api/preapprovals':
                status, payload = server.handle_preapproval_grant(body)
                self._send_json(status, payload)
            elif parsed.path.startswith('/api/studio/'):
                from .webui_studios import handle_studio_post
                status, payload = handle_studio_post(server, parsed.path, body)
                self._send_json(status, payload)
            elif parsed.path == '/api/conversations':
                self._send_json(200, server.handle_conversation_create(body))
            elif parsed.path == '/api/ideas':
                self._send_json(200, server.handle_idea_create(body))
            elif parsed.path == '/api/hub/runs':
                result = server.handle_hub_run_create(body)
                self._send_json(200 if result.get('ok') else 400, result)
            elif parsed.path.startswith('/api/hub/runs/') and parsed.path.endswith('/events'):
                run_id = parsed.path[len('/api/hub/runs/'):-len('/events')].strip('/')
                self._send_json(200, server.handle_hub_run_event(run_id, body))
            elif parsed.path.startswith('/api/hub/runs/') and parsed.path.endswith('/stage'):
                run_id = parsed.path[len('/api/hub/runs/'):-len('/stage')].strip('/')
                self._send_json(200, server.handle_hub_run_stage(run_id, body))
            elif parsed.path.startswith('/api/hub/runs/') and parsed.path.endswith('/trace'):
                run_id = parsed.path[len('/api/hub/runs/'):-len('/trace')].strip('/')
                self._send_json(200, server.handle_hub_run_trace(run_id, body))
            elif parsed.path.startswith('/api/hub/runs/') and parsed.path.endswith('/finish'):
                run_id = parsed.path[len('/api/hub/runs/'):-len('/finish')].strip('/')
                result = server.finish_hub_run(run_id)
                self._send_json(200 if result.get('ok') else 404, result)
            elif parsed.path == '/api/reports/generate':
                self._send_json(200, server.handle_report_generate(body))
            elif parsed.path == '/api/tts':
                status, payload = server.handle_tts(body)
                audio = payload.pop('_audio', None) if isinstance(payload, dict) else None
                if status == 200 and isinstance(audio, (bytes, bytearray)):
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Content-Length', str(len(audio)))
                    self.end_headers()
                    try:
                        self.wfile.write(audio)
                    except (BrokenPipeError, ConnectionResetError):
                        return
                else:
                    self._send_json(status, payload)
            elif parsed.path == '/api/tts/stream':
                try:
                    response = server.open_tts_stream(body)
                except Exception as exc:
                    self._send_json(500, {'error': type(exc).__name__})
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/mpeg')
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    try:
                        while True:
                            chunk = response.read(4 * 1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        response.close()
            elif parsed.path == '/api/goals/with-images':
                self._send_json(200, server.handle_goal_with_images(body))
            elif parsed.path == '/api/plugins/adopt':
                result = server.handle_plugin_adopt(str(body.get('path', '')))
                self._send_json(200 if result.get('ok') else 400, result)
            elif parsed.path == '/api/practice':
                result = server.handle_practice_create(body)
                self._send_json(200 if 'schedule' in result else 400, result)
            elif parsed.path == '/api/practice/delete':
                self._send_json(200, server.handle_practice_delete(body))
            elif parsed.path == '/api/practice/run':
                result = server.handle_practice_run(body)
                self._send_json(200 if result.get('started') else 400, result)
            elif parsed.path in ('/webhook/whatsapp', '/webhooks/whatsapp'):
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, payload = server.handle_whatsapp_post(body, raw, headers)
                self._send_json(status, payload)
            elif parsed.path in ('/webhook/slack', '/webhooks/slack'):
                headers = {k.lower(): v for k, v in self.headers.items()}
                status, payload = server.handle_slack_post(raw, headers, body)
                self._send_json(status, payload)
            else:
                self._send_json(404, {'error': 'not found'})

        def do_DELETE(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith(('/webhook/', '/webhooks/')):
                from .webui_auth import is_allowed_host, is_authorized
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not is_authorized(headers, server.webui_token, self.client_address[0]):
                    self._send_json(401, {'error': 'unauthorized'})
                    return
                if not is_allowed_host(self.headers.get('Host', ''), server.webui_allowed_hosts):
                    self._send_json(403, {'error': 'foreign host'})
                    return
            if parsed.path.startswith('/api/memory/episodes/') or parsed.path.startswith('/api/memory/knowledge/'):
                parts = parsed.path.split('/')
                collection = parts[3]
                point_id = '/'.join(parts[4:]).strip('/')
                result = server.handle_memory_point_delete(collection, point_id)
                self._send_json(200 if result['ok'] else 404, result)
            elif parsed.path.startswith('/api/preapprovals/'):
                rest = parsed.path[len('/api/preapprovals/'):].strip('/')
                parts = rest.split('/')
                spec_id = parts[0] if parts else ''
                kind = parts[1] if len(parts) > 1 else ''
                status, payload = server.handle_preapproval_revoke(spec_id, kind)
                self._send_json(status, payload)
            elif parsed.path.startswith('/api/dags/'):
                dag_id = parsed.path[len('/api/dags/'):]
                self._send_json(200, server.handle_dag_delete(dag_id))
            elif parsed.path.startswith('/api/conversations/'):
                conv_id = parsed.path[len('/api/conversations/'):]
                result = server.handle_conversation_delete(conv_id)
                self._send_json(200 if result['ok'] else 404, result)
            elif parsed.path.startswith('/api/ideas/'):
                idea_id = parsed.path[len('/api/ideas/'):]
                result = server.handle_idea_delete(idea_id)
                self._send_json(200 if result['ok'] else 404, result)
            elif parsed.path.startswith('/api/mode-stops/'):
                stop_id = parsed.path[len('/api/mode-stops/'):].strip('/')
                result = server.handle_mode_stop_delete(stop_id)
                self._send_json(200 if result['ok'] else 404, result)
            elif parsed.path.startswith('/api/library/'):
                item_id = parsed.path[len('/api/library/'):].strip('/')
                from ..memory.library import Library
                ok = Library(server.home).delete_item(item_id)
                self._send_json(200 if ok else 404, {'ok': ok})
            elif parsed.path.startswith('/api/missions/'):
                mission_id = parsed.path[len('/api/missions/'):].strip('/')
                result = server.handle_mission_delete(mission_id)
                self._send_json(200 if result['ok'] else 404, result)
            else:
                self._send_json(404, {'error': 'not found'})

        def do_PUT(self):
            parsed = urlparse(self.path)
            if not parsed.path.startswith(('/webhook/', '/webhooks/')):
                from .webui_auth import is_allowed_host, is_authorized
                headers = {k.lower(): v for k, v in self.headers.items()}
                if not is_authorized(headers, server.webui_token, self.client_address[0]):
                    self._send_json(401, {'error': 'unauthorized'})
                    return
                if not is_allowed_host(self.headers.get('Host', ''), server.webui_allowed_hosts):
                    self._send_json(403, {'error': 'foreign host'})
                    return
            raw = self._bounded_body()
            if raw is None:
                return
            try:
                body = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                body = {}
            if parsed.path.startswith('/api/conversations/') and parsed.path.endswith('/brief'):
                conversation_id = parsed.path[len('/api/conversations/'):-len('/brief')].strip('/')
                self._send_json(200, server.handle_brief_save(conversation_id, body))
            else:
                self._send_json(404, {'error': 'not found'})

        def log_message(self, *args):
            pass
    return Handler
