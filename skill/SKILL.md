---
name: denamer
description: PDF 비실명화 — 익명화(김OO·주소 부분보존) 또는 가명화(A·B·C 일관 치환, 대장 관리). 검은박스 덮기가 아니라 PyMuPDF apply_redactions로 텍스트·스캔픽셀을 진짜 삭제한 뒤 대체어를 삽입한다. 트리거 - "비실명화", "익명화", "가명화", "개인정보 지워줘", "이 PDF 마스킹", "denamer", "redact this pdf". 스캔 PDF는 ocrmypdf 자동 전처리. 원본 불변, 결과는 원본명_masked.pdf(익명화)/_aliased.pdf(가명화).
---

# denamer — PDF 비실명화 (익명화/가명화)

엔진 위치 (기기별 — 실행은 반드시 해당 폴더의 venv로):
- Messi(맥북): `~/Claude Code/AI/denamer/`
- Ronaldo(맥스튜디오): `~/denamer/` (github.com/dillettante/denamer 클론)
- 그 외: `git clone https://github.com/dillettante/denamer.git` 후 아래 venv 구성

## 실행 절차

1. **모드 확인** — 사용자가 익명화/가명화를 명시하지 않았으면 반드시 물어본다
   (AskUserQuestion 권장):
   - **익명화(anon)**: 이름→김OO(성 보존), 주소→첫 토큰(시·도) 보존+나머지 O,
     주민번호·계좌·전화·카드→검은 박스 완전 삭제. 출력: `원본명_masked.pdf`
   - **가명화(pseudo)**: 이름→A·B·C…(대장 기반 일관 치환), 그 외는 익명화와 동일.
     출력: `원본명_aliased.pdf`. **여러 문서 교차 시 같은 사람=같은 가명**이 목적.

2. **가명화면 대장 위치 확인** — 기본은 출력 폴더의 `pseudonym_ledger.json`.
   같은 사건의 문서들은 같은 대장을 쓰도록 `--ledger` 경로를 통일한다.
   ※ 대장엔 실명↔가명 매핑이 들어간다. **산출물과 함께 전달하면 가명화 무효** —
   로컬 보관 필수임을 사용자에게 고지한다.

3. **실행**:
   ```bash
   cd ~/Claude\ Code/AI/denamer
   ./venv/bin/python b_redact.py "입력.pdf" --mode anon      # 익명화
   ./venv/bin/python b_redact.py "입력.pdf" --mode pseudo    # 가명화
   # 출력 경로를 지정하려면 두 번째 위치 인자로
   ```
   - venv가 없으면(다른 기기): `python3 -m venv --system-site-packages venv &&
     ./venv/bin/pip install pymupdf "ko-pii @ git+https://github.com/Marker-Inc-Korea/ko-pii.git@635ade22cfe8d89761ed0e8948b5470e2307506e"`
   - ko-pii 없이도 동작하나(정규식만) 무라벨 이름 탐지가 빠진다.
   - 스캔 PDF(텍스트 레이어 없음)는 ocrmypdf를 자동 실행한다(kor+eng).
     ocrmypdf 미설치면 에러 메시지대로 안내. 수백 쪽이면 수십 분 걸림을 예고할 것.

4. **결과 검증·보고** — JSON 리포트를 읽고 사용자에게 보고:
   - `residual`·`unmapped`가 비어야 정상. **비어있지 않으면 exit 1 — 출력물을
     신뢰하지 말라고 명확히 경고**하고 해당 값을 보여준다.
   - `persons` 목록을 반드시 보여준다 — 오탐(일반명사)이 섞일 수 있고,
     누락된 이름은 사람 눈만 잡을 수 있다. "이 목록에 없는 실명이 본문에
     남아있는지 최종 육안 확인" 안내를 붙인다.
   - 결과 PDF는 SendUserFile 등으로 전달.

## 원칙 (절대 어기지 말 것)

- **원본 파일은 절대 수정·삭제하지 않는다.** 결과는 항상 별도 파일.
- 검은 박스/흰 박스는 "덮기"가 아니다 — apply_redactions가 텍스트·이미지를
  문서에서 삭제한 뒤 그리는 표시다. 이 성질을 훼손하는 수정(도형만 얹기) 금지.
- 자동 탐지는 완전하지 않다(룰+ko-pii 기반). 외부 제출 전 사람 검토가 계약이다.
- 리포트·로그에 원문 PII 값을 남기지 않는다(residual/unmapped의 값 표시는
  사용자 검증 목적으로만).

## 한계 (사용자 질문 시 답변용)

- 라벨·조사·문맥 없는 자유문장 속 이름 일부는 놓칠 수 있다.
- 산문 문서에선 일반명사 오탐이 persons에 섞인다(과잉 방향 — 유출 아님).
- 출력 파일이 원본보다 커질 수 있다(스캔 이미지 재인코딩).
