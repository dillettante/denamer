#!/bin/bash
# 웹판(denamer.html)이 파이썬 코어와 같은 결과를 내는지 대조한다.
# sync_web.py가 데이터·정규식을 생성해 주지만, 손으로 쓴 JS 알고리즘까지는
# 보장하지 못한다. 이 스크립트가 그 나머지를 실측으로 잡는다.
# 필요: node
set -e
cd "$(dirname "$0")"
PY="${DENAMER_PY:-$HOME/.venvs/denamer/bin/python}"
SC=$(mktemp -d); trap 'rm -rf "$SC"' EXIT
S=$(grep -n '── 탐지 규칙·사전 ──' denamer.html | cut -d: -f1)
E=$(grep -n '^// ── UI 배선 ──' denamer.html | cut -d: -f1)
sed -n "${S},$((E-1))p" denamer.html > "$SC/app.js"
sed -i '' '1i\
const localStorage = { _d:{}, getItem(k){return this._d[k]??null;}, setItem(k,v){this._d[k]=v;}, removeItem(k){delete this._d[k];} };
' "$SC/app.js"
cat >> "$SC/app.js" <<'JS'
const CASES = JSON.parse(process.env.CASES);
const out = {};
for (const [name, text] of CASES) out[name] = detect(text).map(([l,v]) => l+"|"+v).sort();
console.log(JSON.stringify(out));
JS
"$PY" - <<'PY' > "$SC/cases.json"
from test_denamer import MUST_DETECT, MUST_NOT_DETECT
import json
print(json.dumps([[f"C{i}", t] for i,(d,t,_) in enumerate(MUST_DETECT + MUST_NOT_DETECT)],
                 ensure_ascii=False))
PY
CASES=$(cat "$SC/cases.json") node "$SC/app.js" > "$SC/js.json"
"$PY" - "$SC/js.json" <<'PY'
import json, sys; sys.path.insert(0, ".")
from detect import detect
from test_denamer import MUST_DETECT, MUST_NOT_DETECT
js = json.load(open(sys.argv[1])); same = diff = 0
for i, (d, t, _) in enumerate(MUST_DETECT + MUST_NOT_DETECT):
    # ko-pii는 웹판에 없다 — 정규식·이름엔진이 만든 결과만 비교한다
    py = sorted(f"{l}|{v}" for l, v in detect(t) if l != "PERSON")
    j = sorted(js[f"C{i}"])
    if py == j:
        same += 1
    else:
        diff += 1
        print(f"  DIFF [{d}]\n    core={py}\n    web ={j}")
print(f"웹판↔코어 일치 {same} / 불일치 {diff}")
sys.exit(1 if diff else 0)
PY
