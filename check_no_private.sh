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
echo "개인정보 검사 통과 (추적 파일 $(git ls-files | wc -l | tr -d ' ')건)"
