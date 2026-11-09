<!-- SPDX-License-Identifier: GPL-3.0-only -->
# MCP 어댑터 유지보수

이 문서는 **private MCP 어댑터 `kapa_mcp`만** 대상으로 한다. 원본도 private로 유지하고 공개 업로드·endpoint 배포를 하지 않는다.
자격증명 파일을 읽거나 토큰·환경 전체를 출력하지 않는다. 개인 경로와 비밀값을 문서에 저장하지 않는다.

## 읽기·사용 입구

- [README: 설치·실행과 도구 계약](README.md)
- [AGENTS: 변경 작업 규칙과 소스 계약](AGENTS.md)
- [기존 stdio 등록 예시](examples/client-registration.md)
- [선택적 인증 loopback HTTP](examples/private-http.md)
- [README: 검증과 테스트 명령](README.md#검증), [현재 CI](.github/workflows/ci.yml)

업무 조회는 `kapa_route_task(task)`부터 시작하고 반환된 `entry_documents`를
`kapa_read_document(document_id)`로 읽는다. resources 지원 시 `kapa://entry`와
`kapa://catalog/repositories`도 입구다. 문항은 `kapa_read_question(question_id)`로 읽는다.

## 네 원본 저장소와의 경계

`kapa_edu_1`, `kapa_edu_2`, `kapa_skills`, `kapa_knowledge`는 읽기 전용 입력이다.
각 저장소의 갱신은 **각자의 지침·승인 정책에 따르는 별도 작업**이다.
여기서 네 저장소의 pull/update, 수집, 색인 갱신, 검토 상태 변경을 자동화하지 않는다.
아래 Git 명령도 어댑터 저장소에서만 실행한다. 재귀 갱신이나 일괄 upstream 갱신은 하지 않는다.
일반 검색·발견·읽기는 현재 로컬 checkout을 읽으며 원격 최신본을 자동으로 가져오지 않는다.
이미 고정된 evidence bundle과 render plan은 원본 변경 후 stale 상태일 수 있다.

## 운영자 업데이트 순서

아래는 Windows PowerShell 예시다. 모든 `<...>`는 승인된 값으로 교체한다.
기존 등록의 Python·workspace·jobs 경로를 로컬에서 확인하되 비밀값은 출력하지 않는다.
이 절차는 안정된 신뢰 호스트의 Windows 저장소 `.venv` 전용이며 sandbox를 제공하지 않는다.
기존 등록의 실제 인터프리터가 다르면 중단하고 별도 승인된 환경별 절차를 준비한다.
자동 재등록이나 사용자 설정 변경은 하지 않는다. 모든 Git 명령은 replacement object를 무시한다.

1. 현재 커밋과 깨끗한 작업 트리를 확인하고 승인된 배포 대상 커밋을 확보한다.
   이전 exact-head 리뷰·CI 및 병합 기록에서 별도로 확보한 `PREVIOUS_APPROVED_COMMIT_SHA`와
   현재 HEAD가 일치해야 한다. 현재 HEAD 자체를 승인 근거로 삼지 않는다.
   변경·미추적 파일, 예상 밖 브랜치, 확인되지 않은 커밋이 있으면 중단한다.
   로컬 작업은 담당자와 별도로 보존·정리하며 reset/clean으로 버리지 않는다.

```powershell
Set-Location -LiteralPath '<ABSOLUTE_ADAPTER_DIRECTORY>' -ErrorAction Stop
$pending = git --no-replace-objects status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $pending) { throw '변경·미추적 파일이 있거나 상태 확인 실패: 중단' }
$branch = git --no-replace-objects branch --show-current
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw '운영 main checkout 필요: 중단' }
$previousApproved = '<PREVIOUS_APPROVED_COMMIT_SHA>'
$baseline = git --no-replace-objects rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $baseline -ne $previousApproved) { throw '이전 승인 SHA와 현재 HEAD 불일치 또는 확인 실패: 중단' }
$baseline
$registeredPython = (Resolve-Path -LiteralPath '<ABSOLUTE_REGISTERED_PYTHON>' -ErrorAction Stop).ProviderPath
$projectVenv = Join-Path (Get-Location).ProviderPath '.venv'
$expectedPython = (Resolve-Path -LiteralPath (Join-Path $projectVenv 'Scripts/python.exe') -ErrorAction Stop).ProviderPath
if ($registeredPython -ne $expectedPython) { throw '기존 등록이 저장소 .venv Python과 다름: 중단' }
```

2. 새 조판·bundle·plan 생성 요청을 멈춘다. 소유한 모든 서버의 알려진 job ID를
   `kapa_get_job(job_id)`로 조회해 `preparing`, `queued`, `running`이 없어질 때까지 기다린다.
   성공 또는 실패 등 종료 상태를 확인한 다음 클라이언트의 정상 종료 기능으로
   소유한 stdio 서버를 종료한다. 자동 재접속도 업데이트 동안 멈춘다.
   선택적 HTTP는 직접 소유한 서버의 실행 콘솔·관리자에서 정상 종료한다.
   프로세스 이름으로 일괄 강제 종료하거나 다른 사용자의 서버를 종료하지 않는다.
   어댑터에는 drain/shutdown MCP 도구가 없으므로 이 순서는 운영자가 조율한다.

3. 서버가 멈춘 뒤 작업 트리를 다시 확인하고 승인된 `main`만 fast-forward한다.
   아래 블록은 깨끗한 운영용 `main` checkout에서만 사용한다.
   개발 브랜치라면 중단하고 운영 checkout을 별도로 준비한다.

```powershell
$pending = git --no-replace-objects status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $pending) { throw '변경이 있거나 상태 확인 실패: 중단' }
$branch = git --no-replace-objects branch --show-current
if ($LASTEXITCODE -ne 0 -or $branch -ne 'main') { throw '운영 main checkout 필요' }
git --no-replace-objects fetch --no-recurse-submodules origin main
if ($LASTEXITCODE -ne 0) { throw 'fetch 실패' }
$approved = '<APPROVED_MAIN_COMMIT_SHA>'
$fetched = git --no-replace-objects rev-parse refs/remotes/origin/main
if ($LASTEXITCODE -ne 0 -or $fetched -ne $approved) { throw '승인 SHA와 원격 main 불일치' }
git --no-replace-objects merge-base --is-ancestor HEAD $approved
if ($LASTEXITCODE -ne 0) { throw '분기 또는 로컬 선행 커밋: 중단' }
git --no-replace-objects merge --ff-only $approved
if ($LASTEXITCODE -ne 0) { throw '분기 또는 갱신 실패: 중단' }
$updated = git --no-replace-objects rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $updated -ne $approved) { throw '최종 HEAD 불일치: 중단' }
$updated
$pending = git --no-replace-objects status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $pending) { throw 'merge 후 변경 또는 상태 확인 실패: 중단' }
```

   divergence·로컬 선행 커밋·승인 SHA 불일치가 있으면 중단한다.
   최종 HEAD가 승인 SHA와 같은지 확인한다. 강제 reset, rebase, 자동 병합으로 우회하지 않는다.

4. 해당 revision의 lockfile로 설치하고 격리된 가상환경 Python으로 버전을 확인한다.

```powershell
$env:UV_PROJECT_ENVIRONMENT = $projectVenv
uv sync --locked --python 3.12
if ($LASTEXITCODE -ne 0) { throw 'lock 설치 실패: 재시작 중단' }
& $registeredPython -I -B -c 'import kapa_mcp; print(kapa_mcp.__version__)'
if ($LASTEXITCODE -ne 0) { throw '설치 버전 확인 실패' }
```

   출력 버전은 승인된 소스의 `src/kapa_mcp/__init__.py`와 비교한다.
   버전 문자열만으로 커밋 일치를 판단하지 말고 위 HEAD도 함께 기록한다.
   `--locked` 실패 시 운영 중 `uv lock`으로 해결하지 않는다.

5. 기존 등록을 유지하고 **실제 클라이언트/세션이 소유한 stdio 프로세스**를 다시 시작한다.
   클라이언트의 재연결·세션 재시작 기능을 사용하고 필요하면 클라이언트를 정상 재실행한다.
   재등록만으로 이미 실행 중인 서버 코드가 교체된다고 가정하지 않는다.
   기존 등록의 가상환경 Python과 명시적 `KAPA_WORKSPACE`, `KAPA_JOBS_DIR`를 사용한다.
   HTTP 사용자는 소유한 서버를 새 환경으로 재실행하고 기존 클라이언트를 재연결한다.
   HTTP는 opt-in `streamable-http`, `127.0.0.1` 전용이며 토큰은 안전한 로컬 환경으로 공급한다.
   명령·URL·로그에 토큰을 남기지 않는다. 자세한 실행 조건은 위 HTTP 안내를 따른다.

## 재시작 후 읽기 전용 확인

- 실제 사용하는 클라이언트에서 초기화와 도구 발견을 확인한다. 현재 계약은 **17개 도구**다.
- `kapa_list_repositories()`로 네 저장소의 revision, dirty, warnings를 확인한다.
  기대한 승인 원본 SHA와 비교하고 불명확한 revision·dirty·경고를 성공으로 숨기지 않는다.
- `kapa_route_task(task='<READ_ONLY_CHECK_TASK>')`와 반환된 입구 문서 읽기를 확인한다.
  필요한 문항 읽기·검색·관계 조회도 기존 ID로 확인한다. 조판은 기본 점검에 포함하지 않는다.
- 읽은 원문의 revision·sha256·head_verified와 경고를 보존한다.
  어댑터 HEAD와 원본 revision은 서로 다른 값이다. SDK 점검만으로 실제 제품 연결을 단정하지 않는다.

## 고정 근거·계획과 보존 파일

원본이나 어댑터 변경 후 기존 bundle은 `kapa_validate_evidence_bundle(bundle_id)`로
`valid`, `stale`, `checks`를 확인한다. `valid=true`, `stale=false`가 아니면 재사용하지 않는다.
`kapa_get_evidence_bundle`도 변경된 입력이나 불변 출처 부족을 거부한다.
현재 원문을 다시 읽고 새 bundle과 새 plan을 만든다. 기존 고정 파일을 수정하지 않는다.
`kapa_get_render_plan` 조회 성공만으로 현재 원문과 일치한다고 판단하지 않는다.
시작 시 plan 원문 해시와 연결 근거를 다시 검사하며, 변경된 plan은 새로 생성한다.
연구 근거가 필요한 plan에는 `require_evidence=true`와 해당 bundle을 명시한다.
재현성은 법적 검증이 아니며 coverage/gaps와 법적 미검증 경고를 유지한다.

재시작은 중단된 조판을 이어 실행하지 않는다. job/PDF/plan 파일을 보존한다.
job·plan 각 128개는 registry 조회 창이며 평생 실행 할당량이나 디스크 quota가 아니다.
퇴출된 job·plan ID는 다시 조회·시작할 수 없지만 파일은 자동 삭제하지 않는다.
bundle은 활성 128개 창에서 오래된 디렉터리를 archive로 옮기고 정확한 ID로 계속 검증한다.
이 역시 파일 자동 삭제나 디스크 quota를 뜻하지 않는다. 정리 작업은 별도 승인 범위다.

## 롤백

아래 사전 점검 후 새 요청을 멈추고 위와 같이 job 종료·소유 서버 정상 종료를 확인한다.
작업 트리가 깨끗할 때 1단계에서 독립된 리뷰·CI·병합 기록과 대조한 **동일한 이전 승인 SHA**로
새 로컬 rollback 브랜치를 만든다. 임의의 현재 HEAD나 미검토 조상 커밋으로 대체하지 않는다.
새 PowerShell 세션에서는 이전 승인 기록을 다시 확보·검증해 `$previousApproved`를 설정하고,
실제 기존 등록 경로도 다시 확인해 `$registeredPython`에 설정한다. 현재 HEAD에서 추정하지 않는다.

```powershell
Set-Location -LiteralPath '<ABSOLUTE_ADAPTER_DIRECTORY>' -ErrorAction Stop
if (-not (Get-Variable previousApproved -ErrorAction SilentlyContinue) -or [string]::IsNullOrWhiteSpace($previousApproved)) { throw '이전 승인 기록 재확보·검증 필요' }
$rollbackTarget = git --no-replace-objects rev-parse --verify "$previousApproved^{commit}"
if ($LASTEXITCODE -ne 0 -or $rollbackTarget -ne $previousApproved) { throw '이전 승인 전체 SHA 확인 실패' }
if (-not (Get-Variable registeredPython -ErrorAction SilentlyContinue) -or [string]::IsNullOrWhiteSpace($registeredPython)) { throw '실제 기존 등록 Python 재확인 필요' }
$registeredPython = (Resolve-Path -LiteralPath $registeredPython -ErrorAction Stop).ProviderPath
$projectVenv = Join-Path (Get-Location).ProviderPath '.venv'
$expectedPython = (Resolve-Path -LiteralPath (Join-Path $projectVenv 'Scripts/python.exe') -ErrorAction Stop).ProviderPath
if ($registeredPython -ne $expectedPython) { throw '기존 등록이 저장소 .venv Python과 다름: 중단' }
$pending = git --no-replace-objects status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $pending) { throw '변경이 있거나 상태 확인 실패: 중단' }
```

위 사전 점검에 통과하면 새 요청 차단·job 종료·소유 서버 정상 종료를 완료한다.
그다음 같은 PowerShell 세션에서 아래 전환·설치를 실행한다.

```powershell
git --no-replace-objects switch -c '<NEW_ROLLBACK_BRANCH>' $previousApproved
if ($LASTEXITCODE -ne 0) { throw '롤백 전환 실패' }
$restored = git --no-replace-objects rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $restored -ne $previousApproved) { throw '롤백 HEAD 불일치: 중단' }
$pending = git --no-replace-objects status --porcelain --untracked-files=all
if ($LASTEXITCODE -ne 0 -or $pending) { throw 'checkout 후 변경 또는 상태 확인 실패: 중단' }
$env:UV_PROJECT_ENVIRONMENT = $projectVenv
uv sync --locked --python 3.12
if ($LASTEXITCODE -ne 0) { throw '롤백 lock 설치 실패' }
& $registeredPython -I -B -c 'import kapa_mcp; print(kapa_mcp.__version__)'
if ($LASTEXITCODE -ne 0) { throw '롤백 버전 확인 실패: 재시작 중단' }
```

이후 기존 클라이언트의 실제 서버를 재시작하고 같은 읽기 전용 확인을 수행한다.
코드 롤백이 registry·bundle·plan 저장 형식의 하위 호환성을 보장하지는 않는다.
호환되지 않으면 기존 jobs를 보존하고 별도의 빈 격리 `KAPA_JOBS_DIR`로 실행한다.
필요한 로컬 실행 설정만 명시적으로 조정하며 기존 registry를 덮어쓰거나 파일을 삭제하지 않는다.

## 개발자 변경

feature 브랜치에서 범위를 좁혀 변경하고 `main`에 직접 커밋하지 않는다.
작은 synthetic fixture의 의미 있는 테스트와 [README 검증 절차](README.md#검증)를 수행한다.
PR 최종 HEAD에 대해 전체 Ubuntu/Windows CI와 리뷰를 확인하고 변경 후 다시 확인한다.
의존성 변경은 명시적 작업에서만 `uv lock`을 실행해 `pyproject.toml`·`uv.lock` diff를 리뷰한다.
현재 고정 SDK `mcp==2.3.0`의 API·초기화·도구·전송 호환성을 검증하며 임의 업그레이드하지 않는다.
