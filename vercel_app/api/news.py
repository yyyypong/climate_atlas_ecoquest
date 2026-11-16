"""Vercel serverless function: /api/news?lang=zh|en&refresh=1"""
import json
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _ecoquest import news_feed  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        query = parse_qs(urlsplit(self.path).query)
        lang = (query.get('lang') or ['zh'])[0]
        force = (query.get('refresh') or ['0'])[0] == '1'
        try:
            payload = news_feed(lang, force)
        except Exception as exc:  # never take the whole page down
            payload = {'items': [], 'fetchedAt': None, 'mode': 'snapshot', 'refreshFailed': True,
                       'error': type(exc).__name__}
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)
