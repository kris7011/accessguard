"""AccessGuard research prototype. Python 3.11+, standard library only."""
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def http_json(url, token=None, payload=None):
    headers = {}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        data = json.dumps(payload).encode()
    request = Request(url, data=data, headers=headers)
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        response = opener.open(request, timeout=30)
    except HTTPError as error:
        response = error
    with response:
        raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError('Response exceeds limit')
        return response.status, json.loads(raw)


@contextmanager
def fixture(vulnerable=False):
    """Only synthetic data. The server exists only for the duration of the run."""
    users = {'demo-a': 'a', 'demo-b': 'b'}
    records = {
        '101': {'id': '101', 'tenant': 'a', 'marker': 'SYNTHETIC-A-101'},
        '202': {'id': '202', 'tenant': 'b', 'marker': 'SYNTHETIC-B-202'},
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            tenant = users.get(self.headers.get('Authorization', '').removeprefix('Bearer '))
            status, body = 401, {'error': 'unauthorized'}
            if tenant:
                record = records.get(self.path.removeprefix('/documents/')) if self.path.startswith('/documents/') else None
                if record is None:
                    status, body = 404, {'error': 'not found'}
                elif not vulnerable and record['tenant'] != tenant:
                    status, body = 403, {'error': 'forbidden'}
                else:
                    status, body = 200, record
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@dataclass(frozen=True)
class Case:
    id: str
    actor: str
    owner: str
    document: str
    allowed: bool


CASES = [
    Case('a-own', 'a', 'a', '101', True),
    Case('b-own', 'b', 'b', '202', True),
    Case('a-cross', 'a', 'b', '202', False),
    Case('b-cross', 'b', 'a', '101', False),
    Case('anonymous', 'anonymous', 'a', '101', False),
]


def verdict(allowed, baseline, observed):
    """Fail closed on coverage: errors and ambiguous replies are inconclusive."""
    bs, bb = baseline
    status, body = observed
    if bs != 200 or not isinstance(bb, dict) or not bb.get('marker'):
        return 'INCONCLUSIVE'
    if allowed:
        return 'PASS' if status == 200 and body == bb else 'INCONCLUSIVE'
    if status in (401, 403, 404):
        return 'PASS'
    if status == 200 and isinstance(body, dict) and body.get('marker') == bb['marker']:
        return 'FAIL'
    return 'INCONCLUSIVE'


def validate_plan(ids):
    expected = {c.id for c in CASES}
    if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
        raise ValueError('Plan must be a list of case IDs')
    if len(ids) != len(expected) or set(ids) != expected:
        raise ValueError('Planner must preserve every approved case exactly once')
    return sorted(CASES, key=lambda c: ids.index(c.id))


def ai_plan(model):
    """Optional real LLM call. Model can reorder cases, never invent HTTP targets."""
    status, response = http_json('http://127.0.0.1:11434/api/chat', payload={
        'model': model, 'stream': False, 'format': 'json',
        'messages': [
            {'role': 'system', 'content': 'Prioritize authorization tests. Return JSON with order: every supplied case ID exactly once, highest-risk first. Do not add cases.'},
            {'role': 'user', 'content': json.dumps([asdict(c) for c in CASES])},
        ],
    })
    if status != 200:
        raise ValueError('Ollama request failed')
    return validate_plan(json.loads(response['message']['content'])['order'])


def scan(base, cases):
    results = []
    for case in cases:
        token = None if case.actor == 'anonymous' else 'demo-' + case.actor
        try:
            baseline = http_json(base + '/documents/' + case.document, 'demo-' + case.owner)
            observed = http_json(base + '/documents/' + case.document, token)
            result = verdict(case.allowed, baseline, observed)
            status = observed[0]
        except (OSError, ValueError):
            result, status = 'INCONCLUSIVE', None
        results.append({**asdict(case), 'result': result, 'http_status': status,
                        'evidence': 'Synthetic owner marker returned to unauthorized account' if result == 'FAIL' else None})
    return results


def demo(model=None):
    cases = ai_plan(model) if model else CASES
    runs = {}
    for name, vulnerable in [('vulnerable', True), ('fixed', False)]:
        with fixture(vulnerable) as base:
            runs[name] = scan(base, cases)
    return {'version': '0.1.0', 'planner': 'ollama:' + model if model else 'deterministic baseline (no AI)',
            'scope': 'Local synthetic fixture only; not a security certification', 'runs': runs}


def save_report(report, directory):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    rows = ''.join('<tr>' + ''.join('<td>' + html.escape(str(v)) + '</td>' for v in
                   (mode, r['id'], r['allowed'], r['http_status'], r['result'])) + '</tr>'
                   for mode, records in report['runs'].items() for r in records)
    page = '''<!doctype html><html lang="da"><meta charset="utf-8"><title>AccessGuard — testresultat</title>
<style>body{font:17px system-ui;background:#101827;color:#e8eef9;max-width:1000px;margin:60px auto;padding:24px}h1{font-size:42px}table{width:100%;border-collapse:collapse}td,th{padding:14px;text-align:left;border-bottom:1px solid #34435a}p{line-height:1.6}code{color:#74dfb3}</style>
<h1>AccessGuard / 0.1</h1><p>Reelle HTTP-tests mod et lokalt demo-API med syntetiske data. To kunders adgang sammenlignes før og efter en rettelse.</p>
<p>Planner: <code>''' + html.escape(report['planner']) + '''</code></p>
<table><thead><tr><th>Version</th><th>Test</th><th>Adgang tilladt</th><th>HTTP</th><th>Resultat</th></tr></thead><tbody>''' + rows + '''</tbody></table>
<p>PASS gælder kun det viste testtilfælde. INCONCLUSIVE betyder, at kontrollen ikke kunne afgøre resultatet. Rapporten er ikke en garanti for sikkerhed.</p>
<p>Rettelse i demo: Afvis dokumentadgang, når dokumentets tenant ikke matcher den autentificerede brugers tenant. Alle tests køres igen mod den rettede version.</p></html>'''
    (directory / 'report.html').write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ollama-model', help='Optional installed local Ollama model; no silent fallback')
    parser.add_argument('--out', type=Path, default=Path('reports'))
    args = parser.parse_args()
    report = demo(args.ollama_model)
    save_report(report, args.out)
    for name, results in report['runs'].items():
        print(name, {key: sum(r['result'] == key for r in results) for key in ('PASS', 'FAIL', 'INCONCLUSIVE')})
    print('Report:', (args.out / 'report.html').resolve())
    return 0 if ([r['id'] for r in report['runs']['vulnerable'] if r['result'] == 'FAIL'] and
                 all(r['result'] == 'PASS' for r in report['runs']['fixed'])) else 1


if __name__ == '__main__':
    raise SystemExit(main())
