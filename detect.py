"""탐지 코어 — 정규식 규칙 + 이름 엔진 + ko-pii를 합쳐 (라벨, 값) 목록을 만든다.

외부용(denamer_out)과 내부용(denamer_in)이 이 모듈 하나를 공유한다.
규칙을 두 벌 두면 한쪽만 고쳐 놓고 다른 쪽이 새는 사고가 나므로, 탐지는 여기에만 둔다.

설계 원칙:
  · 텍스트를 정규화하지 않는다. 정규화하면 오프셋 역매핑이 필요해지고,
    그 매핑이 좌표 어긋남 버그의 발생 지점이다. 대신 '값'을 뽑아 전수 검색한다.
  · 값 단위로 다루므로 한 번 확정된 값은 문서 전체의 출현이 자동으로 처리된다.
"""
import re
from collections import namedtuple

from names import CORP_TAIL, LABEL_WORDS, detect_names

# ── 표기 변형 빌딩블록 ──────────────────────────────────────────
DASH = r"[-−–—―.·]"       # ASCII '-' + 유니코드 마이너스/대시류 + 점
SP = r"[ \t]"             # 같은 줄 안의 공백만 — \s는 개행을 넘어가 여러 줄 숫자를
                          # 이어붙인 괴물 매치를 만든다(실문서 64쪽 실측)
SEP = rf"{SP}*{DASH}?{SP}*"        # 구분자 선택
SEP1 = rf"(?:{SP}*{DASH}{SP}*|{SP}+)"   # 구분자 필수


def spaced(n: int) -> str:
    """자간 벌어진 숫자열: '9 2 0 3 1 5'."""
    return r"\d" + rf"(?:{SP}*\d){{{n - 1}}}"


# 시·도 지명 (무라벨 주소의 앵커) — 정식·축약 병기
REGIONS = (
    "서울특별시|부산광역시|대구광역시|인천광역시|광주광역시|대전광역시|울산광역시|"
    "세종특별자치시|경기도|강원특별자치도|강원도|충청북도|충청남도|전북특별자치도|"
    "전라북도|전라남도|경상북도|경상남도|제주특별자치도|제주도"
)

# 국번 화이트리스트. 이것이 없으면 '계약번호 1234-5678'이 전화번호로 잡혀
# 검은 박스가 된다(실측). 실재하는 국번만 인정한다.
PHONE_PREFIX = r"(?:01[016789]|02|0[3-6][1-5]|070|050\d)"
REP_PREFIX = r"1[568]\d{2}"        # 대표번호 1588·1644·1800 …

# 계좌번호 문맥어 — 라벨이 없어도 은행명만 인접하면 계좌로 본다.
#   이전 구현은 '계좌'류 라벨을 요구해 "국민은행 123456-78-901234 입금"을 놓쳤다.
ACCOUNT_CONTEXT = (
    "계좌", "입금", "송금", "이체", "예금주", "통장", "무통장", "환급", "납부",
    "지급받을", "은행", "국민", "신한", "우리", "하나", "농협", "기업", "수협",
    "우체국", "새마을", "신협", "산업", "씨티", "케이뱅크", "카카오뱅크",
    "토스뱅크", "저축은행", "증권",
)
# 사건번호 2자리 연도형은 차량번호와 형태가 같다(81나3166 vs 81나3166).
#   사건 문맥일 때만 사건번호로 인정하고, 아니면 차량번호가 가져간다.
CASE_CONTEXT = ("사건", "판결", "선고", "소송", "항소", "상고", "결정", "재판",
                "법원", "심리", "판시", "판례", "기록", "병합")

# 법원 사건부호
_COURT_CODES = (
    "가합|가단|가소|나|다|라|마|머|재가합|재가단|재나|재다|재마|"
    "고합|고단|고정|고약|고약전|노|도|로|모|초|보|전고|전노|전도|"
    "구합|구단|구|누|두|아|드합|드단|드|르|므|느합|느단|느|허|후|"
    "카합|카단|카기|카담|카확|카허|카명|카불|카임|카정|카소|카열|카조|카구|카경|"
    "타경|타기|타채|타인|타배|회합|회단|회확|하합|하단|하확|개회|개확|개기|"
    "즈기|즈합|즈단|브|스|우|수|추|버|푸|크"
)
_COURT_CODES_SHORT = (
    "가합|가단|가소|나|다|라|마|머|고합|고단|고정|고약|노|도|로|모|초|"
    "구합|구단|구|누|두|드합|드단|르|므|허|후|카합|카단|카기|카담|타경|타기|회합|회단"
)
# 실제 자동차 번호판에 쓰이는 음절만 — [가-힣] 전체를 열면 '678은 2005'가 잡힌다
_CAR_SYLLABLES = "가나다라마거너더러머버서어저고노도로모보소오조구누두루무부수우주아바사자배하허호"

# ── 법인명 ─────────────────────────────────────────────────────
# 라벨을 상호로 오인하는 것을 막는다. '피고 주식회사 동방화학'에서
# <피고 주식회사>만 잡으면 정작 상호인 '동방화학'이 그대로 남는다(V2 실측 유출).
_ORG_ROLE_BLOCK = (
    r"담당변호사|담당노무사|소송복대리인|소송수계인|담당판사|담당검사|"
    r"원고|피고인|피고|신청인|피신청인|청구인|피청구인|고소인|피고소인|"
    r"고발인|피고발인|채권자|채무자|상고인|항소인|항고인|소외|참가인|증인|"
    r"피의자|피해자|가해자|상대방|대리인|소송대리인|법정대리인|의뢰인|"
    r"대표이사|대표자|대표|이사장|이사|감사|사장|부사장|회장|부회장|"
    r"본부장|실장|센터장|팀장|부장|차장|과장|대리|주임|소장|원장|국장|"
    r"청장|처장|위원장|담당자|담당|위|같은|해당|당해|"
    # 지시·자칭어 — '저희 법무법인은'에서 '저희'가 상호로 잡혔다(실측)
    r"저희|우리|본|당사|귀사|상기|전기|해당"
)
# 역할어 뒤에 조사가 붙어도 차단이 유지돼야 한다. 안쪽을 `(?![가-힣])`만 두면
# '대표이사는'의 '는' 때문에 안쪽이 실패하고, 이중부정이 뒤집혀 차단이 통째로
# 풀린다 — 실문서에서는 조사가 붙는 쪽이 더 흔하다(실측: ORG가 직함까지 흡수).
_NO_ROLE = rf"(?!(?:{_ORG_ROLE_BLOCK})(?:[은는이가을를와과의도로에]|(?![가-힣])))"
# 상호는 글자·숫자로 시작한다. 여는 괄호를 허용하면 '(소송대리인 법무법인'처럼
# 괄호부터 삼켜 정작 상호를 놓친다. 첫 글자만 제한하고 이후엔 괄호를 허용한다.
_ORG_HEAD = r"[가-힣A-Za-z0-9]"
# '(이하'는 상호가 아니라 약칭 도입구다 — 삼키면 "사단법인 X(이하" 가 한 값이 된다(실측)
_ORG_REST = r"(?:(?!\(이하)[가-힣A-Za-z0-9·&().-])"
# 상호 안의 공백은 같은 줄에서만 허용한다. `\s`로 열어 두면 개행을 넘어가
# 다음 줄 첫 단어까지 상호로 삼킨다 — 실측: "법무법인(유한) 가나" 다음 줄이
# "주        문"이었는데 '가나\n주'가 한 값으로 잡혀 판결문의 '주문'이 훼손됐다.
_ORG_NAME = (rf"{_NO_ROLE}{_ORG_HEAD}{_ORG_REST}{{1,29}}?"
             rf"(?:{SP}{_NO_ROLE}{_ORG_HEAD}{_ORG_REST}{{0,19}}?)?")
# 접미 **앞**에 오는 상호는 글자로 시작해야 한다. 숫자를 허용하면 목록번호를 삼킨다
# (실측: "(2) 사단법인 …"에서 '2) 사단법인'이 한 법인명으로 잡혔다).
_ORG_NAME_COMPACT = rf"{_NO_ROLE}[가-힣A-Za-z]{_ORG_REST}{{1,29}}?"
# 여는 괄호도 멈춤 자리다. 넣지 않으면 '(이하' 앞에서 멈추지 못해 매치 자체가 깨진다
_ORG_STOP = r"(?=(?:과|와|은|는|이|가|을|를)?(?:\s|[,.;:()\[\]]|$))"
_COMPANY_SUFFIX = r"주식회사|유한회사|유한책임회사|합자회사|합명회사|사단법인|재단법인|\(주\)|㈜"
_FIRM_SUFFIX = r"법무법인(?:\(유(?:한)?\))?|노무법인|법률사무소|회계법인|세무법인|법무사사무소|특허법인"


def _org_pattern(suffix: str) -> str:
    """접미 앞·뒤 어느 쪽에 상호가 오든 잡는다(주식회사 동방화학 / 한빛전자 주식회사)."""
    return (rf"(?<![가-힣A-Za-z0-9])(?:(?:{suffix}){SP}*{_ORG_NAME}{_ORG_STOP}"
            rf"|{_ORG_NAME_COMPACT}(?<![과와은는이가을를]){SP}*(?:{suffix})"
            # 접미 뒤에 조사가 붙어도 인정한다. `(?![가-힣…])`만 두면 '주식회사와'
            # 처럼 조사가 붙은 흔한 표기에서 매치가 통째로 깨진다(실측).
            rf"(?=[과와은는이가을를의도로에]|[^가-힣A-Za-z0-9]|$))")


# ── 검증기 ─────────────────────────────────────────────────────
def _digits(s: str) -> str:
    return "".join(ch for ch in s if ch.isdigit())


def _rrn_plausible(value: str) -> bool:
    """앞 6자리가 날짜로 성립해야 한다 (실측 오탐 '308098…' = 월 80).

    체크섬은 보지 않는다 — 2020. 10. 5. 주민등록법 시행규칙 개정으로 뒷 6자리가
    임의번호가 되어, 그 이후 발급분은 구 검증번호 공식을 만족하지 않는다.
    마스킹 도구에서 검증 실패는 곧 미마스킹이고 그것은 유출이다.
    """
    d = _digits(value)
    if len(d) < 6:
        return False
    return 1 <= int(d[2:4]) <= 12 and 1 <= int(d[4:6]) <= 31


def _business_no_ok(value: str) -> bool:
    """사업자등록번호 체크섬 — 이쪽은 개정 이력이 없어 그대로 쓸 수 있다."""
    d = _digits(value)
    if len(d) != 10:
        return False
    weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
    total = sum(int(d[i]) * weights[i] for i in range(9)) + (int(d[8]) * 5) // 10
    return (10 - total % 10) % 10 == int(d[9])


def _card_luhn_ok(value: str) -> bool:
    """Luhn 체크섬 — 임의 16자리(문서번호 등)를 카드로 잡지 않게 한다."""
    d = _digits(value)
    if not (13 <= len(d) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(d)):
        n = ord(ch) - 48
        if i % 2 == 1:
            n = n * 2 - 9 if n * 2 > 9 else n * 2
        total += n
    return total % 10 == 0


_PHONE_LIKE = re.compile(rf"^{PHONE_PREFIX}")


def _account_ok(value: str) -> bool:
    """계좌번호 형태 검증.

    앞자리로 은행을 특정할 수 없다 — 문서에 적히는 것은 기관코드가 아니라
    통장번호이고 형식이 은행마다 다르기 때문이다(국민 123456-78-901234,
    우리 1002-123-456789, 하나 123-456789-12345, 농협 302-1234-5678-91).
    그래서 '계좌번호일 수 있는 숫자열'로만 보고, 오탐은 문맥어가 억제한다.
    """
    d = _digits(value)
    if not (10 <= len(d) <= 16):
        return False
    if len(set(d)) <= 1:               # 0000000000 같은 자리표시자
        return False
    if len(d) <= 12 and _PHONE_LIKE.match(d):   # 전화번호 오인 차단
        return False
    return True


# ── 규칙 ───────────────────────────────────────────────────────
# context: 값 주변 ±40자 안에 이 단어 중 하나가 있어야 인정 (None이면 무조건)
# 'v' 그룹이 있으면 그 그룹이, 없으면 전체 매치가 제거 대상 값이다
# 한글 경계. `\b`를 쓰면 안 된다 — 파이썬은 한글을 단어문자로 보지만 JS는 아니라서,
# 같은 정규식이 웹판에서만 조용히 미탐이 된다(실측: '조심2019서1234' 웹판 미탐).
BL = r"(?<![0-9A-Za-z가-힣])"
BR = r"(?![0-9A-Za-z가-힣])"
# 사건번호 오른쪽 경계. BR을 그대로 쓰면 '2020가합12345이며'처럼 조사가 붙는 순간
# `\d+`가 아무리 되짚어도 경계를 못 맞춰 매치가 통째로 사라진다 — 부분 탐지가
# 아니라 완전 미탐이라 원문이 조용히 남는다(실측). 사건부호(가합·고단…)가 이미
# 판별자 역할을 하므로 오른쪽에서 한글까지 막을 이유가 없다.
BR_CASE = r"(?![0-9A-Za-z])"


Rule = namedtuple("Rule", "label pattern context validator")


def _r(label, pattern, context=None, validator=None):
    return Rule(label, re.compile(pattern), context, validator)


RULES: list[Rule] = [
    # 주민/외국인등록번호 — 자간·대시류 변형 허용, 무하이픈 13자리 포함
    _r("RRN", rf"(?<!\d){spaced(6)}{SP}*{DASH}{SP}*[1-8](?:{SP}*\d){{6}}(?!\d)"
              rf"|(?<!\d)\d{{6}}[1-8]\d{{6}}(?!\d)", validator=_rrn_plausible),
    _r("CARD", rf"(?<!\d)\d{{4}}(?:{SEP}\d{{4}}){{3}}(?!\d)|(?<!\d)\d{{16}}(?!\d)",
       validator=_card_luhn_ok),
    _r("PASSPORT", r"(?<![A-Za-z0-9])(?:[MSRODmsrod]\d{8}|[A-Za-z]{2}\d{7})(?![A-Za-z0-9])"),
    _r("BIZNO", rf"(?<!\d)\d{{3}}{SP}*{DASH}{SP}*\d{{2}}{SP}*{DASH}{SP}*\d{{5}}(?!\d)",
       validator=_business_no_ok),
    _r("CORPNO", rf"(?<!\d)\d{{6}}{SP}*{DASH}{SP}*\d{{7}}(?!\d)",
       context=("법인등록번호", "법인번호", "등기", "등록번호")),
    _r("DRIVER", rf"(?<!\d)\d{{2}}{SP}*{DASH}{SP}*\d{{2}}{SP}*{DASH}{SP}*"
                 rf"\d{{6}}{SP}*{DASH}{SP}*\d{{2}}(?!\d)"),
    # 계좌 — 넓게 잡고 문맥어로 억제. 무하이픈 10~16자리도 포함
    _r("ACCOUNT", rf"(?<![\d\-−–—―])\d{{2,7}}(?:{SP}*{DASH}{SP}*\d{{2,7}}){{1,3}}(?![\d\-−–—―])"
       # 7자리 세그먼트를 쓰는 실제 형식(3333-01-1234567 등)은 상한 6이면 매치가
       # 성립하지 않는다. 매치가 깨지면 PHONE 규칙이 뒷부분만 가져가 앞자리가
       # 그대로 노출됐다(실측). 자릿수 검증(10~16)과 문맥어가 오탐을 막는다.
                  rf"|(?<!\d)\d{{10,16}}(?!\d)",
       context=ACCOUNT_CONTEXT, validator=_account_ok),
    # 전화 — 국번 화이트리스트가 '계약번호 1234-5678' 오탐을 막는다
    _r("PHONE", rf"(?<![\d)])\(?{PHONE_PREFIX}\)?{SEP}\d{{3,4}}{SEP}\d{{4}}(?!\d)"),
    _r("PHONE", rf"(?<!\d){PHONE_PREFIX}\d{{7,8}}(?!\d)"),          # 무구분
    _r("PHONE", rf"(?<!\d){REP_PREFIX}{SEP1}\d{{4}}(?!\d)"),        # 대표번호(구분자 필수)
    # 이메일 — 로컬파트 상한 64자(RFC 5321). 상한이 없으면 긴 숫자열에서
    #   끝까지 삼킨 뒤 '@'를 찾아 한 글자씩 되짚는 O(n²) 백트래킹이 된다
    #   (실측: 숫자 10만자에 42.5초).
    _r("EMAIL", r"[A-Za-z0-9._%+-]{1,64}\s*@\s*[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,}"),
    # 사건번호 — 4자리 연도형은 형태가 특이해 문맥이 없어도 안전하다
    _r("CASE", rf"{BL}\d{{4}}{SP}*(?:{_COURT_CODES}){SP}*\d+{BR_CASE}"),
    # 2자리 연도형은 차량번호(81나3166)와 형태가 같다 → 사건 문맥일 때만
    _r("CASE", rf"{BL}\d{{2}}{SP}*(?:{_COURT_CODES_SHORT}){SP}*\d{{2,6}}{BR_CASE}",
       context=CASE_CONTEXT),
    _r("CASE", rf"{BL}(?:20)?\d{{2}}{SP}*(?:형제|전형제|중제|공제|징수){SP}*\d+호?{BR_CASE}"),
    _r("CASE", rf"{BL}(?:서울|부산|인천|경기|강원|충북|충남|전북|전남|경북|경남|제주|중노위)?"
               rf"{SP}*\d{{4}}{SP}*(?:부해|부노|부차|부당){SP}*\d+{BR_CASE}"),
    _r("CASE", rf"{BL}조심{SP}*\d{{4}}{SP}*[가-힣]?{SP}*\d{{3,5}}{BR_CASE}"),
    _r("CASE", rf"{BL}(?:중앙행심|행심){SP}*\d{{4}}[-–]?\d{{1,6}}{BR_CASE}"),
    _r("CASE", rf"{BL}(?:의결|심결|재결|결정|명령){SP}*제?{SP}*\d{{4}}[-–]\d{{1,4}}"
               rf"(?:[-–]\d{{1,4}})?{SP}*호{BR_CASE}"),
    # 차량번호 — 사건부호와 겹치는 한 글자(나·다·라·마)는 문맥이 구분한다.
    #   두 글자 부호(가합·고단 등)만 제외해 '2020가합123'을 차량으로 안 잡는다.
    _r("CAR", rf"{BL}\d{{2,3}}(?!가합|가단|가소|고합|고단|고정|고약|구합|구단|드합|드단|"
              rf"카합|카단|카기|카담|타경|타기|회합|회단)[{_CAR_SYLLABLES}]{SP}*\d{{4}}{BR}"),
    # 법인·법무법인
    _r("ORG", _org_pattern(_COMPANY_SUFFIX)),
    _r("ORG", _org_pattern(_FIRM_SUFFIX)),
    # 주소① 라벨 문맥 — 쉼표는 주소 내부에 흔하다("효원로 241, 302동"), 줄 끝까지가 값
    _r("ADDRESS", r"(?:주\s*소|주\s*거|소\s*재\s*지|등\s*록\s*기\s*준\s*지|거\s*소|사\s*업\s*장)"
                  r"(?:\s|[:：])+"          # 구분자 필수 — '소재지는'의 조사를 안 삼키게
                  r"(?P<v>[가-힣][^\n;]{4,70})"),
    # 주소② 상세주소 괄호 줄 — "(송파동, 미성아파트)". 단독 줄로 남아 유출됐던 실측
    # 괄호 상세주소. 문자군에 `\s`를 쓰면 개행을 넘어 '(환\n가)' 같은 괴물 값이
    # 만들어진다 — 지울 수도 없어 영구 잔존으로 남는다(실측). 같은 줄로 제한한다.
    _r("ADDR_DETAIL", r"(?P<v>\([가-힣0-9 \t,·.-]{2,30}(?:동|가|리|로|길)[가-힣0-9 \t,·.-]{0,30}\))"),
    # 주소③ 무라벨 — 시·도 지명 앵커 + 번지수까지(같은 줄 안에서만)
    _r("ADDRESS", rf"(?P<v>(?:{REGIONS}){SP}?[가-힣\d \t\-·,()]{{4,60}}?\d+(?:-\d+)?(?:호|번지)?)"),
]

# 라벨 → 사람이 읽는 유형명 (리포트·CLI 표시용)
LABEL_NAMES = {
    "RRN": "주민·외국인등록번호", "CARD": "카드번호", "PASSPORT": "여권번호",
    "BIZNO": "사업자등록번호", "CORPNO": "법인등록번호", "DRIVER": "운전면허번호",
    "ACCOUNT": "계좌번호", "PHONE": "전화번호", "EMAIL": "이메일",
    "CASE": "사건번호", "CAR": "차량번호", "ORG": "법인명",
    "ADDRESS": "주소", "ADDR_DETAIL": "상세주소",
    "NAME": "이름", "PERSON": "이름",
}
# --skip 에서 쓸 수 있는 유형 (NAME/PERSON은 'NAME' 하나로 묶어 끈다)
SKIPPABLE = sorted(set(LABEL_NAMES) - {"PERSON"})


# ── ko-pii (선택) ──────────────────────────────────────────────
try:
    import ko_pii            # 형태소 기반 2차 탐지기 — 없으면 정규식만으로 동작
except ImportError:
    ko_pii = None

_KO_PII_MIN_CONF = 0.6
_KO_PII_TAKE = {"PERSON", "ADDRESS"}   # 숫자류는 자체 정규식 전담(실측에서 ko-pii가 저품질)


def _person_ok(r, value: str) -> bool:
    """ko-pii PERSON 채택 기준 — 실문서 인스턴스 전수 실측으로 도출.

    conf는 못 믿는다: '운영자금'이 사전 가산으로 conf 1.00을 받는다.
    문맥(직함·PII 인접)도 못 믿는다: PII 밀집 문서에선 일반어도 인접 보증을 받는다.
    실측에서 갈린 조합은 '성씨 근거 + 이름끝음절 근거 + 3자 이상 + conf 0.75'였다.
    """
    from names import COMPOUND_SURNAMES
    if len(value) >= 4 and not value.startswith(COMPOUND_SURNAMES):
        return False
    ev = r.evidence
    return (any(e.startswith("pos:surname") for e in ev)
            and any(e.startswith("pos:name_final_syllable") for e in ev)
            and len(value) >= 3
            and r.confidence >= 0.75)


def _ko_pii_targets(text: str) -> list[tuple[str, str]]:
    if ko_pii is None:
        return []
    out = []
    person_hits: dict[str, dict] = {}
    for r in ko_pii.detect_all(text):
        if r.label not in _KO_PII_TAKE:
            continue
        value = text[r.start:r.end].strip()
        if len(value) < 2 or r.confidence < _KO_PII_MIN_CONF or "\n" in value:
            continue      # 개행 포함 = 여러 줄을 이어붙인 인공물
        if re.search(r"[\x00-\x1f\x7f]", value):
            continue      # 컨트롤 문자 = 추출 인공물
        if r.label == "PERSON":
            slot = person_hits.setdefault(value, {"ok": False, "ctx": False})
            slot["ok"] = slot["ok"] or _person_ok(r, value)
            slot["ctx"] = slot["ctx"] or any(
                e.startswith(("pos:title", "pos:deterministic_pii_nearby", "pos:field_label"))
                for e in r.evidence)
            continue
        out.append((r.label, value))
    # 고빈도 PERSON은 강한 문맥례 필수 — 산문에선 '오염수'·'노동자' 같은 일반명사가
    # 성씨+끝음절 필터를 통과한다. 오탐 명사는 수십 번 반복되고, 실명은 최소 한 번은
    # 직함·라벨 곁에서 등장한다는 실측 차이를 쓴다.
    for value, slot in person_hits.items():
        if slot["ok"] and not (text.count(value) >= 6 and not slot["ctx"]):
            out.append(("PERSON", value))
    return out


# ── 값 정제 ────────────────────────────────────────────────────
_JOSA_HEAD = re.compile(
    r"(?:에게서|으로서|으로써|에게|한테|께서|에서|으로|이다|이라|이며|이고|이란|"
    r"라는|라고|로서|로써|부터|까지|밖에|조차|마저|처럼|보다|"
    r"은|는|이|가|을|를|과|와|의|도|만|로|에|께|나|랑|야|씨|님)")


_TRAILING_JOSA = ("에게서", "으로서", "에게", "에서", "으로", "은", "는", "이", "가",
                  "을", "를", "과", "와", "의", "도", "에", "만", "로")


def _strip_josa(word: str) -> str:
    for j in _TRAILING_JOSA:
        if word.endswith(j) and len(word) - len(j) >= 2:
            return word[:-len(j)]
    return word


def _after_label(text: str, pos: int) -> bool:
    """값 바로 앞이 라벨이면 붙어 있어도 독립 출현으로 본다.

    표 서식에서는 라벨과 값이 붙는다 — '참    조서민수님'처럼. 앞 글자가 한글이라는
    이유로 접미 토막으로 보면 정작 실명을 놓친다(실측: 의견서 머리 표의 서민수 미탐).
    """
    before = re.sub(r"\s+", "", text[max(0, pos - 14):pos])
    return any(before.endswith(w) for w in LABEL_WORDS)


def _standalone_occurrence(text: str, value: str) -> bool:
    """이름 값이 문서 어딘가에서 '독립 단어'로 나타나는가.

    앞이 한글이면 접미 토막, 뒤로 한글이 이어지면 긴 단어의 앞토막
    ('가나자'⊂가나자산신탁)이다. 모든 출현이 토막이면 이름이 아니라 파편이다.

    뒤에 오는 것이 조사면 정상 출현이다. 이전에는 '뒤 한글 2자'를 무조건 토막으로
    봐서 '김철수에게'·'강서연이다'처럼 두 글자 조사가 붙은 실명이 전부 파편으로
    걸러졌다(실측 미탐).

    이어지는 단어가 회사·기관 꼬리면 법인명 앞토막으로 본다. 이 검사는 이름 엔진
    바깥에서 들어오는 ko-pii 값에도 걸어야 한다 — 실측에서 '가나자 산신탁'의
    '가나자'가 ko-pii PERSON으로 새어 나왔다.
    """
    hits = list(re.finditer(re.escape(value), text))
    if not hits:
        # 자간 표기('홍 길 동')를 정규화한 값은 원문에 문자열로 나타나지 않는다.
        # 조각 판정의 대상이 아니며, 제거 단계의 워드 폴백이 좌표를 찾는다.
        return True
    for m in hits:
        before = text[m.start() - 1:m.start()]
        if before and "가" <= before <= "힣" and not _after_label(text, m.start()):
            continue
        tail = text[m.end():m.end() + 16]
        if re.match(r"[가-힣]{2}", tail) and not _JOSA_HEAD.match(tail):
            continue
        w = re.match(r"[ \t]*\n?\s*([가-힣]+)", tail)
        if w and CORP_TAIL.match(_strip_josa(w.group(1))):
            continue
        return True
    return False


def _has_context(text: str, start: int, end: int, words) -> bool:
    window = text[max(0, start - 40):min(len(text), end + 40)]
    return any(w in window for w in words)


def detect(text: str, *, skip=(), extra_names=(), excluded_names=()) -> list[tuple[str, str]]:
    """(라벨, 값) 목록 — 정규식 ∪ 이름엔진 ∪ ko-pii. 값 기준 중복 제거.

    skip:           끄고 싶은 라벨들(LABEL_NAMES의 키). 과잉 마스킹을 되돌릴 때 쓴다.
    extra_names:    사용자 사전 — 규칙이 놓치는 이름을 강제로 추가(음차 외국인명 등).
    excluded_names: 사용자 사전 — 반복되는 이름 오탐을 영구히 제외.
    """
    skip = set(skip)
    if "NAME" in skip:
        skip.add("PERSON")
    found: dict[str, str] = {}

    for rule in RULES:
        if rule.label in skip:
            continue
        for m in rule.pattern.finditer(text):
            value = (m.groupdict().get("v") or m.group(0)).strip()
            if len(value) < 2:
                continue
            if rule.context and not _has_context(text, m.start(), m.end(), rule.context):
                continue
            if rule.validator and not rule.validator(value):
                continue
            found.setdefault(value, rule.label)

    if "NAME" not in skip:
        for name in detect_names(text, extra_names, excluded_names):
            found.setdefault(name, "NAME")
        for label, value in _ko_pii_targets(text):
            if label in skip:
                continue
            if label == "PERSON" and value in excluded_names:
                continue
            found.setdefault(value, label)

    items = [(label, value) for value, label in found.items()
             if not (label in ("NAME", "PERSON") and not _standalone_occurrence(text, value))]

    # 숫자 포함 값이 다른 값의 부분문자열이면 제외 — 주민번호 뒷자리 '1234567'이
    # PHONE으로 중복 탐지되면 전수 검색이 무관한 7자리를 과잉 제거한다.
    # 한글 이름엔 적용 금지: '홍길동'⊂'홍길동은'이지만 둘 다 별개 출현을 가진
    # 정당한 타깃이라, 지우면 맨이름 출현이 무방비가 된다(실측 유출 사고).
    # 라벨의 끝 글자가 상호 앞에 붙어 잡히는 일이 있다 — 자간 라벨 '수    신' 뒤의
    # '세마피엠씨 주식회사'가 '신세마피엠씨 주식회사'로도 잡혔다(실측).
    # 앞 한두 글자를 뗀 형태도 법인명으로 잡혔다면 그쪽이 진짜 상호다.
    orgs = {v for l, v in items if l == "ORG"}
    items = [(l, v) for l, v in items
             if not (l == "ORG" and any(v[i:] in orgs for i in (1, 2)))]

    return [(l, v) for l, v in items
            if not (any(ch.isdigit() for ch in v)
                    and any(v != w and v in w for _, w in items))]


def load_word_list(path: str | None) -> set[str]:
    """사용자 사전 파일 — 한 줄에 한 단어, '#'로 시작하는 줄은 주석."""
    if not path:
        return set()
    out = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            word = line.strip()
            if word and not word.startswith("#"):
                out.add(word)
    return out
