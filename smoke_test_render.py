#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REQUIRED = [
    ('/', 'AOE4IT'),
    ('/play', 'Queue Command Center'),
    ('/tournaments', 'Featured Event'),
    ('/ladder', 'Ladder'),
    ('/players', 'Player'),
    ('/news', 'News'),
    ('/premium', 'Premium'),
    ('/login', '_csrf_token'),
    ('/register', '_csrf_token'),
    ('/healthz', '"ok": true'),
    ('/readyz', '"ok": true'),
]


def fetch(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={'User-Agent': 'aoe4it-smoke/1.0'})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode('utf-8', errors='replace')
        return resp.status, body


def main() -> int:
    if len(sys.argv) != 2:
        print('Usage: python smoke_test_render.py https://your-render-url.onrender.com')
        return 2
    base = sys.argv[1].rstrip('/')
    failures = 0
    for path, marker in REQUIRED:
        url = base + path
        try:
            status, body = fetch(url)
        except urllib.error.HTTPError as exc:
            print(f'FAIL {path}: HTTP {exc.code}')
            failures += 1
            continue
        except Exception as exc:
            print(f'FAIL {path}: {exc}')
            failures += 1
            continue
        ok = status == 200 and marker in body
        print(f'{"PASS" if ok else "FAIL"} {path}: HTTP {status}')
        if not ok:
            failures += 1
    if failures:
        print(f'\nSmoke test finished with {failures} failure(s).')
        return 1
    print('\nSmoke test finished cleanly.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
