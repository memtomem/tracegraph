# Tracegraph handoff

## 현재 전달 상태 — 2026-10-14 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp` @ `9d28b13`(PR #41 병합), 작업 트리 깨끗. PyPI 최신은
  0.4.0이고, 그 뒤 착지한 미릴리스 변경은 PR #41 하나다(11번). 다음 큰 작업은
  [이슈 #37](https://github.com/memtomem/tracegraph/issues/37)이며 출발점은 14번 항목에 있다.
- **상태:** [PR #28](https://github.com/memtomem/tracegraph/pull/28)로 착지했다(merge `23e94697`, 2026-10-13). 기준 커밋은 `e5d86c1`(PR #27 병합)이고, 그 위 커밋 19개다.
  브랜치 16개를 한 줄로 쌓아 최상단 `docs/unset-status-by-source` 하나로 PR을 열었고, merge
  commit으로 합쳐 아래 표의 커밋이 그대로 남아 있다.
- **릴리스:** `agent-tracegraph` 0.3.0을 PyPI에 공개했다(2026-10-13). 준비 커밋은
  [PR #29](https://github.com/memtomem/tracegraph/pull/29)(merge `008502d`)이고, `test-v0.3.0`과
  `v0.3.0`이 모두 이 커밋을 가리킨다. 아래 7번 항목에 검증 결과를 적었다.
- **0.3.1 릴리스:** [PR #31](https://github.com/memtomem/tracegraph/pull/31)(merge `be1930c`,
  후속 3번)과 [PR #32](https://github.com/memtomem/tracegraph/pull/32)(merge `f325e82`, 후속 4번)을
  0.3.1로 PyPI에 공개했다(2026-10-13). 준비 커밋은
  [PR #34](https://github.com/memtomem/tracegraph/pull/34)(merge `54ab5e1`, PR #33 포함)이고,
  `test-v0.3.1`과 `v0.3.1`이 모두 이 커밋을 가리킨다. 아래 8번 항목에 검증 결과를 적었다.
- **0.4.0 릴리스:** [PR #36](https://github.com/memtomem/tracegraph/pull/36)(merge `b248954`)이
  더한 `interrupt()`로 멈춘 LangGraph task 보고와 artifact `schema_version` 5를 0.4.0으로 PyPI에
  공개했다(2026-10-14). 0.3.1은 v5 artifact를 읽지 못하므로 minor다. 준비 커밋은
  [PR #39](https://github.com/memtomem/tracegraph/pull/39)(merge `3451374`)이고, `test-v0.4.0`과
  `v0.4.0`이 모두 이 커밋을 가리킨다. 범위는 아래 9번, 릴리스 검증은 10번 항목에 적었다.
- **계기:** 0.2.0 공개 이후 첫 전체 cold 리뷰. 탐색 결과를 재현으로 확인하고, 계획을 리뷰
  게이트 2라운드에 통과시킨 뒤 소유자 결정(0.3.0, D2 식별자 필터, D3 포함, 집계 범위·서버
  핀·Python matrix 보류)을 받아 구현했다.
- **공개 계약:** 커밋된 모든 fixture에서 canonical artifact, 분석 보고서, review 후보의 바이트는
  그대로다. 그 밖의 입력에서는 바뀔 수 있다. 예를 들어 자유 텍스트 annotation이 있는 Phoenix
  export를 다시 가져오면 artifact 바이트와 digest가 달라진다(D2). PR #28의 바뀐 출력과 호환성
  영향은 `CHANGELOG.md`의 `0.3.0 - 2026-10-13`에, 그 뒤 PR #31·#32의 것은 `0.3.1 - 2026-10-13`에
  있다.

### 브랜치와 범위

| 순서 | 브랜치 | head | 내용 |
| --- | --- | --- | --- |
| 1 | `fix/artifact-load-boundary` | `c839d70` | `schema_version` 정수 강제, envelope이 본문보다 낮은 버전을 선언하면 거부, v4 판정을 TREE_PARENT edge만으로, NaN/Infinity 거부, LadybugDB INT64 범위 밖 `seq`를 사용 오류로, InMemoryStore dangling edge 거부 |
| 2 | `docs/changelog-backfill` | `60b3e88` | 0.2.0 이후 누락된 계약 변경 3건(schema v4, Phoenix naive→UTC, `export-incident-memo`) 기록, README Status를 기능·한계 표로 |
| 3 | `fix/adapter-ingest-boundary` | `d9f1037` | 읽을 수 없는 timestamp는 "사용 불가 + stderr 공시", 잘못된 id·bool status 거부, bool 토큰 수 무시, raw OTLP의 evaluation 주입 차단, LangGraph 파생 id 충돌 거부와 task 이름 해석 실패 공시 |
| 4 | `fix/cli-render-escape` | `b5288ce` | `diff`·`validate`·출력 경로 escape, `diff --structure`의 빈 label 짝짓기 제거, 이름 없는 step 라벨을 kind로 통일, memo `links_preserved` 표기 통일 |
| 5 | `fix/diagnose-metric-deltas` | `5c545e8` | 토큰·비용 delta도 duration처럼 양쪽 전 step 커버리지일 때만 발행, 통화 혼재 공시 |
| 6 | `fix/untimed-spans-never-pick-a-parent` | `ee4f0d5` | 시작 시각이 없거나 읽을 수 없는 span은 반복 `graph.node.parent_id`의 "가장 가까운 앞선 실행" 선택에 참여하지 않음 |
| 7 | `refactor/consolidate-writers-and-dead-code` | `9f8938c` | atomic writer 4벌 통합(쓰기 실패 시 임시 파일이 남던 2곳 수정), 공유 상수, 도달 불가 코드 삭제 |
| 8 | `refactor/normalize-and-analysis-helpers` | `5683fcb` | `raw_layer`, `ProjectionRule`, `incoming_edges_by_step`, `ancestor_ids`, `pattern_findings` |
| 9 | `refactor/cli-phoenix-layer` | `1f98e73` | px 명령 인자, 로드 오류, Match 생성 중복 제거. Phoenix 계층 모듈 분리는 이득이 없어 하지 않음 |
| 10 | `chore/mypy-gate` | `376c26e` | mypy 오류 26건→0, CI lint job에 mypy gate(`typing` 그룹), px 인자 고정 테스트 보강 |
| 11 | `chore/ladybug-0.21` | `dee60e0` | `[cypher]` extra `ladybug>=0.18,<0.22`, lockfile 0.21.2 |
| 12 | `chore/phoenix-cli-1.18` | `301d014` | CI 핀 Phoenix CLI 1.18.8. 런타임 하한 1.0.4는 그대로 |
| 13 | `ci/upstream-drift` | `613e764` | 최신 langgraph·langgraph-checkpoint·ladybug·px로 주간 실행하는 별도 workflow |
| 14 | `fix/phoenix-annotation-body-free` | `72770a5` | Phoenix artifact에서 자유 텍스트 annotation name/label 제거(D2) |
| 15 | `docs/handoff-2026-10-13` | `23b3571` | 이 인계 기록. 코드 변경 없음 |
| 16 | `docs/unset-status-by-source` | `c89315a` | D3 (a): OTLP와 Phoenix의 UNSET 해석 차이를 README·`USAGE_KO.md`·adapter 주석에 기록, 이 문서의 D3 항목 갱신. 동작 변경 없음 |

### 검증 증거

환경: macOS/arm64, Python 3.12.11. 모두 최상단 코드 커밋(`72770a5`)에서 이 세션에 실행했다. 그 위의 15번 브랜치는 이 문서만 바꾼다.

| 검증 | 결과 |
| --- | --- |
| 전체 테스트 `pytest -q` | **709 passed** (기준 620) |
| 성능 guard `pytest -m perf` | **4 passed** |
| lint `uvx ruff@0.14.2 check src tests scripts examples` | 통과 |
| 타입 `uv sync --locked --extra cypher --group typing && uv run --no-sync mypy` | **0 errors** (기준 26). 오류를 일부러 넣으면 rc=1 확인 |
| `uv lock --check` | 최신 |
| 골든 하네스 | 모든 커밋 fixture의 artifact·보고서·memo·review 후보·CLI 출력 172개 파일을 `e5d86c1`과 비교. artifact·분석 보고서·후보는 **바이트 동일**. 바뀐 것은 브랜치 4의 의도된 출력뿐(CLI 11개, memo 14개) |
| LadybugDB 0.21.2 환경의 골든 | 0.18.1과 동일 |
| 최신 업스트림(langgraph 1.2.15, langgraph-checkpoint 4.3.0, langgraph-checkpoint-sqlite 3.1.2, ladybug 0.21.2), `drift.yml`과 같은 설치 절차 | **705 passed**, 4 deselected (perf 제외. 잠긴 환경과 같은 수) |
| Phoenix CLI 1.18.8 | CI의 read-contract 단계와 `scripts/verify_phoenix_cli_contract.py` 통과 |
| drift 탐지력 | task-id 재계산 상수 하나를 바꾸면 native-failure 테스트 10개 실패 |
| 회귀 테스트 판별력 | 새 테스트는 수정 전 소스에서 실패, 경계·인과 가드는 하나씩 제거해 해당 테스트가 실패함을 확인 |
| 리뷰 게이트 | 계획 2라운드, 브랜치마다 diff 1~3라운드. 모든 지적을 반영하거나 아래 후속 항목으로 보류. 브랜치 7·8·9는 SHIP |

골든 하네스는 저장소 밖 임시 스크립트로 돌렸고 커밋하지 않았다.

원격 증거(PR #28 head `c89315a`와 merge `23e94697`):

- PR·push CI 전체 job 통과: lint(mypy 포함), core, Cypher, perf, 설치 wheel core·Cypher,
  Phoenix CLI 1.18.8 계약. drift workflow의 최신 업스트림·최신 px job, CLA도 통과.
- [Phoenix + SyncMill E2E](https://github.com/memtomem/tracegraph/actions/runs/38359701650)
  attempt 3 통과(Phoenix CLI 1.18.8, server 18.0.0). attempt 1·2는 SyncMill checkout에서
  `could not read Username`으로 실패했다. 원인은 2026-07-14에 설정한 `SYNCMILL_REPO_TOKEN`의
  만료였고, 소유자가 갱신한 뒤 통과했다. 이 token은 만료 주기마다 갱신해야 한다.
- merge `23e94697`의 [tests](https://github.com/memtomem/tracegraph/actions/runs/38377765113) 7개 job 통과.

editable 설치를 공유하는 git worktree에서 테스트할 때는 `PYTHONPATH=src`가 필요하다.
그렇지 않으면 다른 checkout의 소스를 import한다.

### 남은 결정과 후속 항목

1. **D3 — OTLP UNSET 보존: 결정됨(2026-10-13), (a) 현행 유지와 차이 문서화.** 승인 후
   측정으로 전제가 뒤집혀 보존은 구현하지 않았다. OTel에서
   UNSET은 성공 span의 기본 상태이고 SyncMill도 그렇게 낸다. `unset`을 보존하면
   `gate-failure-after-success`(`{status=ok}`)가 계약 fixture `compete-gate-reject`에서 사라지고
   계약 테스트 1개가 실패하며 artifact 11개·보고서 13개가 바뀐다. 선택지: (a) 현행 유지와
   차이 문서화, (b) Phoenix도 UNSET→OK, (c) UNSET 보존 + 프리셋이 unset을 성공으로
   취급하고 버전 bump. 소유자가 (a)를 골랐고 브랜치 16이 차이를 문서화했다. SyncMill fixture
   10개를 UNSET 보존으로 다시 읽으면 trace status는 모두 같고, preset 결과가 달라지는 것은
   `compete-gate-reject`의 `gate-failure-after-success` 하나다. 서로 다른 경로로 읽은 실행을
   baseline으로 비교하면 `ok`→`unset` behavior change가 생기므로(`route-fallback` 대
   `route-success`에서 `syncmill.run` 1건 측정) 문서는 같은 경로끼리 비교하라고 적었다. 같은 실행을 Phoenix로 읽으면
   이 preset이 나오지 않는 것은 그대로이며, 이를 바꾸려면 (b)나 (c)를 다시 열어야 한다.
2. **반복 `graph.node.parent_id`의 시간 기반 선택.** 리뷰어가 두 번 "timing으로 인과를 정한다"고
   지적했다. 기존 설계 결정으로 유지했고, 시각 없는 span만 제외했다(브랜치 6). 2026-10-13
   소유자 결정: 실제 루프 trace로 측정하기 전까지 현행 유지. 지금까지 측정한 trace 중 이 분기를
   탄 것은 없다. 다른 producer나 설정까지 배제하는 근거는 아니다. 커밋된 OTLP fixture 11개의 `graph.node.parent_id` span 42개는 모두
   후보가 하나였고, SyncMill 계약은 `graph.node.id`를 trace 안에서 유일하게 정한다.
   저장소 밖 scratch 환경에서 `openinference-instrumentation-langchain` 0.1.79로 LangGraph
   1.2.15의 agent↔tool 루프와 `Send` fan-out 하나씩을 계측한 trace에는 `graph.node.*` 속성이 아예 없어, 모든 edge가
   `SPAN_PARENT_FALLBACK`이었다. 다시 열 조건은 node id를 반복해서 내보내는 producer의 실제
   trace다. 그때의 선택지는 구조 조상 확인, origin 강등, `parentSpanId`로 되돌림이다. origin
   강등은 새 origin 값을 만들면 artifact 스키마 버전이 필요하고, 기존 값을 재사용하면 그 값의
   의미와 projection 호환성을 먼저 확인해야 한다.
3. **부분 커버리지 합계와 거부된 텔레메트리 값: 해결(PR #31, merge `be1930c`).**
   - 부분 합계는 이미 공시되고 있었다. 토큰·비용 합계가 하나라도 있으면 보고서에 "coverage
     may be partial" 경고가 붙고, delta는 모든 step이 값을 보고할 때만 낸다. 바꾸지 않았다.
   - 버려지던 값은 실제 버그였다. importer가 읽을 수 없는 `llm.cost.total`을 버려서 보고서가
     나머지 비용만 더한 과소 합계를 경고 없이 냈다. 이제 고정 표식 `"unreadable"`을 저장해 합계를
     보류한다. 토큰 수, 개별 비용, 통화, Phoenix 점수는 그대로 unavailable이지만 stderr로
     공시한다. `intValue` 소수의 절삭(`1.5`→`1`)도 고쳤다.
   - 커밋된 fixture 전체의 artifact·보고서·메모·후보·CLI stdout은 `fe6aefb`와 바이트가 같다.
     Phoenix fixture의 `llm.cost.prompt = "SECRET COST TEXT"` 때문에 그 fixture를 읽는 명령의
     stderr에 공시 한 줄이 추가됐다(값은 출력하지 않는다).
4. **LangGraph checkpoint 형태 검증: 해결(PR #32, merge `f325e82`).**
   - fuzz(실제 이력에서 필드 하나씩 바꾼 64가지)로 약 30가지 traceback과 조용한 결과 변경
     (부모 버림→root, `step` `true`/`1.5`→1, 잘못된 쓰기 무시)을 측정했다.
   - 계획 리뷰 2라운드가 손으로 짠 형태 검사의 빈틈을 4건씩 더 찾아, 소유자 결정으로
     LangGraph가 선언한 `Checkpoint`/`CheckpointMetadata` 타입을 pydantic strict로 검증하는
     방식으로 바꿨다. 어댑터가 읽는 필드만 검사한다(`counters_since_delta_snapshot`은 선언은
     tuple이지만 saver가 list로 돌려주므로 제외). 디코딩 중 예외는 사용 오류로 바꾸고, 잘못된
     쓰기는 개수를 stderr로 공시한다.
   - langgraph-checkpoint 4.1.0(하한)과 4.3.0(최신)에서 동작을 확인했고, golden은 `be1930c`와
     차이 0건이다.
5. **보류된 계획 항목.** Phoenix 서버 핀 20.21.0(E2E 필요), Python 3.13/3.14 matrix(branch
   protection의 required check 이름 변경 필요), 토큰·비용 집계 범위 축소, adapter
   `from_file`의 크기 제한.
6. **기존 보류 결정**(아래 2026-09-15 절의 3·4·5·7·8번)은 그대로다.
7. **0.3.0 릴리스: 완료(2026-10-13).** [릴리스 런북](releasing.md)의 절차를 따랐고, 마지막의
   프로젝트 페이지 확인만 아래처럼 브라우저 없이 했다.
   - TestPyPI 예행: [run 38380512000](https://github.com/memtomem/tracegraph/actions/runs/38380512000).
     production: [run 38381060049](https://github.com/memtomem/tracegraph/actions/runs/38381060049).
   - wheel `03fecb59…6348`, sdist `0c56e110…2a56`. TestPyPI 파일, production build, PyPI 파일이
     바이트 단위로 같고, 두 파일 모두 `release.yml`·`pypi` environment의 publish attestation이 있다.
   - PyPI에서 받은 wheel의 깨끗한 core·`[cypher]` 설치에서 `scripts/verify_wheel.py` 통과.
   - [GitHub Release v0.3.0](https://github.com/memtomem/tracegraph/releases/tag/v0.3.0)은
     prerelease로 만들었다.
   - 프로젝트 페이지: PyPI JSON의 description이 `v0.3.0`의 `README.md`와 바이트 단위로 같고
     (`text/markdown`), project URL 4개와 페이지가 HTTP 200을 돌려준다. 렌더링 결과를 눈으로
     보는 확인은 하지 않았다.
8. **0.3.1 릴리스: 완료(2026-10-13).** 0.3.0과 같은 절차를 따랐다.
   - TestPyPI 예행: [run 38400016801](https://github.com/memtomem/tracegraph/actions/runs/38400016801).
     production: [run 38404287958](https://github.com/memtomem/tracegraph/actions/runs/38404287958).
   - wheel `d2c955ee…0e97`, sdist `7d23fa70…7fbf`. TestPyPI 파일, production build, PyPI 파일이
     바이트 단위로 같고, 두 파일 모두 `release.yml`·`pypi` environment의 publish attestation이 있다.
   - PyPI에서 받은 wheel의 깨끗한 core·`[cypher]` 설치에서 `scripts/verify_wheel.py` 통과.
     PyPI description은 `v0.3.1`의 `README.md`와 같고 페이지는 HTTP 200이다.
   - 업로드 직후 `uvx --from agent-tracegraph==0.3.1`이 "no version"으로 한 번 실패했다. JSON API에는
     이미 있었고 약 15초 뒤 재시도에서 설치됐다. simple index 반영 지연이다.
   - [GitHub Release v0.3.1](https://github.com/memtomem/tracegraph/releases/tag/v0.3.1)은
     prerelease로 만들었다.
9. **P1b — `interrupt()`로 멈춘 task 보고: 범위를 좁혀 착지(PR #36, merge `b248954`, 2026-10-14).**
   - thread의 가장 최근 root checkpoint에서 pending write가 정확히 `{__interrupt__}`인 task만
     `unset` step(`source=interrupt`)으로 보고한다. 이 step은 "기록된 이력 안에서 멈춘 채 resume도
     결과도 없다"는 뜻이며, 지금 LangGraph가 재개할 task라는 주장이 아니다. 다른 그래프로 다시
     실행한 thread가 checkpoint를 남기지 않고 끝나는 경우를 재현했다.
   - interrupt가 resume·오류와 함께 기록되고 결과가 없으면 checkpoint만으로는 판정할 수 없어
     ingest 때 공시만 한다. LangGraph 1.2.2에서는 resume 뒤 아무것도 쓰지 않은 task와 두 번째
     `interrupt()`에 걸린 task가 같은 write를 남긴다. 1.2.15는 앞의 경우에 `__no_writes__`를 남겨
     완료로 읽힌다.
   - 소유자 결정: 새 `StepSource.INTERRUPT`와 내용 기준 artifact v5(D-A), trace status 유지(D-B).
     다른 artifact의 바이트는 그대로이고, 0.3.1은 v5를 이름으로 거부한다.
   - 처음 계획은 그래프 없이 trigger 채널로 예약된 task를 추론했는데, 리뷰 2라운드가 같은
     단위에서 완료된 실행을 미완료로 읽는 경우를 계속 찾았다(직렬화된 barrier, 예약 채널 이름의
     user state key, 같은 thread의 그래프 교체, 반복 subgraph namespace). 시작하지 않은 task
     (recursion limit, kill, `interrupt_before`/`after`)는 컴파일된 그래프가 필요해
     [이슈 #37](https://github.com/memtomem/tracegraph/issues/37)로 남겼다. subgraph 안의 interrupt는
     root의 subgraph 노드로만 보고한다.
   - 검증: 테스트 928개(신규 35개, InMemorySaver·SqliteSaver), 의도적 회귀 6개 모두 검출, 커밋된
     fixture 출력 121개가 `b05d919`와 바이트 단위로 같다. 최신 LangGraph 1.2.15·checkpoint 4.3.0에서도
     전체 테스트가 통과한다. 리뷰 게이트는 좁힌 계획 2라운드, 구현 2라운드(SHIP, SHIP).
   - 원래 P1b 계획 파일(`2-tracegraph-transient-bonbon.md`)은 사라져 복구하지 못했다. 다시 세운
     계획과 재현 스크립트는 저장소 밖 `~/.claude/plans/p1b-pending-tasks.md`, `p1b-probes/`에 있다.
10. **0.4.0 릴리스: 완료(2026-10-14).** 0.3.1과 같은 절차를 따랐다.
   - TestPyPI 예행: [run 38478479784](https://github.com/memtomem/tracegraph/actions/runs/38478479784).
     production: [run 38479504984](https://github.com/memtomem/tracegraph/actions/runs/38479504984).
   - wheel `dad06577…c347`, sdist `c5bceb9b…dad8`. 예행 build, TestPyPI 파일, production build,
     PyPI 파일이 바이트 단위로 같고, 두 파일 모두 `release.yml`·`pypi` environment의 publish
     attestation이 있다.
   - TestPyPI와 PyPI에서 받은 wheel의 깨끗한 core·`[cypher]` 설치에서 `scripts/verify_wheel.py`
     통과(cypher backend parity 포함). PyPI description은 `v0.4.0`의 `README.md`와 같고 페이지는
     HTTP 200이다. TestPyPI wheel로 `interrupt()`에서 멈춘 SQLite thread를 ingest하면
     `schema_version` 5와 `source=interrupt` step이 나온다.
   - 업로드 직후 `uvx --from agent-tracegraph==0.4.0`이 한 번 "unsatisfiable"로 실패하고 15초 뒤
     재시도에서 설치됐다. 0.3.1 때와 같은 simple index 반영 지연이다.
   - [GitHub Release v0.4.0](https://github.com/memtomem/tracegraph/releases/tag/v0.4.0)은
     prerelease로 만들었다.
   - 관찰: `inspect` 트리는 `unset` step에 표식을 붙이지 않아 interrupt step이 노드 이름만으로
     보였다. 11번에서 해결했다.
11. **`inspect`의 interrupt 표시: 착지, 미릴리스(PR #41, merge `9d28b13`, 2026-10-14).**
   `source=interrupt` step을 노란색 `⏸ interrupted`로 표시하고, 하나 이상이면 요약 줄에
   `· N interrupted`를 붙인다. 다른 `unset` step(Phoenix `unset`, 취소된 task)은 그대로이고,
   interrupt step이 없는 trace의 출력은 바이트 단위로 같다(커밋된 fixture 출력 121개 확인).
   `CHANGELOG.md`의 Unreleased에 있으며 다음 릴리스(0.4.1)에 들어간다. 리뷰 2라운드(NEEDS-FIX:
   테스트의 터미널 폭 의존 → console 폭 고정, SHIP).
12. **`CLAAssistant`를 required check로: 완료(2026-10-14).** 소유자가 branch protection을 바꿔
   `tracegraph-mvp`의 required check가 8개가 됐다(`app_id` 15368, GitHub Actions). PR #41이 이 규칙
   아래 첫 병합이다. 운영 주의: 외부 기여자의 서명 댓글은 PR head에서 check를 다시 돌리지
   않으므로, 서명 뒤 Actions에서 `CLAAssistant`를 재실행해야 병합할 수 있다. `dependabot[bot]`은
   allowlist에 있다.
13. **좁은 터미널에서 실패하던 테스트: 해결(2026-10-14).** 실제 범위는 `test_cli.py` 13개가 아니라
   전체 suite였다. `COLUMNS=40`에서 85개(`test_checkpoint_shape_boundary.py` 60개 포함),
   `TERMINAL_WIDTH=40`에서 77개가 실패했다. pytest capture 아래에서는 실제 터미널이 보이지 않아
   폭은 80이고, 이 두 환경변수만 그것을 바꾼다(40열 pty에서 `COLUMNS` 없이 돌리면 수정 전에도
   전부 통과). 대부분은 typer가 에러마다 새로 만드는 콘솔이라 fixture로는 닿지 않으므로,
   `tests/conftest.py`가 import 시점에 `COLUMNS=80`을 고정하고 `TERMINAL_WIDTH`를 지운다(`TERM=dumb`과
   같은 방식). 네 조합(40, 200, `TERMINAL_WIDTH=40`, 둘 다)에서 937개 통과. 다른 폭이 필요한
   테스트는 PR #41의 `_inspect_at_width`처럼 `Console(width=...)`를 직접 만든다.
14. **이슈 #37 출발점(시작 전).** 시작하지 않은 task(recursion limit, kill,
   `interrupt_before`/`after`)를 opt-in 컴파일 그래프(`--graph module:attr`)로 보고한다.
   - 먼저 정할 계약: 사용자 코드 import라는 신뢰 경계, 그래프 동일성 확인과 artifact 기록,
     "시작 안 함" 표식(새 `StepSource`와 내용 기준 artifact v6 후보), CLI 모양.
   - 그래프 없는 추론을 버린 근거(완료된 실행을 미완료로 읽는 4가지)는 이슈 본문과 9번 항목에
     있다. 수용 기준도 같다: 완료·재개·fork된 실행에 미완료 작업을 표시하지 않는다.
   - `get_state`는 oracle이 아니다. LangGraph 1.2.2는 두 번째 `interrupt()` 뒤 `next=()`를
     보고한다(1.2.15는 바로잡음). `invoke` 반환값과 대조하고, 두 버전 모두에서 측정한다(drift
     워크플로가 최신 버전을 돌린다).
   - 재현 스크립트 `~/.claude/plans/p1b-probes/probe{,2..7}.py`(저장소 밖, `uv run --no-sync python
     <file>`)가 barrier·예약 이름 state key·그래프 교체·subgraph replay 시나리오를 이미 만든다.

---

## 이전 전달 상태 — 2026-09-15 (Asia/Seoul)

아래 본문은 당시 기록이다. 이후 PR #27로 착지했다(`e5d86c1`). 현재 상태와 미해결 목록은 문서 상단을 따른다.


- **문서 작성 시점의 상태:** Phase 1은 기본 브랜치에 병합·푸시 완료. Phase 2.1(TREE_PARENT Origin 우선순위)은 로컬 구현·검증 완료이며, 아래 기록은 커밋 전 작업 트리를 기준으로 한다.
- Phase 2.1 전달 대상은 소스 3개·테스트 1개·문서 2개, 총 6개 파일이다. 이 기록 시점에는 미커밋·미푸시이고 PR·해당 변경의 원격 CI는 대기 상태다. 이후 착지 결과는 이 문서를 포함한 Git 이력과 PR의 최종 SHA로 확인한다.
- 기준 커밋: `8f6c3fa` (Merge feat/phoenix-utc-and-incident-memo), 브랜치 `feat/tree-parent-origin-preference`.
- 이번 범위:
  1. **Phoenix naive timestamp UTC 정규화** (`src/tracegraph/adapters/phoenix_export.py`):
     - timezone-naive datetime을 UTC로 명시 해석하여 `TZ` 환경(로컬 `Asia/Seoul` vs UTC CI)에 따른 `artifact_digest` 불일치를 원천 차단. 기존 `Z` 접미사 타임스탬프는 100% 바이트 불변.
  2. **memtomem LTM incident memo export** (`src/tracegraph/analysis/memo.py`, CLI `export-incident-memo`):
     - 엄격한 allowlist 적용: 원시 `run_id` 대신 결정론적 해시 별칭 `run_digest`(`sha256:...[:16]`), 원시 step/UUID 대신 시퀀스 기반 별칭(`step_<N>_<kind>`), `error_msg`/평가 레이블/페이로드 원천 배제(비밀·토큰 누출 방지).
     - 아티팩트 다이제스트 정규식 검증(`^sha256:[0-9a-f]{64}$`)으로 YAML 주입 방지 및 등록된 프리셋 패턴 한정 출력.
     - 진실한 인과 표현: containment(`span_parent_fallback`) 및 미확인(`legacy_unknown`/None) 에지는 인과 전파 증거가 아니므로 `caused by`가 아닌 `preceded by (containment only)` 등으로 한정 공시.
     - 선형 스케일링: 오류 단계별 선조 중복 순회를 배제하고 단일 `### Causal Ancestry Graph`에 $O(V+E)$로 출력. `analyze()`의 2차 다중 primary failure 탐색을 우회하고 `validate_structure()` 및 `_patterns()`를 직접 호출.
     - 원자적 파일 저장: `NamedTemporaryFile` 진입 즉시 파일명을 확보하여 쓰기/fsync 실패 시 임시 파일 누수 없이 안전 정리.
  3. **`TREE_PARENT` EdgeOrigin 우선순위 및 스키마 v4 도입** (`src/tracegraph/normalize.py`, `src/tracegraph/artifact.py`, `src/tracegraph/store/ladybug.py`):
     - 단일 부모 projection 시 structural execution parent(`GRAPH_PARENT`, `CHECKPOINT_PARENT`: 우선순위 0)가 containment(`SPAN_PARENT_FALLBACK`: 1), cross-reference(`SPAN_LINK`: 2), legacy(`LEGACY_UNKNOWN`/None: 3)보다 우선하도록 변경. 동일 순위 내에서는 기존의 `(seq, ts, step_id)` 시간순 tie-breaker 유지.
     - 하위 호환성 canonical 검증: `validate_normalized()`가 현재의 origin 우선순위 projection뿐 아니라 이전 구현의 temporal-only projection으로 생성된 레거시 아티팩트도 canonical 형태로 인정하여 안전하게 로드.
     - `LadybugStore` 바이트 라운드트립 보존: 레거시 아티팩트 로드 시 `_legacy_projection` 모드를 기억하여 `trace()` 및 `export_artifact()`가 원본 레거시 아티팩트와 바이트 단위로 동일하게 재생성.
     - 아티팩트 `schema_version: 4` 조건부 스탬핑: EdgeOrigin 우선순위에 의해 트리가 변경되는 아티팩트만 `schema_version: 4`로 스탬핑하여 구버전 판독기(1~3 지원)가 명확한 스키마 불일치로 거부하도록 보장. 기존 아티팩트 및 영향 없는 아티팩트는 2 또는 3을 유지하여 기존 골든 다이제스트 바이트 불변성 보장.

### 검증 증거 (2026-09-15)

환경: macOS/arm64, Python 3.12.11.

| 검증 | 명령 | 결과 |
| --- | --- | --- |
| 전체 테스트 | `uv run --no-sync pytest -q` | **620 passed** in 9.99s (이 세션에서 재검증) |
| 성능 guard | `uv run --no-sync pytest -q -m perf` | **4 passed, 616 deselected** in 2.48s |
| Codex 코드 리뷰 | `codex-20260915-164249-65907` | **SHIP** (Major 0, Blocker 0, Nit 1). 소스·테스트 4개 파일 대상이며 테스트는 실행하지 않은 리뷰. README 지적은 후속 반영했고, 이후 작성한 handoff는 이 리뷰 범위 밖이다. |
| lint | `uvx ruff@0.14.2 check src tests scripts examples` | **All checks passed** |
| diff 검증 | `git diff --check` | **Clean** (공백/줄바꿈 결함 없음) |
| 크로스 버전 호환성 | `test_cross_version_schema_signaling_for_origin_priority_and_legacy` | **PASS** (schema 4 스탬핑, 구버전 판독기 거부, 레거시 호환 로드 검증) |
| 레거시 라운드트립 | `test_legacy_temporal_artifact_loads_validates_and_roundtrips` | **PASS** (InMemoryStore 및 LadybugStore 바이트 다이제스트 불변 검증) |

추가 호환성 확인: 기준 HEAD `8f6c3fa`의 실제 `normalize.py`·`artifact.py`를 별도로 로드해
v2/v3 아티팩트를 생성했다. 현재 판독·canonical 검증·Ladybug 재직렬화의 바이트 보존과
기준 HEAD 판독기의 신규 v4 거부를 모두 확인했다. 위 로컬 검증 후에는 문서만 정리했다.
리뷰의 `.final.md`, `.review.json`, `.manifest.json`은 모두 같은 invocation의 `SHIP`으로 일치했다.

원격·실서버 증거는 별도다:

- Phase 1 `8f6c3fa`의 [원격 tests 실행](https://github.com/memtomem/tracegraph/actions/runs/34901110223)은 **성공**했다. Phase 2.1 변경의 CI 증거는 아니다.
- Phase 2.1의 원격 CI와 설치 wheel 검증은 이 기록 시점에 **미실행**이다. PR의 정확한 최종 head에서 확인해야 한다.
- 이번 변경의 실제 Phoenix + SyncMill E2E는 **미실행**이다. [이전 성공 실행](https://github.com/memtomem/tracegraph/actions/runs/34825105597)은 `20d159b` 기준이다.

### 남은 결정 필요 항목 (Tier 2 — 공개 계약 변경)

1. ~~**Phoenix naive ISO의 UTC 해석**~~ — **해결됨 (2026-09-14, `130cb20` / `8f6c3fa`)**: naive datetime을 UTC로 명시 정규화 (`parsed.replace(tzinfo=timezone.utc)`).
2. **TREE_PARENT 선택** — **로컬 구현·검증 완료, 착지 대기 (2026-09-15, 브랜치 `feat/tree-parent-origin-preference`)**: EdgeOrigin 우선순위(GRAPH/CHECKPOINT > SPAN_PARENT_FALLBACK > SPAN_LINK > LEGACY) 도입, 레거시 canonical 검증 호환, LadybugStore 라운드트립 보존, 아티팩트 `schema_version: 4` 조건부 스탬핑.
3. **privacy 기본값**: OTLP `include_error_messages`의 기본값을 `False`로 바꿀지, `--redact-errors` 옵션을 추가할지. README의 범위 설명 정정은 이전 작업에서 완료했다.
4. **review-candidates 스키마**: candidate 수준이 `additionalProperties: true`라 body 필드가 v1으로 검증된다.
5. **`phoenix` 종료 코드**: 같은 환경 실패에 `doctor`는 1, `diagnose`는 2를 쓴다.
6. ~~**LICENSE 부재**~~ — **결정됨(2026-09-12, PR #23)**: Apache-2.0 + DAPADA CLA. 배포명은 `agent-tracegraph`.
7. **Trace-level `status: unset` for incomplete runs**: `interrupt()`나 recursion limit으로 중단된 미완료 실행에 대한 3-way 상태 도입 (아티팩트 스키마 및 마이그레이션 필요).
8. **Phoenix 숫자 타임스탬프**: 숫자 입력을 나노초로 가정하며 하한 검증이 없다. naive ISO의 UTC 정규화와 별개의 미해결 계약 항목이다.

---

## 이전 전달 상태 — 2026-09-10 (Asia/Seoul)

아래 본문은 당시 검증·판단 기록이다. 현재 상태와 미해결 목록은 문서 상단을 따른다.

- **상태: 착지 완료.** [PR #20](https://github.com/memtomem/tracegraph/pull/20)이 병합 커밋
  `6da0886`으로 기본 브랜치 `tracegraph-mvp`에 병합됐다 (2026-09-10 02:55 UTC). 기준은 `0d5091c`,
  커밋 5개다. 아래 "검증 증거"는 병합 **전** 기록이고, 병합 후 원격 CI 결과는 그 아래 별도로 적었다.
- 이번 범위: 전체 코드 리뷰에서 재현된 correctness 결함, 회귀 테스트 45개, lint gate·패키징 위생.
- 공개 계약을 바꾸는 항목은 **수행하지 않았다**. 아래 "남은 결정 필요 항목"을 참조한다. 독립 리뷰어도
  이 6건 때문에 PR을 막을 필요는 없다고 판단했다.

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
| `artifact.py` | `from_obj`가 v1 migration의 전체 복사 때문에 예외를 흘렸다. JSON round-trip은 `TypeError`를, `copy.deepcopy`는 깊게 중첩된 무시 필드에서 `RecursionError`를 냈다. 이제 migration이 수정하는 컨테이너(envelope·header·CAUSED_BY edge)만 복사한다. |
| `adapters/otlp_spans.py` | `traceId` 없는 span을 조용히 버려 자식 span이 잘못된 오류를 냈다. 이제 거부한다. |
| `analysis/ahu.py` | TREE_PARENT 검사가 순회 **뒤에** 있어, root에서 도달 가능한 cycle(자기 간선 등)은 검사에 닿기 전에 무한 순회했다. 세 진입점 모두 순회 전에 `validate_tree`와 중복 step_id를 검사한다. |
| `analysis/diagnose.py` | 커버리지 경고가 end 타임스탬프만 봤다. 누락된 start도 동일하게 duration을 왜곡하므로 함께 공개한다. |
| `analysis/diagnose.py` | baseline 메트릭 요약이 disclosure note를 버려, 신뢰할 수 없는 baseline 값으로 계산한 delta가 경고 없이 실렸다. `baseline:` 접두어로 전달한다. |
| `analysis/diagnose.py` | 음수 검사가 전체 envelope(`max(ends) - min(starts)`)만 봐서, 자기 시작보다 먼저 끝나는 **개별 구간**을 놓쳤다. `[0s,2s]`와 `[10s,1s]`이면 envelope은 멀쩡한 +2s이고 10s 시작은 답에 등장조차 않는다. 이제 구간별로 검사한다. |
| `analysis/diagnose.py` | 두 하한(lower bound)의 차이는 차이의 하한이 아니다. 어느 한쪽이라도 커버리지가 불완전하면 `wall_duration_ms` delta는 크기도 부호도 알 수 없으므로 **보류**한다. 이전 라운드에서 추가한 테스트가 오히려 이 잘못된 값을 고정하고 있었다. |
| `cli.py` | `inspect`/`validate`의 헤더가 `trace_id`·`source_kind`를 escape 없이 넣어, 대괄호가 든 정상 artifact에서 `MarkupError`로 죽었다. step 이름만 escape하고 헤더를 빠뜨렸다. |
| `adapters/langgraph_checkpoint.py` | subgraph entry parent 선택이 `saver.list()` 페이징 순서에 의존했고, namespace 하나당 entry·terminal을 **한 쌍만** 유지해 두 번 진입한 namespace에서 continuation 간선이 사라졌다. namespace의 체크포인트를 invocation 단위로 묶어 각 entry를 자기 invocation의 terminal과 짝지운다. |
| `store/ladybug.py` | 호출자의 `Trace`를 참조로 보관해 `InMemoryStore`와 갈렸다. deep-copy로 맞췄다. `COMMIT`을 `try` 안으로 옮기고 `QueryResult`를 닫는다. |
| `normalize.py` | TREE_PARENT cycle 오류 메시지가 hash 순서에 의존했다. 정규 순서로 고정했다. |

### 당시 검증 증거

환경: macOS/arm64, Python 3.12.11.

| 검증 | 명령 | 결과 |
| --- | --- | --- |
| 전체 테스트 | `uv run --no-sync pytest -q` | **494 passed** (기존 449 + 신규 45) |
| 성능 guard | `uv run --no-sync pytest -q -m perf` | **4 passed** |
| 회귀 테스트 판별력 | 신규 테스트를 수정 전 소스에 실행 | 대부분 실패 (일부는 기존 동작의 커버리지 보강) |
| Codex 리뷰 게이트 | `ask-codex.sh` 작업 트리 3라운드 | **NEEDS-FIX**(Major 4) → SHIP(Nit 1) → **SHIP**(지적 0) |
| Codex 리뷰 게이트 | 커밋된 범위 재검토 | **NEEDS-FIX**(Major 1: 반복 진입 namespace) → 수정 |
| Codex 리뷰 게이트 | 4·5라운드 | **NEEDS-FIX**(중첩 subgraph 복귀를 진입으로 오분류) → **SHIP** |
| Codex 리뷰 게이트 | 전체 커밋 범위 재검토 | **NEEDS-FIX**(deepcopy의 `RecursionError`, 커버리지 테스트 판별력) → 수정 |
| Codex 리뷰 게이트 | 8~10라운드 | migration 입력 shape 관련 소소한 지적 → SHIP. 이전 라운드 결과를 프롬프트에 계속 넣은 탓에 같은 함수만 파고든 것으로, 프롬프트가 만든 편향이었다. |
| Codex 리뷰 게이트 | **선입견 없는 재검토** | **NEEDS-FIX**(duration delta 과신, CLI 헤더 escape 누락) → 수정. 8~10라운드 전체보다 가치 있었다. |
| Codex 리뷰 게이트 | PR #20 전체 재검토 | **NEEDS-FIX**(개별 구간 역전 미검출, Ladybug lifecycle 테스트 부재) → 수정 |
| artifact 바이트 불변 | fixture 13개를 adapter로 재수집해 SHA-256 비교 | **전부 동일**, canonical form도 동일 |
| 보고서 변화 | 동일 fixture의 analysis report 비교 | 5개에서 이전에 삼켜졌던 `error` finding 1건씩 **추가**, 삭제·경고 변화 없음 |
| lint | `uvx ruff@0.14.2 check src tests scripts examples` | **All checks passed** |
| 설치된 wheel | core wheel 환경에서 `scripts/verify_wheel.py --extra core` | **PASS**, `candidate_golden: byte-identical` |

`_iso_ts`의 나노초→마이크로초 변환은 **되돌렸다**. 리뷰가 제기한 float 정밀도 손실은 현재 epoch에서
재현되지 않았고, 정수 연산으로 바꾸면 sub-microsecond 나머지를 가진 실제 OTLP 타임스탬프에서
artifact 바이트가 달라진다(`...000000501` → `.000001`). 실익 없는 계약 변경이므로 원래 식을 유지한다.

**병합 후 원격 CI: 전체 통과.** PR #20의 최종 head `98b358f`에서 14개 check 전부 pass —
`pure-python core`, `with [cypher] extra (LadybugDB)`, `installed wheel (core/cypher)`,
`Phoenix CLI contract`, `perf regression guards`, 그리고 이번에 추가한 `lint`.
한 차례 `lint` 실패가 있었으나 코드가 아니라 `Install uv` 단계의 `fetch failed`(일시적 네트워크)였고,
동일 커밋 재실행으로 통과했다. **setup 단계에서 죽은 job은 검사 목록에서 해당 도구의 실패처럼 보이지만
diff와 무관하다** — 로그가 도구 실행 전에 끝나는 것이 판별점이다.

실제 Phoenix + SyncMill E2E는 이번에도 **미실행**이다. 로컬·CI 통과를 실제 서버 검증으로 표기하지 않는다.

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
6. ~~**LICENSE 부재**~~ — **결정됨(2026-09-12, PR #23)**: Apache-2.0 + DAPADA CLA.
   배포명은 `agent-tracegraph`이고 import 패키지·CLI·스키마 `kind` 상수는 `tracegraph` 그대로다.


## 이전 전달 상태 — 2026-09-08 (Asia/Seoul)

- 기준 HEAD: `d048f400fdd39bb37c9608e45ee5931cba058b46`, 브랜치 `tracegraph-mvp`.
- 아래 결과는 이 HEAD 위의 전달 변경을 **커밋 전에** 검증한 기록이다.
  최종 커밋 SHA는 이 문서를 포함한 Git 이력에서 확인한다. push·릴리스는 수행하지 않았다.
- 이번 범위: CI import 복구, 설치된 wheel 검증 자동화, 문서 상태 연결.
- 제품 소스·의존성·lockfile·fixture·공개 데이터 계약은 변경하지 않았다.

### 당시 해결한 문제와 변경

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

### 당시 검증 증거

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

#### 당시 Wheel 검증 재실행

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
VERSION="$("$WHEEL_ROOT/venv/bin/python" -c "import tomllib,pathlib; print(tomllib.loads(pathlib.Path('pyproject.toml').read_text())['project']['version'])")"
uvx twine@7.0.0 check "$WHEEL_ROOT"/dist/*
"$WHEEL_ROOT/venv/bin/python" scripts/verify_artifacts.py "$WHEEL_ROOT/dist" "$VERSION"
"$WHEEL_ROOT/venv/bin/python" scripts/verify_wheel.py --extra "$WHEEL_EXTRA" --expected-version "$VERSION"
```

`--expected-version`은 배포명 조회가 조용히 실패하는 경우를 막는다. 이름이 틀리면
`PackageNotFoundError`가 삼켜져 정상 설치가 `0.0.0.dev0`을 보고한다.

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

### 릴리스 경로 — 2026-09-12

PR #22(native 실패 수집)와 PR #23(license·CLA·PyPI 메타데이터)에 이어 태그 기반 배포
워크플로를 추가했다. **이 PR 자체는 아무것도 배포하지 않는다.** 배포에는 소유자의 태그 push와
아래 일회성 설정이 모두 필요하다.

- `test-v*` → TestPyPI 예행, `v*` → PyPI. Trusted Publishing이며 장기 토큰은 없다.
- 빌드 전 게이트: 태그 커밋이 기본 브랜치에 있고 그 커밋에 대한 `tests` push 실행이 이미
  성공했는지 확인한다. 대기(polling)하지 않으므로, CI가 끝나기 전에 태그를 밀면 실패하고
  Actions UI에서 재실행하라고 안내한다.
- 절차와 소유자 일회성 설정은 [릴리스 런북](releasing.md), 공개 전환 점검은
  [공개 체크리스트](public-release-checklist.md)에 있다.
- `twine check`는 게이트로 복귀했다. twine 6이 거부하던 `Metadata-Version: 2.5`를
  twine 7.0.0이 수용하며, 배포 액션도 내부적으로 twine 7.0.0을 쓴다. 버전 핀은 유지한다.

### 기능 개선 백로그

| 우선순위 | 후속 범위 | 설계·수용 기준 |
| --- | --- | --- |
| ~~P1~~ (완료) | LangGraph native 예외 관측 | **해결.** `pending_writes.__error__`를 읽어 실패한 task마다 파생 `task` step을 만든다. task id를 checkpoint 데이터에서 재계산해 실패 노드를 지목하므로 이전 정상 checkpoint에 잘못 귀속하지 않는다. 증거가 없으면 이름을 비워 둔다. **중단·재개 구분은 이 범위에 없다**: 미완료 작업(interrupt·recursion limit·crash) 보고는 후속 작업으로 남아 있다. |
| ~~P1b~~ (일부 완료) | 미완료(pending) task 보고 | **`interrupt()`로 멈춘 task는 해결**(PR #36, 위 9번). 시작하지 않은 task(recursion limit, kill, static breakpoint)는 [이슈 #37](https://github.com/memtomem/tracegraph/issues/37)의 opt-in 컴파일 그래프가 필요하다. 수용 기준은 그대로다: 완료된 실행을 미완료로 표시하지 않는다. |
| P2 | 관측 범위 구조화 | 외부/누락 링크의 수·사유와 지표별 coverage를 보존한다. 현재 일반 경고는 이미 있으므로 경고 추가만으로 완료 처리하지 않는다. |
| P3 | 자원 제한 | stdin·px stdout/stderr byte cap 및 batch·매치 계산 예산. `query --limit`의 표시량 제한과 계산량 제한을 구분한다. |
| P4 | U1 사후 분석 예제 | 정상·state-channel 오류·native 예외·복구/중단·fan-in을 다룬다. 상위 8월 handoff의 "문서·예제만으로 즉시 가능"은 native 예외 감지 공백을 고려해 조정한다. |
| P5 | D2 agent 식별 축 | producer의 agent/slot 식별 근거, privacy, 이름 기반 패턴의 버전 정책을 먼저 고정하고 SyncMill과 교차 검증한다. |

후속 기능은 이번 수정에 포함하지 않았다. Pattern DSL, OTLP 재출력, raw DAG 비교, 서버·자체 UI,
추가 backend는 위 신뢰도 작업 이후 평가한다.

유지할 계약:

- raw multi-parent `CAUSED_BY`가 정본이며 `TREE_PARENT`는 손실 가능한 비교·표시용 projection이다.
- artifact reader는 v1~v4를 읽는다. writer는 파일의 TREE_PARENT가 origin 우선순위 projection이고 그것이 시간순 projection과 다를 때 v4, 그 외 task step이 있으면 v3, 나머지는 v2를 쓴다. reader는 본문이 요구하는 것보다 낮은 envelope을 거부한다. 레거시 v2/v3 canonical 아티팩트의 재직렬화 바이트를 보존하며, analysis report v2와 candidate v1은 유지한다.
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
