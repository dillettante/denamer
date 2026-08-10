#!/usr/bin/env python3
"""denamer 외부용 — 밖으로 내보내는 PDF를 비가역으로 비실명화한다.

이 빌드에는 **복원 기능이 없다.** 복원용 매핑표를 산출물과 함께 보내는 사고가
구조적으로 불가능하도록 진입점을 나눈 것이다(질의용은 denamer_in.py).

  1. 탐지: detect.py (정규식 + 이름엔진 + ko-pii)
  2. 제거: 값을 페이지에서 전수 검색해 모든 출현을 redact.
           오프셋→좌표 역매핑을 쓰지 않으므로 그 계열의 좌표 어긋남 버그가 불가능하다.
  3. 검증: 저장본 재오픈 → 재검색. 매핑 실패·잔존 시 exit 1.

검은 박스는 '덮기'가 아니다 — apply_redactions가 텍스트 객체와 스캔 이미지 픽셀을
문서에서 삭제한 뒤 그리는 표시다. 저장본에서 원문 복원은 불가능하다.
"""
import json
import os
import re
import sys

import fitz  # PyMuPDF

import docx_io

from detect import LABEL_NAMES, REGIONS, SKIPPABLE, detect, load_word_list
from ledger import Ledger


# ── 마스킹 정책 ────────────────────────────────────────────────
def _mask_name(v: str) -> str:
    """홍길동 → 홍OO (성 보존, 이름만 가림)."""
    return v[0] + "O" * (len(v) - 1)


_REGION_TOKEN = re.compile(rf"^(?:{REGIONS})$")


def _mask_address(v: str) -> str:
    """시·도로 확인된 첫 토큰만 보존하고 나머지는 글자수만큼 O.

    첫 토큰을 무조건 보존하면 안 된다 — 값이 시·도부터 시작하지 않을 때
    (라벨 규칙이 중간부터 잡거나 OCR이 앞부분을 흘렸을 때) 동(洞) 이름이
    그대로 남는다. 실측: 스캔 판결문에서 '연향동'이 보존돼 잔존으로 걸렸다.
    동 단위는 개인 식별에 기여하므로 익명화 문서에 남으면 안 된다.
    """
    tokens = v.split()
    if len(tokens) > 1 and _REGION_TOKEN.match(tokens[0]):
        return " ".join([tokens[0]] + ["O" * len(t) for t in tokens[1:]])
    return " ".join("O" * len(t) for t in tokens) if tokens else v


def _replacement_for(label: str, value: str, mode: str, ledger: Ledger) -> str | None:
    """None이면 검은 박스(완전 삭제 표시), 문자열이면 흰 바탕에 대체 텍스트.

    법인명·사건번호는 익명화 모드에서도 가명으로 바꾼다. '○○화학'식 부분 가림은
    읽기 어렵고, 같은 법인이 문서 안에서 같은 이름으로 불려야 문장이 성립하기
    때문이다. 다만 익명화 모드에서는 대장을 파일로 남기지 않는다(문서 내 일관까지).
    """
    if label in ("NAME", "PERSON"):
        return ledger.alias("PERSON", value) if mode == "pseudo" else _mask_name(value)
    if label == "ORG":
        return ledger.alias("ORG", value)
    if label == "CASE":
        return ledger.alias("CASE", value)
    if label == "ADDRESS":
        return _mask_address(value)
    if label == "ADDR_DETAIL":
        # 괄호 상세주소는 구조만 남기고 전부 가림: "(송파동, 미성아파트)" → "(OOO)"
        return "(OOO)"
    return None   # 번호류(주민·계좌·전화·카드…)는 부분 보존 없이 전부 삭제


# ── 좌표 확보 ──────────────────────────────────────────────────
def fragments(value: str) -> list[str]:
    """검색 단위: 값이 여러 줄이면 줄 조각별로(search_for는 개행을 못 넘는다).
    각 조각은 원문 + 공백제거 변형을 함께 시도한다."""
    frags = []
    for line in value.splitlines():
        line = line.strip()
        if len(line) < 2:
            continue
        if line.isdigit() and len(line) < 4:
            continue   # '49'·'2023' 같은 짧은 숫자 조각의 전역 검색 = 과잉제거 폭탄
        frags.append(line)
        compact = re.sub(r"\s+", "", line)
        if compact != line and len(compact) >= 2:
            frags.append(compact)
    return frags or [value]


def _word_index(page) -> tuple[list, str, list[int]]:
    """쪽의 단어박스 인덱스: (단어목록, 압축문자열, 압축문자→단어번호 매핑).

    OCR 텍스트는 같은 값도 쪽마다 자간이 달라 리터럴 search_for가 놓친다.
    압축(공백 제거) 문자열에서 값을 찾아 해당 단어들의 박스를 돌려주는 폴백용.
    한 쪽 안에서 get_text("words") 하나만 쓰므로 추출오프셋↔좌표 교차 매핑
    버그가 생길 수 없다.
    """
    words = page.get_text("words")   # (x0,y0,x1,y1, 단어, block, line, word_no)
    parts, char_to_word = [], []
    for i, w in enumerate(words):
        t = re.sub(r"\s+", "", w[4])
        parts.append(t)
        char_to_word.extend([i] * len(t))
    return words, "".join(parts), char_to_word


def _word_fallback_rects(index, value: str) -> list:
    """압축문자열 매칭으로 값의 모든 출현 단어박스를 수집."""
    words, compact, char_to_word = index
    needle = re.sub(r"\s+", "", value)
    # 짧은 값의 압축 매칭은 우연 일치 위험 — 단 한글 3자(이름)는 허용해야
    # "홍길 동"·"홍길⏎동"처럼 흩어진 실명 출현을 잡는다(실측 잔존 2건)
    min_len = 3 if any("가" <= ch <= "힣" for ch in needle) else 4
    if len(needle) < min_len:
        return []
    rects, pos = [], compact.find(needle)
    while pos != -1:
        for wi in sorted(set(char_to_word[pos:pos + len(needle)])):
            rects.append(fitz.Rect(words[wi][:4]))
        pos = compact.find(needle, pos + 1)
    return rects


def _covering_words(rect, words) -> list:
    """박스와 '실질적으로 겹치는'(50%↑) 단어들. 겹침 문턱이 낮으면 스치기만 한
    이웃 단어까지 삼킨다(실측: 30%에서 이름 박스가 옆 라벨 '변호사'의 끝 글자를 삼켰다)."""
    out = []
    for w in words:
        wr = fitz.Rect(w[:4])
        inter = wr & rect
        if not inter.is_empty and wr.get_area() > 0 and inter.get_area() / wr.get_area() > 0.5:
            out.append((round(wr.y0, 1), wr.x0, w[4], wr))
    out.sort()
    return out


def _expand_to_words(rect, words) -> "fitz.Rect":
    """히트 박스를 겹치는 단어박스까지 합쳐 확장.

    스캔본은 OCR 좌표가 인쇄 글리프와 어긋나 가장자리 글자가 삐져나오고,
    "1454718),"처럼 값 꼬리가 단어 중간에서 끝나기도 한다. 단어 전체를 덮어야
    안전하다.
    """
    r = fitz.Rect(rect)
    for _, _, _, wr in _covering_words(rect, words):
        r |= wr
    return r


# 조사 짝 — 앞이 받침이 있으면 왼쪽, 없으면 오른쪽
_JOSA_PAIRS = [("으로서", "로서"), ("으로써", "로써"), ("이라는", "라는"), ("으로", "로"),
               ("이라", "라"), ("이며", "며"), ("이고", "고"), ("이나", "나"),
               ("은", "는"), ("이", "가"), ("을", "를"), ("과", "와")]


def _has_batchim(s: str) -> bool:
    ch = s[-1]
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return False        # 라틴 문자·숫자는 모음으로 읽는 것이 보통(A 에이 · O 오)


def _swallowed_tail(rect, words, value: str) -> str:
    """확장된 박스가 값 뒤에 함께 삼킨 글자(대개 조사)를 돌려준다.

    이름을 조사까지 붙여 탐지하게 되면서("김철수는" → 김철수) 단어박스 확장이
    조사를 함께 지운다. 그대로 두면 "김OO에게 책임이 있다"가 "김OO 책임이 있다"가
    되어 법률 문서의 뜻이 달라진다. 그래서 삼킨 조사를 대체어 뒤에 다시 그린다.
    """
    text = re.sub(r"\s+", "", "".join(t for _, _, t, _ in _covering_words(rect, words)))
    compact = re.sub(r"\s+", "", value)
    i = text.find(compact)
    if i == -1:
        return ""
    tail = text[i + len(compact):]
    # 조사로 볼 수 있는 짧은 한글만 되살린다 — 그 외는 값의 일부일 수 있어 지운 채 둔다
    return tail if 1 <= len(tail) <= 3 and all("가" <= c <= "힣" for c in tail) else ""


def _with_tail(replacement: str, tail: str) -> str:
    """대체어에 맞춰 조사를 고른다 — 'A사을'이 아니라 'A사를'."""
    for hard, soft in _JOSA_PAIRS:
        if tail in (hard, soft):
            return replacement + (hard if _has_batchim(replacement) else soft)
    return replacement + tail


# 쪽당 이 글자수 미만이면 '사실상 이미지 문서'로 보고 OCR을 돌린다.
#
#   레이어가 조금이라도 있으면 OCR을 건너뛰던 이전 판단이 스캔 문서를 무검사로
#   통과시켰다. 실측(비실명 샘플): 10쪽 감정서가 729자(쪽당 73자)뿐인데 탐지 0건으로
#   'OK'가 나왔다 — 사용자는 개인정보가 없다고 믿게 된다.
#
#   문턱값은 실측한 분포에서 골랐다. 쪽당 글자수가 두 무리로 확연히 갈린다.
#     머리글·꼬리글만 있는 스캔본   62 · 73 · 133   (152쪽 감정서는 전 쪽이 정확히
#                                                  133자 — 매쪽 반복되는 상용구다)
#     본문이 실제로 있는 문서        606 · 1,499     (준비서면 · 서증)
#   그 사이인 300을 쓴다. 낮추면 스캔본을 놓치고, 크게 올리면 본문 있는 문서까지
#   불필요하게 OCR한다.
THIN_LAYER_PER_PAGE = 300


def _layer_density(doc, text: str) -> float:
    return len(text.strip()) / max(1, doc.page_count)


def _image_page_ratio(doc) -> float:
    """쪽 면적의 절반 이상을 이미지가 덮는 쪽의 비율.

    글자수만으로는 '짧은 문서'와 '이미지 문서'를 가를 수 없다. 1쪽짜리 판결문
    발췌는 정당하게 250자일 수 있고, 그것까지 OCR하면 원문 텍스트를 래스터로
    갈아 버려 오히려 품질이 떨어진다(실측: 픽스처가 OCR을 타면서 대체어가 깨졌다).
    스캔본은 쪽마다 큰 이미지가 깔려 있다는 점이 확실한 구분점이다.
    """
    hits = 0
    for page in doc:
        page_area = abs(page.rect)
        if not page_area:
            continue
        img_area = sum(abs(fitz.Rect(b["bbox"])) for b in page.get_text("dict")["blocks"]
                       if b.get("type") == 1)
        if img_area / page_area > 0.5:
            hits += 1
    return hits / max(1, doc.page_count)


def _run_ocr(in_path: str, *, force: bool) -> str:
    """스캔 PDF에 텍스트 레이어를 입힌 임시 PDF 경로.

    force=True면 --force-ocr로 전 쪽을 다시 읽는다. 레이어가 얇은 문서는 쪽마다
    도장·머리글 텍스트가 조금씩 있어서, --skip-text를 쓰면 그 쪽들을 건너뛰고
    본문은 그대로 못 읽는다.
    """
    import shutil
    import subprocess
    import tempfile
    if shutil.which("ocrmypdf") is None:
        raise SystemExit("스캔 PDF(텍스트 레이어 없음/부족) — ocrmypdf 설치 후 재시도 "
                         "(brew install ocrmypdf) 또는 OCR 선행 필요")
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False).name
    print("[denamer] 스캔 PDF 감지 — ocrmypdf 실행 중 (쪽수에 따라 수 분)", file=sys.stderr)
    subprocess.run(["ocrmypdf", "-l", "kor+eng",
                    "--force-ocr" if force else "--skip-text",
                    "--optimize", "0", in_path, tmp],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return tmp


# DOCX에는 검은 박스가 없다. 번호류는 고정 길이로 가려 글자수까지 숨긴다
# (길이를 남기면 '몇 자리 계좌인지'가 새고, 값마다 길이가 달라 대조가 쉬워진다).
FIXED_MASK = "■■■■"


def _docx_replacement(label: str, value: str, mode: str, ledger: Ledger) -> str:
    repl = _replacement_for(label, value, mode, ledger)
    return FIXED_MASK if repl is None else repl


def _redact_docx(in_path: str, out_path: str, mode: str, ledger_path: str | None, *,
                 skip, extra_names, excluded_names) -> dict:
    """DOCX 비실명화. 좌표가 아니라 OOXML 파트를 직접 고친다(docx_io 참조)."""
    ledger = Ledger(ledger_path if mode == "pseudo" else None)
    full_text = docx_io.read_text(in_path)
    if not full_text.strip():
        raise SystemExit("텍스트가 없는 DOCX — 내용이 그림뿐인지 확인 필요. "
                         "스캔 이미지를 넣은 문서라면 PDF로 내보낸 뒤 처리할 것.")

    targets = detect(full_text, skip=skip, extra_names=extra_names,
                     excluded_names=excluded_names)
    result = docx_io.mask_docx(in_path, out_path, targets,
                               lambda l, v: _docx_replacement(l, v, mode, ledger),
                               full_text)
    if mode == "pseudo":
        ledger.save()

    return {
        "format": "docx",
        "mode": mode,
        "ocr_applied": False,
        "ledger": ledger_path if mode == "pseudo" else None,
        "targets": len(targets),
        "boxes_applied": result["boxes_applied"],
        "unmapped": result["unmapped"],                     # 비어야 정상
        "residual": docx_io.scan_residual(out_path, targets),  # 비어야 정상
        "warnings": result["warnings"],
        "by_label": sorted({LABEL_NAMES.get(l, l) for l, _ in targets}),
        "persons": sorted(v for l, v in targets if l in ("PERSON", "NAME")),
        "orgs": sorted(v for l, v in targets if l == "ORG"),
    }


def redact(in_path: str, out_path: str, mode: str = "anon",
           ledger_path: str | None = None, *,
           skip=(), extra_names=(), excluded_names=(), no_ocr: bool = False) -> dict:
    """mode: 'anon' = 익명화(김OO·주소 부분보존) / 'pseudo' = 가명화(A·B·C…).

    PDF와 DOCX를 받는다. 형식마다 '지운다'의 의미가 다르다 — PDF는 좌표를 찾아
    apply_redactions로 객체를 삭제하고, DOCX는 XML 파트의 텍스트를 바꿔 쓴다.

    가명 대장은 pseudo 모드에서만 파일로 유지한다 — 같은 대장을 쓰는 문서끼리
    같은 사람이 항상 같은 가명을 받는다(문서 교차 시 인물 동일성 유지).
    ※ 대장엔 실명이 들어가므로 반드시 로컬에만 두고 산출물과 함께 배포 금지.
    """
    if mode not in ("anon", "pseudo"):
        raise ValueError(f"mode는 anon|pseudo: {mode}")
    if docx_io.is_docx(in_path):
        return _redact_docx(in_path, out_path, mode, ledger_path, skip=skip,
                            extra_names=extra_names, excluded_names=excluded_names)
    docx_io.assert_supported(in_path)   # .doc·.hwp·.xlsx·.pptx를 분명히 막는다

    # 익명화 모드에서도 법인·사건번호에는 가명이 필요하다. 다만 파일로는 남기지 않는다.
    ledger = Ledger(ledger_path if mode == "pseudo" else None)

    doc = fitz.open(in_path)
    full_text = "".join(page.get_text() for page in doc)
    warnings: list[str] = []
    ocr_applied = False
    ocr_tmp = None      # OCR 경유 시 임시 PDF — 비실명화 전 원문이므로 종료 시 삭제

    # 레이어가 없거나 '사실상 이미지'면 OCR을 돌린다. 레이어가 조금이라도 있으면
    # 건너뛰던 이전 판단이 스캔 문서를 통째로 무검사 통과시켰다(위 상수 주석 참조).
    density = _layer_density(doc, full_text)
    # 레이어가 아예 없으면 무조건, 있더라도 '얇으면서 이미지가 쪽을 덮는' 경우에 OCR한다
    thin = (not full_text.strip()
            or (density < THIN_LAYER_PER_PAGE and _image_page_ratio(doc) >= 0.5))
    if thin and not no_ocr:
        doc.close()
        ocr_tmp = _run_ocr(in_path, force=bool(full_text.strip()))
        doc = fitz.open(ocr_tmp)
        full_text = "".join(page.get_text() for page in doc)
        ocr_applied = True
        if not full_text.strip():
            raise SystemExit("OCR 후에도 텍스트 없음 — 이미지 품질 확인 필요")
    elif thin:
        warnings.append(f"텍스트 레이어가 얇다(쪽당 {density:.0f}자)인데 --no-ocr 로 "
                        f"OCR을 건너뛰었다 — 본문 대부분이 검사되지 않았다.")

    # OCR 경유 문서는 '조용히 실패'하기 쉽다. 탐지는 OCR이 읽어낸 글자에만 적용되고,
    # OCR이 놓친 글자는 스캔 이미지에 그대로 남는데 사후검증은 그것을 알지 못한다.
    # 리포트가 residual: [] 이라는 이유로 안전하다고 믿으면 안 된다.
    if ocr_applied:
        per_page = _layer_density(doc, full_text)
        warnings.append("OCR 경유 — 탐지는 OCR이 읽어낸 글자에만 적용된다. "
                        "OCR이 놓친 글자는 스캔 이미지에 그대로 남는다.")
        if per_page < 400:
            warnings.append(f"OCR 추출량이 적다(쪽당 {per_page:.0f}자) — 본문의 상당 부분을 "
                            f"읽지 못했을 수 있다. 원본 대조 육안 검토 없이 배포 금지.")

    targets = detect(full_text, skip=skip, extra_names=extra_names,
                     excluded_names=excluded_names)

    # 성능 가드: search_for는 비싸다(실측 64쪽×477타깃 = 5분 초과).
    # 쪽별 텍스트를 한 번만 뽑아 두고, 조각이 그 쪽에 실재할 때만 검색한다.
    page_texts = [page.get_text() for page in doc]
    compact_page_texts = [re.sub(r"\s+", "", t) for t in page_texts]
    word_indexes: dict[int, tuple] = {}
    unmapped: list[str] = []
    boxes = 0

    # 1단계: 히트를 수집만 한다. 바로 annot을 달면 같은 주소를 정규식·ko-pii가
    # 서로 다른 경계로 각각 잡았을 때 겹친 박스마다 대체어가 중복 삽입돼
    # "서울특별시 서울특별시 OOO…"처럼 뒤죽박죽이 된다(실측).
    page_jobs: dict[int, list[tuple]] = {}
    for label, value in targets:
        replacement = _replacement_for(label, value, mode, ledger)
        rects_total = 0
        for pno, (page, ptext) in enumerate(zip(doc, page_texts)):
            hit_rects = []
            for frag in fragments(value):
                if frag in ptext:
                    hit_rects.extend(page.search_for(frag))
            # 압축문자열에 값이 있으면 워드 폴백도 함께 — 리터럴이 일부 출현만
            # 커버할 수 있다(같은 쪽에 '홍길동'과 '홍길⏎동'이 공존하면 뒤엣것이 샜다)
            if re.sub(r"\s+", "", value) in compact_page_texts[pno]:
                if pno not in word_indexes:
                    word_indexes[pno] = _word_index(page)
                hit_rects.extend(_word_fallback_rects(word_indexes[pno], value))
            if hit_rects and pno not in word_indexes:
                word_indexes[pno] = _word_index(page)
            page_words = word_indexes[pno][0] if hit_rects else []
            for rect in hit_rects:
                # 겹침 50%↑ 단어박스로 확장 + 1pt 마진 (좌표 어긋남·꼬리 글자 대비)
                rect = _expand_to_words(rect, page_words) + (-1, -1, 1, 1)
                repl = replacement
                if repl is not None:
                    tail = _swallowed_tail(rect, page_words, value)
                    if tail:
                        repl = _with_tail(repl, tail)
                page_jobs.setdefault(pno, []).append((rect, repl, len(value)))
                rects_total += 1
        if rects_total == 0:
            unmapped.append(f"{label}:{value}")
        boxes += rects_total

    # 2단계-A: 같은 값의 조각 박스 봉합 — 긴 주소는 search_for가 한 줄을 여러
    # 조각으로 돌려주고, 조각마다 전체 대체문을 그리면 글자가 겹쳐 뒤죽박죽이 된다.
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
    # 완전 삭제가 우선한다(유출 > 과잉의 안전 방향).
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

    # 삭제가 끝난 자리에 대체 텍스트("김OO"·"A"·"A사")를 그룹당 1회 삽입.
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
    # 문서 정보와 XMP에 남으면 그게 유출 채널이다
    # 순서가 중요하다: XMP를 먼저 지우고 그 다음 문서정보를 비운다.
    #   거꾸로 하면 XMP가 있는 문서에서 소거가 무효가 된다 — set_metadata({})로 지운
    #   값이 XMP에서 되살아난다. 실측: OCR 경유 산출물에 author=실명이 그대로 남았다
    #   (ocrmypdf 산출물은 XMP를 항상 쓴다). 본문을 다 지워도 작성자가 남으면
    #   그 자체가 유출 채널이다.
    doc.del_xml_metadata()
    doc.set_metadata({})
    doc.save(out_path, garbage=4, deflate=True)
    doc.close()
    if ocr_tmp and os.path.exists(ocr_tmp):
        os.unlink(ocr_tmp)
    if mode == "pseudo":
        ledger.save()

    # ── 사후검증: 저장본 재오픈 → 재검색 ──
    saved = fitz.open(out_path)
    saved_text = "".join(page.get_text() for page in saved)
    leftover_meta = {k: v for k, v in (saved.metadata or {}).items()
                     if v and k not in ("format", "encryption")}
    saved.close()
    compact_saved = re.sub(r"\s+", "", saved_text)
    # 우리가 일부러 그려 넣은 대체어는 잔존이 아니다. 이걸 빼지 않으면 보존한
    # 시·도 토큰이 다른 값의 조각과 겹칠 때 거짓 실패가 난다.
    inserted = {r for jobs in merged_jobs.values() for _, r in jobs if r}
    inserted_compact = {re.sub(r"\s+", "", i) for i in inserted}
    residual = []
    for label, value in targets:
        if label in ("NAME", "PERSON"):
            # 이름은 독립 출현만 잔존으로 판정 — '인천광역시청' 속 '인천'을
            # 잔존으로 오인하면 영구 거짓 경보가 된다(실측).
            from detect import _standalone_occurrence
            hit = value in saved_text and _standalone_occurrence(saved_text, value)
        else:
            compact_value = re.sub(r"\s+", "", value)
            hit = (any(f in saved_text and not any(f in i for i in inserted)
                       for f in fragments(value))
                   or (compact_value in compact_saved
                       and not any(compact_value in i for i in inserted_compact)))
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
        "unmapped": unmapped,        # 비어야 정상
        "residual": residual,        # 비어야 정상
        "warnings": warnings,        # 있으면 반드시 읽을 것
        "by_label": sorted({LABEL_NAMES.get(l, l) for l, _ in targets}),
        # 이름·법인은 오탐 시 일반어가 통째로 칠해지므로 목록을 노출해 사람이 확인케 한다
        "persons": sorted(v for l, v in targets if l in ("PERSON", "NAME")),
        "orgs": sorted(v for l, v in targets if l == "ORG"),
    }


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="denamer 외부용 — 외부 제출·배포용 PDF·DOCX 비실명화(비가역)")
    ap.add_argument("input", help="PDF 또는 DOCX")
    ap.add_argument("output", nargs="?", default=None,
                    help="생략 시 원본 옆에 접미사 부기: 파일명_masked.<확장자> / _aliased.<확장자>")
    ap.add_argument("--mode", choices=["anon", "pseudo"], default="anon",
                    help="anon=김OO·주소 부분보존(기본) / pseudo=A·B·C 일관 가명")
    ap.add_argument("--ledger", default=None,
                    help="가명 대장 JSON 경로 (기본: 출력 폴더의 pseudonym_ledger.json). "
                         "같은 대장을 쓰는 문서끼리 가명이 일치한다. 실명 포함 — 로컬 보관 필수")
    ap.add_argument("--skip", default="",
                    help=f"끌 유형(쉼표 구분). 가능: {','.join(SKIPPABLE)}")
    ap.add_argument("--names", default=None,
                    help="사용자 사전 — 규칙이 놓치는 이름을 강제 마스킹(한 줄에 하나)")
    ap.add_argument("--not-names", dest="not_names", default=None,
                    help="사용자 사전 — 반복되는 이름 오탐을 제외(한 줄에 하나)")
    ap.add_argument("--no-ocr", action="store_true",
                    help="스캔 PDF에도 OCR을 돌리지 않는다. 본문 대부분이 검사되지 않으므로 "
                         "리포트에 경고가 붙는다")
    args = ap.parse_args()

    skip = [s.strip().upper() for s in args.skip.split(",") if s.strip()]
    unknown = [s for s in skip if s not in SKIPPABLE]
    if unknown:
        raise SystemExit(f"--skip: 알 수 없는 유형 {unknown} (가능: {','.join(SKIPPABLE)})")

    output = args.output
    if output is None:                        # 원본은 건드리지 않고 접미사본 생성
        stem, ext = os.path.splitext(args.input)
        output = f"{stem}_{'masked' if args.mode == 'anon' else 'aliased'}{ext}"
    ledger_path = args.ledger
    if args.mode == "pseudo" and not ledger_path:
        ledger_path = os.path.join(os.path.dirname(os.path.abspath(output)),
                                   "pseudonym_ledger.json")

    report = redact(args.input, output, mode=args.mode, ledger_path=ledger_path,
                    skip=skip,
                    extra_names=load_word_list(args.names),
                    excluded_names=load_word_list(args.not_names),
                    no_ocr=args.no_ocr)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    for w in report["warnings"]:
        print(f"경고: {w}", file=sys.stderr)
    if report["unmapped"] or report["residual"]:
        print("FAIL: 매핑 실패 또는 잔존 PII — 출력물을 신뢰하지 말 것", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
