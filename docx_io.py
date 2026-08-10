"""DOCX 비실명화 — OOXML 파트를 직접 다뤄 '숨은 자리'까지 지운다.

PDF와 위험이 다르다. PDF는 좌표를 못 찾으면 못 지우는 게 문제지만, DOCX는
**화면에 안 보이는 곳에 원문이 남는 것**이 문제다. 사용자는 본문에서 사라진 것을
보고 지워졌다고 믿는데 파일 안에는 그대로 있다. 그래서 아래를 전부 훑는다.

  본문·표·텍스트박스   document.xml (텍스트박스도 이 안에 w:p 로 들어 있다)
  머리글·바닥글        header*.xml · footer*.xml
  각주·미주            footnotes.xml · endnotes.xml
  주석                 comments.xml (+ 작성자 이름)
  변경이력             w:ins / w:del — **삭제된 텍스트가 w:delText 로 남아 있다**
  필드 코드            w:instrText (HYPERLINK "mailto:…" 가 여기 들어간다)
  하이퍼링크 대상      _rels/*.rels 의 Target="mailto:…"
  문서 속성            core.xml(작성자·최종수정자) · app.xml(회사·관리자) · custom.xml
  사람 목록            people.xml (주석 작성자)

python-docx를 쓰지 않는다. 본문 단락과 표까지는 다루지만 머리글·각주·주석·
텍스트박스·변경이력에는 손이 닿지 않아, 정작 위험한 자리를 통째로 놓친다.

값이 여러 run 에 쪼개지는 문제:
  Word는 서식·맞춤법 표시·rsid 때문에 한 단어를 여러 <w:t> 로 나눈다.
  '김철수'가 <w:t>김철</w:t><w:t>수</w:t> 로 저장되는 식이다. 그래서 노드 하나씩
  치환하면 대부분 못 찾는다. 단락 단위로 텍스트를 이어 붙여 찾고, 결과를 원래
  노드들에 도로 나눠 담는다(서식 보존).
"""
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
P_TAG = f"{{{W}}}p"
# 텍스트를 담는 노드. delText(변경이력의 삭제분)와 instrText(필드 코드)를 빠뜨리면
# 화면에 안 보이는 원문이 그대로 남는다.
TEXT_TAGS = {f"{{{W}}}t", f"{{{W}}}delText", f"{{{W}}}instrText"}
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

# 본문 텍스트가 들어 있는 파트
_TEXT_PART_RX = re.compile(
    r"^word/(document\d*\.xml|header\d*\.xml|footer\d*\.xml|footnotes\.xml"
    r"|endnotes\.xml|comments\.xml|commentsExtended\.xml)$")
# 사람 이름이 속성으로 들어가는 파트
_AUTHOR_ATTRS = ("author", "initials", "userId")
_META_PARTS = ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml")
# 문서 속성 중 사람·조직이 들어가는 자리 (태그 지역명 기준)
_META_CLEAR = {"creator", "lastModifiedBy", "title", "subject", "description",
               "keywords", "category", "contentStatus", "manager", "company",
               "TitlesOfParts", "Manager", "Company"}


def _register_namespaces(raw: bytes) -> None:
    """원본의 접두사를 그대로 유지한다.

    ElementTree는 등록하지 않은 네임스페이스에 ns0·ns1 같은 접두사를 붙인다.
    Word는 mc:Ignorable 이 가리키는 접두사가 사라지면 파일을 열지 못하므로,
    직렬화 전에 원본에 있던 선언을 전부 등록해야 한다.
    """
    for prefix, uri in re.findall(rb'xmlns:([A-Za-z0-9_.-]+)="([^"]+)"', raw):
        ET.register_namespace(prefix.decode(), uri.decode())


def _paragraph_groups(root):
    """텍스트 노드를 가장 가까운 w:p 로 묶는다 — 값이 run 을 넘나들어도 찾도록.

    텍스트박스는 run 안에 또 w:p 를 품는다. 가장 가까운 조상으로 묶으면
    바깥 단락과 텍스트박스 단락이 자연스럽게 분리된다.
    """
    parent = {child: p for p in root.iter() for child in p}
    groups, order = {}, []
    for node in root.iter():                      # iter()는 문서 순서를 지킨다
        if node.tag not in TEXT_TAGS:
            continue
        anc = parent.get(node)
        while anc is not None and anc.tag != P_TAG:
            anc = parent.get(anc)
        key = id(anc) if anc is not None else id(root)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(node)
    return [groups[k] for k in order]


def _redistribute(nodes, spans) -> None:
    """치환 결과를 원래 노드들에 도로 나눠 담는다.

    대체어는 '매치가 시작되는 노드'에 통째로 넣고, 매치에 덮인 나머지 구간은
    비운다. 이렇게 해야 손대지 않은 run 의 서식이 그대로 남는다.
    """
    pos = 0
    for node in nodes:
        text = node.text or ""
        start, end = pos, pos + len(text)
        pos = end
        if not text:
            continue
        out, cursor = [], start
        for ms, me, repl in spans:
            if me <= start or ms >= end:
                continue
            if ms > cursor:
                out.append(_slice(text, start, cursor, min(ms, end)))
            if start <= ms < end:                 # 이 노드에서 매치가 시작된다
                out.append(repl)
            cursor = max(cursor, min(me, end))
        # 매치에 통째로 덮인 노드는 비워야 한다. 여기서 건너뛰면 값이 세 개 이상
        # run 에 쪼개졌을 때 가운데 run 이 그대로 남는다('김'+'철'+'수' → '김OO철수').
        # Word가 서식·맞춤법 표시 때문에 한 단어를 잘게 쪼개는 것은 예외가 아니라 기본이다.
        if cursor < end:
            out.append(_slice(text, start, cursor, end))
        new = "".join(out)
        if new != text:
            node.text = new
            # 앞뒤 공백이 있으면 Word가 잘라 버린다 — 보존 지시를 붙인다
            if new != new.strip():
                node.set(XML_SPACE, "preserve")


def _slice(text: str, base: int, start: int, end: int) -> str:
    return text[start - base:end - base]


def _mask_part(raw: bytes, pattern, repl_of, canonical, matched: set) -> tuple[bytes, int]:
    """한 XML 파트의 텍스트를 치환하고 작성자 속성을 지운다."""
    _register_namespaces(raw)
    root = ET.fromstring(raw)
    hits = 0

    for nodes in _paragraph_groups(root):
        full = "".join(n.text or "" for n in nodes)
        if not full:
            continue
        spans = []
        if pattern:
            for m in pattern.finditer(full):
                spans.append((m.start(), m.end(), repl_of(m.group(0))))
                matched.add(canonical(m.group(0)))
        if not spans:
            continue
        hits += len(spans)
        _redistribute(nodes, spans)

    # 변경이력·주석의 작성자 이름은 속성에 들어 있어 텍스트 치환으로는 안 지워진다
    for el in root.iter():
        for attr in list(el.attrib):
            if attr.split("}")[-1] in _AUTHOR_ATTRS:
                el.set(attr, "작성자")

    body = ET.tostring(root, encoding="unicode")
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + body).encode("utf-8"), hits


def _scrub_meta(raw: bytes, name: str) -> bytes:
    """문서 속성에서 사람·조직 정보를 비운다.

    본문을 다 지워도 '작성자: 홍길동'이 파일 속성에 남으면 그 자체가 유출 채널이다.
    """
    if name == "docProps/custom.xml":
        # 사용자 지정 속성은 무엇이 들었는지 알 수 없다 — 통째로 비운다
        return (b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                b'<Properties xmlns="http://schemas.openxmlformats.org/'
                b'officeDocument/2006/custom-properties" xmlns:vt="http://'
                b'schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"/>')
    _register_namespaces(raw)
    root = ET.fromstring(raw)
    for el in root.iter():
        if el.tag.split("}")[-1] in _META_CLEAR:
            for child in list(el):
                el.remove(child)
            el.text = ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            + ET.tostring(root, encoding="unicode")).encode("utf-8")


def _mask_rels(raw: bytes, pattern, repl_of, canonical, matched: set) -> bytes:
    """하이퍼링크 대상의 메일 주소·이름을 치환한다 (Target="mailto:…")."""
    if not pattern:
        return raw

    def _sub(m):
        matched.add(canonical(m.group(0)))
        return repl_of(m.group(0))

    return pattern.sub(_sub, raw.decode("utf-8")).encode("utf-8")


def read_text(path: str) -> str:
    """탐지·내부용 추출을 위한 전체 텍스트. 숨은 자리까지 모두 포함한다."""
    chunks = []
    with zipfile.ZipFile(path) as z:
        for name in sorted(z.namelist()):
            if not _TEXT_PART_RX.match(name):
                continue
            raw = z.read(name)
            _register_namespaces(raw)
            root = ET.fromstring(raw)
            for nodes in _paragraph_groups(root):
                line = "".join(n.text or "" for n in nodes)
                if line.strip():
                    chunks.append(line)
    return "\n".join(chunks)


def inspect(path: str) -> dict:
    """마스킹으로 다룰 수 없는 위험 요소를 미리 알린다."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        has_revisions = any(
            b"<w:ins " in z.read(n) or b"<w:del " in z.read(n)
            for n in names if _TEXT_PART_RX.match(n))
    return {
        "images": [n for n in names if n.startswith("word/media/")],
        "embedded": [n for n in names if n.startswith(("word/embeddings/",
                                                       "word/attachedToolbars"))],
        "revisions": has_revisions,
    }


def _flexible(value: str) -> str:
    """글자 사이에 공백이 끼어도 찾는 형태 — '홍 길 동'.

    자간이 벌어진 표기는 정규화된 값('홍길동')이 원문에 문자열로 존재하지 않아
    리터럴 검색으로는 한 건도 못 지운다. PDF 쪽의 압축 매칭에 대응하는 폴백이다.
    """
    chars = [c for c in value if not c.isspace()]
    return r"[ \t]*".join(re.escape(c) for c in chars)


def _build_pattern(values, full_text: str):
    """리터럴 우선, 원문에 그대로 없는 값만 자간 허용으로 찾는다.

    자간 허용을 모든 값에 걸면 짧은 값이 엉뚱한 곳에서 걸린다. 리터럴이 있는 값은
    리터럴로만 찾고, 없는 값(=자간 표기에서 온 값)에만 폴백을 쓴다. 3자 미만은
    우연 일치 위험이 커서 폴백 대상에서 뺀다(PDF 쪽과 같은 기준).
    """
    literal = [v for v in values if v in full_text]
    spaced = [v for v in values
              if v not in full_text and len(re.sub(r"\s+", "", v)) >= 3]
    parts = [re.escape(v) for v in sorted(literal, key=len, reverse=True)]
    parts += [_flexible(v) for v in sorted(spaced, key=len, reverse=True)]
    return re.compile("|".join(parts)) if parts else None


def mask_docx(src: str, dst: str, targets, repl_for, full_text: str = "") -> dict:
    """DOCX 비실명화. targets 는 detect() 결과, repl_for(label, value) → 대체 문자열."""
    repl_map = {v: repl_for(label, v) for label, v in targets}
    # 자간 표기로 찾은 매치는 값과 글자가 달라진다('홍 길 동' vs '홍길동').
    # 공백을 지운 형태를 열쇠로 삼아 대체어와 원래 값을 되찾는다.
    by_compact = {re.sub(r"\s+", "", v): (v, r) for v, r in repl_map.items()}
    pattern = _build_pattern(list(repl_map), full_text or read_text(src))

    # 리터럴로 먼저 찾는다. 공백만 다른 값('과학기술인 공제회'/'과학기술인공제회')은
    # 압축 열쇠가 같아 사전에서 충돌한다 — 압축만 쓰면 한쪽이 '치환 안 됨'으로 잘못
    # 보고되어 exit 1 이 난다(실측).
    def repl_of(text: str) -> str:
        if text in repl_map:
            return repl_map[text]
        hit = by_compact.get(re.sub(r"\s+", "", text))
        return hit[1] if hit else text

    def canonical(text: str) -> str:
        if text in repl_map:
            return text
        hit = by_compact.get(re.sub(r"\s+", "", text))
        return hit[0] if hit else text

    info = inspect(src)
    warnings = []
    if info["revisions"]:
        warnings.append("변경이력(수정 추적)이 있다 — 삭제된 텍스트까지 함께 가렸지만, "
                        "배포 전에 Word에서 '변경 내용 모두 적용'으로 이력을 없애는 것이 안전하다.")
    if info["images"]:
        warnings.append(f"그림 {len(info['images'])}개가 들어 있다 — 그림 속 글자는 "
                        f"가려지지 않는다. 스캔 이미지가 삽입된 문서면 육안 확인 필수.")
    if info["embedded"]:
        warnings.append(f"임베디드 개체 {len(info['embedded'])}개(엑셀 등)가 들어 있다 — "
                        f"그 안의 내용은 가려지지 않는다.")

    hits = 0
    matched: set[str] = set()
    with zipfile.ZipFile(src) as zin, \
         zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            raw = zin.read(item.filename)
            name = item.filename
            if _TEXT_PART_RX.match(name):
                raw, n = _mask_part(raw, pattern, repl_of, canonical, matched)
                hits += n
            elif name in _META_PARTS:
                raw = _scrub_meta(raw, name)
            elif name == "word/people.xml":
                # 주석 작성자 목록 — 이름은 속성에 있어 텍스트 치환 대신 속성 소거로 지운다
                raw, _ = _mask_part(raw, None, repl_of, canonical, matched)
            elif name.endswith(".rels"):
                raw = _mask_rels(raw, pattern, repl_of, canonical, matched)
            zout.writestr(item, raw)

    # 탐지는 됐는데 한 번도 치환되지 않은 값 — 단락 경계를 넘어선 값 등
    unmapped = [f"{label}:{value}" for label, value in targets if value not in matched]
    return {"boxes_applied": hits, "unmapped": unmapped, "warnings": warnings}


def scan_residual(path: str, targets) -> list:
    """저장본을 다시 열어 원문이 남아 있는지 검사한다.

    두 가지로 본다. 파트 원문(raw)만 보면 run 에 쪼개진 값을 놓치고, 단락 텍스트만
    보면 속성(하이퍼링크 대상·작성자)에 남은 값을 놓친다.
    """
    residual = []
    with zipfile.ZipFile(path) as z:
        raws = {n: z.read(n).decode("utf-8", "ignore") for n in z.namelist()
                if n.endswith((".xml", ".rels"))}
    joined_raw = "\n".join(raws.values())
    joined_text = read_text(path)
    for label, value in targets:
        if value in joined_raw or value in joined_text:
            residual.append(f"{label}:{value}")
    return residual


def is_docx(path: str) -> bool:
    if Path(path).suffix.lower() != ".docx":
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return "word/document.xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


# 처리할 수 있는 형식은 둘뿐이다. 나머지는 **차단 목록이 아니라 허용 목록**으로 막는다 —
# 차단 목록은 빠뜨린 확장자(.xls 를 빠뜨렸다)가 그대로 통과해 라이브러리 트레이스백을
# 사용자에게 던진다. 개인정보 도구에서 정체불명의 실패는 그 자체가 위험이다.
SUPPORTED = {".pdf", ".docx"}
_ADVICE = {
    ".doc": "Word에서 .docx로 저장한 뒤 다시 시도할 것 "
            "(변환본에도 원문이 남으므로 처리 후 변환본을 지울 것).",
    ".hwp": "PDF로 내보낸 뒤 처리할 것.",
    ".hwpx": "PDF로 내보낸 뒤 처리할 것.",
    ".xls": "PDF로 내보낸 뒤 처리할 것 (숨겨진 시트·피벗 캐시는 PDF 변환에서 사라진다).",
    ".xlsx": "PDF로 내보낸 뒤 처리할 것 (숨겨진 시트·피벗 캐시는 PDF 변환에서 사라진다).",
    ".ppt": "PDF로 내보낸 뒤 처리할 것 (슬라이드 노트는 PDF 변환에서 사라진다).",
    ".pptx": "PDF로 내보낸 뒤 처리할 것 (슬라이드 노트는 PDF 변환에서 사라진다).",
    ".msg": "본문과 첨부를 각각 꺼내 PDF로 저장한 뒤 처리할 것.",
    ".eml": "본문과 첨부를 각각 꺼내 PDF로 저장한 뒤 처리할 것.",
}


def assert_supported(path: str) -> None:
    """지원하지 않는 형식을 조용히 실패하지 않고 분명히 막는다."""
    suffix = Path(path).suffix.lower()
    if suffix in SUPPORTED:
        return
    advice = _ADVICE.get(suffix, "외부용은 PDF와 DOCX만 처리한다.")
    raise SystemExit(f"{suffix or '확장자 없는 파일'}은 지원하지 않는다 — {advice}")
