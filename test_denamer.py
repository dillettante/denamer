#!/usr/bin/env python3
"""denamer 회귀 테스트.

  1. 탐지 단위 — 규칙마다 '잡아야 할 것'과 '잡으면 안 될 것'을 쌍으로 둔다.
     미탐은 유출이고 오탐은 본문 훼손이라, 한쪽만 재면 다른 쪽이 조용히 무너진다.
  2. 경계조건 — 빈 문서·긴 숫자열·제어문자에서 멈추거나 폭주하지 않는가.
  3. 종단(반출판) — PDF를 실제로 비실명화해 잔존·과잉제거·메타데이터를 검사.
  4. 가명 대장 — 유형별 가명 꼴과 문서 간 일관성.
  5. 종단(질의판) — 마스킹 → 복원 왕복이 값을 뒤바꾸지 않는가.

실행: ~/.venvs/denamer/bin/python test_denamer.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).parent))
import denamer_ask
from denamer_out import redact
from detect import detect
from ledger import Ledger

_fail = 0
_pass = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global _fail, _pass
    if cond:
        _pass += 1
    else:
        _fail += 1
        print(f"  FAIL  {label}  {detail}")


# ══════════════════════════════════════════════════════════════
# 1. 탐지 단위
# ══════════════════════════════════════════════════════════════
# (설명, 입력, 반드시 탐지되어야 하는 값)
MUST_DETECT = [
    # 번호류 — 표기 변형
    ("주민번호 하이픈", "주민등록번호 880101-1234567", "880101-1234567"),
    ("주민번호 무하이픈", "본인 8801011234567 확인", "8801011234567"),
    ("주민번호 유니코드대시", "신청인 주민번호 880101−1234567 확인", "880101−1234567"),
    ("주민번호 자간", "등록번호 9 2 0 3 1 5 - 2 6 5 4 3 2 1", "9 2 0 3 1 5 - 2 6 5 4 3 2 1"),
    ("카드 전체", "카드번호 4422-1566-1860-6786 결제", "4422-1566-1860-6786"),
    ("여권", "여권번호 M12345678 소지", "M12345678"),
    ("사업자등록번호", "사업자등록번호 220-81-62517", "220-81-62517"),
    ("운전면허번호", "운전면허번호 12-34-567890-12", "12-34-567890-12"),
    ("전화 하이픈양옆공백", "전화 010 - 1234 - 5678 입니다", "010 - 1234 - 5678"),
    ("전화 괄호국번", "사무실 (02)123-4567", "(02)123-4567"),
    ("전화 무구분 휴대폰", "연락처 01012345678 로", "01012345678"),
    ("전화 무구분 지역번호", "사무실 0212345678 연락", "0212345678"),
    ("전화 대표번호", "고객센터 1588-1234 문의", "1588-1234"),
    ("계좌 라벨없음(은행명)", "국민은행 123456-78-901234 입금", "123456-78-901234"),
    ("계좌 우리은행꼴", "입금계좌 우리 1002-123-456789", "1002-123-456789"),
    ("이메일", "회신은 hong@example.co.kr 로", "hong@example.co.kr"),
    ("차량번호", "승용차 81나3166 운전", "81나3166"),
    # 이름 — 문맥 규칙
    ("판결주문 원고", "피고 김철수는 원고 이영희에게 지급하라", "이영희"),
    ("판결주문 피고", "피고 김철수는 원고 이영희에게 지급하라", "김철수"),
    ("조사앞 공백", "피고인 김철수 는 부인하였다", "김철수"),
    ("여격 조사", "김철수에게 지급을 명한다", "김철수"),
    ("서술어 결합", "피고소인은 강서연이다", "강서연"),
    ("괄호 식별자", "홍길동(880101-1234567)은", "홍길동"),
    ("친족 관계어", "배우자 김영희와 장남 박민수", "김영희"),
    ("호칭 접미", "김철수씨는 이날", "김철수"),
    ("직함 접미", "담당 박민수 부장 결재", "박민수"),
    ("라벨 연쇄", "소송대리인 변호사 김대한", "김대한"),
    ("쉼표 연쇄", "변호사 홍길동, 김철수", "김철수"),
    ("조사끝 실명", "원고 이지은은 출석하였다", "이지은"),
    ("자간 이름", "성명: 홍 길 동", "홍길동"),
    ("이미 마스킹된 표기", "김○○은 출석하였다", "김○○"),
    ("당사자 라벨 확장(채무자)", "채무자 박민수는 변제하지 않았다", "박민수"),
    ("당사자 라벨 확장(임차인)", "임차인 정수현이 인도받았다", "정수현"),
    # 법인·사건번호
    ("법인명 라벨뒤", "피고 주식회사 동방화학 대표이사", "주식회사 동방화학"),
    ("법무법인", "소송대리인 법무법인(유한) 가나", "법무법인(유한) 가나"),
    ("사건번호 4자리연도", "이 사건 2020가합12345 판결", "2020가합12345"),
    ("사건번호 조세심판", "조심2019서1234 결정", "조심2019서1234"),
    # 주소
    ("주소 라벨", "주소: 경기도 수원시 팔달구 효원로 241", "경기도 수원시 팔달구 효원로 241"),
]

# (설명, 입력, 절대 탐지되면 안 되는 값)
MUST_NOT_DETECT = [
    ("문서번호를 전화로", "계약번호 1234-5678 참조", "1234-5678"),
    ("5:3 가결", "표결 결과 5:3 으로 가결", "5:3"),
    ("원심은", "원심은 이를 배척하였다", "원심"),
    ("정답을", "정답을 기재하지 아니한", "정답"),
    ("이사회는", "이사회는 이를 승인하였다", "이사회"),
    ("한다는", "한다는 주장은 이유 없다", "한다"),
    ("문항", "문항 배점이 잘못되었다", "문항"),
    ("조합원에게", "조합원에게 배당하였다", "조합원"),
    ("배우자 명의로", "배우자 명의로 취득한 부동산", "명의"),
    ("피고 패소", "피고 패소 부분을 파기한다", "패소"),
    ("청구인 주장", "청구인 주장은 받아들이지 않는다", "주장"),
    ("담당변호사→담당", "담당변호사 홍길동 제출", "담당"),
    ("김포시", "김포시는 이를 허가하였다", "김포"),
    ("회사명 앞토막", "피고 가나자 산신탁은", "가나자"),
    ("줄바꿈 회사명", "피고 인천도시\n공사는", "인천도시"),
    ("사내이사", "사내이사 선임의 건", "사내"),
    ("임의 16자리(Luhn 불통과)", "문서 1234567812345678 참조", "1234567812345678"),
    ("월 80 주민번호꼴", "번호 308098-1234567 은 무효", "308098-1234567"),
]


def test_detection() -> None:
    print("1. 탐지 단위")
    for desc, text, want in MUST_DETECT:
        got = {v for _, v in detect(text)}
        check(want in got, f"미탐 [{desc}]", f"want={want!r} got={sorted(got)}")
    for desc, text, bad in MUST_NOT_DETECT:
        got = {v for _, v in detect(text)}
        check(bad not in got, f"오탐 [{desc}]", f"not={bad!r} got={sorted(got)}")


def test_skip_and_dictionary() -> None:
    print("2. 유형 on/off · 사용자 사전")
    text = "피고 김철수는 2020가합12345 사건에서 주식회사 동방화학을 상대로"
    check("2020가합12345" not in {v for _, v in detect(text, skip=["CASE"])},
          "--skip CASE 가 사건번호를 끈다")
    check("주식회사 동방화학" not in {v for _, v in detect(text, skip=["ORG"])},
          "--skip ORG 가 법인명을 끈다")
    check("김철수" in {v for _, v in detect(text, skip=["CASE", "ORG"])},
          "다른 유형을 꺼도 이름은 남는다")
    # 한글 음차 외국인명은 성씨 게이트 구조상 자동 탐지에서 빠진다 → 사용자 사전이 메운다
    foreign = "피고인 스미스는 혐의를 부인하였다"
    check("스미스" not in {v for _, v in detect(foreign)}, "음차 외국인명은 규칙으로 미탐(알려진 한계)")
    check("스미스" in {v for _, v in detect(foreign, extra_names={"스미스"})},
          "--names 사전이 음차 외국인명을 채운다")
    check("김철수" not in {v for _, v in detect(text, excluded_names={"김철수"})},
          "--not-names 사전이 반복 오탐을 끈다")


def test_boundaries() -> None:
    print("3. 경계조건")
    check(detect("") == [], "빈 문서")
    check(detect("   \n\t  ") == [], "공백만")
    check(isinstance(detect("방지\x01제어문자\x00"), list), "제어문자")

    t0 = time.perf_counter()
    detect("1234567890" * 10000)          # 숫자 10만자
    dt = time.perf_counter() - t0
    check(dt < 3.0, "숫자 10만자 3초 이내", f"{dt:.2f}초")

    t0 = time.perf_counter()
    detect("가나다라마바사아자차" * 20000)   # 한글 20만자 한 줄
    dt = time.perf_counter() - t0
    check(dt < 5.0, "한글 20만자 5초 이내", f"{dt:.2f}초")


# ══════════════════════════════════════════════════════════════
# 4. 종단 (반출판)
# ══════════════════════════════════════════════════════════════
FIX_OFFICIAL = (
    [
        "수원시 환경정책과", "",
        "제 목: 개인정보 처리 위탁 계약 체결 알림",
        "문서번호: 환경정책과-2026-0712", "",
        "1. 계약 당사자",
        "   성명: 홍길동 (직위: 행정 6급)",
        "   주민등록번호: 880101-1234567",
        "   휴대전화: 010-1234-5678",
        "   전화번호: 031) 228-2114",
        "   이메일: gildong.hong@example.go.kr",
        "   주소: 경기도 수원시 팔달구 효원로 241, 302동 1104호", "",
        "2. 대금 지급",
        "   입금계좌: 국민은행 123456-04-789012 (예금주: 홍길동)",
        "   카드번호: 4571-9700-1234-5678", "",
        "3. 담당자",
        "   담당자: 김철수 주무관 (직통전화: 02-2100-1234)",
        "   여권번호: M12345678", "",
        "붙임: 위탁계약서 1부. 끝.", "", "수원시장",
    ],
    ["880101-1234567", "010-1234-5678", "228-2114", "2100-1234",
     "gildong.hong", "4571-9700", "M12345678", "123456-04-789012",
     "홍길동", "김철수", "효원로 241"],
    ["수원시 환경정책과", "위탁계약서", "행정 6급"],
)
FIX_ADVERSARIAL = (
    [
        "민원 접수 대장 (발췌)", "",
        "가. 신청인 주민번호 880101−1234567 확인함",          # 유니코드 마이너스
        "나. 대리인 등록번호 9 2 0 3 1 5 - 2 6 5 4 3 2 1",    # 자간
        "다. 연락처 010.9876.5432 로 통보",
        "라. 박영희는 2026. 6. 1. 이의신청서를 제출하였다",
        "마. 소재지는 경상북도 포항시 남구 청림동 123-4 이다",
        "바. 회신은 younghee.park",
        "@example.com 으로 한다",
        "사. 환급계좌:",
        "    농협 302-0123-4567-89",
        "아. 담당자: 이도 주무관",
        "자. 본 건은 개인정보 보호법 제17조에 따른다",
    ],
    ["1234567", "2 6 5 4 3 2 1", "9876", "박영희", "청림동 123-4",
     "younghee.park", "302-0123-4567-89", "이도"],
    ["개인정보 보호법 제17조", "민원 접수 대장", "등록번호", "이의신청서"],
)
FIX_LEGAL = (
    [
        "서울중앙지방법원 판결", "",
        "사        건   2020가합12345 손해배상(기)",
        "원        고   이영희",
        "피        고   주식회사 동방화학",
        "소송대리인 법무법인(유한) 가나", "",
        "주        문", "",
        "1. 피고는 원고 이영희에게 50,000,000원을 지급하라.",
        "2. 소송비용은 피고가 부담한다.", "",
        "이        유", "",
        "피고 대표이사 김철수는 2020. 3. 5. 원고에게 이를 통지하였고,",
        "이영희는 같은 날 이의를 제기하였다. 김철수에게 책임이 있다.",
    ],
    ["2020가합12345", "이영희", "김철수", "동방화학"],
    ["손해배상", "소송비용", "서울중앙지방법원"],
)


def make_pdf(lines: list[str], path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    font = fitz.Font("korea")
    tw = fitz.TextWriter(page.rect)
    y = 60
    for line in lines:
        if line:
            tw.append((60, y), line, font=font, fontsize=11)
        y += 22
    tw.write_text(page)
    # 실무 PDF처럼 작성자 메타데이터를 심어 둔다 — 스크럽이 실제로 도는지 검증용
    doc.set_metadata({"author": "홍길동", "title": "내부 검토용", "creator": "HWP 2022"})
    doc.save(path)
    doc.close()


def run_pdf_case(name, lines, must_gone, must_keep, mode="anon",
                 ledger_path=None, must_have=None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/in.pdf", f"{tmp}/out.pdf"
        make_pdf(lines, src)
        report = redact(src, dst, mode=mode, ledger_path=ledger_path)
        check(not report["unmapped"], f"{name}: 매핑 실패 없음", str(report["unmapped"]))
        check(not report["residual"], f"{name}: 자체검증 잔존 없음", str(report["residual"]))
        out = fitz.open(dst)
        leftover = {k: v for k, v in out.metadata.items()
                    if v and k not in ("format", "encryption")}
        check(not leftover, f"{name}: 메타데이터 소거", str(leftover))
        text = "".join(p.get_text() for p in out)
        out.close()
        for v in must_gone:
            check(v not in text, f"{name}: PII 잔존", repr(v))
        for v in must_keep:
            check(v in text, f"{name}: 과잉 제거", repr(v))
        for v in (must_have or []):
            check(v in text, f"{name}: 대체 텍스트 누락", repr(v))
        return text


def test_end_to_end_out() -> None:
    print("4. 종단 — 반출판 PDF")
    run_pdf_case("공문서·익명화", *FIX_OFFICIAL, must_have=["홍OO", "김OO", "경기도"])
    run_pdf_case("적대적 변형·익명화", *FIX_ADVERSARIAL, must_have=["박OO", "경상북도"])
    # 법률문서: 익명화 모드에서도 법인명·사건번호는 가명으로 치환한다
    text = run_pdf_case("판결문·익명화", *FIX_LEGAL, must_have=["이OO", "김OO"])
    check("A사" in text or "B사" in text, "법인명이 가명(A사)으로 치환", repr(text[:0]))
    check("사건A" in text, "사건번호가 가명(사건A)으로 치환")


def test_pseudo_and_ledger() -> None:
    print("5. 가명화 · 대장")
    with tempfile.TemporaryDirectory() as tmp:
        led = f"{tmp}/ledger.json"
        run_pdf_case("공문서·가명화 1회차", *FIX_OFFICIAL, mode="pseudo", ledger_path=led)
        m1 = json.load(open(led, encoding="utf-8"))
        check("홍길동" in m1["PERSON"] and "김철수" in m1["PERSON"],
              "대장에 실명 등재", str(m1.get("PERSON")))
        run_pdf_case("공문서·가명화 2회차", *FIX_OFFICIAL, mode="pseudo", ledger_path=led)
        m2 = json.load(open(led, encoding="utf-8"))
        check(m1 == m2, "재실행 시 가명 드리프트 없음", f"{m1} → {m2}")

        # 다른 문서라도 같은 대장을 쓰면 같은 사람이 같은 가명을 받는다
        led2 = f"{tmp}/cross.json"
        run_pdf_case("교차문서 1", *FIX_OFFICIAL, mode="pseudo", ledger_path=led2)
        before = json.load(open(led2, encoding="utf-8"))["PERSON"]["김철수"]
        run_pdf_case("교차문서 2", *FIX_LEGAL, mode="pseudo", ledger_path=led2)
        after = json.load(open(led2, encoding="utf-8"))["PERSON"]["김철수"]
        check(before == after, "문서 간 가명 일관", f"{before} vs {after}")

    # 익명화 모드는 대장을 파일로 남기지 않는다 — 대장 자체가 유출 채널이기 때문
    with tempfile.TemporaryDirectory() as tmp:
        led = f"{tmp}/should_not_exist.json"
        src, dst = f"{tmp}/in.pdf", f"{tmp}/out.pdf"
        make_pdf(FIX_LEGAL[0], src)
        redact(src, dst, mode="anon", ledger_path=led)
        check(not Path(led).exists(), "익명화 모드는 대장 파일을 만들지 않는다")

    # 유형별 가명 꼴이 섞이지 않는다
    led = Ledger(None)
    check(led.alias("PERSON", "홍길동") == "A", "사람 가명 = A")
    check(led.alias("ORG", "동방화학") == "A사", "법인 가명 = A사")
    check(led.alias("CASE", "2020가합1") == "사건A", "사건 가명 = 사건A")
    check(led.alias("PERSON", "김철수") == "B", "두 번째 사람 = B")
    check(led.alias("PERSON", "홍길동") == "A", "같은 사람은 같은 가명")


def test_ask_roundtrip() -> None:
    print("6. 종단 — 질의판 왕복")
    text = ("피고 김철수(880101-1234567)는 원고 이영희에게 010-1234-5678로 연락하였다.\n"
            "이영희의 주민등록번호는 900202-2345678이다.")
    result = denamer_ask.mask(text)
    masked, tmap = result["masked"], result["token_map"]

    check(not result["collisions"], "토큰 충돌 없음", str(result["collisions"]))
    for value in ("김철수", "이영희", "880101-1234567", "900202-2345678", "010-1234-5678"):
        check(value not in masked, "질의용 텍스트에 원문 잔존", repr(value))
    check("[[PERSON_" in masked, "이름이 토큰으로 치환")

    # 같은 유형의 값이 둘이면 서로 다른 토큰을 받아야 한다.
    # (V2 검증에서 첫 사람 주민번호가 둘째 사람 번호로 복원되는 오염이 있었다.)
    rrn_tokens = {t for t, e in tmap.items() if e["original"].startswith(("880101", "900202"))}
    check(len(rrn_tokens) == 2, "같은 유형 값 둘이 서로 다른 토큰", str(rrn_tokens))

    restored = denamer_ask.restore(masked, tmap)
    check(restored["restored"] == text, "왕복 복원이 원문과 완전 일치")
    check(not restored["unresolved"], "미해결 토큰 없음", str(restored["unresolved"]))

    # LLM 답변처럼 일부 토큰만 등장하는 경우도 정확히 복원된다
    answer = "[[PERSON_01]]님의 연락처는 [[PHONE_01]]입니다."
    r2 = denamer_ask.restore(answer, tmap)
    check("[[" not in r2["restored"], "부분 답변 복원", r2["restored"])


if __name__ == "__main__":
    test_detection()
    test_skip_and_dictionary()
    test_boundaries()
    test_end_to_end_out()
    test_pseudo_and_ledger()
    test_ask_roundtrip()
    print(f"\n{_pass} pass / {_fail} fail")
    sys.exit(1 if _fail else 0)
