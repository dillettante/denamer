#!/usr/bin/env python3
"""denamer 질의판 — LLM에 물어보려고 가린 뒤, 답변을 원문으로 되돌린다.

반출판(denamer_out.py)과 목적이 정반대다.

    반출판  밖으로 내보내는 PDF · 비가역 · 매핑표 없음 · 사람이 읽는 대체어(김OO·A사)
    질의판  안에서 쓰는 텍스트 · 가역   · 매핑표 필수 · 기계가 읽는 토큰([[PERSON_01]])

빌드를 나눈 이유는 안전이다. 한 도구가 둘을 겸하면 '복원용 매핑표를 산출물과 함께
보내는' 사고가 언젠가 난다. 반출판에는 복원 코드 자체가 없어 그 사고가 불가능하다.

    ┌ 마스킹 ─ 문서.pdf → 문서_질의용.txt + 문서_복원키(원문포함).json
    └ 복원   ─ LLM답변.txt + 복원키.json → 원문이 되살아난 답변

**복원키에는 원문 개인정보가 그대로 들어 있다. 외부 전달 금지.**
"""
import json
import os
import re
import sys

from detect import LABEL_NAMES, SKIPPABLE, detect, load_word_list

TOKEN_RX = re.compile(r"\[\[([A-Z_]+_\d{2,})\]\]")

# 복원키 파일명에 붙이는 경고 — 파일 목록만 봐도 위험을 알 수 있어야 한다
KEY_SUFFIX = "_복원키(원문포함).json"


def _read_text(path: str) -> str:
    """PDF·TXT·MD를 텍스트로 읽는다. PDF는 텍스트 레이어만 쓴다(질의용이므로 OCR 없음)."""
    if path.lower().endswith(".pdf"):
        import fitz
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)
        if not text.strip():
            raise SystemExit(
                "텍스트 레이어가 없는 스캔 PDF입니다 — 질의판은 OCR을 하지 않습니다. "
                "반출판(denamer_out.py)으로 OCR을 먼저 돌리거나 텍스트를 직접 주세요.")
        return text
    with open(path, encoding="utf-8") as f:
        return f.read()


def mask(text: str, *, skip=(), extra_names=(), excluded_names=()) -> dict:
    """원문 → 토큰이 박힌 텍스트 + 복원 매핑표.

    같은 값은 항상 같은 토큰을 받고, 서로 다른 값은 절대 같은 토큰을 받지 않는다.
    (V2 검증에서 '첫 사람 주민번호가 둘째 사람 번호로 복원'되는 오염이 있었는데,
     같은 유형의 값 여럿에 토큰 하나를 공유시킨 것이 원인이었다.)
    """
    targets = detect(text, skip=skip, extra_names=extra_names,
                     excluded_names=excluded_names)
    if not targets:
        return {"masked": text, "token_map": {}, "collisions": []}

    # 긴 값부터 치환해야 짧은 값이 긴 값의 일부를 먼저 먹지 않는다
    targets.sort(key=lambda t: -len(t[1]))

    counters: dict[str, int] = {}
    token_map: dict[str, dict] = {}
    value_to_token: dict[str, str] = {}
    for label, value in targets:
        kind = "PERSON" if label in ("NAME", "PERSON") else label
        counters[kind] = counters.get(kind, 0) + 1
        token = f"{kind}_{counters[kind]:02d}"
        value_to_token[value] = token
        token_map[token] = {"original": value, "type": LABEL_NAMES.get(label, label)}

    # 원문에 토큰 꼴 문자열이 이미 있으면 복원 때 뒤섞인다 — 조용히 넘기지 않는다
    collisions = sorted(set(TOKEN_RX.findall(text)) & set(token_map))

    pattern = re.compile("|".join(re.escape(v) for _, v in targets))
    masked = pattern.sub(lambda m: f"[[{value_to_token[m.group(0)]}]]", text)
    return {"masked": masked, "token_map": token_map, "collisions": collisions}


def restore(text: str, token_map: dict) -> dict:
    """토큰이 박힌 텍스트(주로 LLM 답변) → 원문 복원."""
    replaced = 0

    def _sub(m):
        nonlocal replaced
        entry = token_map.get(m.group(1))
        if entry is None:
            return m.group(0)
        replaced += 1
        return entry["original"] if isinstance(entry, dict) else entry

    out = TOKEN_RX.sub(_sub, text)
    unresolved = sorted(set(TOKEN_RX.findall(out)))
    return {"restored": out, "replaced": replaced, "unresolved": unresolved}


def _cmd_mask(args) -> int:
    text = _read_text(args.input)
    skip = [s.strip().upper() for s in args.skip.split(",") if s.strip()]
    unknown = [s for s in skip if s not in SKIPPABLE]
    if unknown:
        raise SystemExit(f"--skip: 알 수 없는 유형 {unknown} (가능: {','.join(SKIPPABLE)})")

    result = mask(text, skip=skip,
                  extra_names=load_word_list(args.names),
                  excluded_names=load_word_list(args.not_names))

    stem = os.path.splitext(args.input)[0]
    out_path = args.output or f"{stem}_질의용.txt"
    key_path = args.key or f"{stem}{KEY_SUFFIX}"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(result["masked"])
    with open(key_path, "w", encoding="utf-8") as f:
        json.dump(result["token_map"], f, ensure_ascii=False, indent=2)

    # 마스킹 결과에 원문이 남았는지 즉시 재검사한다 — 탐지한 값에 한해서지만,
    # 치환 누락(정규식 이스케이프 사고 등)은 여기서 반드시 걸린다
    residual = [t for t, e in result["token_map"].items() if e["original"] in result["masked"]]

    print(json.dumps({
        "질의용_파일": out_path,
        "복원키": key_path,
        "탐지": len(result["token_map"]),
        "유형별": sorted({e["type"] for e in result["token_map"].values()}),
        "잔존": residual,              # 비어야 정상
        "토큰충돌": result["collisions"],   # 비어야 정상
    }, ensure_ascii=False, indent=2))
    print(f"\n※ {os.path.basename(key_path)} 에는 원문 개인정보가 들어 있습니다. "
          f"외부로 보내지 마세요.", file=sys.stderr)

    if residual or result["collisions"]:
        print("FAIL: 잔존 또는 토큰 충돌 — 질의용 파일을 그대로 쓰지 말 것", file=sys.stderr)
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
        description="denamer 질의판 — LLM 질의용 마스킹과 답변 복원(가역)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mask", help="원문 → 질의용 텍스트 + 복원키")
    m.add_argument("input", help="PDF 또는 텍스트 파일")
    m.add_argument("-o", "--output", default=None, help="질의용 텍스트 경로")
    m.add_argument("-k", "--key", default=None, help="복원키 JSON 경로 (원문 포함)")
    m.add_argument("--skip", default="", help=f"끌 유형(쉼표 구분). 가능: {','.join(SKIPPABLE)}")
    m.add_argument("--names", default=None, help="사용자 사전 — 강제 마스킹할 이름")
    m.add_argument("--not-names", dest="not_names", default=None,
                   help="사용자 사전 — 제외할 이름 오탐")
    m.set_defaults(func=_cmd_mask)

    r = sub.add_parser("restore", help="LLM 답변 + 복원키 → 원문 복원")
    r.add_argument("input", help="토큰이 남아 있는 텍스트 파일(LLM 답변)")
    r.add_argument("-k", "--key", required=True, help="복원키 JSON 경로")
    r.add_argument("-o", "--output", default=None, help="복원 결과 경로")
    r.set_defaults(func=_cmd_restore)

    args = ap.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
