"""Local smoke test for the Vercel serverless handlers.

Runs the real handler classes against in-process HTTP requests, so the same
code path Vercel uses is exercised before deploying.
"""
import importlib.util
import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
API = BASE / 'api'


def load(path):
    spec = importlib.util.spec_from_file_location('mod_' + path.stem.replace('.', '_'), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def serve(handler_cls):
    """Mount a handler class on a real port and return (server, base_url)."""
    server = HTTPServer(('127.0.0.1', 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, 'http://127.0.0.1:%d' % server.server_port


def main():
    import urllib.request

    health = load(API / 'health.py').handler
    status = load(API / 'ai' / 'status.py').handler
    news = load(API / 'news.py').handler
    opponent = load(API / 'ai' / 'opponent.py').handler

    servers = []
    failures = []

    def check(name, url):
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                body = json.loads(response.read().decode('utf-8'))
            print('OK  ' + name + ' -> ' + json.dumps(body, ensure_ascii=False)[:150])
        except Exception as exc:
            failures.append(name + ': ' + repr(exc))
            print('ERR ' + name + ': ' + repr(exc))

    for module, routes in (
        (health, ['/api/health']),
        (status, ['/api/ai/status']),
        (news, ['/api/news?lang=zh', '/api/news?lang=en&refresh=1']),
    ):
        server, base = serve(module)
        servers.append(server)
        for route in routes:
            check(route, base + route)

    server, base = serve(opponent)
    servers.append(server)
    body = json.dumps({'lang': 'zh', 'event': {'options': [{'id': 'e1', 'label': 'A'}]},
                       'action': {'options': [{'id': 'a1', 'label': 'B'}]}}).encode()
    request = urllib.request.Request(base + '/api/ai/opponent', data=body,
                                     headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode('utf-8'))
        print('OK  POST /api/ai/opponent -> ' + json.dumps(data, ensure_ascii=False)[:180])
    except Exception as exc:
        print('ERR POST /api/ai/opponent: ' + repr(exc))
        failures.append('POST /api/ai/opponent: ' + repr(exc))

    for server in servers:
        server.shutdown()

    if failures:
        print('\nFAILURES:')
        for item in failures:
            print(' - ' + item)
        sys.exit(1)
    print('\nAll handlers responded.')


if __name__ == '__main__':
    main()
