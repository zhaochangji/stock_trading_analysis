#!/bin/sh
# Run against the locally running 9989 dashboard; never refreshes market sources.
set -eu
BASE_URL="${BASE_URL:-http://127.0.0.1:9989}"
CODE="${CODE:-002815}"
python3 - "$BASE_URL" "$CODE" <<'PY'
import sys
import urllib.error
import urllib.parse
import urllib.request

base, code = sys.argv[1:]

def page(path):
    with urllib.request.urlopen(base + path, timeout=15) as response:
        assert response.status == 200, (path, response.status)
        return response.read().decode('utf-8')

home = page('/')
assert '/analyze' in home and '分析买点' in home
empty = page('/analyze')
assert '股票代码或名称' in empty and '买点分析' in empty
invalid = page('/analyze?query=%3Cscript%3Ealert(1)%3C%2Fscript%3E')
assert '<script>alert(1)</script>' not in invalid
assert '&lt;script&gt;alert(1)&lt;/script&gt;' in invalid

# A warm analysis is checked in full. Cold cache must show polling instead of blocking.
analysis = page('/analyze?query=' + urllib.parse.quote(code))
if '正在获取行情' in analysis:
    assert '/analyze/status?code=' + code in analysis
else:
    assert 'analysis-verdict' in analysis
    assert analysis.count('class="analysis-check ') == 9
    assert '参考价格计划' in analysis and '消息面 · 原始标题' in analysis
    assert '不执行任何买卖' in analysis

status = page('/analyze/status?code=' + code)
assert '"ready"' in status and ('true' in status or 'false' in status)
print('PASS: /, /analyze, query escaping, cold/warm analysis, status')
PY
