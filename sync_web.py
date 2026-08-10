#!/usr/bin/env python3
"""웹판(denamer.html)의 탐지 규칙·사전을 파이썬 코어에서 생성한다.

왜 생성하는가:
  웹판은 브라우저에서 단독 실행돼야 해서 규칙 사본을 가질 수밖에 없다. 그런데
  사본을 손으로 옮기면 반드시 어긋난다 — 데스크톱판만 고치고 웹판은 옛 규칙으로
  남아 조용히 새는 것이 가장 흔한 사고다. 그래서 사본을 '만들어' 넣는다.

  이 스크립트는 HTML의 두 마커 사이만 갈아 끼운다. 알고리즘(후보 생성·필터)은
  마커 밖 손으로 쓴 JS에 있고, 드리프트가 나기 쉬운 **데이터와 정규식**만 생성한다.

실행: ~/.venvs/denamer/bin/python sync_web.py   (denamer.html을 제자리 수정)
"""
import re
import sys
from pathlib import Path

import detect as D
import names as N
import stopwords as S

BEGIN = "// <<<GENERATED — sync_web.py 가 생성한다. 직접 고치지 말 것"
END = "// GENERATED>>>"

# JS에서 이름으로 참조할 검증기 (손으로 쓴 JS 쪽에 같은 이름의 함수가 있어야 한다)
VALIDATOR_JS = {
    D._rrn_plausible: "rrnPlausible",
    D._business_no_ok: "businessNoOk",
    D._card_luhn_ok: "cardLuhnOk",
    D._account_ok: "accountOk",
}


def js_str(s: str) -> str:
    """파이썬 문자열 → JS 문자열 리터럴."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def js_set(name: str, values) -> str:
    items = ",".join(js_str(v) for v in sorted(values))
    return f"const {name} = new Set([{items}]);"


def js_list(name: str, values) -> str:
    items = ",".join(js_str(v) for v in values)
    return f"const {name} = [{items}];"


def to_js_regex(pattern: str) -> str:
    """파이썬 정규식 문자열 → JS에서 쓸 수 있는 형태.

    이름 있는 그룹만 손보면 된다. 나머지(전방·후방탐색, 수량자, 문자클래스)는
    두 언어의 문법이 같다.
    """
    return re.sub(r"\(\?P<\w+>", "(", pattern)


def anchored(pattern: str) -> str:
    """파이썬 `.match()`는 앞이 고정이지만 JS `.test()`는 아니다 — `^`를 붙여 맞춘다."""
    return pattern if pattern.startswith("^") else "^" + pattern


def generate() -> str:
    out = [
        BEGIN,
        f"//   생성 원본: detect.py · names.py · stopwords.py",
        f"const DASH = {js_str(D.DASH)};",
        f"const SP = {js_str(D.SP)};",
        f"const SEP = {js_str(D.SEP)};",
        f"const REGIONS = {js_str(D.REGIONS)};",
        "",
        js_set("SURNAME_STOPWORDS", S.SURNAME_STOPWORDS),
        js_set("RELATION_CONTEXT_STOPWORDS", S.RELATION_CONTEXT_STOPWORDS),
        js_set("NAME_STOPWORDS", S.NAME_STOPWORDS),
        js_set("LABEL_WORDS", N.LABEL_WORDS),
        js_list("COMPOUND_SURNAMES", N.COMPOUND_SURNAMES),
        js_list("JOSA_SUFFIXES", N._JOSA_SUFFIXES),
        js_list("JOSA_ABSORB", sorted(S._JOSA_ABSORB | S._LOC_SUFFIX)),
        "",
        f"const SURNAME_HEAD = new RegExp({js_str(anchored(N.SURNAME_HEAD.pattern))});",
        f"const CORP_TAIL = new RegExp({js_str(anchored(N.CORP_TAIL.pattern))});",
        f"const JOSA_HEAD = new RegExp({js_str(anchored(D._JOSA_HEAD.pattern))});",
        f"const PHONE_LIKE = new RegExp({js_str('^' + D.PHONE_PREFIX)});",
        f"const NAME_RX = {js_str(N.NAME_RX)};",
        f"const REGION_TOKEN = new RegExp({js_str('^(?:' + D.REGIONS + ')$')});",
        "",
        "// [라벨, 정규식, 문맥어 배열|null, 검증기명|null]",
        "const RULES = [",
    ]
    for rule in D.RULES:
        ctx = ("[" + ",".join(js_str(w) for w in rule.context) + "]") if rule.context else "null"
        val = VALIDATOR_JS.get(rule.validator)
        val_js = val if val else "null"
        out.append(f"  [{js_str(rule.label)}, "
                   f"new RegExp({js_str(to_js_regex(rule.pattern.pattern))}, \"g\"), "
                   f"{ctx}, {val_js}],")
    out.append("];")
    out.append("")
    out.append("// 이름 문맥 규칙 — 1번 그룹이 이름 자리다")
    out.append("const NAME_PATTERNS = [")
    for pat in (N._PAT_LEGAL_PREFIX, N._PAT_GENERAL_PREFIX, N._PAT_RELATION_PREFIX,
                N._PAT_LEGAL_SUFFIX, N._PAT_GENERAL_SUFFIX, N._PAT_HONORIFIC,
                N._PAT_PAREN_ID, N._PAT_COPULA, N._PAT_MASKED_STYLE, N._PAT_DATIVE):
        # 관계어 규칙만 추가 조건이 붙는다(친족 문맥 불용어)
        kind = "relation" if pat is N._PAT_RELATION_PREFIX else "plain"
        # 쉼표 연쇄를 이어붙일 규칙(라벨 앞머리 계열)
        chain = pat in (N._PAT_LEGAL_PREFIX, N._PAT_GENERAL_PREFIX, N._PAT_RELATION_PREFIX)
        # d 플래그가 있어야 m.indices로 이름 그룹의 위치를 알 수 있다.
        # (JS는 파이썬의 m.start("name")에 해당하는 기능이 이 플래그에만 있다.)
        out.append(f"  [new RegExp({js_str(to_js_regex(pat.pattern))}, \"gd\"), "
                   f"{js_str(kind)}, {str(chain).lower()}],")
    out.append("];")
    out.append("")
    out.append("const LABEL_NAMES = {" + ",".join(
        f"{js_str(k)}:{js_str(v)}" for k, v in sorted(D.LABEL_NAMES.items())) + "};")
    out.append(END)
    return "\n".join(out)


def main() -> None:
    html_path = Path(__file__).parent / "denamer.html"
    html = html_path.read_text(encoding="utf-8")
    start, stop = html.find(BEGIN), html.find(END)
    if start == -1 or stop == -1:
        raise SystemExit(f"마커를 찾지 못했습니다. denamer.html에 다음 두 줄이 있어야 합니다:\n"
                         f"  {BEGIN}\n  {END}")
    new_html = html[:start] + generate() + html[stop + len(END):]
    if new_html == html:
        print("변경 없음 — 웹판이 이미 코어와 같습니다.")
        return
    html_path.write_text(new_html, encoding="utf-8")
    print(f"denamer.html 갱신 — 규칙 {len(D.RULES)}종, 이름 규칙 10종, "
          f"비이름 사전 {len(S.SURNAME_STOPWORDS)}단어")


if __name__ == "__main__":
    sys.exit(main())
