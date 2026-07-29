"""이름 탐지 엔진 — 라벨·직함·관계어·서술어 문맥으로 실명을 확정한다.

denamer의 제거 단계는 '값 전수 검색'이라, 한 번 확정된 이름은 문서 전체의 출현이
자동으로 처리된다. 그래서 이 모듈은 오프셋이 아니라 **값 집합**만 돌려준다
(선행 도구의 '문서 전파' 단계가 denamer에서는 구조적으로 불필요한 이유).

설계 근거 — 선행 도구가 판례·재결례 100건(68만자) 실측으로 도달한 결론을 이식했다.
  · 성씨 게이트: 모든 후보는 실제 성씨로 시작해야 한다. 없으면 '피고 패소'→패소,
    '청구인 주장'→주장처럼 라벨 뒤 아무 단어나 이름이 된다.
  · 자유문장 규칙은 여격 조사(에게·한테·께서·께)로만 좁힌다. 모든 조사를 허용하면
    '한다는'→한다, '정답을'→정답 식으로 오탐이 폭증한다(V2 실측 6,787건).
  · 직함은 공백 필수, 호칭은 붙여쓰기. 하나로 묶으면 '담당변호사'→담당,
    '사내이사'→사내처럼 붙여 쓴 일반명사가 통째로 걸린다.
"""
import re

from stopwords import (
    NAME_JOSA_CHARS,
    NAME_STOPWORDS,
    RELATION_CONTEXT_STOPWORDS,
    SURNAME_STOPWORDS,
    is_word_plus_josa,
)

# ── 성씨 ────────────────────────────────────────────────────────
# 한국 4자 실명은 복성뿐 — '정화비용'류 복합명사 오탐을 길이로 거른다
COMPOUND_SURNAMES = ("남궁", "황보", "선우", "제갈", "사공", "서문", "독고",
                     "동방", "어금", "망절", "무본")
_SINGLE_SURNAME = ("[김이박최정강조윤장임한오서신권황안송전홍고문양손배백허유남심노"
                   "하곽성차주우구민류나지엄변방채원천공현함여염추도소석선설마길연위"
                   "표명기반라왕금옥육인맹제모진탁국어은편용예봉경리]")
_COMPOUND_ALT = "|".join(COMPOUND_SURNAMES)
SURNAME_HEAD = re.compile(rf"^(?:{_COMPOUND_ALT}|{_SINGLE_SURNAME})")

# ── 이름 경계 ───────────────────────────────────────────────────
# 최대 4음절을 그대로 집은 뒤, 필터에 걸리면 **조사를 떼고 다시 시도**한다.
#   "피고 김철수는"    → '김철수는'(4) 실패 → '는' 제거 → 김철수 ✓
#   "원고 이영희에게"  → '이영희에'(4) 실패 → '에' 제거 → 이영희 ✓
#   "소송대리인 김대한" → '김대한'(3) 그대로 통과 ✓
#   "원고 이지은"      → '이지은'(3) 그대로 통과 ✓  (조사부터 떼면 '이지'가 된다)
#
# 조사 문자 집합으로 경계를 잡는 방법은 쓸 수 없다. 한국어 조사의 첫 음절
# (한테의 '한', 보다의 '보', 처럼의 '처')이 이름 끝음절과 겹쳐 '김대한'이
# '김대'로 잘리고, '이영희에게'처럼 5음절 연속은 4음절 상한 안에서 경계를
# 만나지 못해 아예 미탐이 된다(둘 다 실측).
#
# 원형을 먼저 시도하는 순서가 중요하다. 조사부터 떼면 '이지은'·'김민도'처럼
# 조사 음절로 끝나는 실명이 한 글자씩 잘려 나간다.
#
# 자간 대안('홍 길 동')은 음절 사이 공백을 **정확히 한 칸**으로 고정한다.
# `[ \t]*`처럼 열어 두면 "성명 홍길동  주민등록번호"에서 공백을 건너뛰어
# '홍길동주'까지 삼킨다(V2 실측 결함).
NAME_RX = r"(?:[가-힣]{2,4}|[가-힣](?:[ \t][가-힣]){1,3})"

# 이름 뒤에서 떼어낼 조사·서술어 (긴 것부터 — 한 번만 뗀다)
_JOSA_SUFFIXES = (
    "에게서", "으로서", "으로써", "에게", "한테", "께서", "에서", "으로",
    "이다", "이라", "이며", "이고", "이란", "라는", "라고", "로서", "로써",
    "부터", "까지", "밖에", "조차", "마저", "처럼", "보다",
    "은", "는", "이", "가", "을", "를", "과", "와", "의", "도", "만",
    "로", "에", "께", "나", "랑", "야", "씨", "님",
)

# ── 라벨 ────────────────────────────────────────────────────────
LEGAL_PREFIX_LABELS = [
    "담당변호사", "소송복대리인", "소송대리인", "변호사", "변호인",
    "재판장", "수석부장판사", "부장판사", "대법관", "헌법재판관", "판사", "검사",
]
# 당사자 라벨. 이전 구현은 16종뿐이라 '채무자 박민수'·'임차인 정수현'이 미탐이었다.
GENERAL_PREFIX_LABELS = [
    "원고", "피고", "소외", "증인", "참가인", "보조참가인", "신청인", "피신청인",
    "채권자", "채무자", "상소인", "항소인", "상고인",
    "대표이사", "이사", "대표", "담당자",
    "작성자", "성명", "참조", "수신", "발신", "담당", "제출자",
    "법정대리인", "감정인", "참고인", "의뢰인",
    "피고인", "원고인", "청구인", "피청구인",
    "고소인", "피고소인", "고발인", "피고발인", "진정인", "피진정인",
    "피항소인", "피상고인", "항고인", "피항고인", "재항고인",
    "연대보증인", "보증인", "물상보증인",
    "매도인", "매수인", "임대인", "임차인", "전차인",
    "수취인", "발행인", "배서인", "예금주", "위임인", "수임인",
    "위탁자", "수탁자", "신탁자", "수익자", "양도인", "양수인",
    "유증자", "수유자", "유언자",
    "상대방", "통역인", "목격자",
    "피의자", "피해자", "가해자", "피내사자", "용의자",
    "국선변호인", "임의대리인", "대표자",
    "신고인", "피신고인", "제보자", "본인",
]
# 친족 관계어 — 가사·상속 문서에서 이름 앞에 온다.
#   '원고' 뒤엔 거의 항상 이름이 오지만 '배우자' 뒤엔 '명의로'·'양육비'가 흔하고
#   그 첫 글자(명·양)가 전부 성씨 음절이라, 이 그룹만 조건을 두 겹 더 건다.
RELATION_PREFIX_LABELS = [
    "배우자", "남편", "아내", "부친", "모친", "아버지", "어머니", "부모",
    "장남", "장녀", "차남", "차녀", "삼남", "삼녀", "아들", "딸", "자녀",
    "형제", "자매", "동생", "누나", "오빠", "언니", "조카", "사촌",
    "손자", "손녀", "조부", "조모", "외조부", "외조모", "친척",
    "피상속인", "상속인", "공동상속인", "망인", "고인",
    "친권자", "후견인", "피후견인", "부양의무자", "세대주", "동거인",
]
# 전문자격사 — 이름 뒤에 공백을 두고 온다
LEGAL_SUFFIX_LABELS = [
    "변호사", "판사", "검사", "대법관", "헌법재판관",
    "법무사", "노무사", "회계사", "세무사", "변리사", "감정평가사",
]
# 직위 — 반드시 공백을 두고 이름 뒤에 온다 ('박민수 부장')
GENERAL_SUFFIX_LABELS = [
    "박사", "교수", "의사", "주무관", "계장",
    "대표이사", "대표", "부사장", "사장", "부회장", "회장", "이사장", "이사",
    "감사", "상무", "전무", "본부장", "실장", "센터장", "팀장", "부장",
    "차장", "과장", "대리", "주임", "사원", "원장", "소장", "국장",
    "청장", "처장", "위원장",
]
# 호칭 — 이름에 붙여 쓴다 ('김철수씨는')
HONORIFIC_SUFFIX_LABELS = ["씨", "님", "선생님"]

_PREFIX_LABELS = LEGAL_PREFIX_LABELS + GENERAL_PREFIX_LABELS
_BLOCKED_PREFIX_NAMES = {re.sub(r"\s+", "", label) for label in _PREFIX_LABELS}

# 라벨 단어 자체는 이름이 아니다. 목록에서 자동으로 만들어야 라벨을 추가할 때마다
# 사람 손으로 배제 목록을 따라 고치는 일이 없다.
#   실측: 여격 조사 규칙이 "원고에게"의 '원고'를 이름으로 확정했다
#   ('원'은 성씨 음절, '고'는 이름 음절이라 성씨 게이트를 통과한다).
#   그 결과 판결문의 '원        고' 라벨이 '원O'로 훼손됐다.
_LABEL_WORDS = {re.sub(r"\s+", "", label) for label in
                (LEGAL_PREFIX_LABELS + GENERAL_PREFIX_LABELS + RELATION_PREFIX_LABELS
                 + LEGAL_SUFFIX_LABELS + GENERAL_SUFFIX_LABELS + HONORIFIC_SUFFIX_LABELS)}

# 회사·기관명 꼬리 — 이름 뒤 첫 단어가 이걸로 끝나면 당사자는 법인이다.
#   양쪽정렬 문서는 회사명 안에도 공백을 끼우므로("가나자 산신탁"), 이름+공백+한글을
#   무조건 이름으로 보면 회사명 앞토막이 이름이 된다.
#   앞의 `[가-힣]*`가 핵심이다 — '산신탁'처럼 꼬리 앞에 글자가 붙은 형태를 잡는다.
CORP_TAIL = re.compile(
    r"[가-힣]*(?:공사|공단|신탁|산업|건설|은행|증권|보험|금융|캐피탈|자산|개발|물산|"
    r"전자|화학|중공업|시스템|테크|주식회사|조합|재단|법인|위원회|협회|"
    r"연구원|연구소|대학교|대학|병원|센터|공장|본부|지사|지점)(?![가-힣])")


def _spaced(value: str) -> str:
    """라벨 자간 허용 — 공문서는 '피 고 인'처럼 라벨을 벌려 쓴다."""
    return r"\s*".join(re.escape(ch) for ch in value)


def _label_group(labels) -> str:
    """긴 라벨을 먼저 매치시킨다 — '피고인'이 '피고'보다 앞서야 뒤 이름을 안 놓친다."""
    return "|".join(_spaced(label) for label in sorted(labels, key=len, reverse=True))


_LABEL_SEP = r"(?:[ \t]*[:：][ \t]*|[ \t]+)"


class _NameCollector:
    """확정된 이름을 모으며 공통 필터를 적용한다."""

    def __init__(self, text: str):
        self.text = text
        self.names: set[str] = set()

    def _following_ok(self, end: int) -> bool:
        """이름 뒤 문맥 판정 — 회사명 앞토막('가나자 산신탁')만 거른다.

        이전 구현은 '같은 줄에 한글 단어가 이어지면 거부'라는 넓은 규칙이었는데,
        조사 인지 경계를 도입하자 이름 바로 뒤가 거의 항상 조사가 되어
        ("김철수" + "는") 정상 출현까지 통째로 거부했다 — 실측 미탐의 직접 원인.
        지금은 성씨 게이트·비이름 사전·is_word_plus_josa가 오탐을 막으므로,
        여기서는 회사명 판정만 남긴다.
        """
        m = re.match(r"[ \t]*\n?\s*([가-힣]+)?", self.text[end:end + 14])
        word = m.group(1)
        return not word or not CORP_TAIL.match(word)

    @staticmethod
    def _accepts(cand: str) -> bool:
        """이름 후보 공통 필터."""
        if not (2 <= len(cand) <= 4):
            return False
        # 한국 4자 실명은 복성뿐 — 그 외 4자는 '정화비용'류 복합명사다
        if len(cand) == 4 and not cand.startswith(COMPOUND_SURNAMES):
            return False
        if cand in NAME_STOPWORDS or cand in SURNAME_STOPWORDS or cand in _LABEL_WORDS:
            return False
        if is_word_plus_josa(cand):                # '진술이'·'김포시'
            return False
        # 후보 자체가 회사·기관명이면 이름이 아니다. 복성이 상호 앞머리와 겹치는
        # 경우가 있다 — '동방화학'은 복성 '동방'으로 시작해 4자 규칙을 통과한다.
        if CORP_TAIL.match(cand):
            return False
        return bool(SURNAME_HEAD.match(cand))      # 성씨 게이트

    def candidates(self, raw: str) -> list[str]:
        """원형 우선, 실패 시 조사를 뗀 형태. 뗀 결과가 2자 미만이면 시도하지 않는다."""
        out = [raw]
        for josa in _JOSA_SUFFIXES:
            if raw.endswith(josa) and len(raw) - len(josa) >= 2:
                out.append(raw[:-len(josa)])
                break                              # 한 번만 뗀다
        return out

    def add(self, start: int, end: int, extra_ok=None) -> bool:
        """필터를 통과하면 이름으로 확정. 쉼표 연쇄 중단 판단에 결과를 쓴다.

        extra_ok: 후보별로 추가 조건을 거는 콜백(친족 관계어 규칙이 쓴다).
        """
        raw = re.sub(r"\s+", "", self.text[start:end])
        if not self._following_ok(end):            # 회사명 앞토막 차단
            return False
        for cand in self.candidates(raw):
            if not self._accepts(cand):
                continue
            if extra_ok and not extra_ok(cand):
                continue
            self.names.add(cand)
            return True
        return False

    def add_comma_chain(self, pos: int, extra_ok=None) -> None:
        """'변호사 홍길동, 김철수'처럼 쉼표로 이어지는 이름들."""
        while True:
            m = re.match(rf"[ \t]*,[ \t]*(?P<name>{NAME_RX})", self.text[pos:])
            if not m:
                return
            start, end = pos + m.start("name"), pos + m.end("name")
            raw = re.sub(r"\s+", "", self.text[start:end])
            if raw in NAME_STOPWORDS or any(
                    raw.startswith(label) for label in _BLOCKED_PREFIX_NAMES):
                return
            if not self.add(start, end, extra_ok):
                return
            pos += m.end()


def _relation_candidate_ok(cand: str) -> bool:
    """친족 관계어 뒤 후보의 추가 조건 — 조사를 떼고 문맥 불용어와 대조."""
    if not SURNAME_HEAD.match(cand):
        return False
    tails = ("에게서", "으로서", "으로써", "에게", "한테", "께서", "으로", "라는",
             "이라", "로서", "로써", "부터", "까지", "만을", "만이",
             "은", "는", "이", "가", "을", "를", "과", "와", "의", "도",
             "로", "에", "께", "만", "및")
    stems = {cand}
    for josa in tails:
        if cand.endswith(josa) and len(cand) - len(josa) >= 2:
            stems.add(cand[:-len(josa)])
    return not (stems & RELATION_CONTEXT_STOPWORDS) and cand not in SURNAME_STOPWORDS


# 라벨이 연달아 오는 서식 흡수 — '소송대리인 변호사 김대한', '재판장 부장판사 김○○'.
# 중간 라벨을 흡수하지 않으면 두 번째 라벨이 이름 자리에 잡혀 거부되고,
# 정작 뒤의 진짜 이름은 아무도 보지 않는다.
_INTERSTITIAL = (rf"(?:(?:{_label_group(_PREFIX_LABELS + LEGAL_SUFFIX_LABELS)})"
                 rf"{_LABEL_SEP})?")

_PAT_LEGAL_PREFIX = re.compile(
    rf"(?:^|(?<![가-힣])){_INTERSTITIAL}"
    rf"(?:{_label_group(LEGAL_PREFIX_LABELS)}){_LABEL_SEP}"
    rf"{_INTERSTITIAL}(?P<name>{NAME_RX})")
_PAT_GENERAL_PREFIX = re.compile(
    rf"(?:^|(?<![가-힣]))(?:{_label_group(GENERAL_PREFIX_LABELS)}){_LABEL_SEP}"
    rf"{_INTERSTITIAL}(?P<name>{NAME_RX})")
_PAT_RELATION_PREFIX = re.compile(
    rf"(?:^|(?<![가-힣]))(?:{_label_group(RELATION_PREFIX_LABELS)}){_LABEL_SEP}"
    rf"(?P<name>{NAME_RX})")
_PAT_LEGAL_SUFFIX = re.compile(
    rf"(?:^|(?<![가-힣]))(?P<name>{NAME_RX})[ \t]+"
    rf"(?:{_label_group(LEGAL_SUFFIX_LABELS)})(?=[{NAME_JOSA_CHARS}]|[^가-힣]|$)")
_PAT_GENERAL_SUFFIX = re.compile(
    rf"(?:^|(?<![가-힣]))(?P<name>{NAME_RX})[ \t]+"
    rf"(?:{_label_group(GENERAL_SUFFIX_LABELS)})(?=[{NAME_JOSA_CHARS}]|[^가-힣]|$)")
_PAT_HONORIFIC = re.compile(
    rf"(?:^|(?<![가-힣]))(?P<name>{NAME_RX})"
    rf"(?:{_label_group(HONORIFIC_SUFFIX_LABELS)})(?=[{NAME_JOSA_CHARS}]|[^가-힣]|$)")
# 괄호 식별자 — "홍길동(880101-1234567)". 신원을 괄호로 특정하는 서식이라
# 이름일 확신이 가장 높은 문맥인데 이전 구현은 아무 규칙도 보지 않았다.
_PAT_PAREN_ID = re.compile(
    rf"(?:^|(?<![가-힣]))(?P<name>{NAME_RX})[ \t]*\([ \t]*"
    r"(?:주민(?:등록)?번호|생년월일|외국인등록번호|[0-9]{6}[ \t]*[-‐-―−－][ \t]*[1-8])")
# 라벨 + 조사 + 이름 + 서술어 — "피고소인은 강서연이다", "채무자는 박민수이며"
_PAT_COPULA = re.compile(
    rf"(?:^|(?<![가-힣]))(?:{_label_group(_PREFIX_LABELS + RELATION_PREFIX_LABELS)})"
    rf"(?:은|는|이|가|:|：)?[ \t]*(?P<name>[가-힣]{{2,4}}?)"
    r"(?=이다|이었|였|이라|이며|이고|입니|이란|라고|으로서|로서)")
# 이미 부분 마스킹된 표기 — "김○○", "박OO". 앞선 처리에서 일부만 가려진 문서를
# 다시 넣는 경우가 실무에서 흔하다.
_PAT_MASKED_STYLE = re.compile(
    rf"(?<![가-힣])(?P<name>[가-힣][○◯〇OoＯｏ]{{1,2}})"
    rf"(?=[{NAME_JOSA_CHARS}]|[^가-힣○◯〇OoＯｏ]|$)")
# 여격 조사 — 에게·한테·께서·께는 사실상 사람에게만 붙는다.
#   "김철수에게 지급하라"는 잡고 "한다는·정답을"은 잡지 않는다.
_PAT_DATIVE = re.compile(
    rf"(?<![가-힣])(?P<name>(?:{_COMPOUND_ALT})[가-힣]{{1,2}}|{_SINGLE_SURNAME}[가-힣]{{1,2}})"
    r"(?=(?:에게|한테|께서|께)(?![가-힣]))")


def detect_names(text: str, extra_names=(), excluded=()) -> set[str]:
    """문맥 규칙으로 확정한 이름 값 집합.

    extra_names: 사용자 사전(강제 추가) — 규칙이 놓치는 한글 음차 외국인명 등.
    excluded:    사용자 사전(제외) — 반복되는 오탐을 영구히 끈다.
    """
    c = _NameCollector(text)

    for pat in (_PAT_LEGAL_PREFIX, _PAT_GENERAL_PREFIX):
        for m in pat.finditer(text):
            c.add(m.start("name"), m.end("name"))
            c.add_comma_chain(m.end("name"))

    for m in _PAT_RELATION_PREFIX.finditer(text):
        # 친족 관계어는 조건이 두 겹 더 붙는다 — 조사를 뗀 뒤 후보마다 검사한다
        if c.add(m.start("name"), m.end("name"), _relation_candidate_ok):
            c.add_comma_chain(m.end("name"), _relation_candidate_ok)

    for pat in (_PAT_LEGAL_SUFFIX, _PAT_GENERAL_SUFFIX, _PAT_HONORIFIC,
                _PAT_PAREN_ID, _PAT_COPULA, _PAT_MASKED_STYLE, _PAT_DATIVE):
        for m in pat.finditer(text):
            c.add(m.start("name"), m.end("name"))

    names = c.names | {n for n in extra_names if n}
    return {n for n in names if n not in excluded}
