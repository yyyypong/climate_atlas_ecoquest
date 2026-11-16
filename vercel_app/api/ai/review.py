"""Vercel serverless function: POST /api/ai/review"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _ecoquest import handle_review  # noqa: E402

MAX_BODY = 512 * 1024


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self._send({'ok': False, 'code': 'bad_request'}, 400)
        if length <= 0 or length > MAX_BODY:
            return self._send({'ok': False, 'code': 'bad_request'}, 400)
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self._send({'ok': False, 'code': 'bad_request'}, 400)
        if not isinstance(payload, dict):
            return self._send({'ok': False, 'code': 'bad_request'}, 400)
        self._send(handle_review(payload))
