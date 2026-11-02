"""WebUI studio POST route handlers."""

from __future__ import annotations


def handle_studio_post(server, path: str, body: dict) -> tuple[int, dict]:
    if path == '/api/studio/memory':
        if str(body.get('action', '')) == 'edit':
            result = server.handle_memory_edit(str(body.get('collection', 'episodic')), str(body.get('point_id', '')), str(body.get('text', '')))
            return 200 if result.get('ok') else 400, result
        elif server.memory is None:
            return 500, {'error': 'no memory'}
        else:
            action = str(body.get('action', ''))
            collection = str(body.get('collection', 'episodic'))
            point_id = str(body.get('point_id', ''))
            if action == 'forget':
                return 200, {'ok': server.memory.forget(collection, point_id)}
            elif action == 'prune':
                return 200, server.memory.prune(collection)
            elif action == 'promote':
                point = server.memory.store.get(collection, point_id)
                if point is None:
                    return 404, {'error': 'point not found'}
                else:
                    return 200, {'ok': True, 'result': str(server.memory.promote_to_ltm(point))}
            else:
                return 400, {'error': 'unknown memory studio action'}
    if path == '/api/studio/knowledge':
        if server.memory is None:
            return 500, {'error': 'no memory'}
        else:
            action = str(body.get('action', ''))
            if action == 'recall':
                return 200, {'results': [str(r) for r in server.memory.recall('knowledge', str(body.get('query', '')))]}
            elif action == 'ingest':
                server.memory.record('knowledge', str(body.get('text', '')))
                return 200, {'ok': True}
            elif action == 'delete':
                point_id = str(body.get('point_id', ''))
                return 200, {'ok': server.memory.forget('knowledge', point_id)}
            elif action == 'edit':
                point_id = str(body.get('point_id', ''))
                text = str(body.get('text', ''))
                if not point_id or not text:
                    return 400, {'error': 'edit needs point_id and text'}
                else:
                    ok = server.memory.forget('knowledge', point_id)
                    server.memory.record('knowledge', text)
                    return 200, {'ok': ok, 'edited': True}
            else:
                return 400, {'error': 'unknown knowledge studio action'}
    if path == '/api/studio/capabilities':
        action = str(body.get('action', ''))
        if action == 'edit':
            result = server.handle_skill_edit(str(body.get('name', '')), str(body.get('description', '')))
            return 200 if result.get('ok') else 400, result
        elif action in ('success', 'failure'):
            from ..capabilities.skill_lifecycle import LifecycleStore
            lifecycle = LifecycleStore(server.home)
            name = str(body.get('name', ''))
            result = lifecycle.record_success(name) if action == 'success' else lifecycle.record_failure(name)
            return 200, {'ok': True, 'state': result}
        elif action == 'probe':
            from ..capabilities.security_probe import probe_plugin
            path = str(body.get('path', ''))
            if not path:
                return 400, {'error': 'probe needs a path'}
            else:
                return 200, {'findings': probe_plugin(path)}
        else:
            return 400, {'error': 'unknown capabilities studio action'}
    return 404, {'error': 'unknown studio route'}
