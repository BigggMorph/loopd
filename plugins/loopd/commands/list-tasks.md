---
description: loopd가 알고 있는 task 목록을 status별로 출력 (read-only)
argument-hint: '[status]'
allowed-tools:
  - Bash(ls:*)
  - Bash(jq:*)
---

# loopd /list-tasks

`~/.loopd/state/{pending,active,waiting_human,completed,failed}/` 디렉토리의 task 메타데이터를 요약합니다.

```!
for st in pending active waiting_human completed failed; do
  dir="$HOME/.loopd/state/$st"
  [[ ! -d "$dir" ]] && continue
  count=$(ls "$dir" 2>/dev/null | grep -c '\.json$' || true)
  echo "## $st ($count)"
  for f in "$dir"/*.json; do
    [[ -f "$f" ]] || continue
    id=$(basename "$f" .json)
    title=$(jq -r '.title // .prompt[:80]' "$f" 2>/dev/null || echo '(unparseable)')
    echo "- \`$id\` — $title"
  done
  echo
done
```

위 출력을 사용자에게 그대로 보고하세요. 추가 분석이나 도구 호출은 불필요합니다.
