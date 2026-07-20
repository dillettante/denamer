#!/usr/bin/env python3
"""B v3: 자체 PDF 비실명화 엔진 (정규식 + ko-pii 이중 탐지).

원칙:
  1. 탐지: ① 자체 정규식(표기 변형 관용) + ② ko-pii(형태소 기반, 무라벨 이름 담당)
     의 합집합. 텍스트를 정규화하지 않는다 — 정규화하면 오프셋 역매핑이 필요해지고,
     그 매핑이 A(Nothing)의 김철수 유출 버그를 만든 지점이다.
  2. 제거: 값을 페이지에서 전수 검색해 모든 출현 redact. 여러 줄 값은 조각별 검색.
  3. 검증: 저장본 재오픈 → 재검색. 매핑 실패·잔존 시 exit 1.

ko-pii PERSON 오탐 대책: Nothing(A)은 오탐("등록번호"→PERSON conf 1.00) 탓에
PERSON 라벨을 통째로 껐지만, 여기선 근거(evidence)로 거른다 — 진짜 이름에만
'pos:surname'(성씨) 근거가 붙는다. ko-pii 미설치 환경에선 정규식만으로 동작.

사용: ./venv/bin/python b_redact.py input.pdf output.pdf   (ko-pii는 venv에 있음)
"""
import json
import os
import re
import sys

import fitz  # PyMuPDF

# ── 표기 변형 빌딩블록 ──
DASH = r"[-−–—―.·]"            # ASCII '-' + 유니코드 마이너스/대시류 + 점
SP = r"[ \t]"                        # 같은 줄 안의 공백만 — \s는 개행을 넘어가
                                     # 여러 줄 숫자를 이어붙인 괴물 매치를 만든다
                                     # (실문서 64쪽에서 2자 조각 수천 개 → 과잉제거+폭주)
SEP = rf"{SP}*{DASH}?{SP}*"          # 구분자: 대시류(선택) + 같은 줄 공백 허용
def spaced(n: int) -> str:           # 자간 벌어진 숫자열: "9 2 0 3 1 5"
    return r"\d" + rf"(?:{SP}*\d){{{n - 1}}}"

# 시·도 지명 (무라벨 주소의 앵커) — 정식·축약 병기
REGIONS = (
    "서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|"
    "세종특별자치시|경기도|강원특별자치도|강원도|충청북도|충청남도|전북특별자치도|"
    "전라북도|전라남도|경상북도|경상남도|제주특별자치도|제주도"
)

# ── 탐지 규칙: 'v' 그룹(없으면 전체 매치)이 제거 대상 값 ──
RULES: list[tuple[str, re.Pattern]] = [
    # 주민/외국인번호: 자간·대시류 변형 허용, 뒷자리 첫 숫자 1-8
    ("RRN",      re.compile(rf"(?<!\d){spaced(6)}{SEP}[1-8](?:{SP}*\d){{6}}(?!\d)")),
    ("CARD",     re.compile(rf"(?<!\d)\d{{4}}(?:{SEP}\d{{4}}){{3}}(?!\d)")),
    ("PASSPORT", re.compile(r"(?<![A-Za-z0-9])(?:[MSRODmsrod]\d{8}|[A-Za-z]{2}\d{7})(?![A-Za-z0-9])")),
    # 계좌: 라벨 뒤 은행명·개행이 끼어도 허용 (A가 두 번 놓친 지점)
    # — 라벨↔값 사이만 개행 허용(의도), 값 자체는 같은 줄 안에서 끝나야 한다
    ("ACCOUNT",  re.compile(
        rf"(?:계좌번호|입금계좌|환급계좌|납부계좌|은행계좌|계좌){SP}*[:：]?\s*"
        rf"(?:[가-힣A-Za-z()\s]{{0,12}}?)"
        rf"(?P<v>\d(?:[\d\-]|{SP}){{7,24}}\d)")),
    # 전화: 구분자 필수 (없으면 '20230809' 같은 날짜가 전화로 오인된다)
    ("PHONE",    re.compile(
        rf"(?<!\d)(?:0\d{{1,2}}\)?{SEP})?\d{{3,4}}{SP}*(?:{DASH}|{SP}){SP}*\d{{4}}(?!\d)")),
    # 전화(무구분 휴대폰): 01X로 시작하는 10~11자리만 허용
    ("PHONE",    re.compile(r"(?<!\d)01[016789]\d{7,8}(?!\d)")),
    # 이메일: @ 앞뒤 개행 허용 (줄바꿈으로 쪼개진 케이스)
    ("EMAIL",    re.compile(r"[A-Za-z0-9._%+-]+\s*@\s*[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # 이름: 역할 라벨 문맥 — 공문서·소송문서는 라벨 자간이 벌어진다("피 고 인")
    # 쉼표 나열("변호사 홍길동, 김철수")은 run 전체를 잡아 detect에서 분해
    ("NAME",     re.compile(
        r"(?:성\s*명|담\s*당\s*자|예\s*금\s*주|신\s*청\s*인|대\s*표\s*자|"
        r"피\s*신\s*청\s*인|위\s*임\s*인|수\s*임\s*인|피\s*고\s*인|피\s*고|"
        r"원\s*고|변\s*호\s*인|변\s*호\s*사|검\s*사|증\s*인|참\s*고\s*인)"
        r"(?:\s|[:：])+"    # 라벨-이름 구분자 필수: "피고인들은"에서 '들은' 캡처 방지
        # 경계는 느슨하게 잡고(한글 직결만 금지) 의미 검증은 detect()에서:
        # 뒤따르는 한글 단어가 직함일 때만 이름 인정 — "이도 주무관" O,
        # "가나자 산신탁"(양쪽정렬이 회사명 안에 공백을 끼운 것) X.
        # 캡처 뒤 콜론 금지: "3. 담당자\n담당자: 김철수"에서 둘째 라벨이 값으로
        # 소비되면 진짜 이름 매치가 시도조차 안 된다 — 실패시켜 안쪽 재스캔 유도
        r"(?P<v>[가-힣]{2,4}(?:\s*,\s*[가-힣]{2,4})*)(?![가-힣])(?!\s*[:：])")),
    # 주소①: 라벨 문맥 — 주거·소재지·등록기준지 등 공문서·행정문서 라벨 포함.
    # 쉼표는 주소 내부에 흔하다("효원로 241, 302동") — 줄 끝까지가 값이다
    ("ADDRESS",  re.compile(
        r"(?:주\s*소|주\s*거|소\s*재\s*지|등\s*록\s*기\s*준\s*지|거\s*소|사\s*업\s*장)"
        r"(?:\s|[:：])+"    # 구분자 필수 — "소재지는"의 조사를 값으로 삼키지 않게
        r"(?P<v>[가-힣][^\n;]{4,70})")),
    # 주소③: 상세주소 괄호 줄 — "(송파동, 미성아파트)" 류. 단독 줄로 남아 유출됐던 실측
    ("ADDR_DETAIL", re.compile(
        r"(?P<v>\([가-힣0-9\s,·.-]{2,30}(?:동|가|리|로|길)[가-힣0-9\s,·.-]{0,30}\))")),
    # 주소②: 무라벨 — 시·도 지명 앵커 + 번지수까지 (같은 줄 안에서만: \s는 개행을
    # 넘어 '부산광역시교통,물류\n8' 같은 비주소 괴물 값을 만든다 — 실측)
    ("ADDRESS",  re.compile(
        rf"(?P<v>(?:{REGIONS}){SP}?[가-힣\d \t\-·,()]{{4,60}}?\d+(?:-\d+)?(?:호|번지)?)")),
]


try:
    import ko_pii  # 형태소 기반 2차 탐지기 (venv 전용) — 없으면 정규식만으로 동작
except ImportError:
    ko_pii = None

# ko-pii PERSON은 성씨 근거 있을 때만 채택 (필드명 오탐 차단), 그 외 라벨은 conf 문턱만
_KO_PII_MIN_CONF = 0.6


def _person_ok(r, value: str) -> bool:
    """PERSON 채택 기준 — 실문서(스캔 공문서 64쪽) 인스턴스 전수 실측으로 도출.

    conf는 못 믿는다: '운영자금'이 name_dict_boost 가산으로 conf 1.00을 받는다.
    문맥(직함·PII인접)도 못 믿는다: PII 밀집 문서에선 일반어도 인접 보증을 받고,
    같은 문자열의 여러 출현 중 하나만 통과해도 값 전역 제거라 오염이 번진다.

    실측에서 갈린 조합: 정탐(홍길동·김철수·박영희 등)은 전원
    이름끝음절(pos:name_final_syllable)+3자 이상+conf≥0.75.
    오탐은 전원 끝음절 없음('운영자금'·'공모지침') 또는 2자('기재'·'송달'·'안다').
    2자 실명('이도')은 자체 정규식의 역할라벨 문맥이 담당하므로 여기서 버려도 유출 아님.
    """
    ev = r.evidence
    if len(value) >= 4 and not value.startswith(_COMPOUND_SURNAMES):
        return False  # 4자 실명은 복성(남궁·황보…)뿐 — '정화비용' 복합명사 오탐 차단
    return (
        any(e.startswith("pos:surname") for e in ev)             # 성씨
        and any(e.startswith("pos:name_final_syllable") for e in ev)  # 이름 끝음절
        and len(value) >= 3
        and r.confidence >= 0.75
    )


# ko-pii는 정규식이 못 하는 문맥 판단(이름·주소)만 담당. 숫자류(전화·계좌·주민 등)는
# 자체 정규식 전담 — 실측에서 ko-pii 숫자 라벨은 중복이면서 저품질이었다
# (날짜를 PHONE/DT_BIRTH로 오인, 개행 넘은 괴물 값 '1732\n2023'가 조각나
#  문서 전역의 '2023'을 칠하는 과잉제거 1,400박스).
_KO_PII_TAKE = {"PERSON", "ADDRESS"}


def _ko_pii_targets(text: str) -> list[tuple[str, str]]:
    if ko_pii is None:
        return []
    out = []
    person_hits: dict[str, dict] = {}   # 값 → {"ok": 기본필터 통과례 有, "ctx": 강한 문맥례 有}
    for r in ko_pii.detect_all(text):
        if r.label not in _KO_PII_TAKE:
            continue
        value = text[r.start:r.end].strip()
        if len(value) < 2 or r.confidence < _KO_PII_MIN_CONF or "\n" in value:
            continue  # 개행 포함 = normalize 모드가 여러 줄을 이어붙인 인공물
        if re.search(r"[\x00-\x1f\x7f]", value):
            continue  # 컨트롤 문자 = 추출 인공물('방지\x01' — 토론회 자료 실측)
        if r.label == "PERSON":
            slot = person_hits.setdefault(value, {"ok": False, "ctx": False})
            slot["ok"] = slot["ok"] or _person_ok(r, value)
            slot["ctx"] = slot["ctx"] or any(
                e.startswith(("pos:title", "pos:deterministic_pii_nearby", "pos:field_label"))
                for e in r.evidence)
            continue
        out.append((r.label, value))
    # 고빈도 PERSON은 강한 문맥례 필수 — 산문 문서에선 '오염수'·'노동자' 같은
    # 일반명사가 성씨+끝음절 필터를 통과한다. 오탐 명사는 수십 번 반복되고
    # 실명은 최소 한 번은 직함·라벨·확정PII 곁에서 등장한다는 실측 차이를 쓴다
    for value, slot in person_hits.items():
        if not slot["ok"]:
            continue
        if text.count(value) >= 6 and not slot["ctx"]:
            continue
        out.append(("PERSON", value))
    return out


# 라벨 문맥에 걸리지만 이름이 아닌 것들 (실측: "변호인 법무법인 일선", "피고인 주식회사…")
_NAME_STOPWORDS = {"법무법인", "주식회사", "유한회사", "합자회사", "합명회사",
                   "관련사항", "선임서", "아래",
                   "변호사", "변호인", "검사", "피고인", "대표이사",  # 직함어 자체는 이름이 아니다
                   "담당자", "성명", "예금주", "신청인", "대표자", "증인", "참고인",  # 라벨 단어가
                   # 값으로 잡히는 사고 방지("3. 담당자" 제목줄 뒤 다음 줄 라벨 캡처 — 웹판 실측)
                   "신문", "조서", "진술", "작성", "참여",  # "검사 신문"류 절차어 (실측)
                   "들의", "들이", "사장", "측에서"}  # "피고 들의 주장"류 정렬 파편 (실측)

# 한국 4자 실명은 복성뿐 — '정화비용'류 복합명사 오탐을 길이로 거른다
_COMPOUND_SURNAMES = ("남궁", "황보", "선우", "제갈", "사공", "서문", "독고", "동방", "어금", "망절")

# 이름 뒤에 같은 줄로 이어져도 되는 한글 단어 = 직함·호칭뿐.
# 양쪽정렬 문서는 회사명 안에도 공백을 끼우므로("가나자 산신탁") 이름+공백+한글을
# 무조건 이름으로 보면 회사명 앞토막이 이름이 된다 — 직함 화이트리스트로만 허용
_TITLES_AFTER_NAME = re.compile(
    r"(?:주무관|팀장|과장|부장|차장|실장|국장|본부장|대리|사원|주임|계장|"
    r"검사|판사|변호사|위원|대표|이사|감사|씨|님|군|양|외)(?![가-힣])")

# 회사·기관명 꼬리 — 라벨 뒤 첫 단어가 이걸로 끝나면 당사자는 법인이다
_CORP_SUFFIX = re.compile(
    r"[가-힣]*(?:공사|공단|신탁|산업|건설|은행|증권|보험|금융|캐피탈|자산|"
    r"개발|물산|전자|화학|중공업|시스템|테크|주식회사|조합|재단|법인)(?![가-힣])")


def _name_context_ok(text: str, end: int) -> bool:
    """이름 캡처 뒤 문맥 판정 — 양쪽정렬 문서('가나자 산신탁')과
    줄바꿈 회사명('인천도시\\n공사') 토막을 의미로 거른다."""
    m = re.match(r"([ \t]*\n?\s*)([가-힣]+)?", text[end:end + 14])
    ws, word = m.group(1), m.group(2)
    if not word:
        return True                        # 비한글 경계(괄호·숫자·문장부호) = 정상
    if _TITLES_AFTER_NAME.match(word):
        return True                        # 이름 + 직함
    if _CORP_SUFFIX.match(word):
        return False                       # 회사명 앞토막
    # 그 외 한글 단어: 줄바꿈 뒤면 다음 항목 라벨일 가능성(허용),
    # 같은 줄이면 정렬 공백으로 쪼개진 단어일 가능성(거부)
    return "\n" in ws
_JOSA_FINAL = set("은는이가을를과와의도")


def _name_capture_ok(v: str) -> bool:
    """정규식 NAME 캡처 정제. 3자 이상이 조사로 끝나면 이름+조사 결합
    ('홍길동은')으로 보고 버린다 — 흐름문장 속 이름은 형태소를 아는 ko-pii가
    '홍길동'으로 정확히 끊어 주므로 유출이 아니다. 2자엔 적용하지 않는다
    ('이도'처럼 조사 음절로 끝나는 실명을 죽인 회귀 실측)."""
    if len(v) >= 4 and not v.startswith(_COMPOUND_SURNAMES):
        return False  # 4자 실명은 복성뿐 (PERSON 필터와 동일 근거)
    return v not in _NAME_STOPWORDS and not (len(v) >= 3 and v[-1] in _JOSA_FINAL)


def _rrn_plausible(value: str) -> bool:
    """RRN 오탐 필터: 앞 6자리가 날짜로 성립해야 한다 (실측 오탐 '308098...'=월 80)."""
    digits = re.sub(r"\D", "", value)
    month, day = int(digits[2:4]), int(digits[4:6])
    return 1 <= month <= 12 and 1 <= day <= 31


def _standalone_occurrence(text: str, value: str) -> bool:
    """이름 값이 문서 어딘가에서 '독립 단어'로 나타나는가.

    앞이 한글이면 접미 토막, 뒤로 한글이 2자 이상 이어지면 긴 단어의 앞 토막
    ('가나자'⊂가나자산신탁)이다. 조사 1자('홍길동이')는 허용 — 이름 뒤 조사는
    정상 출현이다. 모든 출현이 토막이면 그 값은 이름이 아니라 파편이다.
    """
    for m in re.finditer(re.escape(value), text):
        before = text[m.start() - 1:m.start()]
        if before and "가" <= before <= "힣":
            continue
        if re.match(r"[가-힣]{2}", text[m.end():m.end() + 2]):
            continue
        return True
    return False


def detect(text: str) -> list[tuple[str, str]]:
    """(라벨, 값) 목록 — 정규식 ∪ ko-pii. 값 기준 중복 제거."""
    found: dict[str, str] = {}
    for label, pat in RULES:
        for m in pat.finditer(text):
            value = (m.groupdict().get("v") or m.group(0)).strip()
            if label == "RRN" and not _rrn_plausible(value):
                continue
            if label == "NAME" and not _name_context_ok(text, m.end("v")):
                continue
            # 쉼표 나열 이름은 개별 값으로 분해 ("홍길동, 김철수" → 각각)
            values = [p.strip() for p in value.split(",")] if label == "NAME" else [value]
            for v in values:
                if label == "NAME" and not _name_capture_ok(v):
                    continue
                if len(v) >= 2:
                    found.setdefault(v, label)
    for label, value in _ko_pii_targets(text):
        found.setdefault(value, label)
    items = [(label, value) for value, label in found.items()
             if not (label in ("NAME", "PERSON")
                     and not _standalone_occurrence(text, value))]
    # 숫자 포함 타깃이 다른 타깃의 부분문자열이면 제외 — 주민번호 뒷자리 "1234567"이
    # PHONE으로 중복 탐지되면 전수검색이 무관한 7자리를 과잉 제거한다.
    # 한글 이름엔 적용 금지: '홍길동'⊂'홍길동은'이지만 둘 다 별개 출현을 가진
    # 정당한 타깃이라, 지우면 맨이름 출현이 무방비가 된다(실측 유출 사고).
    return [(l, v) for l, v in items
            if not (any(ch.isdigit() for ch in v)
                    and any(v != w and v in w for _, w in items))]


def fragments(value: str) -> list[str]:
    """검색 단위: 값이 여러 줄이면 줄 조각별로 (search_for는 개행을 못 넘는다).
    각 조각은 원문+공백제거 변형을 함께 시도."""
    frags = []
    for line in value.splitlines():
        line = line.strip()
        if len(line) < 2:
            continue
        if line.isdigit() and len(line) < 4:
            continue  # '49'·'2023' 같은 짧은 숫자 조각의 전역 검색 = 과잉제거 폭탄
        frags.append(line)
        compact = re.sub(r"\s+", "", line)
        if compact != line and len(compact) >= 2:
            frags.append(compact)
    return frags or [value]


def _word_index(page) -> tuple[list, str, list[int]]:
    """쪽의 단어박스 인덱스: (단어목록, 압축문자열, 압축문자→단어번호 매핑).

    OCR 텍스트는 같은 값도 쪽마다 자간이 달라 리터럴 search_for가 놓친다.
    압축(공백 제거) 문자열에서 값을 찾고 해당 단어들의 박스를 돌려주는 폴백용.
    같은 쪽 안에서 get_text("words") 하나만 쓰므로 A(Nothing)의
    추출오프셋↔좌표 교차 매핑 버그 계열이 생길 수 없다.
    """
    words = page.get_text("words")  # (x0,y0,x1,y1, 단어, block, line, word_no)
    compact_parts: list[str] = []
    char_to_word: list[int] = []
    for i, w in enumerate(words):
        t = re.sub(r"\s+", "", w[4])
        compact_parts.append(t)
        char_to_word.extend([i] * len(t))
    return words, "".join(compact_parts), char_to_word


def _word_fallback_rects(index, value: str) -> list:
    """압축문자열 매칭으로 값의 모든 출현 단어박스를 수집."""
    words, compact, char_to_word = index
    needle = re.sub(r"\s+", "", value)
    # 짧은 값의 압축 매칭은 우연 일치 위험 — 단 한글 3자(이름)는 허용해야
    # "홍길 동"·"홍길⏎동"처럼 흩어진 실명 출현을 잡는다(실측 잔존 2건)
    min_len = 3 if any("가" <= ch <= "힣" for ch in needle) else 4
    if len(needle) < min_len:
        return []
    rects = []
    pos = compact.find(needle)
    while pos != -1:
        for wi in sorted(set(char_to_word[pos:pos + len(needle)])):
            rects.append(fitz.Rect(words[wi][:4]))
        pos = compact.find(needle, pos + 1)
    return rects


def _expand_to_words(rect, words) -> "fitz.Rect":
    """히트 박스를 '실질적으로 겹치는' OCR 단어박스까지 합쳐 확장.

    겹침 비율 50% 문턱: 스치기만 한 이웃 단어까지 삼키지 않기 위해
    (실측: 30%에서 이름 박스가 옆 라벨 '변호사'의 끝 글자를 삼켰다).
    "1454718),"처럼 값 꼬리가 단어 중간에서 끝나도 단어 전체가 칠해진다.
    """
    r = fitz.Rect(rect)
    for w in words:
        wr = fitz.Rect(w[:4])
        inter = wr & r
        if not inter.is_empty and wr.get_area() > 0 and inter.get_area() / wr.get_area() > 0.5:
            r |= wr
    return r


# ── 마스킹 정책: 익명화(부분 보존) / 가명화(일관 치환) ──
_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _mask_name(v: str) -> str:
    """홍길동 → 홍OO (성 보존, 이름만 가림)."""
    return v[0] + "O" * (len(v) - 1)


def _mask_address(v: str) -> str:
    """첫 토큰(시·도)만 보존, 나머지는 글자수만큼 O."""
    tokens = v.split()
    if len(tokens) <= 1:
        return v[0] + "O" * (len(v) - 1)
    masked = [tokens[0]] + ["O" * len(t) for t in tokens[1:]]
    return " ".join(masked)


def _pseudonym(ledger: dict, name: str) -> str:
    """가명 부여: A, B, … Z, AA, AB … — 대장에 이미 있으면 그 가명 재사용."""
    if name not in ledger:
        n = len(ledger)
        code = ""
        while True:
            code = _ALPHA[n % 26] + code
            n = n // 26 - 1
            if n < 0:
                break
        ledger[name] = code
    return ledger[name]


def _replacement_for(label: str, value: str, mode: str, ledger: dict) -> str | None:
    """None이면 검은 박스(완전 삭제 표시), 문자열이면 흰 바탕에 대체 텍스트."""
    if label in ("NAME", "PERSON"):
        return _pseudonym(ledger, value) if mode == "pseudo" else _mask_name(value)
    if label == "ADDRESS":
        return _mask_address(value)
    if label == "ADDR_DETAIL":
        # 괄호 상세주소는 구조만 남기고 전부 가림: "(송파동, 미성아파트)" → "(OOO)"
        return "(OOO)"
    return None  # 번호류(주민·계좌·전화·카드)는 부분 보존 없이 전부 삭제


def redact(in_path: str, out_path: str, mode: str = "anon",
           ledger_path: str | None = None) -> dict:
    """mode: 'anon' = 익명화(김OO·주소 첫 토큰 보존) / 'pseudo' = 가명화(A·B·C…).

    가명화 대장(ledger)은 JSON 파일로 유지 — 같은 대장을 쓰는 문서들끼리
    같은 사람이 항상 같은 가명을 받는다(문서 교차 시 인물 동일성 유지).
    ※ 대장엔 실명이 들어가므로 반드시 로컬에만 두고 산출물과 함께 배포 금지.
    """
    if mode not in ("anon", "pseudo"):
        raise ValueError(f"mode는 anon|pseudo: {mode}")
    ledger: dict = {}
    if mode == "pseudo" and ledger_path and os.path.exists(ledger_path):
        ledger = json.load(open(ledger_path, encoding="utf-8"))

    doc = fitz.open(in_path)
    full_text = "".join(page.get_text() for page in doc)
    ocr_applied = False
    ocr_tmp = None   # OCR 경유 시 임시 PDF 경로 — 종료 시 삭제(비실명화 전 원문 포함)
    if not full_text.strip():
        # 텍스트 레이어 없는 순수 스캔 → ocrmypdf로 레이어를 입힌 뒤 진행
        import shutil
        import subprocess
        import tempfile
        if shutil.which("ocrmypdf") is None:
            raise SystemExit("스캔 PDF(텍스트 레이어 없음) — ocrmypdf 설치 후 재시도 "
                             "(brew install ocrmypdf) 또는 OCR 선행 필요")
        doc.close()
        ocr_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
        print(f"[denamer] 스캔 PDF 감지 — ocrmypdf 실행 중 (쪽수에 따라 수 분)", file=sys.stderr)
        subprocess.run(["ocrmypdf", "-l", "kor+eng", "--skip-text", "--optimize", "0",
                        in_path, ocr_tmp],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        doc = fitz.open(ocr_tmp)
        full_text = "".join(page.get_text() for page in doc)
        ocr_applied = True
        if not full_text.strip():
            raise SystemExit("OCR 후에도 텍스트 없음 — 이미지 품질 확인 필요")

    targets = detect(full_text)
    # 성능 가드: search_for는 비싸다(실측 64쪽×477타깃=5분 초과).
    # 쪽별 텍스트를 한 번만 뽑아 두고, 조각이 그 쪽에 실재할 때만 검색한다.
    page_texts = [page.get_text() for page in doc]
    compact_page_texts = [re.sub(r"\s+", "", t) for t in page_texts]
    word_indexes: dict[int, tuple] = {}   # 쪽번호 → _word_index (필요할 때만 생성)
    unmapped: list[str] = []
    boxes = 0
    # 1단계: 모든 히트를 수집만 한다 (rect, 대체어, 값 길이) — 바로 annot을 달면
    # 같은 주소를 정규식·ko-pii가 서로 다른 경계로 각각 잡았을 때 겹친 박스마다
    # 대체어가 중복 삽입돼 "서울특별시 서울특별시 OOO…" 뒤죽박죽이 된다(실측)
    page_jobs: dict[int, list[tuple]] = {}   # pno → [(rect, replacement|None, vlen)]
    for label, value in targets:
        replacement = _replacement_for(label, value, mode, ledger)
        rects_total = 0
        for pno, (page, ptext) in enumerate(zip(doc, page_texts)):
            hit_rects = []
            for frag in fragments(value):
                if frag not in ptext:
                    continue
                hit_rects.extend(page.search_for(frag))
            # 압축문자열에 값이 있으면 워드 폴백도 함께 — 리터럴이 일부 출현만
            # 커버할 수 있다(실측: 같은 쪽에 붙은 '홍길동'과 줄바꿈 '홍길⏎동'가
            # 공존하면 리터럴이 앞엣것만 잡고 뒤엣것이 샜다). 중복 박스는 병합이 정리
            compact_value = re.sub(r"\s+", "", value)
            if compact_value in compact_page_texts[pno]:
                if pno not in word_indexes:
                    word_indexes[pno] = _word_index(page)
                hit_rects.extend(_word_fallback_rects(word_indexes[pno], value))
            if hit_rects and pno not in word_indexes:
                word_indexes[pno] = _word_index(page)
            for rect in hit_rects:
                # 스캔본은 OCR 좌표가 인쇄 글리프와 어긋나 가장자리 글자가
                # 삐져나온다 → 겹침 50%↑ 단어박스로 확장 + 1pt 마진
                rect = _expand_to_words(rect, word_indexes[pno][0]) + (-1, -1, 1, 1)
                page_jobs.setdefault(pno, []).append((rect, replacement, len(value)))
                rects_total += 1
        if rects_total == 0:
            unmapped.append(f"{label}:{value}")
        boxes += rects_total

    # 2단계-A: 같은 값의 조각 박스 봉합 — 긴 주소는 search_for가 한 줄을 여러
    # 조각으로 돌려주고, 조각마다 전체 대체문을 그리면 글자가 겹쳐 뒤죽박죽이
    # 된다(실측). 같은 대체문 + 같은 줄 + 인접(간격 12pt↓)이면 하나로 union.
    def _same_line(r1, r2) -> bool:
        overlap = min(r1.y1, r2.y1) - max(r1.y0, r2.y0)
        return overlap > 0.6 * min(r1.height, r2.height)

    stitched_jobs: dict[int, list[tuple]] = {}
    for pno, jobs in page_jobs.items():
        stitched: list[dict] = []
        for rect, repl, vlen in jobs:
            for s in stitched:
                if (s["repl"] == repl and repl is not None
                        and _same_line(s["rect"], rect)
                        and rect.x0 - s["rect"].x1 < 12 and s["rect"].x0 - rect.x1 < 12):
                    s["rect"] |= rect
                    break
            else:
                stitched.append({"rect": fitz.Rect(rect), "repl": repl, "vlen": vlen})
        stitched_jobs[pno] = [(s["rect"], s["repl"], s["vlen"]) for s in stitched]

    # 2단계-B: 서로 다른 값(정규식 vs ko-pii 경계 차이)의 겹침 박스 병합 —
    # 가장 긴 값(=가장 넓은 문맥)의 대체어가 대표. 검은 박스(None)가 섞이면
    # 완전 삭제가 우선한다(유출>과잉의 안전 방향).
    merged_jobs: dict[int, list[tuple]] = {}
    for pno, jobs in stitched_jobs.items():
        merged: list[dict] = []
        for rect, repl, vlen in sorted(jobs, key=lambda j: -j[2]):
            for m in merged:
                inter = m["rect"] & rect
                smaller = min(m["rect"].get_area(), rect.get_area())
                if not inter.is_empty and smaller > 0 and inter.get_area() / smaller > 0.5:
                    m["rect"] |= rect
                    if repl is None:
                        m["repl"] = None
                    break
            else:
                merged.append({"rect": fitz.Rect(rect), "repl": repl})
        merged_jobs[pno] = [(m["rect"], m["repl"]) for m in merged]

    for pno, jobs in merged_jobs.items():
        for rect, repl in jobs:
            doc[pno].add_redact_annot(rect, fill=(0, 0, 0) if repl is None else (1, 1, 1))
    for page in doc:
        page.apply_redactions()   # 텍스트·이미지 실제 삭제 (덮기 아님)
    # 삭제가 끝난 자리에 대체 텍스트("김OO"·"A"·주소 첫 토큰+O)를 그룹당 1회 삽입.
    # 원문은 이미 문서에서 소거됐으므로 복사·OCR·LLM으로도 복원 불가는 그대로다.
    kfont = fitz.Font("korea")
    for pno, jobs in merged_jobs.items():
        page = doc[pno]
        if any(repl for _, repl in jobs):
            page.insert_font(fontname="krmask", fontbuffer=kfont.buffer)
        for rect, repl in jobs:
            if not repl:
                continue
            fontsize = max(6.0, min(rect.height * 0.72, 12.0))
            # insert_textbox는 공간 부족 시 조용히 아무것도 안 쓴다(fit 검사) →
            # 기준선 방식 insert_text로 무조건 그린다
            baseline = fitz.Point(rect.x0 + 1, rect.y1 - rect.height * 0.28)
            page.insert_text(baseline, repl, fontname="krmask", fontsize=fontsize)
    # 메타데이터·XMP 소거 — 본문을 다 지워도 Author/Creator(워드 계정명·소속)가
    # 문서 정보와 XMP에 그대로 남으면 그게 유출 채널이다
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    if ocr_tmp and os.path.exists(ocr_tmp):
        os.unlink(ocr_tmp)
    if mode == "pseudo" and ledger_path:
        with open(ledger_path, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=2)

    # ── 사후검증: 저장본 재오픈 → 조각 재검색 ──
    saved = fitz.open(out_path)
    saved_text = "".join(page.get_text() for page in saved)
    # 메타데이터도 검증 대상 — 값이 하나라도 남아 있으면 잔존으로 취급
    leftover_meta = {k: v for k, v in (saved.metadata or {}).items()
                     if v and k not in ("format", "encryption")}
    saved.close()
    compact_saved = re.sub(r"\s+", "", saved_text)
    residual = []
    for label, value in targets:
        compact_value = re.sub(r"\s+", "", value)
        if label in ("NAME", "PERSON"):
            # 이름은 독립 출현만 잔존으로 판정 — '인천광역시청' 속 '인천'을
            # 잔존으로 오인하면 영구 거짓 경보가 된다(실측).
            # 흩어진 형태("홍길 동")는 제거 단계의 워드 폴백이 담당한다.
            hit = value in saved_text and _standalone_occurrence(saved_text, value)
        else:
            hit = any(f in saved_text for f in fragments(value)) or \
                  compact_value in compact_saved
        if hit:
            residual.append(f"{label}:{value}")
    for k, v in leftover_meta.items():
        residual.append(f"META:{k}={v}")

    return {
        "mode": mode,
        "ocr_applied": ocr_applied,
        "ledger": ledger_path if mode == "pseudo" else None,
        "targets": len(targets),
        "boxes_applied": boxes,
        "unmapped": unmapped,     # 비어야 정상
        "residual": residual,     # 비어야 정상
        "by_label": sorted({label for label, _ in targets}),
        # 이름은 오탐 시 일반어가 통째로 칠해지므로 목록을 노출해 사람이 확인케 한다
        "persons": sorted(v for l, v in targets if l in ("PERSON", "NAME")),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="PDF 비실명화 — 익명화(anon) / 가명화(pseudo)")
    ap.add_argument("input")
    ap.add_argument("output", nargs="?", default=None,
                    help="생략 시 원본 옆에 접미사 부기: 파일명_masked.pdf(익명화) / 파일명_aliased.pdf(가명화)")
    ap.add_argument("--mode", choices=["anon", "pseudo"], default="anon",
                    help="anon=김OO·주소 부분보존(기본) / pseudo=A·B·C 일관 가명")
    ap.add_argument("--ledger", default=None,
                    help="가명화 대장 JSON 경로 (기본: 출력 폴더의 pseudonym_ledger.json). "
                         "같은 대장을 쓰는 문서끼리 가명이 일치한다. 실명 포함 — 로컬 보관 필수")
    args = ap.parse_args()
    output = args.output
    if output is None:                       # 원본은 건드리지 않고 접미사본 생성
        stem, ext = os.path.splitext(args.input)
        suffix = "masked" if args.mode == "anon" else "aliased"
        output = f"{stem}_{suffix}{ext}"
    ledger_path = args.ledger
    if args.mode == "pseudo" and not ledger_path:
        ledger_path = os.path.join(os.path.dirname(os.path.abspath(output)),
                                   "pseudonym_ledger.json")
    report = redact(args.input, output, mode=args.mode, ledger_path=ledger_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if report["unmapped"] or report["residual"]:
        print("FAIL: 매핑 실패 또는 잔존 PII — 출력물을 신뢰하지 말 것", file=sys.stderr)
        raise SystemExit(1)
