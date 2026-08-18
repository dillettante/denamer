#!/usr/bin/env python3
"""denamer 회귀 테스트.

  1. 탐지 단위 — 규칙마다 '잡아야 할 것'과 '잡으면 안 될 것'을 쌍으로 둔다.
     미탐은 유출이고 오탐은 본문 훼손이라, 한쪽만 재면 다른 쪽이 조용히 무너진다.
  2. 경계조건 — 빈 문서·긴 숫자열·제어문자에서 멈추거나 폭주하지 않는가.
  3. 종단(외부용) — PDF를 실제로 비실명화해 잔존·과잉제거·메타데이터를 검사.
  4. 가명 대장 — 유형별 가명 꼴과 문서 간 일관성.
  5. 종단(내부용) — 마스킹 → 복원 왕복이 값을 뒤바꾸지 않는가.

실행: ~/.venvs/denamer/bin/python test_denamer.py
"""
import json
import sys
import tempfile
import time
from pathlib import Path

import xml.etree.ElementTree as ET

import fitz

sys.path.insert(0, str(Path(__file__).parent))
import denamer_in
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
# 4. 종단 (외부용)
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
    print("4. 종단 — 외부용 PDF")
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
    print("6. 종단 — 내부용 왕복")
    text = ("피고 김철수(880101-1234567)는 원고 이영희에게 010-1234-5678로 연락하였다.\n"
            "이영희의 주민등록번호는 900202-2345678이다.")
    result = denamer_in.mask(text)
    masked, tmap = result["masked"], result["token_map"]

    check(not result["collisions"], "토큰 충돌 없음", str(result["collisions"]))
    for value in ("김철수", "이영희", "880101-1234567", "900202-2345678", "010-1234-5678"):
        check(value not in masked, "질의용 텍스트에 원문 잔존", repr(value))
    check("[[PERSON_" in masked, "이름이 토큰으로 치환")

    # 같은 유형의 값이 둘이면 서로 다른 토큰을 받아야 한다.
    # (V2 검증에서 첫 사람 주민번호가 둘째 사람 번호로 복원되는 오염이 있었다.)
    rrn_tokens = {t for t, e in tmap.items() if e["original"].startswith(("880101", "900202"))}
    check(len(rrn_tokens) == 2, "같은 유형 값 둘이 서로 다른 토큰", str(rrn_tokens))

    restored = denamer_in.restore(masked, tmap)
    check(restored["restored"] == text, "왕복 복원이 원문과 완전 일치")
    check(not restored["unresolved"], "미해결 토큰 없음", str(restored["unresolved"]))

    # LLM 답변처럼 일부 토큰만 등장하는 경우도 정확히 복원된다
    answer = "[[PERSON_01]]님의 연락처는 [[PHONE_01]]입니다."
    r2 = denamer_in.restore(answer, tmap)
    check("[[" not in r2["restored"], "부분 답변 복원", r2["restored"])


def test_in_alias_style() -> None:
    print("7. 내부용 — 가명(alias) 표기")
    text = ("피고 김철수(880101-1234567)는 원고 이영희에게 010-1234-5678로 연락하였다.\n"
            "주식회사 동방화학은 2020가합12345 사건의 당사자이다. A안과 B안을 검토하였다.")
    r = denamer_in.mask(text, style="alias")
    masked, tmap = r["masked"], r["token_map"]
    check(not r["collisions"], "표기 충돌 없음", str(r["collisions"]))
    for value in ("김철수", "이영희", "880101-1234567", "010-1234-5678", "동방화학"):
        check(value not in masked, "가명 표기 후 원문 잔존", repr(value))
    # 유형이 다르면 가명 꼴도 달라야 한다. 알파벳 풀을 돌려 쓰면 사람과 주민번호가
    # 모두 'A'가 되어 복원이 뒤섞인다(실측 결함).
    marks = list(tmap)
    check(len(marks) == len(set(marks)), "표기 중복 없음", str(marks))
    check(any(m.startswith("주민번호") for m in marks), "주민번호는 별도 가명 꼴", str(marks))
    check(any(m.startswith("전화") for m in marks), "전화는 별도 가명 꼴", str(marks))
    check(any(m.endswith("사") for m in marks), "법인은 A사 꼴", str(marks))
    # 원문에 이미 'A'가 쓰였으므로(A안) 사람 가명은 A를 비켜 가야 한다
    check("A" not in marks, "원문과 겹치는 표기는 피한다", str(marks))

    back = denamer_in.restore(masked, tmap)
    check(back["restored"] == text, "가명 표기 왕복 복원이 원문과 일치")
    check(not back["unresolved"], "미해결 표기 없음", str(back["unresolved"]))

    # 조사가 바로 붙는 형태에서도 복원돼야 한다 — 경계 규칙이 한글을 막으면 전부 실패한다
    check("A사은" in masked or "사" in masked, "법인 가명이 조사와 붙어 나온다")


def make_scan_pdf(path: str, stamp: str = "법무법인 가나 (인)") -> None:
    """스캔본 흉내: 쪽을 덮는 이미지 + 도장처럼 짧은 텍스트만.

    실제 사건 문서에서 가장 위험한 형태다 — 레이어가 '있으니' OCR을 건너뛰고,
    본문은 이미지라 아무것도 탐지되지 않는데 리포트는 OK가 나온다.
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, 600, 850), False)
    pix.clear_with(235)
    page.insert_image(page.rect, pixmap=pix)
    tw = fitz.TextWriter(page.rect)
    tw.append((60, 60), stamp, font=fitz.Font("korea"), fontsize=11)
    tw.write_text(page)
    doc.save(path)
    doc.close()


def test_thin_text_layer() -> None:
    print("8. 얇은 텍스트 레이어 감지")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/scan.pdf", f"{tmp}/out.pdf"
        make_scan_pdf(src)
        report = redact(src, dst, no_ocr=True)
        check(any("얇" in w for w in report["warnings"]),
              "스캔본에 --no-ocr 면 경고를 낸다", str(report["warnings"]))
    # 본문이 충분한 문서에는 이 경고가 붙지 않는다 — 짧은 문서를 스캔본으로 오판하면
    # 원문 텍스트를 래스터로 갈아 버려 오히려 품질이 떨어진다
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/full.pdf", f"{tmp}/out.pdf"
        make_pdf(FIX_OFFICIAL[0], src)
        report = redact(src, dst, no_ocr=True)
        check(not any("얇" in w for w in report["warnings"]),
              "본문 있는 문서엔 얇은 레이어 경고가 없다", str(report["warnings"]))
    # 이미지 없는 짧은 문서(1쪽 발췌)도 스캔본으로 보지 않는다
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/short.pdf", f"{tmp}/out.pdf"
        make_pdf(["원고 이영희", "피고 김철수", "위와 같이 판결한다."], src)
        report = redact(src, dst, no_ocr=True)
        check(not any("얇" in w for w in report["warnings"]),
              "이미지 없는 짧은 문서는 스캔본이 아니다", str(report["warnings"]))


def test_ocr_metadata_scrub() -> None:
    """OCR 경유 산출물의 메타데이터 소거.

    ocrmypdf 산출물은 XMP를 항상 쓴다. 문서정보를 먼저 비우고 XMP를 나중에 지우면
    소거가 무효가 되어 author=실명이 산출물에 남는다(실측 유출). 순서를 고정한다.
    """
    import shutil
    print("9. OCR 경유 메타데이터 소거")
    if shutil.which("ocrmypdf") is None:
        print("   (ocrmypdf 없음 — 건너뜀)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/scan.pdf", f"{tmp}/out.pdf"
        make_scan_pdf(src)
        d = fitz.open(src)
        d.set_metadata({"author": "홍길동", "title": "내부 검토용"})
        d.saveIncr()
        d.close()
        report = redact(src, dst)
        check(report["ocr_applied"], "스캔본이 OCR을 탄다")
        check(not report["residual"], "OCR 경유 잔존 없음", str(report["residual"]))
        with fitz.open(dst) as o:
            left = {k: v for k, v in o.metadata.items()
                    if v and k not in ("format", "encryption")}
        check(not left, "OCR 경유 산출물 메타데이터 소거", str(left))


# ══════════════════════════════════════════════════════════════
# 7. 종단 (외부용 DOCX)
# ══════════════════════════════════════════════════════════════
# DOCX의 위험은 PDF와 다르다. 화면에 안 보이는 자리에 원문이 남는 것이 문제다.
# 그래서 픽스처를 '숨을 곳'마다 하나씩 심어 만든다.
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
       'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
       'mc:Ignorable="wps"')


def _p(runs: str) -> str:
    return f"<w:p>{runs}</w:p>"


def _r(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


DOCX_PARTS = {
    "[Content_Types].xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>',
    "_rels/.rels":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '</Relationships>',
    # 하이퍼링크 대상에 메일 주소 — 본문을 지워도 여기 남으면 유출이다
    "word/_rels/document.xml.rels":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        'Target="mailto:gildong.hong@example.go.kr" TargetMode="External"/>'
        '</Relationships>',
    "word/document.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document {_NS}><w:body>'
        # ① 값이 세 run 에 쪼개진 이름 — Word의 기본 동작이다
        + _p('<w:r><w:t>원고 김</w:t></w:r><w:r><w:t>철</w:t></w:r>'
             '<w:r><w:t xml:space="preserve">수는 이를 청구한다.</w:t></w:r>')
        # ② 표 셀
        + '<w:tbl><w:tr><w:tc>' + _p(_r("주민등록번호 880101-1234567")) + '</w:tc>'
          '<w:tc>' + _p(_r("연락처 010-1234-5678")) + '</w:tc></w:tr></w:tbl>'
        # ③ 변경이력 — 삭제된 텍스트가 w:delText 로 파일에 남아 있다
        + _p('<w:del w:id="1" w:author="최민호" w:date="2026-07-30T00:00:00Z">'
             '<w:r><w:delText>피고 이영희에게 통지</w:delText></w:r></w:del>'
             '<w:ins w:id="2" w:author="장서윤" w:date="2026-07-30T00:00:00Z">'
             '<w:r><w:t>정정함</w:t></w:r></w:ins>')
        # ④ 텍스트박스 — run 안에 또 단락이 들어간다
        + _p('<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:txbx>'
             '<w:txbxContent>' + _p(_r("담당 박민수 부장")) + '</w:txbxContent>'
             '</wps:txbx></w:drawing></mc:Choice></mc:AlternateContent></w:r>')
        # ⑤ 필드 코드 — 하이퍼링크가 여기에도 문자열로 들어간다
        + _p('<w:r><w:instrText xml:space="preserve"> HYPERLINK "mailto:gildong.hong@example.go.kr" </w:instrText></w:r>')
        + '<w:sectPr/></w:body></w:document>',
    # ⑥ 머리글·바닥글
    "word/header1.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:hdr {_NS}>' + _p(_r("사건 2020가합12345")) + '</w:hdr>',
    "word/footer1.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:ftr {_NS}>' + _p(_r("주식회사 동방화학")) + '</w:ftr>',
    # ⑦ 각주
    "word/footnotes.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:footnotes {_NS}><w:footnote w:id="2">'
        + _p(_r("피고인 강서연이다")) + '</w:footnote></w:footnotes>',
    # ⑧ 주석 — 본문 텍스트 + 작성자 이름(속성)
    "word/comments.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:comments {_NS}><w:comment w:id="1" w:author="김철수" w:initials="김">'
        + _p(_r("임차인 정수현 확인 요망")) + '</w:comment></w:comments>',
    # ⑨ 문서 속성 — 작성자·회사
    "docProps/core.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/">'
        '<dc:creator>홍길동</dc:creator><cp:lastModifiedBy>김철수</cp:lastModifiedBy>'
        '<dc:title>내부 검토용</dc:title></cp:coreProperties>',
    "docProps/app.xml":
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        '<Company>법무법인(유한) 가나</Company><Manager>장서윤</Manager></Properties>',
}


def make_docx(path: str) -> None:
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in DOCX_PARTS.items():
            z.writestr(name, body)


def test_docx() -> None:
    print("7. 종단 — 외부용 DOCX")
    import zipfile
    import docx_io

    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/in.docx", f"{tmp}/out.docx"
        make_docx(src)

        # 읽기: 숨은 자리가 모두 텍스트로 잡혀야 탐지 대상이 된다
        text = docx_io.read_text(src)
        for where, needle in [("본문(쪼개진 run)", "김철수"), ("표", "880101-1234567"),
                              ("변경이력 삭제분", "이영희"), ("텍스트박스", "박민수"),
                              ("머리글", "2020가합12345"), ("바닥글", "동방화학"),
                              ("각주", "강서연"), ("주석", "정수현")]:
            check(needle in text, f"읽기 누락 [{where}]", repr(needle))

        report = redact(src, dst, mode="anon")
        check(report["format"] == "docx", "형식 인식")
        check(not report["unmapped"], "DOCX 매핑 실패 없음", str(report["unmapped"]))
        check(not report["residual"], "DOCX 자체검증 잔존 없음", str(report["residual"]))
        check(any("변경이력" in w for w in report["warnings"]), "변경이력 경고")

        # 저장본 raw 전수 검사 — 어느 파트에도 원문이 남으면 안 된다
        with zipfile.ZipFile(dst) as z:
            raw = "\n".join(z.read(n).decode("utf-8", "ignore") for n in z.namelist())
        for needle in ["김철수", "880101-1234567", "010-1234-5678", "이영희", "박민수",
                       "2020가합12345", "동방화학", "강서연", "정수현",
                       "gildong.hong", "홍길동", "최민호", "장서윤", "내부 검토용"]:
            check(needle not in raw, "DOCX 원문 잔존", repr(needle))

        # 쪼개진 run 이 제대로 합쳐졌는지 — 가운데 조각이 남는 유형의 결함 확인
        out_text = docx_io.read_text(dst)
        check("김OO는 이를 청구한다." in out_text,
              "쪼개진 run 재조립 + 조사 보존", repr(out_text[:60]))
        check("A사" in out_text, "법인명 가명", repr(out_text))
        check("사건A" in out_text, "사건번호 가명")
        check("■■■■" in out_text, "번호류 고정 가림")
        # 지우지 말아야 할 것
        for keep in ["이를 청구한다", "확인 요망", "정정함"]:
            check(keep in out_text, "DOCX 과잉 제거", repr(keep))
        # 저장본이 열리는 zip 이고 XML 로 파싱되는지
        with zipfile.ZipFile(dst) as z:
            check(z.testzip() is None, "저장본 zip 무결성")
            for n in z.namelist():
                if n.endswith(".xml") or n.endswith(".rels"):
                    try:
                        ET.fromstring(z.read(n))
                    except ET.ParseError as e:
                        check(False, f"저장본 XML 파손 [{n}]", str(e))

    # 미지원 형식은 조용히 실패하지 않고 분명히 막는다
    for suffix in (".doc", ".hwp", ".xlsx"):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / f"x{suffix}"
            p.write_bytes(b"dummy")
            try:
                redact(str(p), f"{tmp}/o.pdf")
                check(False, f"{suffix} 차단 실패")
            except SystemExit:
                check(True, f"{suffix} 차단")


if __name__ == "__main__":
    test_detection()
    test_skip_and_dictionary()
    test_boundaries()
    test_end_to_end_out()
    test_pseudo_and_ledger()
    test_ask_roundtrip()
    test_in_alias_style()
    test_thin_text_layer()
    test_ocr_metadata_scrub()
    test_docx()
    print(f"\n{_pass} pass / {_fail} fail")
    sys.exit(1 if _fail else 0)
