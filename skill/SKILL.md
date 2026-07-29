---
name: denamer
description: 한국어 문서 비실명화 — 반출판(밖으로 내보내는 PDF, 비가역: 익명화 김OO / 가명화 A·B·C)과 질의판(LLM에 물어보려고 가린 뒤 답변 복원, 가역)으로 나뉜다. 검은박스 덮기가 아니라 PyMuPDF apply_redactions로 텍스트·스캔픽셀을 진짜 삭제한다. 법인명은 A사, 사건번호는 사건A로 가명화. 트리거 - "비실명화", "익명화", "가명화", "개인정보 지워줘", "이 PDF 마스킹", "LLM에 물어보게 가려줘", "denamer", "redact this pdf". 스캔 PDF는 ocrmypdf 자동 전처리. 원본 불변.
---

# denamer — 한국어 문서 비실명화

엔진 위치 (기기별):
- Messi(맥북): `~/Library/CloudStorage/GoogleDrive-redacted@gmail.com/내 드라이브/REDACTED_ORG/일반/[015]디지털TF/비실명화/denamer/`
  (Google Drive 폴더라 venv는 밖에 둔다 → `~/.venvs/denamer`)
- Ronaldo(맥스튜디오): `~/denamer/` (github.com/dillettante/denamer 클론, venv는 폴더 안)
- 그 외: `git clone https://github.com/dillettante/denamer.git` 후 아래 venv 구성

## 0단계 — 어느 빌드인지 먼저 가른다

| 사용자 의도 | 빌드 | 결과물 |
|---|---|---|
| 밖으로 내보낼 문서를 가린다 (제출·배포·공유) | **반출판** `denamer_out.py` | 비실명화된 PDF (복원 불가) |
| LLM에 물어보려고 가린다 (답변을 다시 원문으로 되돌릴 것) | **질의판** `denamer_ask.py` | 질의용 텍스트 + 복원키 |

불명확하면 물어본다. **복원이 필요한지**가 갈림길이다.

## 반출판 절차

1. **모드 확인** — 명시하지 않았으면 반드시 물어본다(AskUserQuestion 권장):
   - **익명화(anon)**: 이름→김OO(성 보존), 주소→시·도만 보존. 출력 `원본명_masked.pdf`
   - **가명화(pseudo)**: 이름→A·B·C(대장 기반 일관 치환). 출력 `원본명_aliased.pdf`
     **여러 문서 교차 시 같은 사람=같은 가명**이 목적.
   - 두 모드 공통: 법인명→A사, 사건번호→사건A, 번호류→검은 박스 완전 삭제.

2. **가명화면 대장 위치 확인** — 기본은 출력 폴더의 `pseudonym_ledger.json`.
   같은 사건의 문서들은 `--ledger` 경로를 통일한다.
   ※ 대장엔 실명↔가명 매핑이 들어간다. **산출물과 함께 전달하면 가명화 무효** —
   로컬 보관 필수임을 사용자에게 고지한다.
   (익명화 모드는 대장 파일을 만들지 않는다.)

3. **실행**:
   ```bash
   cd "$HOME/Library/CloudStorage/GoogleDrive-redacted@gmail.com/내 드라이브/REDACTED_ORG/일반/[015]디지털TF/비실명화/denamer"
   ~/.venvs/denamer/bin/python denamer_out.py "입력.pdf" --mode anon      # 익명화
   ~/.venvs/denamer/bin/python denamer_out.py "입력.pdf" --mode pseudo    # 가명화
   ```
   유용한 옵션:
   - `--skip ORG,CASE` — 유형별로 끈다(법인명·사건번호를 살려야 할 때). 과잉 마스킹은
     되돌릴 수 있고 미탐은 알아채기 어려우므로, 끄기 전에 사용자 확인을 받는다.
   - `--names 파일` — 규칙이 놓치는 이름을 강제 마스킹. **한글 음차 외국인명(스미스 등)은
     구조상 자동 탐지에서 빠지므로** 그런 문서에선 반드시 안내한다.
   - `--not-names 파일` — 반복되는 이름 오탐을 영구 제외.

4. **결과 검증·보고** — JSON 리포트를 읽고 사용자에게 보고:
   - `residual`·`unmapped`가 비어야 정상. **비어있지 않으면 exit 1 — 출력물을
     신뢰하지 말라고 명확히 경고**하고 해당 값을 보여준다.
   - `warnings`가 있으면 반드시 그대로 전달한다(특히 OCR 관련).
   - `persons`·`orgs` 목록을 반드시 보여준다 — 오탐(일반명사)이 섞일 수 있고,
     누락된 이름은 사람 눈만 잡을 수 있다. "이 목록에 없는 실명이 본문에 남아있는지
     최종 육안 확인" 안내를 붙인다.
   - 결과 PDF는 SendUserFile 등으로 전달.

## 질의판 절차

```bash
~/.venvs/denamer/bin/python denamer_ask.py mask "문서.pdf"
#   → 문서_질의용.txt            (LLM에 넣을 것)
#   → 문서_복원키(원문포함).json  (외부 전달 금지)
~/.venvs/denamer/bin/python denamer_ask.py restore "답변.txt" -k "문서_복원키(원문포함).json"
```

- **복원키는 원문 개인정보 그 자체다.** 사용자에게 파일로 전달할 때 외부 공유 금지를
  명시하고, 대화창에 내용을 붙여넣지 않는다.
- `잔존`·`토큰충돌`이 비어야 정상(비어있지 않으면 exit 1).
- 질의판은 OCR을 하지 않는다. 스캔 PDF면 반출판으로 OCR을 먼저 돌리도록 안내.

## 환경 구성 (venv 없을 때)

```bash
python3 -m venv --system-site-packages ~/.venvs/denamer
~/.venvs/denamer/bin/pip install pymupdf "ko-pii @ git+https://github.com/Marker-Inc-Korea/ko-pii.git@635ade22cfe8d89761ed0e8948b5470e2307506e"
```
(Messi 기준 — 다른 기기는 저장소 폴더 안 `venv/` 그대로)
스캔 PDF 자동 OCR에는 `ocrmypdf`(brew install ocrmypdf)가 필요하다. 수백 쪽이면
수십 분 걸림을 미리 예고할 것.

## 원칙 (절대 어기지 말 것)

- **원본 파일은 절대 수정·삭제하지 않는다.** 결과는 항상 별도 파일.
- 검은 박스/흰 박스는 "덮기"가 아니다 — apply_redactions가 텍스트·이미지를 문서에서
  삭제한 뒤 그리는 표시다. 이 성질을 훼손하는 수정(도형만 얹기) 금지.
- **탐지 규칙은 `detect.py` 한 곳에만 둔다.** 반출판·질의판이 공유하며, 웹판
  (`denamer.html`)의 사본은 `sync_web.py`가 생성한다 — 웹판을 손으로 고치지 말 것.
  규칙을 고쳤으면 `python sync_web.py` → `./check_web_parity.sh` → `test_denamer.py`.
- 자동 탐지는 완전하지 않다. 외부 제출 전 사람 검토가 계약이다.
- 리포트·로그에 원문 PII 값을 남기지 않는다(residual/persons 표시는 사용자 검증 목적).

## 한계 (사용자 질문 시 답변용)

- 라벨·직함·관계어·여격 조사 어디에도 걸리지 않는 이름은 놓칠 수 있다.
- 한글 음차 외국인명은 성씨 게이트 구조상 자동 탐지에서 빠진다(`--names`로 보완).
- OCR 경유 문서는 OCR이 읽어낸 글자에만 적용된다. OCR이 놓친 글자는 스캔 이미지에
  그대로 남고 사후검증도 그것을 모른다 — 리포트 `warnings` 참조.
- 산문 문서에선 일반명사 오탐이 persons에 섞인다(과잉 방향 — 유출 아님).
- 출력 파일이 원본보다 커질 수 있다(스캔 이미지 재인코딩).
