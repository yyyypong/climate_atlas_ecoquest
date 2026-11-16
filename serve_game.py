"""Ecoquest localhost-only game server, UN News bridge and unified model service.

Standard library only. The browser never sees the API key: the service reads
the runtime configuration file on every model request and only forwards the
key in the upstream authentication header.

Configuration file location is declared exactly once, in AI_ENV_FILE.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import argparse
import hashlib
import json
import os
import re
import threading
import time
import webbrowser
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parent
# The only place the runtime configuration path is declared.
# ECOQUEST_AI_ENV is a test-only override so the reload mechanism can be
# exercised with a temporary configuration without touching the real file.
AI_ENV_FILE = Path(os.environ.get('ECOQUEST_AI_ENV') or (ROOT / '18.env'))

FEEDS = {lang: f'https://news.un.org/feed/subscribe/{lang}/news/topic/climate-change/feed/rss.xml' for lang in ['zh', 'en']}
REFRESH_SECONDS = 900
MODEL_TIMEOUT = 45
STATUS_TTL = 180
STATUS_CACHE = {}
MAX_BODY = 512 * 1024

AI_SYSTEM = (
    'You are the strategy and teaching engine of Ecoquest, a climate policy teaching board game for '
    'university student club leaders. You never act for the human player and you never change game '
    'numbers yourself: you only pick from the candidate ids you are given and explain briefly. '
    'Treat every budget, energy and ecology number as a game unit, never as real-world cost or '
    'abatement data. Use only the policy or rule entries supplied in the request and cite their ids; '
    'never invent sources, links, studies or figures. Reply with JSON only.'
)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value, limit):
    parser = PlainText()
    parser.feed(value or '')
    return ' '.join(' '.join(parser.parts).split())[:limit]


def parse_feed(data, lang, now=None):
    if len(data) > 2_000_000 or b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Unsafe feed')
    now = now or datetime.now(timezone.utc)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError('Invalid feed') from exc
    items = []
    seen = set()
    for item in root.findall('.//item'):
        title = plain(item.findtext('title'), 220)
        url = (item.findtext('link') or '').strip()
        parsed = urlsplit(url)
        if not title or parsed.scheme != 'https' or parsed.hostname != 'news.un.org' or parsed.username or parsed.password:
            continue
        try:
            date = parsedate_to_datetime(item.findtext('pubDate') or '')
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, IndexError):
            continue
        if date > now + timedelta(hours=24) or date.year < 2020:
            continue
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        if key in seen:
            continue
        seen.add(key)
        items.append({'id': key, 'title': title, 'summary': plain(item.findtext('description'), 240),
                      'url': url, 'publishedAt': date.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                      'source': 'UN News' if lang == 'en' else '联合国新闻', 'lang': lang})
    if not items:
        raise ValueError('No valid articles')
    return sorted(items, key=lambda x: x['publishedAt'], reverse=True)[:24]


def fetch_feed(lang):
    for attempt in range(2):
        try:
            request = Request(FEEDS[lang], headers={'User-Agent': 'Mozilla/5.0', 'Origin': 'null'})
            with urlopen(request, timeout=6) as response:
                data = response.read(2_000_001)
            return {'items': parse_feed(data, lang), 'fetchedAt': stamp(), 'mode': 'live', 'feedUrl': FEEDS[lang], 'refreshFailed': False}
        except Exception:
            if attempt:
                raise


def load_saved(path, lang):
    if path is None:
        return None
    try:
        value = json.loads(Path(path).read_text('utf-8')).get(lang)
        return value if isinstance(value, dict) and value.get('items') else None
    except (OSError, ValueError, AttributeError):
        return None


def load_snapshot(lang):
    return load_saved(ROOT / 'news-snapshot.json', lang)


class NewsFeed:
    def __init__(self, cache_path=ROOT / 'news-cache.json'):
        self.cache_path = cache_path
        self.cache = {}
        self.last_attempt = {}
        self.success_at = {}
        self.lock = threading.RLock()

    def get(self, lang, force=False):
        lang = 'en' if lang == 'en' else 'zh'
        with self.lock:
            now = time.monotonic()
            prior = self.cache.get(lang)
            floor = 30 if force else REFRESH_SECONDS
            if prior and now - self.last_attempt.get(lang, 0) < floor:
                result = dict(prior)
                result['mode'] = 'cache' if result.get('mode') != 'snapshot' else 'snapshot'
                return result
            self.last_attempt[lang] = now
            try:
                result = fetch_feed(lang)
                self.cache[lang] = result
                if self.cache_path:
                    try:
                        saved = {key: load_saved(self.cache_path, key) for key in FEEDS}
                        saved.update(self.cache)
                        target = Path(self.cache_path)
                        temp = target.with_suffix('.tmp')
                        temp.write_text(json.dumps(saved, ensure_ascii=False), 'utf-8')
                        temp.replace(target)
                    except OSError:
                        pass
                return dict(result)
            except Exception:
                saved = prior or load_saved(self.cache_path, lang)
                result = dict(saved or load_snapshot(lang) or {'items': [], 'fetchedAt': None})
                result.update(mode='cache' if saved and saved.get('mode') != 'snapshot' else 'snapshot', refreshFailed=True)
                self.cache[lang] = result
                return dict(result)


# ---------------------------------------------------------------- model layer

ENV_LINE = re.compile(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$')


def parse_env(text):
    values = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        match = ENV_LINE.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        values[name] = value.strip()
    return values


def ai_config():
    """Read and parse the runtime configuration on every call.

    Environment variables win over the configuration file, so a deployment can
    inject the key without shipping a credential file. The file stays the
    fallback for the offline launcher.
    """
    values = {}
    file_readable = True
    try:
        values = parse_env(AI_ENV_FILE.read_text('utf-8'))
    except OSError:
        values = {}
        file_readable = False
    values.update({name: value for name, value in os.environ.items() if value})
    env_has_key = any(name.endswith('_API_KEY') and value for name, value in os.environ.items())
    if not env_has_key and not file_readable:
        return {'ok': False, 'code': 'config_unreadable', 'missing': []}

    def pick(suffix, preferred):
        if values.get(preferred):
            return values[preferred]
        for name, value in sorted(values.items()):
            if name.endswith(suffix) and value:
                return value
        return ''

    key = pick('_API_KEY', 'DEEPSEEK_API_KEY')
    base = pick('_BASE_URL', 'DEEPSEEK_BASE_URL')
    model = pick('_MODEL', 'DEEPSEEK_MODEL')
    missing = [name for name, value in (('API key', key), ('base URL', base), ('model', model)) if not value]
    if missing:
        return {'ok': False, 'code': 'missing_config', 'missing': missing}
    return {'ok': True, 'key': key, 'base': base.rstrip('/'), 'model': model}


def ai_status():
    """Report the model service state.

    The configuration file alone does not prove that the service accepts the
    key, so the credential is verified once with a minimal request and the
    verdict is cached for a few minutes. The fingerprint is derived from the
    configuration values but is never returned to the browser.
    """
    config = ai_config()
    if not config['ok']:
        return {'enabled': False, 'code': config['code'], 'missing': config.get('missing', [])}
    fingerprint = hashlib.sha256(
        (config['key'] + '|' + config['base'] + '|' + config['model']).encode('utf-8')).hexdigest()
    cached = STATUS_CACHE.get('entry')
    now = time.monotonic()
    if cached and cached['fingerprint'] == fingerprint and now - cached['at'] < STATUS_TTL:
        verdict = cached['verdict']
    else:
        verdict = verify_credential(config)
        STATUS_CACHE['entry'] = {'fingerprint': fingerprint, 'at': now, 'verdict': verdict}
    if verdict['ok']:
        return {'enabled': True, 'model': config['model'], 'verifiedAt': stamp()}
    return {'enabled': False, 'code': verdict.get('code', 'unavailable'), 'model': config['model']}


def verify_credential(config):
    """Send a minimal request so the reported state matches the real one."""
    body = json.dumps({
        'model': config['model'],
        'messages': [{'role': 'user', 'content': 'ping'}],
        'max_tokens': 1,
        'temperature': 0,
    }, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Authorization': 'Bearer ' + config['key']}
    for url in endpoint_candidates(config['base']):
        try:
            request = Request(url, data=body, headers=headers, method='POST')
            with urlopen(request, timeout=MODEL_TIMEOUT) as response:
                response.read(4096)
            return {'ok': True}
        except HTTPError as exc:
            code = exc.code
            if code in (401, 403):
                return {'ok': False, 'code': 'auth_failed'}
            if code in (402, 429):
                return {'ok': False, 'code': 'quota_exceeded'}
            if code == 404:
                continue
            if 500 <= code < 600:
                return {'ok': False, 'code': 'service_error'}
            return {'ok': False, 'code': 'request_failed'}
        except (URLError, TimeoutError, OSError):
            return {'ok': False, 'code': 'network_error'}
        except (ValueError, KeyError, IndexError, TypeError):
            return {'ok': False, 'code': 'invalid_response'}
    return {'ok': False, 'code': 'endpoint_not_found'}


def endpoint_candidates(base):
    if base.endswith('/chat/completions'):
        return [base]
    if base.endswith('/v1'):
        return [base + '/chat/completions']
    return [base + '/chat/completions', base + '/v1/chat/completions']


def extract_json(text):
    if not text:
        raise ValueError('empty')
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json)?', '', cleaned)
    cleaned = re.sub(r'```$', '', cleaned).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        match = re.search(r'\{.*\}', cleaned, re.S)
        if not match:
            raise ValueError('no json')
        return json.loads(match.group(0))


def call_model(system, payload, temperature=0.5):
    config = ai_config()
    if not config['ok']:
        return {'ok': False, 'code': config['code'], 'missing': config.get('missing', [])}
    body = json.dumps({
        'model': config['model'],
        'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
        'temperature': temperature,
    }, ensure_ascii=False).encode('utf-8')
    headers = {'Content-Type': 'application/json; charset=utf-8', 'Authorization': 'Bearer ' + config['key']}
    last = 'unavailable'
    for url in endpoint_candidates(config['base']):
        try:
            request = Request(url, data=body, headers=headers, method='POST')
            with urlopen(request, timeout=MODEL_TIMEOUT) as response:
                raw = response.read(4_000_001)
            data = json.loads(raw.decode('utf-8'))
            content = data['choices'][0]['message']['content']
            return {'ok': True, 'content': content, 'model': config['model']}
        except HTTPError as exc:
            code = exc.code
            if code in (401, 403):
                return {'ok': False, 'code': 'auth_failed'}
            if code in (402, 429):
                return {'ok': False, 'code': 'quota_exceeded'}
            if code == 404:
                last = 'endpoint_not_found'
                continue
            if 500 <= code < 600:
                return {'ok': False, 'code': 'service_error'}
            return {'ok': False, 'code': 'request_failed'}
        except (URLError, TimeoutError, OSError):
            return {'ok': False, 'code': 'network_error'}
        except (ValueError, KeyError, IndexError, TypeError):
            return {'ok': False, 'code': 'invalid_response'}
    return {'ok': False, 'code': last}


def knowledge_ids(payload):
    ids = []
    for entry in payload.get('knowledge', []) or []:
        identifier = entry.get('id')
        if isinstance(identifier, str) and identifier and identifier not in ids:
            ids.append(identifier)
    return ids


def clean_citations(result, allowed):
    cited = result.get('citedIds') if isinstance(result, dict) else None
    if not isinstance(cited, list):
        return []
    return [item for item in cited if isinstance(item, str) and item in allowed]


RUBRIC_DIMENSIONS = [
    ('goalFit', 'National goal fit', [
        '0 unrelated to the national goal and gives up needed progress',
        '1 clearly off the national goal',
        '2 partially relevant but not the current priority',
        '3 moves in the direction of the national goal',
        '4 directly addresses the tightest current goal gap',
        '5 advances the national goal optimally among the available paths, at a controllable cost']),
    ('tradeoffs', 'Resource and opportunity cost', [
        '0 ignores budget or cost entirely',
        '1 cost is beyond what can be carried and no alternative was considered',
        '2 spends resources with no use for them',
        '3 spends reasonably but does not compare alternatives',
        '4 explicitly compares opportunity cost and takes the better option',
        '5 states clearly what was given up and why it was worth it']),
    ('resilience', 'Long-term risk and resilience', [
        '0 only looks at the present and creates new long-term risk',
        '1 ignores an obvious long-term risk',
        '2 notices risk but takes no action',
        '3 takes some resilience measures',
        '4 deliberately trades resources for long-term stability',
        '5 balances long-term stability and short-term gain with a reason']),
    ('cooperation', 'Cooperation and shared goals', [
        '0 ignores the shared goal',
        '1 pursues only the national interest and damages the shared goal',
        '2 does not consider cooperation',
        '3 cooperates when cooperation is available',
        '4 cooperates actively and improves the shared outcome',
        '5 creates cooperation at the right moment on terms fair to both sides']),
]


def handle_opponent(payload):
    event_options = (payload.get('event') or {}).get('options') or []
    action_options = (payload.get('action') or {}).get('options') or []
    event_ids = [o['id'] for o in event_options if isinstance(o, dict) and isinstance(o.get('id'), str)]
    action_ids = [o['id'] for o in action_options if isinstance(o, dict) and isinstance(o.get('id'), str)]
    if not event_ids or not action_ids:
        return {'ok': False, 'code': 'no_candidates'}
    system = AI_SYSTEM + (
        ' Task: you control a computer delegation. Choose one event option and one action option from the '
        'candidate lists given in the request. Return JSON: '
        '{"eventChoiceId": "<id from event.options>", "actionChoiceId": "<id from action.options>", '
        '"reason": "<one or two sentences explaining the trade-off in the language of the lang field>", '
        '"confidence": <0-1>}. Use only the information in the request: never invent cards, budgets, '
        'future events or hidden hands. Never output anything else.')
    answer = call_model(system, payload, temperature=0.65)
    if not answer['ok']:
        return {'ok': False, 'code': answer['code']}
    try:
        data = extract_json(answer['content'])
    except ValueError:
        return {'ok': False, 'code': 'invalid_response'}
    if not isinstance(data, dict):
        return {'ok': False, 'code': 'invalid_response'}
    event_choice = data.get('eventChoiceId')
    action_choice = data.get('actionChoiceId')
    if event_choice not in event_ids or action_choice not in action_ids:
        return {'ok': False, 'code': 'invalid_choice'}
    reason = data.get('reason') if isinstance(data.get('reason'), str) else ''
    return {'ok': True, 'result': {'eventChoiceId': event_choice, 'actionChoiceId': action_choice,
                                   'reason': reason.strip()[:600], 'model': answer['model']}}


def rubric_text():
    lines = []
    for key, label, levels in RUBRIC_DIMENSIONS:
        lines.append(f'- {key} ({label}): ' + ' | '.join(levels))
    return '\n'.join(lines)


def handle_coach(payload):
    allowed = knowledge_ids(payload)
    system = AI_SYSTEM + (
        ' Task: give short teaching feedback about a decision the human player already made. '
        'Use only information available at decision time. Never judge with later events. '
        'Return JSON: {"headline": "<one sentence>", "match": "<how it fits the goals>", '
        '"gained": "<what was gained>", "sacrificed": "<what was given up>", '
        '"alternative": {"label": "<an alternative worth considering>", "condition": "<when it would apply>"}, '
        '"nextFocus": "<one concrete thing for the next round>", '
        '"scores": {"goalFit": {"level": <0-5 or null>, "why": "<short reason>"}, '
        '"tradeoffs": {"level": <0-5 or null>, "why": "<short reason>"}, '
        '"resilience": {"level": <0-5 or null>, "why": "<short reason>"}, '
        '"cooperation": {"level": <0-5 or null>, "why": "<short reason>"}}, '
        '"citedIds": ["<ids>"]}. Use null for a dimension that cannot be judged from the information given. '
        'Money spent is not automatically wrong, saving is not automatically good, and cooperation is not always best.'
        '\nThe four learning dimensions use this fixed rubric (level: meaning):\n' + rubric_text() +
        '\nUse exactly these ids for citedIds, and only ids present in the supplied knowledge list. '
        'Write every text field in the language of the "lang" field.')
    answer = call_model(system, payload, temperature=0.4)
    if not answer['ok']:
        return {'ok': False, 'code': answer['code']}
    try:
        data = extract_json(answer['content'])
    except ValueError:
        return {'ok': False, 'code': 'invalid_response'}
    if not isinstance(data, dict):
        return {'ok': False, 'code': 'invalid_response'}
    scores = {}
    raw_scores = data.get('scores') if isinstance(data.get('scores'), dict) else {}
    for key in ('goalFit', 'tradeoffs', 'resilience', 'cooperation'):
        item = raw_scores.get(key)
        level = None
        why = ''
        if isinstance(item, dict) and isinstance(item.get('level'), (int, float)) and not isinstance(item.get('level'), bool):
            level = max(0, min(5, int(item['level'])))
            why = str(item.get('why') or '')[:240]
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            level = max(0, min(5, int(item)))
        scores[key] = {'level': level, 'why': why}
    result = {
        'headline': str(data.get('headline') or '')[:300],
        'match': str(data.get('match') or '')[:400],
        'gained': str(data.get('gained') or '')[:400],
        'sacrificed': str(data.get('sacrificed') or '')[:400],
        'alternative': {
            'label': str((data.get('alternative') or {}).get('label') or '')[:300],
            'condition': str((data.get('alternative') or {}).get('condition') or '')[:300],
        } if isinstance(data.get('alternative'), dict) else {'label': '', 'condition': ''},
        'nextFocus': str(data.get('nextFocus') or '')[:300],
        'scores': scores,
        'citedIds': clean_citations(data, allowed),
    }
    if not result['headline']:
        return {'ok': False, 'code': 'invalid_response'}
    result['model'] = answer['model']
    return {'ok': True, 'result': result}


def handle_review(payload):
    allowed = knowledge_ids(payload)
    system = AI_SYSTEM + (
        ' Task: write an end-of-game review for the human player based only on the supplied decision log. '
        'Do not invent actions the player did not take. '
        'Return JSON: {"summary": "<2-3 sentences>", '
        '"learning": {"goalFit": {"level": <0-5 or null>, "why": "<short>"}, "tradeoffs": {...}, "resilience": {...}, "cooperation": {...}}, '
        '"keyDecisions": [{"situation": "<what the situation was>", "choice": "<what was chosen>", '
        '"tradeoff": "<the main trade-off>", "alternative": "<a possible alternative>"}], '
        '"keep": "<one practice worth keeping>", "improve": "<one thing to improve next time>", '
        '"campus": {"action": "<one concrete campus action>", "goal": "<what it aims at>", '
        '"firstStep": "<the first step>", "who": "<who is needed>", "observe": "<how to observe the effect>", '
        '"verify": "<what still needs checking>"}, "citedIds": ["<ids>"]}. '
        'The campus suggestion must follow the player\'s own experience and must not turn game numbers into real costs or abatement.'
        '\nThe four learning dimensions use this fixed rubric (level: meaning):\n' + rubric_text() +
        '\nUse exactly these ids for citedIds, and only ids present in the supplied knowledge list. '
        'Write every text field in the language of the "lang" field.')
    answer = call_model(system, payload, temperature=0.45)
    if not answer['ok']:
        return {'ok': False, 'code': answer['code']}
    try:
        data = extract_json(answer['content'])
    except ValueError:
        return {'ok': False, 'code': 'invalid_response'}
    if not isinstance(data, dict):
        return {'ok': False, 'code': 'invalid_response'}
    scores = {}
    raw_scores = data.get('learning') if isinstance(data.get('learning'), dict) else {}
    for key in ('goalFit', 'tradeoffs', 'resilience', 'cooperation'):
        item = raw_scores.get(key)
        level = None
        why = ''
        if isinstance(item, dict) and isinstance(item.get('level'), (int, float)) and not isinstance(item.get('level'), bool):
            level = max(0, min(5, int(item['level'])))
            why = str(item.get('why') or '')[:240]
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            level = max(0, min(5, int(item)))
        scores[key] = {'level': level, 'why': why}
    decisions = []
    for item in data.get('keyDecisions') or []:
        if not isinstance(item, dict):
            continue
        decisions.append({
            'situation': str(item.get('situation') or '')[:400],
            'choice': str(item.get('choice') or '')[:400],
            'tradeoff': str(item.get('tradeoff') or '')[:400],
            'alternative': str(item.get('alternative') or '')[:400],
        })
    campus = data.get('campus') if isinstance(data.get('campus'), dict) else {}
    result = {
        'summary': str(data.get('summary') or '')[:800],
        'learning': scores,
        'keyDecisions': decisions[:3],
        'keep': str(data.get('keep') or '')[:400],
        'improve': str(data.get('improve') or '')[:400],
        'campus': {key: str(campus.get(key) or '')[:400] for key in ('action', 'goal', 'firstStep', 'who', 'observe', 'verify')},
        'citedIds': clean_citations(data, allowed),
    }
    if not result['summary'] or not result['campus']['action']:
        return {'ok': False, 'code': 'invalid_response'}
    result['model'] = answer['model']
    return {'ok': True, 'result': result}


AI_ROUTES = {'/api/ai/opponent': handle_opponent, '/api/ai/coach': handle_coach, '/api/ai/review': handle_review}


def main():
    args = argparse.ArgumentParser()
    args.add_argument('--port', type=int, default=8765)
    args.add_argument('--open', action='store_true')
    args.add_argument('--game', type=Path)
    opts = args.parse_args()
    game = opts.game or ROOT / 'Ecoquest-Climate-Atlas.html'
    if not game.exists():
        game = ROOT.parent / 'outputs/Ecoquest-Climate-Atlas.html'
    if not game.exists():
        raise SystemExit('Place the launcher next to Ecoquest-Climate-Atlas.html')
    feed = NewsFeed()
    last_request = [time.monotonic()]
    replay_lock = threading.Lock()
    replay_cache = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, data, status=200, mime='application/json; charset=utf-8'):
            self.send_response(status)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            origin = self.headers.get('Origin')
            if origin == 'null' or origin in ('http://127.0.0.1:' + str(self.server.server_port), 'http://localhost:' + str(self.server.server_port)):
                self.send_header('Access-Control-Allow-Origin', origin)
                self.send_header('Vary', 'Origin')
            self.end_headers()
            self.wfile.write(data)

        def local_origin_ok(self):
            origin = self.headers.get('Origin')
            if origin is None:
                return True
            if origin == 'null':
                return True
            return origin in ('http://127.0.0.1:' + str(self.server.server_port), 'http://localhost:' + str(self.server.server_port))

        def do_GET(self):
            host = urlsplit('http://' + self.headers.get('Host', '')).hostname
            if host not in ('localhost', '127.0.0.1'):
                return self.reply(b'Forbidden', 403, 'text/plain')
            last_request[0] = time.monotonic()
            url = urlsplit(self.path)
            if url.path == '/api/health':
                return self.reply(b'{"app":"Ecoquest","version":2}')
            if url.path == '/api/ai/status':
                return self.reply(json.dumps(ai_status(), ensure_ascii=False).encode())
            if url.path == '/api/news':
                query = parse_qs(url.query)
                value = feed.get(query.get('lang', ['zh'])[0], query.get('refresh', ['0'])[0] == '1')
                return self.reply(json.dumps(value, ensure_ascii=False).encode())
            if url.path in ('/', '/Ecoquest-Climate-Atlas.html'):
                return self.reply(game.read_bytes(), mime='text/html; charset=utf-8')
            return self.reply(b'Not found', 404, 'text/plain')

        def do_POST(self):
            host = urlsplit('http://' + self.headers.get('Host', '')).hostname
            if host not in ('localhost', '127.0.0.1'):
                return self.reply(b'Forbidden', 403, 'text/plain')
            if not self.local_origin_ok():
                return self.reply(b'Forbidden', 403, 'text/plain')
            url = urlsplit(self.path)
            handler = AI_ROUTES.get(url.path)
            if not handler:
                return self.reply(b'Not found', 404, 'text/plain')
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                return self.reply(b'Bad request', 400, 'text/plain')
            if length <= 0 or length > MAX_BODY:
                return self.reply(b'Bad request', 400, 'text/plain')
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                return self.reply(b'Bad request', 400, 'text/plain')
            if not isinstance(payload, dict):
                return self.reply(b'Bad request', 400, 'text/plain')
            request_id = payload.get('requestId')
            if isinstance(request_id, str) and request_id:
                with replay_lock:
                    if request_id in replay_cache:
                        return self.reply(json.dumps(replay_cache[request_id], ensure_ascii=False).encode())
            outcome = handler(payload)
            if isinstance(request_id, str) and request_id:
                with replay_lock:
                    replay_cache[request_id] = outcome
                    if len(replay_cache) > 400:
                        for key in list(replay_cache)[:200]:
                            replay_cache.pop(key, None)
            return self.reply(json.dumps(outcome, ensure_ascii=False).encode())

    server = None
    for port in range(opts.port, opts.port + 10):
        try:
            server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            break
        except OSError:
            try:
                with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=.5) as response:
                    existing = json.load(response)
                if existing.get('app') == 'Ecoquest' and existing.get('version') == 2:
                    if opts.open:
                        webbrowser.open(f'http://127.0.0.1:{port}/')
                    return
            except Exception:
                pass
    if server is None:
        raise SystemExit('No available local port')
    server.daemon_threads = True
    server.timeout = 1
    address = f'http://127.0.0.1:{server.server_port}/'
    print(address, flush=True)
    if opts.open:
        webbrowser.open(address)
    try:
        while time.monotonic() - last_request[0] < 7200:
            server.handle_request()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
