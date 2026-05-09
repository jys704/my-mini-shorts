import os
import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import httpx


ENV_PATH = Path('.env')
PORT = 8765
REDIRECT_URI = f'http://127.0.0.1:{PORT}/callback'
SCOPE = 'https://www.googleapis.com/auth/youtube.upload'


def load_env_file(env_path: Path = ENV_PATH):
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def upsert_env(key: str, value: str, env_path: Path = ENV_PATH):
    lines = []
    found = False
    if env_path.exists():
        lines = env_path.read_text(encoding='utf-8').splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f'{key}='):
            lines[i] = f'{key}={value}'
            found = True
            break
    if not found:
        lines.append(f'{key}={value}')
    env_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')


load_env_file()
CLIENT_ID = os.getenv('YOUTUBE_CLIENT_ID', '').strip()
CLIENT_SECRET = os.getenv('YOUTUBE_CLIENT_SECRET', '').strip()

if not CLIENT_ID or not CLIENT_SECRET:
    print('\n[오류] .env 파일에 아래 2개를 먼저 넣어주세요.')
    print('YOUTUBE_CLIENT_ID=...')
    print('YOUTUBE_CLIENT_SECRET=...')
    raise SystemExit(1)

state = 'youtube-local-upload'
result = {'code': None, 'error': None}


class OAuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != '/callback':
            self.send_response(404)
            self.end_headers()
            return
        qs = parse_qs(parsed.query)
        result['code'] = qs.get('code', [None])[0]
        result['error'] = qs.get('error', [None])[0]
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        if result['code']:
            self.wfile.write(b'<h2>YouTube authorization completed. You can close this window.</h2>')
        else:
            self.wfile.write(b'<h2>Authorization failed. You can close this window.</h2>')

    def log_message(self, format, *args):
        return


params = {
    'client_id': CLIENT_ID,
    'redirect_uri': REDIRECT_URI,
    'response_type': 'code',
    'scope': SCOPE,
    'access_type': 'offline',
    'prompt': 'consent',
    'state': state,
}
auth_url = 'https://accounts.google.com/o/oauth2/v2/auth?' + urlencode(params)

print('\n[1/3] 브라우저 인증을 시작합니다.')
print('브라우저가 자동으로 열리지 않으면 아래 주소를 직접 여세요:\n')
print(auth_url)

server = HTTPServer(('127.0.0.1', PORT), OAuthHandler)
webbrowser.open(auth_url)

print(f'\n[2/3] 브라우저에서 로그인/동의 후 돌아오면 자동 처리됩니다. 대기 포트: {PORT}')
while not result['code'] and not result['error']:
    server.handle_request()

if result['error']:
    print('\n[오류] 승인 실패:', result['error'])
    raise SystemExit(1)

print('\n[3/3] refresh token 발급 중...')
resp = httpx.post(
    'https://oauth2.googleapis.com/token',
    data={
        'client_id': CLIENT_ID,
        'client_secret': CLIENT_SECRET,
        'code': result['code'],
        'grant_type': 'authorization_code',
        'redirect_uri': REDIRECT_URI,
    },
    timeout=30,
)
data = resp.json()
refresh_token = data.get('refresh_token')
if not refresh_token:
    print('\n[오류] refresh_token이 반환되지 않았습니다.')
    print(json.dumps(data, ensure_ascii=False, indent=2))
    print('\n팁: 기존 승인 이력이 있으면 refresh_token이 안 올 수 있습니다. 이 경우 prompt=consent로 다시 시도하거나 Google 계정 연결을 해제 후 재시도하세요.')
    raise SystemExit(1)

upsert_env('YOUTUBE_REFRESH_TOKEN', refresh_token)
print('\n[완료] .env 파일에 YOUTUBE_REFRESH_TOKEN 저장 완료')
print('이제 shorts_complete.py 또는 shorts_complete_fixed.py를 다시 실행하면 자동 업로드를 사용할 수 있습니다.')
