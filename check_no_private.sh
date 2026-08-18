#!/bin/bash
# push 전 개인정보 검사 — 이 저장소는 공개다.
#
# 왜 필요한가: 사용자별 SKILL.md(설치 경로·계정·기기명 포함)를 저장소 안 공개판에
# 그대로 복사해 넣는 사고가 실제로 났다. 커밋 eef76cc가 그걸 지우려고 만든 커밋인데
# 이후 작업에서 되살아났다. 사람 기억에 맡기지 않고 검사로 막는다.
#
# 실행: ./check_no_private.sh   (통과 시 exit 0)
set -uo pipefail
cd "$(dirname "$0")"

# 개인 식별 흔적. 두 파일은 제외한다.
#   denamer.html        번들 라이브러리의 저작권 헤더에 저자 메일이 들어 있다
#   check_no_private.sh 이 스크립트 자신 — 아래 패턴 문자열이 그대로 걸린다
PATTERN='CloudStorage|GoogleDrive-|/Users/[a-z]|내 드라이브|[A-Za-z0-9._%+-]+@(gmail|naver|daum|kakao)\.'
HITS=$(git ls-files -z \
       | xargs -0 grep -nEI "$PATTERN" 2>/dev/null \
       | grep -vE '^(denamer\.html|check_no_private\.sh):' || true)

if [ -n "$HITS" ]; then
  echo "개인정보 의심 항목이 추적 파일에 있습니다 — push하지 마세요:"
  echo "$HITS"
  exit 1
fi

# ── 2차: 실명·소속·사내 프로젝트명 ──────────────────────────────
# 위 정규식은 경로·계정·메일만 본다. 실측: 주석의 출처 표기와 테스트 픽스처
# (DOCX 변경이력 w:author, docProps 의 Company·Manager)로 실명과 소속이 다시
# 들어왔는데 검사를 그대로 통과했다.
#
# 금칙어는 .private_terms(추적 제외)에 한 줄 하나씩 둔다. 목록을 이 스크립트에
# 직접 적으면 지우려던 값이 공개 저장소에 그대로 실린다.
TERMS=".private_terms"
if [ -s "$TERMS" ]; then
  NAME_HITS=$(git ls-files -z \
              | xargs -0 grep -nIF -f "$TERMS" 2>/dev/null \
              | grep -vE '^(denamer\.html|check_no_private\.sh):' || true)
  if [ -n "$NAME_HITS" ]; then
    echo "실명·소속 의심 항목이 추적 파일에 있습니다 — push하지 마세요:"
    echo "$NAME_HITS"
    exit 1
  fi
  echo "실명 검사 통과 (금칙어 $(grep -cve '^[[:space:]]*$' "$TERMS")개)"
else
  echo "주의: $TERMS 가 없어 실명 검사를 건너뜁니다."
  echo "      실명·소속·사내 프로젝트명을 한 줄에 하나씩 적어 두세요(이 파일은 추적되지 않습니다)."
fi

echo "개인정보 검사 통과 (추적 파일 $(git ls-files | wc -l | tr -d ' ')건)"
