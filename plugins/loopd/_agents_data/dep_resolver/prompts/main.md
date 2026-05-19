# Dependency Resolver - Task Ordering Analysis

같은 프로젝트(repo)에 속한 태스크들의 실행 순서를 분석합니다.

## 태스크 목록
{{TASK_LIST}}

## 분석 규칙
1. 각 태스크의 prompt에서 선행 작업 의존성을 파악
2. Story 번호, 기능 참조, "~이후", "~필요", "depends on" 등의 표현에서 의존성 추출
3. 의존성이 없는 태스크는 빈 배열 []
4. 순환 의존성을 만들지 말 것

## 응답 형식
JSON만 응답:
{ "dependencies": { "<task-id>": ["<dep-task-id>", ...], ... } }
