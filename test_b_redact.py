#!/usr/bin/env python3
"""b_redact.py 회귀 테스트 — 픽스처 2종 생성 → 비실명화 → 잔존·과잉제거 검사.

실행: python3 test_b_redact.py   (전부 통과하면 'ALL OK', 아니면 AssertionError)
"""
import sys
import tempfile
from pathlib import Path

import fitz

sys.path.insert(0, str(Path(__file__).parent))
from b_redact import redact

# ── 픽스처 정의: (줄들, 잔존 금지 조각, 보존 필수 조각) ──
FIX1 = (
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
FIX2 = (
    [
        "민원 접수 대장 (발췌)", "",
        "가. 신청인 주민번호 880101−1234567 확인함",          # 유니코드 마이너스
        "나. 대리인 등록번호 9 2 0 3 1 5 - 2 6 5 4 3 2 1",   # 자간
        "다. 연락처 010.9876.5432 로 통보",
        "라. 박영희는 2026. 6. 1. 이의신청서를 제출하였다",    # 무라벨 이름 (ko-pii 담당)
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
    # "등록번호"·"이의신청서" 보존 = ko-pii PERSON 오탐(성씨 근거 없음)이 걸러졌다는 증거
    ["개인정보 보호법 제17조", "민원 접수 대장", "등록번호", "이의신청서"],
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


def run_case(name: str, lines: list[str], must_gone: list[str], must_keep: list[str],
             mode: str = "anon", ledger_path: str | None = None,
             must_have: list[str] | None = None) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = f"{tmp}/in.pdf", f"{tmp}/out.pdf"
        make_pdf(lines, src)
        report = redact(src, dst, mode=mode, ledger_path=ledger_path)
        assert not report["unmapped"], f"{name}: 매핑 실패 {report['unmapped']}"
        assert not report["residual"], f"{name}: 자체검증 잔존 {report['residual']}"
        out = fitz.open(dst)
        leftover = {k: v for k, v in out.metadata.items()
                    if v and k not in ("format", "encryption")}
        assert not leftover, f"{name}: 메타데이터 잔존 → {leftover}"
        text = "".join(p.get_text() for p in out)
        for v in must_gone:
            assert v not in text, f"{name}: PII 잔존 → {v}"
        for v in must_keep:
            assert v in text, f"{name}: 과잉 제거 → {v}"
        for v in (must_have or []):
            assert v in text, f"{name}: 대체 텍스트 누락 → {v}"
    print(f"{name}: OK ({report['targets']} targets, {report['boxes_applied']} boxes)")
    return text


if __name__ == "__main__":
    import json as _json

    # 익명화: 성 보존(김OO)·주소 첫 토큰 보존 확인
    run_case("fixture1(공문서·익명화)", *FIX1,
             must_have=["홍OO", "김OO", "경기도"])
    run_case("fixture2(적대적 변형·익명화)", *FIX2,
             must_have=["박OO", "경상북도"])

    # 가명화: 같은 대장으로 두 번 돌리면 같은 사람이 같은 가명을 받는다
    with tempfile.TemporaryDirectory() as tmp:
        ledger = f"{tmp}/ledger.json"
        t1 = run_case("fixture1(가명화 1회차)", *FIX1, mode="pseudo", ledger_path=ledger)
        m1 = _json.load(open(ledger, encoding="utf-8"))
        assert "홍길동" in m1 and "김철수" in m1, f"가명대장에 실명 등재 누락: {m1}"
        t2 = run_case("fixture1(가명화 2회차·같은 대장)", *FIX1, mode="pseudo", ledger_path=ledger)
        m2 = _json.load(open(ledger, encoding="utf-8"))
        assert m1 == m2, f"재실행 시 가명 드리프트: {m1} → {m2}"
    print("ALL OK")
