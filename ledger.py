"""가명 대장 — 같은 대상에게 항상 같은 가명을 준다.

유형별로 가명 꼴을 나눈다. 한 풀에서 A·B·C를 돌려 쓰면 사람과 법인이 섞여
"A가 A사에 청구" 같은 읽기 어려운 문장이 된다.
    사람   홍길동      → A,   B,   C …
    법인   동방화학    → A사, B사, C사 …
    사건   2020가합123 → 사건A, 사건B …

대장 파일에는 실명↔가명 매핑이 들어간다. **산출물과 함께 전달하면 가명화가 무효**다.
그래서 익명화 모드에서는 파일로 저장하지 않는다(문서 내 일관까지만 유지).
"""
import json
import os

_ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# 대장에서 관리하는 유형과 가명 꼴
KINDS = ("PERSON", "ORG", "CASE")


def _code(n: int) -> str:
    """0→A, 25→Z, 26→AA … 엑셀 열 이름과 같은 방식."""
    out = ""
    while True:
        out = _ALPHA[n % 26] + out
        n = n // 26 - 1
        if n < 0:
            return out


def _format(kind: str, n: int) -> str:
    code = _code(n)
    if kind == "ORG":
        return f"{code}사"
    if kind == "CASE":
        return f"사건{code}"
    return code


class Ledger:
    """유형별 가명 대장. path가 None이면 메모리에만 두고 저장하지 않는다."""

    def __init__(self, path: str | None = None):
        self.path = path
        self.maps: dict[str, dict[str, str]] = {k: {} for k in KINDS}
        if path and os.path.exists(path):
            self._load(path)

    def _load(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        # 구 버전 대장은 {"홍길동": "A"} 평면 구조였다 — 사람 대장으로 읽어들인다
        if data and not any(k in data for k in KINDS):
            self.maps["PERSON"] = dict(data)
            return
        for kind in KINDS:
            self.maps[kind].update(data.get(kind) or {})

    def alias(self, kind: str, value: str) -> str:
        """가명 조회·부여. 이미 있으면 그 가명을 그대로 쓴다."""
        table = self.maps.setdefault(kind, {})
        if value not in table:
            table[value] = _format(kind, len(table))
        return table[value]

    def save(self) -> None:
        if not self.path:
            return
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.maps, f, ensure_ascii=False, indent=2)

    def is_empty(self) -> bool:
        return not any(self.maps.values())
