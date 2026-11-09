<!-- SPDX-License-Identifier: GPL-3.0-only -->
# Private KAPA MCP adapter

이 패키지와 네 원본 저장소는 **private 유지**가 필수다. 공개 패키지 업로드,
공개 저장소 생성, 공개 endpoint 배포를 하지 않는다. 로컬 stdio 전용 실행 진입점을
제공하며 모델 API나 법령 API 키가 필요하지 않다.

## 범위와 계약

`Catalog`의 저장된 문항·지식·원문 조회와 `RenderManager`의 격리된 조판을
연결하는 독립 어댑터다. 원본 저장소는 읽기 전용 입력이다. 수집, 자동 최신화,
색인 재생성, 관계·검토 상태 변경, upstream CLI의 갱신 작업은 제공하지 않는다.
임의 파일 읽기나 shell 도구도 없다. 자격증명 파일을 읽거나 환경 전체를 출력하지 않는다.

`AGENTS.md`가 현재 모듈 계약이다. 설계 제안의 evidence bundle/plan 강제,
과거 법령 판본 검증, 임의 문항 목록의 재배열 조판, HTTP/OAuth, 다중 사용자 ACL은
이 어댑터에서 구현하지 않았다. 검색 결과의 경고·revision·해시와 검토 상태를
그대로 전달하며, 조판 가능 범위와 검증은 renderer가 결정한다.

## 설치와 실행

Python 3.12 이상, 공식 Python SDK **`mcp==2.3.0`** 하나만 지원한다.
설치된 SDK의 `mcp.server.mcpserver.MCPServer`, `ToolAnnotations`, `Client`
API를 확인했다. 1.x FastMCP와 혼용하지 않는다.

uv 0.11.14를 사용한다. 패키지 루트에서 lockfile에 맞춰 가상환경을 만들고 설치한다:

```powershell
uv sync --locked --python 3.12
```

Linux/macOS에서는 가상환경 Python 경로를 `.venv/bin/python`으로 바꾼다.
실제 조판에는 Python 외에 XeLaTeX, 원본 문서가 요구하는 TeX 패키지와 폰트가 필요하다.
원본 저장소는 승인된 로컬 checkout을 별도로 준비한다.

직접 의존성 `mcp==2.3.0`과 전이 의존성은 저장소의 `uv.lock`에 고정돼 있다.
`--locked`는 프로젝트 정의와 lockfile이 맞지 않으면 실패하며 lockfile을 갱신하지 않는다.
`uv lock --check`로 일치 여부를 검증할 수 있다. 의존성을 변경할 때만
`uv lock`으로 갱신하고 diff를 검토한다. CI에서도 동일한 lockfile을 사용한다.

실행 전 다음 두 환경변수를 명시적으로 지정한다. 아래 값은 모두 교체할 placeholder다.

```powershell
$env:KAPA_WORKSPACE = '<ABSOLUTE_WORKSPACE_DIRECTORY>'
$env:KAPA_JOBS_DIR = '<ABSOLUTE_ISOLATED_JOBS_DIRECTORY>'
& '<ABSOLUTE_VENV_PYTHON>' -I -B -m kapa_mcp
```

`KAPA_WORKSPACE`는 `kapa_edu_1`, `kapa_edu_2`, `kapa_skills`, `kapa_knowledge`의
부모 디렉터리다. `KAPA_JOBS_DIR`는 adapter를 포함한 소스 저장소 밖의 작업 디렉터리이며 원본을
포함하는 상위 디렉터리도 허용하지 않는다. 설정 단계에서는 디렉터리를 만들지 않는다.
권장 배치는 다음과 같다. `KAPA_WORKSPACE`를 아래 `workspace`의 절대경로로,
`KAPA_JOBS_DIR`를 `workspace/_kapa_jobs`의 절대경로로 지정한다.

```text
workspace/
  kapa_mcp/
  kapa_edu_1/
  kapa_edu_2/
  kapa_skills/
  kapa_knowledge/
  _kapa_jobs/
```

`kapa_mcp/.jobs`도 소스 저장소 내부이므로 차단된다. `.gitignore`에 `.jobs/`가
있더라도 실행 시 허용되는 출력 위치라는 뜻은 아니다. 환경변수 값은 절대경로로
설정하고 실제 경로는 로컬 설정에만 보관한다.

누락·잘못된 설정은 stderr에 변수명과 오류를 쓰고 종료 코드 2를 반환한다.
stdout은 MCP 메시지 전용이다. 설치 후 다른 cwd에서도 `-m kapa_mcp`로 실행 가능하다.

## MCP 인터페이스

| Tool | 계약 메서드 | 동작 |
|---|---|---|
| `kapa_list_repositories` | `Catalog.list_repositories` | 저장소 가용성 |
| `kapa_route_task` | `Catalog.route_task` | 업무 입구·원문 조회 경로 |
| `kapa_search_questions` | `Catalog.search_questions` | 문항 검색·페이지 |
| `kapa_read_question` | `Catalog.read_question` | 문항 원문 |
| `kapa_search_knowledge` | `Catalog.search_knowledge` | 저장된 지식 검색 |
| `kapa_read_document` | `Catalog.read_document` | 허용 문서 페이지 |
| `kapa_get_relations` | `Catalog.get_relations` | 관계·검토 상태 |
| `kapa_start_render` | `RenderManager.start_render` | 격리 job 생성 |
| `kapa_get_job` | `RenderManager.get_job` | job 조회 |
| `kapa_get_artifact` | `RenderManager.get_artifact` | 허용 산출물 조회 |

모든 조회 도구는 `readOnlyHint=true`, `destructiveHint=false`,
`idempotentHint=true`, `openWorldHint=false`다. `kapa_start_render`는 실제 쓰기이므로
`readOnlyHint=false`, `idempotentHint=false`다. annotations는 클라이언트용 힌트이며
접근 통제의 대체물이 아니다. `noanswers` 기본값은 `true`다.

resources 목록에는 `kapa://entry`와 `kapa://catalog/repositories`가 있다.
entry는 업무 흐름과 tools fallback을, repositories는 저장소 목록을 제공한다.
원문은 `kapa_read_document(<repo>:<allowed-relative-path>)`, 문항은
`kapa_read_question(edu/<1|2>/<subject>/<round>/<seq>)`로 읽는다.
원문 resource template이나 revision 고정 URI를 구현했다고 주장하지 않는다.
prompt `kapa_research_and_render(task)`는 선택 가능한 업무 양식이다.

서버 instructions는 위키 검색을 따로 요청하지 않아도 `kapa_route_task`부터
시작하고 입구·읽기 규칙·원문·관계를 확인하도록 안내한다. 조판 요청에는 조회 후
`kapa_start_render` → `kapa_get_job` → `kapa_get_artifact` 순서를 안내한다.
클라이언트가 instructions를 따르는지, 모든 근거를 조사했는지 강제하거나 보증하지 않는다.

## 클라이언트 연결

[등록 예시](examples/client-registration.md)의 placeholder를 로컬 실행 환경에 맞게
교체한 뒤 원하는 클라이언트에 등록한다. 실제 경로는 로컬 설정에만 저장한다.
로컬 stdio로 읽은 자료도 클라이언트가 선택한 모델 서비스에 전달될 수 있으므로
자료 공유 범위는 해당 조직 정책을 따른다.

## 검증

테스트 의존성을 포함하여 설치한 뒤 전체 synthetic suite를 실행한다.
Windows PowerShell 예시:

```powershell
uv sync --locked --extra test --python 3.12
uv run --locked --extra test --python 3.12 python -I -B -m pytest -p no:cacheprovider
```

위 uv 명령은 Linux/macOS에서도 동일하다. 설치 후 가상환경 Python으로 직접 실행하려면
Windows에서는 `.venv/Scripts/python.exe`, Linux/macOS에서는 `.venv/bin/python`을 사용한다.
서버 테스트는 정상 설치된 패키지를 import하며 별도 `PYTHONPATH` 설정은 필요 없다.
작은 임시 fixture와 주입한 서비스로 모든 도구의 인자 전달, annotations,
resources와 prompt를 확인한다. SDK의 legacy 모드는 context 진입 시 실제
`initialize` handshake를 수행한다. 별도 stdio 프로세스에도 초기화 →
`list_tools` → `call_tool`을 수행한다. 이 검증은 실제 데이터·키·모델 API·TeX를
사용하지 않으며 renderer의 PDF 품질이나 데이터 해석 정확성을 대신하지 않는다.

[GitHub Actions CI](.github/workflows/ci.yml)는 Ubuntu와 Windows의 Python 3.12에서
uv 0.11.14와 `uv.lock`으로 같은 설치·pytest 명령을 실행한다. adapter 저장소만 checkout하고
`contents: read`, `persist-credentials: false`를 사용한다. 네 원본 저장소 checkout,
저장소 secrets, 실제 자격증명 파일, 모델·법령 API, TeX 설치가 필요하지 않다.
테스트가 임시 원본 구조와 가짜 build 프로그램을 직접 만든다. 패키지 설치에는
Python 패키지 인덱스 접근이 필요하며, 링크 생성 권한에 따라 일부 테스트가 skip될 수 있다.

2026-11-09 Windows 로컬 통합 검증에서 `uv sync --locked --extra test`가 성공했고,
정상 권한의 전체 pytest 실행 결과는 **97 passed, 2 skipped / 48.37초**였다.
실제 stdio에서도 초기화·도구 목록과 10개 도구 전체 흐름이 통과했다.
네 저장소 조회, 1차·2차 문항 원문 읽기, 지식 검색, 관계 조회에서
confirmed issue 17개·verified 17개를 확인하고 조판·산출물 조회까지 검증했다.

단일 문항 조판은 기존 upstream `--single` 동작을 사용하므로 `00.tex` 공통 지시문이 포함되지 않는다. 공통 지시문까지 필요하면 전체 회차를 조판한다.

Windows 실제 단일 문항 조판 결과 PDF는 46,030 bytes였고 누락 glyph는 0개였다.
PDF 페이지를 이미지로 확인하여 한글·선택지·여백이 정상임을 확인했다.
이 결과는 해당 로컬 환경과 단일 문항에 대한 검증이며, 모든 문항의 시각 품질이나
법적·정답 정확성을 보증하지 않는다. 호스팅된 Ubuntu·Windows CI 결과는 별도로 확인한다.

렌더러는 검토된 신뢰 가능한 checkout과 TeX만 대상으로 한다. 파일·시간·동시 작업
제한과 shell escape 차단은 방어 수단이며 OS/네트워크 격리나 강제 메모리·CPU 제한을
제공하지 않는다. 산출물은 로컬 경로 메타데이터로 제공하고, job registry는 서버 프로세스
수명 동안만 유지된다. 실제 데이터 smoke와 실제 조판 검증은 로컬에서 별도로 수행한다.

조회 입력의 하드링크를 거부하고, 관계 검토의 URL·인용문을 해시 확인된 저장 원문과
대조한다. Git 무시 대상의 허용 문서도 clean HEAD 근거로 표시하지 않는다. 탐색 예산은
파일과 디렉터리 모두에 적용한다. 조판은 부모 종료 뒤에도 소유 process group/Windows
Job Object를 정리하며, 전체 compiler log를 순차 검사한다. 준비 실패도 소유 작업 기록과
작업 수 한도에 포함한다. 이 방어 수단을 호스트 OS 격리로 해석하지 않는다.

HEAD 출처 확인은 상태 표시만 사용하지 않는다. 요청마다 Git HEAD manifest와 실제 파일의
raw Git blob 해시를 대조하므로 `assume-unchanged`나 수정된 lock도 clean/resolved로
잘못 표시하지 않는다. Git 출력은 읽는 동안 크기와 시간을 제한한다. 1차 정답 조회는
해당 과목의 40행 블록을 벗어나지 않으며, JSON의 비유한 수치는 거부한다.

읽기·HEAD 대조·탐색에는 요청 전체 64MiB·60초·12만 항목 예산도 적용하며 초과 시
전용 오류로 중단한다. 관계 ID가 중복된 ledger는 검토 이력을 붙이기 전에 거부한다.
조판의 복사 입력과 생성 출력은 별도 예산이다. 기록된 복사본이 그대로일 때만 출력
계산에서 제외하고, 변경된 복사본은 거부하며 예상 밖 생성 파일·디렉터리도 계산한다.

Windows XeTeX의 PDF backend 실행에 필요한 `COMSPEC`은 호출자가 제공한 값을
상속하지 않고 `SystemRoot/System32/cmd.exe`로 구성한다. 신뢰 경계에는 실행 환경의
`SystemRoot`, Windows 시스템 실행 파일, 설치된 TeX 도구와 backend도 포함된다.
`-no-shell-escape`, `shell_escape=f`, `openout_any=p`는 유지하지만 PDF backend의
프로세스 실행 자체를 없애지는 않는다. 원본 wrapper의 상위 디렉터리 import를 위해
입력 정책은 `openin_any=r`이며 호스트 파일 읽기를 격리하지 않는다.
신뢰하지 않는 checkout·TeX를 처리하려면 별도 OS/네트워크 격리가 필요하다.

공식 참고: [Python SDK](https://github.com/modelcontextprotocol/python-sdk),
[v2 마이그레이션](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/migration.md),
[uv GitHub Actions 연동](https://docs.astral.sh/uv/guides/integration/github/).
