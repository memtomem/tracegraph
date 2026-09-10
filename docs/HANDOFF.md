# Tracegraph handoff

## 현재 전달 상태 — 2026-09-10 (Asia/Seoul)

- 기준 HEAD: `0d5091c`, 브랜치 `tracegraph-mvp`. 전체 코드 리뷰 후 확인된 결함을 수정했다.
- 이번 범위: 리뷰에서 재현된 correctness 결함(Tier 1), 회귀 테스트, lint gate·패키징 위생.
- 공개 계약을 바꾸는 항목(Tier 2)은 **수행하지 않았다**. 아래 "남은 결정 필요 항목"을 참조한다.

### 수정한 결함

| 위치 | 결함 |
| --- | --- |
| `analysis/diagnose.py` | preset 간 중복 제거가 step tuple만 사용해 `tool-failure`/`error` 발견이 사라지고 `pattern_count_deltas`가 왜곡됐다. 키를 `(pattern_id, match)`로 바꿨다. |
| `analysis/diagnose.py` | 음수 `wall_duration_ms`, 부분 커버리지, 파싱 불가 `total_cost`를 조용히 흡수했다. 이제 값을 보류하고 `warnings`로 공개한다. |
| `analysis/diagnose.py` | redaction 경고가 보고서에 도달하지 않은 이름 때문에도 발생했다. 완성된 보고서에 alias가 실제로 있을 때만 공개한다. |
| `analysis/diagnose.py` | `analyze()`가 검증되지 않은 trace에서 `KeyError`를 냈다. `validate_structure()`로 `ValueError`를 보장한다. |
| `analysis/ahu.py` | root에서 도달 불가능한 component를 조용히 버려, 서로 다른 trace가 `identical`로 보고됐다. 이제 `ValueError`다. 미사용 `_render`도 삭제했다. |
| `cli.py` | `explain`/`query`가 대괄호를 포함한 이름에서 `MarkupError`로 죽고, `inspect`는 이름·오류 텍스트를 조용히 삭제했다. 모든 경로를 `escape()`한다. |
| `cli.py` | `--json-out`/`--save-artifact` 대상이 쓰기 불가일 때 raw traceback을 냈다. 이제 exit 2의 usage error다. |
| `cli.py` | `inspect` 트리 정렬이 seq 동률에서 hash 순서였다. 정규 `(seq, step_id)` 키로 바꿨다. |
| `artifact.py` | `from_obj`가 v1 migration의 JSON round-trip 때문에 `TypeError`를 냈다. `copy.deepcopy`로 대체했다. |
| `adapters/otlp_spans.py` | `traceId` 없는 span을 조용히 버려 자식 span이 잘못된 오류를 냈다. 이제 거부한다. 타임스탬프 변환은 정수 연산으로 바꿨다(현재 epoch에서 출력 동일). |
| `adapters/langgraph_checkpoint.py` | subgraph entry parent 선택이 `saver.list()` 페이징 순서에 의존했다. chronological 순서로 고정했다. |
| `store/ladybug.py` | 호출자의 `Trace`를 참조로 보관해 `InMemoryStore`와 갈렸다. deep-copy로 맞췄다. `COMMIT`을 `try` 안으로 옮기고 `QueryResult`를 닫는다. |
| `normalize.py` | TREE_PARENT cycle 오류 메시지가 hash 순서에 의존했다. 정규 순서로 고정했다. |

### 검증 증거

환경: macOS/arm64, Python 3.12.11.

| 검증 | 명령 | 결과 |
| --- | --- | --- |
| 전체 테스트 | `uv run --no-sync pytest -q` | **469 passed** (기존 449 + 신규 20) |
| 성능 guard | `uv run --no-sync pytest -q -m perf` | **4 passed** |
| 회귀 테스트 판별력 | 신규 테스트를 수정 전 소스에 실행 | **17 failed** (나머지 3개는 기존 동작의 커버리지 보강) |
| artifact 바이트 불변 | fixture 13개를 adapter로 재수집해 SHA-256 비교 | **전부 동일**, canonical form도 동일 |
| 보고서 변화 | 동일 fixture의 analysis report 비교 | 5개에서 이전에 삼켜졌던 `error` finding 1건씩 **추가**, 삭제·경고 변화 없음 |
| lint | `uvx ruff@0.14.2 check src tests scripts examples` | **All checks passed** |
| 설치된 wheel | core wheel 환경에서 `scripts/verify_wheel.py --extra core` | **PASS**, `candidate_golden: byte-identical` |

원격 CI와 실제 Phoenix + SyncMill E2E는 이번에도 **미실행**이다. 로컬 통과를 원격 성공으로 표기하지 않는다.

### 남은 결정 필요 항목 (Tier 2 — 공개 계약 변경)

1. **Phoenix 타임스탬프**: naive ISO를 로컬 타임존으로 해석해 `artifact_digest`가 기계마다 달라진다
   (`TZ=Asia/Seoul`과 UTC CI가 9시간 차이). UTC로 고정하면 digest가 바뀌므로 fixture 재생성이 필요하다.
   숫자 타임스탬프도 나노초로 가정하며 하한 검증이 없다.
2. **TREE_PARENT 선택**: 가장 이른 원인을 고르므로 `SPAN_LINK`가 containment parent를 이긴다.
   `EdgeOrigin` 우선순위를 도입하면 link를 가진 모든 artifact의 derived layer가 바뀐다.
3. **privacy 기본값**: OTLP `include_error_messages`의 기본값을 `False`로 바꿀지, `--redact-errors`
   옵션을 추가할지. 이번에는 README의 범위 설명만 정정했다.
4. **review-candidates 스키마**: candidate 수준이 `additionalProperties: true`라 body 필드가 v1으로 검증된다.
5. **`phoenix` 종료 코드**: 같은 환경 실패에 `doctor`는 1, `diagnose`는 2를 쓴다.
6. **LICENSE 부재**: 배포용으로 패키징되어 있으나 라이선스가 없다. 라이선스 선택은 소유자의 결정이다.


## 현재 전달 상태 — 2026-09-08 (Asia/Seoul)

- 기준 HEAD: `d048f400fdd39bb37c9608e45ee5931cba058b46`, 브랜치 `tracegraph-mvp`.
- 아래 결과는 이 HEAD 위의 전달 변경을 **커밋 전에** 검증한 기록이다.
  최종 커밋 SHA는 이 문서를 포함한 Git 이력에서 확인한다. push·릴리스는 수행하지 않았다.
- 이번 범위: CI import 복구, 설치된 wheel 검증 자동화, 문서 상태 연결.
- 제품 소스·의존성·lockfile·fixture·공개 데이터 계약은 변경하지 않았다.

## 해결한 문제와 변경

2026-09-08의 [기준 HEAD CI](https://github.com/memtomem/tracegraph/actions/runs/34198950320)는
core `389 passed, 2 failed`, Cypher `430 passed, 2 failed`였다. 실패한 두 테스트는
`examples`와 `scripts`를 저장소 루트에서 import할 수 있다고 가정했다.
`pytest`에서는 실패하고 `python -m pytest`에서는 통과하는 차이를 로컬에서도 재현했다.
과거의 Actions billing 차단과는 다른 문제다.

- WAL snapshot 테스트의 예제 import를 기존 테스트와 같은 `from tiny_agent import run`으로 정렬했다.
- E2E report assertion 테스트는 테스트 파일 기준 절대 경로에서 검증 스크립트를 `importlib`로 읽는다.
  `sys.path`에 저장소 루트를 추가하거나 테스트 내용을 건너뛰지 않는다.
- [일반 CI](../.github/workflows/test.yml)에 Linux/Python 3.12의 `installed wheel (core/cypher)`
  두 job을 추가했다. wheel/sdist 빌드 → lockfile에서 runtime 의존성 export → hash 검증 설치 →
  wheel `--no-deps` 설치 → 의존성 검사 → CLI smoke 순서다.
- [wheel verifier](../scripts/verify_wheel.py)는 임시 디렉터리에서 설치된 console entrypoint를 실행한다.
  격리 Python 프로세스로 import 위치가 환경의 `site-packages`인지, core에 Ladybug가 없는지 확인한다.
  v1 fixture 읽기, v2 ingest, baseline 진단, diff의 동일/상이 종료 코드, query의 매치/미매치,
  candidate golden의 exact bytes/digest, report privacy, Cypher 설명·매치 및 artifact round-trip을 검증한다.
  이 검증기는 설치 환경에 개발 의존성을 요구하지 않는다. 공개 JSON schema는 계속 저장소-vendored 계약이다.

## 검증 증거

환경: macOS/arm64, Python 3.12.11. 아래 결과는 위 기준 HEAD와 이번 작업 트리 변경의 조합이다.

| 검증 | 명령 / 방법 | 결과 |
| --- | --- | --- |
| 기존 실패 2개 | `pytest`와 `python -m pytest`로 두 node ID 실행 | 각각 **2 passed** |
| 독립 core + 설치 wheel | 잠긴 dev 의존성, Ladybug 미설치 확인 후 `pytest -q -m 'not perf'` | **391 passed, 3 skipped, 4 deselected** |
| 기존 Cypher 개발 환경 | `.venv/bin/pytest -q -m 'not perf'` | **432 passed, 4 deselected** |
| 성능 | `.venv/bin/pytest -q -m perf` | **4 passed, 432 deselected** |
| wheel/sdist | `uv build --out-dir /private/tmp/tracegraph-wheel-20260908/dist` | **PASS** |
| core runtime-only wheel 환경 | 해당 환경 Python으로 `scripts/verify_wheel.py --extra core` | **PASS**, Ladybug 없음 |
| Cypher runtime-only wheel 환경 | 해당 환경 Python으로 `scripts/verify_wheel.py --extra cypher` | **PASS**, backend parity 포함 |

전체 테스트의 첫 sandbox 실행은 로컬 HTTP 바인딩 제한으로 3개 실패했다.
동일 테스트를 권한이 허용된 환경에서 재실행한 결과가 위 Cypher 432개 통과다.
uv 캐시 접근 역시 권한 확장 후 검증했다. 이를 제품 실패나 외부 서버 검증으로 해석하지 않는다.

원격 상태는 별도다:

- **수정본의 GitHub Actions: 미실행.** 로컬 통과를 새 원격 CI 성공으로 표기하지 않는다.
- **수정본의 실제 Phoenix + SyncMill E2E: 미실행.** 최근 확인한
  [2026-09-07 성공 실행](https://github.com/memtomem/tracegraph/actions/runs/34099825293)은
  이전 Tracegraph SHA `e1a4d44aad5dcd5da736d75f48974173241422e6`의 증거다.
- 업로드 이후에는 해당 커밋 SHA의 core/Cypher/perf/Phoenix CLI/wheel job 결과를 기록한다.
  실제 서버 E2E를 실행하면 Tracegraph·SyncMill SHA, Phoenix image digest, px pin도 함께 기록한다.
  절차는 [E2E runbook](phoenix-syncmill-e2e.md)과 [runtime pin runbook](phoenix-cli-contract.md)을 따른다.

### Wheel 검증 재실행

Linux/macOS에서 저장소 루트의 Bash로 실행한다. `uv`와 Python 3.12가 필요하다.
`WHEEL_EXTRA=cypher`로 바꾸면 선택 backend도 검증한다. 임시 설치 경로는 공유 작업 트리와 분리된다.

```bash
set -euo pipefail
WHEEL_EXTRA=core
WHEEL_ROOT=$(mktemp -d)
uv build --out-dir "$WHEEL_ROOT/dist"
if [ "$WHEEL_EXTRA" = cypher ]; then
  uv export --locked --no-dev --no-emit-project --extra cypher --output-file "$WHEEL_ROOT/requirements.txt"
else
  uv export --locked --no-dev --no-emit-project --output-file "$WHEEL_ROOT/requirements.txt"
fi
uv venv --python 3.12 "$WHEEL_ROOT/venv"
uv pip sync --python "$WHEEL_ROOT/venv/bin/python" --require-hashes "$WHEEL_ROOT/requirements.txt"
uv pip install --python "$WHEEL_ROOT/venv/bin/python" --no-deps "$WHEEL_ROOT"/dist/*.whl
uv pip check --python "$WHEEL_ROOT/venv/bin/python"
"$WHEEL_ROOT/venv/bin/python" scripts/verify_wheel.py --extra "$WHEEL_EXTRA"
```

## 다음 작업과 계약 경계

### 2026-09-08 리뷰 후속 수정

`7ba259b` 리뷰에서 발견된 두 항목을 해당 커밋 위 작업 트리에서 수정했다.

- Wheel verifier는 `diff` 차이/`query` 미매치의 exit 1 외에도 각각 `NOT IDENTICAL`/
  `no matches` 출력 줄을 확인한다. stdout/stderr의 traceback은 실패로 처리하며,
  출력 계약 없이 nonzero를 성공으로 지정하는 것도 거부한다.
- 재실행 명령과 CI에서 빈 Bash 배열을 제거하고 core/Cypher export를 분기했다.
  macOS 기본 Bash 3.2의 `set -u` 아래에서도 빈 배열 확장 오류가 발생하지 않는다.
- 실제 child process의 정상·빈 출력·잘못된 출력·traceback·잘못된 exit를 검사하는
  신규 회귀 **13개 통과**, 기존 리뷰 회귀와 함께 **69개 통과**.
- 수정된 verifier로 기존 독립 core/Cypher wheel 환경의 smoke **모두 통과**.
  제품 소스와 패키지 의존성은 이 후속 수정에서도 바꾸지 않았다.
- Bash 3.2에서 문서/CI 각각 core/Cypher의 **4개 shell 경로 통과**.
  이 검사는 uv stub으로 export 인자와 shell 동작을 검증했으며 새 설치 실행 증거는 아니다.

### 기능 개선 백로그

| 우선순위 | 후속 범위 | 설계·수용 기준 |
| --- | --- | --- |
| P1 | LangGraph native 예외 관측 | 실제 `pending_writes.__error__=1`인데 현재 `status=ok`, `error_count=0`인 사례를 해결. task 실패를 이전 정상 checkpoint에 잘못 귀속하지 않으며 중단·재개·복구도 구분한다. |
| P2 | 관측 범위 구조화 | 외부/누락 링크의 수·사유와 지표별 coverage를 보존한다. 현재 일반 경고는 이미 있으므로 경고 추가만으로 완료 처리하지 않는다. |
| P3 | 자원 제한 | stdin·px stdout/stderr byte cap 및 batch·매치 계산 예산. `query --limit`의 표시량 제한과 계산량 제한을 구분한다. |
| P4 | U1 사후 분석 예제 | 정상·state-channel 오류·native 예외·복구/중단·fan-in을 다룬다. 상위 8월 handoff의 "문서·예제만으로 즉시 가능"은 native 예외 감지 공백을 고려해 조정한다. |
| P5 | D2 agent 식별 축 | producer의 agent/slot 식별 근거, privacy, 이름 기반 패턴의 버전 정책을 먼저 고정하고 SyncMill과 교차 검증한다. |

후속 기능은 이번 수정에 포함하지 않았다. Pattern DSL, OTLP 재출력, raw DAG 비교, 서버·자체 UI,
추가 backend는 위 신뢰도 작업 이후 평가한다.

유지할 계약:

- raw multi-parent `CAUSED_BY`가 정본이며 `TREE_PARENT`는 손실 가능한 비교·표시용 projection이다.
- artifact v1/v2 읽기와 v2 쓰기, analysis report v2, candidate v1을 유지한다.
  report schema는 추가 필드를 거부하므로 관측 필드 추가 전에 버전·reader 호환성을 설계한다.
- 자동 retry는 명시적 producer marker만 사용한다. heuristic은 governance export에 사용하지 않는다.
- `safe-v1`은 구조 식별자를 유지하는 본문 제거 정책이며 익명화가 아니다.
  raw artifact를 report privacy 때문에 재작성하지 않고 candidate digest는 입력 파일의 exact bytes에 묶는다.
- 실행·enforcement는 SyncMill, 사전 정책 근거는 Toolgraph, 사후 분석은 Tracegraph의 역할이다.

## 기록 찾아가기

- [2026-09-07 구현 리뷰](reviews/2026-09-07-implementation-review.md): 원검토와 F-01~10 해결 기록.
  앞부분의 재현 결과와 line anchor는 당시 SHA 기준이며, 현재 미해결 목록으로 재사용하지 않는다.
- [입문자 가이드](USAGE_KO.md), [생태계 연계](ecosystem-integration-plan.md): 사용법과 생산자/소비자 계약.
- [Feasibility](FEASIBILITY.md): 초기 전략·시장 가설을 기록한 배경 문서이며 현재 경쟁 현황의 검증 자료는 아니다.
- `.dev-trio`와 `mm` 과거 메모리는 설계 이유를 복원하는 자료다. remote 부재, Kùzu 사용,
  PR #19 진행 중, Actions billing 차단, `d048f40` 미푸시 등의 과거 상태를 현재 사실로 복사하지 않는다.
