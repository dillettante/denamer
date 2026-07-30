#!/usr/bin/env python3
"""denamer 내부용 — LLM에 물어보려고 가린 뒤, 답변을 원문으로 되돌린다.

외부용(denamer_out.py)과 목적이 정반대다.

    외부용  밖으로 내보내는 PDF · 비가역 · 매핑표 없음 · 사람이 읽는 대체어(김OO·A사)
    내부용  안에서 쓰는 텍스트 · 가역   · 매핑표 필수 · 복원되는 표기

표기 방식(--style):
    token (기본)  [[PERSON_01]]   기계가 읽는 토큰. 원문과 충돌할 일이 없어 가장 안전하다.
    alias         A · A사 · 사건A  외부용 가명화와 같은 표기. 사람·LLM이 읽기 자연스럽다.

**익명화(김OO) 표기는 내부용에 두지 않는다.** 김철수와 김민수가 모두 '김OO'가 되어
복원이 원리상 불가능하기 때문이다. 내부용은 되돌아오는 표기만 제공한다.

빌드를 나눈 이유는 안전이다. 한 도구가 둘을 겸하면 '복원용 매핑표를 산출물과 함께
보내는' 사고가 언젠가 난다. 외부용에는 복원 코드 자체가 없어 그 사고가 불가능하다.

    ┌ 마스킹 ─ 문서.pdf → 문서_내부용.txt + 문서_복원키(원문포함).json
    └ 복원   ─ LLM답변.txt + 복원키.json → 원문이 되살아난 답변

**복원키에는 원문 개인정보가 그대로 들어 있다. 외부 전달 금지.**
"""
import json
import os
import re
import sys

from detect import LABEL_NAMES, SKIPPABLE, detect, load_word_list
from ledger import Ledger

TOKEN_RX = re.compile(r"\[\[([A-Z_]+_\d{2,})\]\]")

# 복원키 파일명에 붙이는 경고 — 파일 목록만 봐도 위험을 알 수 있어야 한다
KEY_SUFFIX = "_복원키(원문포함).json"

STYLES = ("token", "alias")


def _read_text(path: str) -> str:
    """PDF·TXT·MD를 텍스트로 읽는다. PDF는 텍스트 레이어만 쓴다(내부용은 OCR 없음)."""
    if path.lower().endswith(".pdf"):
        import fitz
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)
        if not text.strip():
            raise SystemExit(
                "텍스트 레이어가 없는 스캔 PDF입니다 — 내부용은 OCR을 하지 않습니다. "
                "외부용(denamer_out.py)으로 OCR을 먼저 돌리거나 텍스트를 직접 주세요.")
        return text
    with open(path, encoding="utf-8") as f:
        return f.read()


def _kind_of(label: str) -> str:
    return "PERSON" if label in ("NAME", "PERSON") else label


def _mark_rx(mark: str) -> str:
    """가명 표기의 경계 규칙 — 마스킹과 복원이 **반드시 같은 규칙**을 써야 한다.

    오른쪽은 한글을 허용한다. 한국어에서는 표기 뒤에 조사가 바로 붙기 때문이다
    ("A사은", "전화A로"). 한글을 막으면 복원이 통째로 실패한다(실측).
    대신 그 때문에 원문의 'A안'도 표기 'A'와 겹치게 되므로, 겹침 검사(_free_alias)가
    같은 규칙으로 판정해 겹치는 표기를 애초에 쓰지 않는다.
    """
    return rf"(?<![A-Za-z0-9가-힣]){re.escape(mark)}(?![A-Za-z0-9])"


def _standalone(mark: str, text: str) -> bool:
    return bool(re.search(_mark_rx(mark), text))


def _free_alias(ledger: Ledger, kind: str, value: str, text: str) -> str:
    """원문에 이미 등장하는 표기는 가명으로 쓰지 않는다.

    가명(alias)은 토큰과 달리 맨몸 문자열이라, 하필 원문에 'A'가 있으면 복원 단계에서
    그 'A'까지 실명으로 바뀐다. 대장에서 받은 가명이 원문에 독립 단어로 있으면
    접미 숫자를 붙여 비켜 간다.
    """
    alias = ledger.alias(kind, value)
    if not _standalone(alias, text):
        return alias
    for n in range(2, 100):
        cand = f"{alias}{n}"
        if not _standalone(cand, text):
            ledger.maps[kind][value] = cand      # 대장에도 실제 쓴 표기를 남긴다
            return cand
    raise SystemExit(f"가명 표기를 만들 수 없습니다({alias}) — --style token 을 쓰세요")


def mask(text: str, *, style: str = "token", ledger: Ledger | None = None,
         skip=(), extra_names=(), excluded_names=()) -> dict:
    """원문 → 마스킹된 텍스트 + 복원 매핑표.

    같은 값은 항상 같은 표기를 받고, 서로 다른 값은 절대 같은 표기를 받지 않는다.
    (V2 검증에서 '첫 사람 주민번호가 둘째 사람 번호로 복원'되는 오염이 있었는데,
     같은 유형의 값 여럿에 토큰 하나를 공유시킨 것이 원인이었다.)
    """
    if style not in STYLES:
        raise ValueError(f"style은 {'|'.join(STYLES)}: {style}")
    targets = detect(text, skip=skip, extra_names=extra_names,
                     excluded_names=excluded_names)
    if not targets:
        return {"masked": text, "token_map": {}, "collisions": [], "style": style}

    # 긴 값부터 치환해야 짧은 값이 긴 값의 일부를 먼저 먹지 않는다
    targets.sort(key=lambda t: -len(t[1]))
    if style == "alias" and ledger is None:
        ledger = Ledger(None)

    counters: dict[str, int] = {}
    token_map: dict[str, dict] = {}
    value_to_mark: dict[str, str] = {}
    for label, value in targets:
        kind = _kind_of(label)
        if style == "alias":
            mark = _free_alias(ledger, kind, value, text)
        else:
            counters[kind] = counters.get(kind, 0) + 1
            mark = f"[[{kind}_{counters[kind]:02d}]]"
        value_to_mark[value] = mark
        token_map[mark] = {"original": value, "type": LABEL_NAMES.get(label, label)}

    # 원문에 표기와 같은 문자열이 이미 있으면 복원 때 뒤섞인다 — 조용히 넘기지 않는다
    collisions = sorted(m for m in token_map if _standalone(m, text))

    pattern = re.compile("|".join(re.escape(v) for _, v in targets))
    masked = pattern.sub(lambda m: value_to_mark[m.group(0)], text)
    return {"masked": masked, "token_map": token_map,
            "collisions": collisions, "style": style}


def restore(text: str, token_map: dict) -> dict:
    """표기가 박힌 텍스트(주로 LLM 답변) → 원문 복원.

    토큰과 가명을 모두 받는다. 가명은 맨몸 문자열이라 단어 경계를 요구해
    'A'가 'API'의 앞토막까지 바꾸지 않게 한다.
    """
    replaced = 0
    if token_map:
        # 긴 표기부터 — 'A'가 'A사'의 앞토막을 먼저 먹지 않게 한다
        parts = [re.escape(m) if m.startswith("[[") else _mark_rx(m)
                 for m in sorted(token_map, key=len, reverse=True)]
        rx = re.compile("|".join(parts))

        def _sub(m):
            nonlocal replaced
            e = token_map.get(m.group(0))
            if e is None:
                return m.group(0)
            replaced += 1
            return e["original"] if isinstance(e, dict) else e

        text = rx.sub(_sub, text)

    unresolved = sorted(set(TOKEN_RX.findall(text)))
    return {"restored": text, "replaced": replaced, "unresolved": unresolved}


def _cmd_mask(args) -> int:
    text = _read_text(args.input)
    skip = [s.strip().upper() for s in args.skip.split(",") if s.strip()]
    unknown = [s for s in skip if s not in SKIPPABLE]
    if unknown:
        raise SystemExit(f"--skip: 알 수 없는 유형 {unknown} (가능: {','.join(SKIPPABLE)})")

    ledger = Ledger(args.ledger) if args.style == "alias" else None
    result = mask(text, style=args.style, ledger=ledger, skip=skip,
                  extra_names=load_word_list(args.names),
                  excluded_names=load_word_list(args.not_names))
    if ledger is not None and args.ledger:
        ledger.save()

    stem = os.path.splitext(args.input)[0]
    out_path = args.output or f"{stem}_내부용.txt"
    key_path = args.key or f"{stem}{KEY_SUFFIX}"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result["masked"])
    with open(key_path, "w", encoding="utf-8") as f:
        json.dump(result["token_map"], f, ensure_ascii=False, indent=2)

    # 마스킹 결과에 원문이 남았는지 즉시 재검사한다 — 탐지한 값에 한해서지만,
    # 치환 누락(정규식 이스케이프 사고 등)은 여기서 반드시 걸린다
    residual = [m for m, e in result["token_map"].items() if e["original"] in result["masked"]]

    print(json.dumps({
        "표기": result["style"],
        "내부용_파일": out_path,
        "복원키": key_path,
        "탐지": len(result["token_map"]),
        "유형별": sorted({e["type"] for e in result["token_map"].values()}),
        "잔존": residual,                   # 비어야 정상
        "표기충돌": result["collisions"],    # 비어야 정상
    }, ensure_ascii=False, indent=2))
    print(f"\n※ {os.path.basename(key_path)} 에는 원문 개인정보가 들어 있습니다. "
          f"외부로 보내지 마세요.", file=sys.stderr)

    if residual or result["collisions"]:
        print("FAIL: 잔존 또는 표기 충돌 — 내부용 파일을 그대로 쓰지 말 것", file=sys.stderr)
        return 1
    return 0


def _cmd_restore(args) -> int:
    with open(args.key, encoding="utf-8") as f:
        token_map = json.load(f)
    with open(args.input, encoding="utf-8") as f:
        text = f.read()
    result = restore(text, token_map)
    out_path = args.output or f"{os.path.splitext(args.input)[0]}_복원.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result["restored"])
    print(json.dumps({
        "복원_파일": out_path,
        "치환": result["replaced"],
        "미해결_토큰": result["unresolved"],   # 비어야 정상
    }, ensure_ascii=False, indent=2))
    return 1 if result["unresolved"] else 0


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(
        description="denamer 내부용 — LLM 질의용 마스킹과 답변 복원(가역)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mask", help="원문 → 내부용 텍스트 + 복원키")
    m.add_argument("input", help="PDF 또는 텍스트 파일")
    m.add_argument("-o", "--output", default=None, help="내부용 텍스트 경로")
    m.add_argument("-k", "--key", default=None, help="복원키 JSON 경로 (원문 포함)")
    m.add_argument("--style", choices=STYLES, default="token",
                   help="token=[[PERSON_01]](기본·가장 안전) / alias=A·A사·사건A(읽기 쉬움)")
    m.add_argument("--ledger", default=None,
                   help="alias 표기의 가명 대장 경로. 여러 문서에서 같은 사람이 같은 가명을 "
                        "받게 한다. 실명 포함 — 로컬 보관 필수")
    m.add_argument("--skip", default="", help=f"끌 유형(쉼표 구분). 가능: {','.join(SKIPPABLE)}")
    m.add_argument("--names", default=None, help="사용자 사전 — 강제 마스킹할 이름")
    m.add_argument("--not-names", dest="not_names", default=None,
                   help="사용자 사전 — 제외할 이름 오탐")
    m.set_defaults(func=_cmd_mask)

    r = sub.add_parser("restore", help="LLM 답변 + 복원키 → 원문 복원")
    r.add_argument("input", help="표기가 남아 있는 텍스트 파일(LLM 답변)")
    r.add_argument("-k", "--key", required=True, help="복원키 JSON 경로")
    r.add_argument("-o", "--output", default=None, help="복원 결과 경로")
    r.set_defaults(func=_cmd_restore)

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
