<!-- SPDX-License-Identifier: GPL-3.0-only -->
# 로컬 stdio 등록 예시

기본 전송은 계속 stdio다. 선택 가능한 인증 loopback HTTP의 환경변수·보안 경계·검증과
아직 완료하지 않은 chat 연결 조건은 [private HTTP 안내](private-http.md)를 참고한다.
실제 클라이언트 설정은 이 문서나 adapter가 자동으로 변경하지 않는다.

가상환경과 환경변수 설정을 확인한 뒤 사용할 클라이언트에 등록한다.
adapter와 원본 저장소는 private로 유지하며 패키지 공개 업로드나 원격 endpoint 생성은 하지 않는다.
먼저 가상환경에 `kapa-mcp`를 정상 설치하고 두 디렉터리 설정을 검증한다.

아래 PowerShell 명령의 세 placeholder를 해당 실행 환경의 절대경로로 교체한다.
문서나 저장소에 개인 절대경로 또는 실제 자격증명을 저장하지 않는다.
클라이언트 프로세스가 접근할 수 있는 Python 실행 파일을 지정해야 하며,
클라이언트가 다른 호스트에서 실행된다면 로컬 경로가 공유된다고 가정하지 않는다.

`<ABSOLUTE_WORKSPACE_DIRECTORY>`는 네 원본 저장소의 부모 디렉터리다.
`<ABSOLUTE_ISOLATED_JOBS_DIRECTORY>`에는 그 아래 `_kapa_jobs`의 절대경로를
사용할 수 있다. 예를 들어 `workspace/kapa_mcp`에 adapter가 있으면 작업 위치는
`workspace/_kapa_jobs`다. `workspace/kapa_mcp/.jobs`는 소스 구역이라 차단된다.
작업 경로로 workspace 자체나 다른 소스 저장소 내부를 지정하지 않는다.

Codex CLI:

```powershell
codex mcp add kapa --env 'KAPA_WORKSPACE=<ABSOLUTE_WORKSPACE_DIRECTORY>' --env 'KAPA_JOBS_DIR=<ABSOLUTE_ISOLATED_JOBS_DIRECTORY>' -- '<ABSOLUTE_VENV_PYTHON>' -I -B -m kapa_mcp
```

Claude Code CLI (현재 프로젝트의 local scope):

```powershell
claude mcp add --scope local --env 'KAPA_WORKSPACE=<ABSOLUTE_WORKSPACE_DIRECTORY>' --env 'KAPA_JOBS_DIR=<ABSOLUTE_ISOLATED_JOBS_DIRECTORY>' --transport stdio kapa -- '<ABSOLUTE_VENV_PYTHON>' -I -B -m kapa_mcp
```

SDK는 `mcp==2.3.0`으로 고정한다. MCP 클라이언트의 v2 지원 범위는 제품 버전에
따라 다르므로 실제 연결에서 초기화·도구 목록·조회 호출을 확인한다.
등록 시 OpenAI/법령 API 키나 Git token을 전달할 필요가 없다.
resources/prompts를 지원하지 않는 클라이언트도 `kapa_route_task`,
`kapa_list_repositories`, `kapa_read_document`, `kapa_read_question`을 사용할 수 있다.
설정의 자동 공유 범위나 도구 승인 정책은 제품별로 확인한다.

2026-11-09 확인한 공식 명령 근거:
[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli),
[Claude Code MCP](https://code.claude.com/docs/en/mcp).
