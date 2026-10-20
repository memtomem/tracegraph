# Tracegraph handoff

## 현재 전달 상태 — 2026-10-20, `4931a33` 기준 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp`의 최신 커밋. 제품 코드의 기준은 `4931a33`(PR #114 병합)다. 시작 전에
  `git switch tracegraph-mvp && git pull --ff-only`로 맞춘다. PR #118(`a0d157e`)이 `uv.lock`을 바꿨으므로 그 뒤
  `uv sync --locked --extra cypher`로 venv도 맞춘다. 2026-10-19~20 세션처럼 worktree가 main venv를 공유하면 그
  venv도 맞춰야 한다. 이 Mac에서 sync 전 main venv의 langgraph는 1.2.2였다.
- **0.6.1을 준비 중이다(PR #118 하나).** release-prep PR이 `## Unreleased`를 `## 0.6.1 - 2026-10-20`으로
  옮겼다. 사용자에게 보이는 변경은 하나다: `langgraph-checkpoint-sqlite` 하한이 `>=2`에서 `>=3.1.1`로
  올랐다(GHSA-47pj-3jcm-6whg). 이 advisory는 store 경로에 관한 것이고 tracegraph는 `SqliteSaver`를 쓴다. PR #118에는
  새 lock에서 전체 테스트 통과와 fixture 출력 일치가 기록돼 있다. lock은 Dependabot 경고 8건을 모두 넘겼다(전부
  `fixed`, 열린 경고 0개). 버전은 소유자가 0.6.1로 정했다.
- **0.6.0 공개 완료(2026-10-20).** `agent-tracegraph` 0.6.0이 PyPI 최신이다. `test-v0.6.0`과 `v0.6.0`이
  모두 `218abe6`(PR #116)을 가리킨다. 업그레이드 주의는
  [GitHub Release v0.6.0](https://github.com/memtomem/tracegraph/releases/tag/v0.6.0)의 "Before you upgrade"에
  있다. release-prep PR이 `## Unreleased`를 `## 0.6.0 - 2026-10-20`으로 옮겼다. 거기에는 아래 이전 절의 항목(#85·#79·#82·#93·#60, #90·#96 문서)과 이번 세션의
  수정 7개가 있다. 0.6.0에서 놓치기 쉬운 동작 변경은 다음과 같다. 전체 목록은 `CHANGELOG.md`의 `## 0.6.0`이다:
  - **cost 상한(PR #108).** 끝까지 풀어 쓴 길이가 64자(`model.MAX_COST_CHARS`)를 넘는 cost는 `analyze`가 합산하지
    않고, 총액을 보류하면서 상한을 밝히는 별도 note를 붙인다. OTLP ingest는 그런 값을 정규화해 저장하지 않고, 읽을 수
    없는 값으로 다뤄 `otlp.unreadable_total_cost`·`otlp.unreadable_component_cost`로 공개한다. model 자체는 cost
    문자열의 길이를 검사하지 않는다. `model`에 `MAX_COST_CHARS`와
    `cost_is_bounded`가 새로 생겼다. load 시점에 artifact를 거부하지는 않는다.
  - **cost 합계와 baseline delta가 정확해졌다(PR #113).** 정밀도 160에 `Inexact`를 trap한다. 유효숫자가 28자리를
    넘는 합계는 이제 반올림하지 않은 값이 나온다. 그래도 반올림이 필요하면 총액(또는 delta만)을 note와 함께 보류한다.
    `1.00 + 1.00`은 여전히 `2.00`이다.
  - **CLI 출력.** 두 모듈 콘솔이 `emoji=False`라서 `:ok:` 같은 이름이 그대로 나온다(PR #106). `main()`이 열린
    stdout·stderr를 `errors="backslashreplace"`로 다시 설정하므로, 인코딩할 수 없는 문자는 crash 대신 escape로
    나온다(PR #107).
  - **해시.** `redacted:` alias, 기본 artifact 파일 이름, memo run digest가 `surrogatepass`로 인코딩한다(PR #107).
    lone surrogate가 없는 문자열은 이전과 같은 바이트다. UTF-8이 아닌 `--graph` 경로는 `_tracegraph_graph_fs_`
    접두어 이름을 받는다(PR #112). 이전에 load되던 경로의 이름은 그대로다.
  - **예외 메시지.** schema-v1 `from_obj()`의 edge 읽기 실패 메시지가 상수 `artifact edges could not be read`가
    됐다(PR #105). 예외 텍스트는 이제 `__cause__`에만 있다. v1 migration은 `KeyboardInterrupt`를 제외한
    `BaseException`도 `ValueError`로 거부한다(PR #114). 잡은 예외를 메시지에 쓰는 네 곳은 새 `tracegraph._errors`
    helper를 거친다. 보통 예외의 메시지는 이전과 같다.
- **열린 이슈 0개.** #100~#103은 이번 세션에 고쳤고, 그 과정에서 범위 밖으로 찾은 #109~#111도 등록한 뒤 같은
  세션에 고쳤다. 소유자 결정을 기다리는 이슈는 없다.

### `ebf09a2` 이후 착지한 변경

| PR | merge | 내용 |
| --- | --- | --- |
| #104 | `519c9a8` | 2026-10-19 절 handoff(문서만) |
| #105 | `3173a65` | #103: v1 migration의 edge 읽기 실패 메시지를 상수로 바꿨다. `__str__`도, metaclass를 거친 type 이름도 포맷하지 않는다 |
| #106 | `dcfa47c` | #102: CLI 모듈 콘솔 두 개를 `Console(emoji=False)`로 만들었다. `summarize`의 `functools.partial`은 지웠다 |
| #107 | `386b1ff` | #101: `diagnose._safe`·`artifact.default_path`·`memo.compute_run_digest`가 `surrogatepass`로 해시한다. LangGraph `_hash_task_id`는 인코딩 실패 시 `None`을 낸다. `main()`이 출력 스트림을 `backslashreplace`로 설정하고, 닫힌 스트림은 건너뛴다 |
| #108 | `acb9015` | #100: `model.cost_is_bounded`(64자)를 OTLP `_as_decimal`과 metrics가 공유한다. 1e1000000의 `decimal.Overflow`, report의 100만 자 합계, OTLP의 10바이트 → 1억 자 저장을 막았다 |
| #113 | `dc49aab` | #111: cost 합계와 delta를 `diagnose._COST_CONTEXT`(정밀도 160, `Inexact` trap)에서 계산한다. #100 CHANGELOG의 "65자·71자"를 "127자·133자"로 정정했다 |
| #112 | `27a1ab2` | #110: `graph_ref._module_name`이 strict UTF-8 해시를 유지하고, 실패할 때만 `os.fsencode` 바이트를 별도 접두어로 해시한다 |
| #114 | `4931a33` | #109: `tracegraph/_errors.py`(`type_name`, `describe`)를 `model._validated_copy`·`langgraph_checkpoint`의 두 곳·`graph_ref`가 쓴다. v1 migration은 shape 검사를 guard 안으로 옮기고 `KeyboardInterrupt`를 제외한 `BaseException`을 잡는다 |
| #115 | `381ede3` | 2026-10-20 절 handoff(문서만) |
| #116 | `218abe6` | 0.6.0 준비: 버전 0.5.0 → 0.6.0, `uv.lock`, CHANGELOG 날짜와 릴리스 전체 기준의 문장 정정, 이 문서 |
| #117 | `7340258` | 0.6.0 릴리스 기록과 lone surrogate `check` 측정(문서만) |
| #118 | `a0d157e` | Dependabot 8건: lock에서 anyio 4.14.2·urllib3 2.8.0·langsmith 0.8.18(0.8.x 유지)·langgraph-sdk 0.4.8·langgraph-checkpoint-sqlite 3.1.1, SDK 때문에 langgraph 1.2.14·langchain-core 1.6.10. `pyproject`의 sqlite 하한 `>=3.1.1`과 CHANGELOG `## Unreleased` |

### 이번 세션의 결정(2026-10-19~20, PR #105~#114)

1. **#100:** 이슈의 선택지 셋 모두 증폭 경로를 다루지 않았다. report에서는 `9e999999`가 100만 자 합계가 됐고,
   OTLP ingest에서는 10바이트 `"1e99999999"`가 1억 자가 됐다. 그래서 소유자가 공유 상한을 골랐다. 상한은 지수가
   아니라 풀어 쓴 길이(64자)이고, `as_tuple()`로 포맷하지 않고 계산한다. load 시 거부는 하지 않는다.
2. **#101:** `_safe`만 고치면 `analyze`·`summarize`만 살아나고 `inspect`·`explain`·`query`는 stdout 쓰기에서 여전히
   exit 1이었다. 그래서 소유자가 해시 지점 전부 + 출력 스트림 설정을 골랐다. 이슈 본문은 "The same helper runs in
   `check`, the incident memo and `summarize`"라고 했지만, memo 경로에서 `_safe` 호출을 세어 보니 0회였다. memo가
   실패한 원인은 `compute_run_digest`였다(PR #107의 mutation 표).
3. **#103:** 승인된 옵션 1(type 이름만 포맷)은 metaclass `__name__` 회귀를 새로 만들었다. 구현 리뷰 1라운드가
   찾았고, 기준 코드는 그 경우에 새지 않았다. 그래서 메시지를 상수로 바꿨고, 이 이탈은 PR #105 본문에 적었다.
4. **#109~#111:** 리뷰와 측정 중에 범위 밖에서 찾은 결함이다. 등록할지 보고만 할지 물었을 때 소유자가 등록을 골랐다.
   각각 재현한 뒤 등록했고, 이어서 고쳤다.
   - #111은 옵션 1+2다. 정밀도 160은 측정한 최소값(10^6 step에서 132자리)이 아니라 여유분이다.
   - #110은 strict UTF-8을 유지한다. 그냥 `fsencode`를 쓰면 Latin-1 같은 파일시스템 인코딩에서 기존 이름이 바뀌고(계획
     리뷰 1라운드), 별도 접두어가 없으면 Shift-JIS에서 두 파일이 이름을 공유한다(2라운드).
   - #109는 옵션 1+3이다. CLI의 `str(exc)` 지점들은 tracegraph가 직접 낸 `ValueError`만 포맷하므로 범위 밖으로 두었다.
5. **CI:** #106(`a40cd0e`)의 첫 CI에서 두 run(push `39278866531`, pull_request `39278871657`)의 job 3개가 cancelled됐다.
   `lint` 2개는 10m0s·7m50s, `installed wheel (core)`는 13m33s가 지난 뒤였다. push run의 `lint`는 ruff와 mypy가
   성공한 뒤 `Post Install uv`가 끝나지 않은 채였다. wheel job은 step이 모두 15:33:25 UTC까지 success로 끝났는데도
   job은 cancelled였다. 실패한 step이 없으므로 코드 문제로 보지 않았다. 두 run에 `gh run rerun --failed`를 했고, 둘 다
   attempt 2에서 success였다. 나머지 PR은 이 세션의 `gh run watch`에서 첫 실행이 통과했다.

### 검증 증거

환경: macOS/arm64. PR 생성 시 수치는 각 PR 본문에 있다. rebase 뒤 수치는 이 세션의 suite-run 출력이며 PR 본문에는 없다.
첫 행의 `4931a33` suite-run만 이 문서를 위해 다시 돌렸다.

| 검증 | 결과 |
| --- | --- |
| 전체 테스트 `pytest` | PR 생성 시: #105 2600, #106 2602, #107 2607, #108 2633, #113 2653, #112 2651(12 skipped), #114 2666 passed. merge 직전 rebase 뒤(head SHA): #107 `fd83dc6` 2613, #108 `c342837` 2648, #112 `6a9aa54` 2656(12 skipped), #114 `a41f6b2` 2674(12 skipped), 모두 CLEAN. #106의 rebase 뒤에는 `tests/test_cli.py`·`tests/test_validation.py`(477 passed)만 돌렸다. `4931a33`에서 suite-run **2674 passed, 12 skipped, CLEAN**. 늘어난 skip 1개는 macOS에서 건너뛰는 #110의 Linux 전용 테스트다. CI(Linux)에서는 실행된다 |
| lint·타입 | PR마다 `uvx ruff@0.14.2 check src tests scripts examples`와 mypy 통과 |
| 골든 하네스 | #105~#108은 `519c9a8`, #112~#114는 `acb9015`와 비교해 커밋된 fixture 출력 265개 중 다른 파일 0개. 대조군 `0bbe9e6`→`43f5ccb`는 39개가 달라 하네스가 살아 있음을 확인했다. 출력 디렉터리는 절대 경로로 넘겨야 한다. 상대 경로를 주면 스크립트가 파일 1개만 쓰고 exit 1로 끝난다 |
| 회귀 테스트 판별력 | PR마다 확인한 방식이 다르다(각 PR 본문). #105: 기준 코드에서 `str` 케이스, type-name 방식에서 `type-name` 케이스가 실패. #106: 기준 코드에서 4개 실패. #107·#114: 지점을 하나씩 되돌리면 그 지점의 테스트만 실패, #114는 단순 helper로 바꾸면 10개 실패. #108: 어댑터·metrics 수정을 빼면 12개, metrics 검사만 빼면 5개 실패. #113: 기준 코드에서 정확 합계 4개 실패. #112: Linux 테스트가 Docker에서 기준 `graph_ref.py`로 실패 |
| CI | #107·#114는 check 17개, 나머지는 15개 모두 통과(#106은 재실행 뒤) |
| 리뷰 게이트 | 계획은 #100~#103 묶음과 #109~#111 묶음으로 각각 2라운드씩 받았다(둘 다 NEEDS-FIX → 반영 → 소유자 승인). 구현: #105 2라운드(NEEDS-FIX → SHIP), #106 1라운드(SHIP), #107 2라운드(NEEDS-FIX: 닫힌 스트림 → SHIP), #108 2라운드(SHIP, Minor 1건 반영 → SHIP), #112·#113 1라운드(SHIP), #114 2라운드(NEEDS-FIX → SHIP). merge 직전 rebase의 충돌은 이 세션의 rebase 출력 기준으로 CHANGELOG(#106·#107·#108·#112·#114. #105·#113은 먼저 머지해 rebase가 없었다)와 `tests/test_cli.py`(#107)뿐이었다. #112와 #114가 함께 고친 `graph_ref.py`는 자동으로 합쳐졌다. rebase 뒤에는 다시 리뷰하지 않았다 |

기록 위치: 결정 근거와 측정은 PR #105~#108·#112~#114와 이슈 #100~#103·#109~#111의 본문에 있다. 리뷰 원문은
저장소에 보존하지 않았다. 이 세션의 임시 디렉터리(`/private/tmp/claude-501/` 아래 세션 `6010de39-…`의
`scratchpad/devtrio*`)에 있으며, 임시 경로이므로 오래 남는다고 가정하지 않는다.

### 남은 결정과 후속 항목

1. **0.6.0 릴리스: 완료(2026-10-20).** 새 명령(`summarize`)과 새 JSON 계약이 있어 minor다. [릴리스
   런북](releasing.md)대로 했다.
   - TestPyPI 예행: [run 39348336333](https://github.com/memtomem/tracegraph/actions/runs/39348336333).
     production: [run 39349638025](https://github.com/memtomem/tracegraph/actions/runs/39349638025).
   - wheel `fb131cc9…7343`, sdist `4fc5a65f…6b84`. production build의 digest, TestPyPI 파일, PyPI 파일이
     바이트 단위로 같고(각 출처에서 직접 읽어 diff), 두 파일 모두 PyPI provenance가 HTTP 200이다.
   - PyPI에서 받은 wheel의 깨끗한 core·`[cypher]` 설치(Python 3.12.11)에서 `scripts/verify_wheel.py` 통과(cypher
     backend parity 포함). PyPI description은 `v0.6.0`의 `README.md`와 바이트 단위로 같다. 게시된 wheel로
     `summarize --out`을 돌려 `tracegraph.motif-summary` v1이 나오는 것을 확인했다.
   - 게시된 0.5.0의 `validate`가 0.6.0이 ingest한 Phoenix·LangGraph artifact(schema v7)를 받아들인다. 노트의
     "0.5.x reads what this release writes"는 이 측정에 근거한다.
   - [GitHub Release v0.6.0](https://github.com/memtomem/tracegraph/releases/tag/v0.6.0)은 prerelease로
     만들었다. 노트는 claims 리뷰 2라운드를 거쳤다(7건, 2건. 2라운드 2건은 모두 1라운드 수정에서 나왔다).
     1라운드는 CHANGELOG에 있지만 노트의 "Before you upgrade"에서 빠진 동작 변경 3개를 찾았다:
     `langgraph.rewritten_error_channel` 갭과 `check`의 방향성, writer의 NaN 거부, v1 `from_obj()`의
     `BaseException` 처리.
2. **lone surrogate와 `check`: 측정 완료(2026-10-20, 게시된 wheel로).** 0.6.0으로 ingest한 Phoenix artifact
   (capture 레코드 있음)의 TOOL 이름을 `"search\ud800"`으로 바꾼 쌍으로, 규칙 4개를 양방향으로 돌렸다.
   - 0.6.0: 규칙 4개 × 방향 2개, 8개 조합 모두 traceback 없이 판정이 나온다. alias는
     `"redacted:" + hashlib.sha256("search\ud800".encode("utf-8", "surrogatepass")).hexdigest()`와 같고,
     `check --json-out`과 `analyze`가 같은 값을
     낸다. `failures-not-increased`·`pattern-not-increased:tool-failure`가 FAIL인 것은 이름이 바뀌어 새 시그니처가
     생겼기 때문이다(surrogate 없는 대조군 base vs base는 PASS). `topology-unchanged`의 exit 3은 대조군에서도 같다.
   - 대조군 0.5.0: crash가 아니라 거부였다. surrogate가 current 쪽에 있으면 count 규칙 둘이 exit 2로 끝난다(`cannot
     check these inputs: 'utf-8' codec can't encode character '\ud800' ...`). baseline 쪽에 있으면 판정이 나온다.
     즉 #107은 `check`에서 유효한 입력의 exit 2 거부를 고쳤다. 같은 입력에서 0.5.0 `analyze`는 실제로
     `UnicodeEncodeError` traceback으로 끝난다(exit 1). 그러니 CHANGELOG #101의 "ends a command in a traceback"은
     `analyze`에는 맞고, `check`에는 정확하지 않다(고치지 않음).
3. **알려진 한계(문서화, 고치지 않음):** #90·#96. README와 CHANGELOG의 Known limitations에 있다.
4. **`summarize` 후속(등록 안 함):** 아래 2026-10-19 절 3번 그대로다.
5. **기존 보류 결정**(아래 2026-10-14 절의 5번, 2026-09-15 절의 3·4·5·7·8번)은 그대로다.
6. **0.6.1 릴리스:** 소유자가 0.6.1로 정했다. release-prep PR 다음 단계는 [릴리스 런북](releasing.md)의
   3~5단계다: merge 뒤 `tests` 통과 확인, `test-v0.6.1` rehearsal(TestPyPI, 환경 승인 필요), 게시된 파일 검증,
   같은 커밋에 `v0.6.1`, PyPI 확인, GitHub Release(prerelease).

---

## 이전 전달 상태 — 2026-10-19, `ebf09a2` 기준 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp`의 최신 커밋. 제품 코드의 기준은 `ebf09a2`(PR #99 병합)다. 시작 전에
  `git switch tracegraph-mvp && git pull --ff-only`로 맞춘다.
- **PyPI 최신은 여전히 0.5.0이다.** `## Unreleased`에 #85·#79·#82·#93·#60과 #90·#96 문서가 쌓였고, 아직
  릴리스하지 않았다. 다음 릴리스에서 놓치기 쉬운 동작 변경은 다음과 같다. 전체 목록은 `CHANGELOG.md`의 `## Unreleased`다:
  - `artifact.dumps()`가 model로 검증한 복사본을 직렬화한다(PR #94). 생성 뒤의 변경 가운데 model이 거부하는
    것(잘못된 타입, `None`이 된 container)은 `ValueError`로 거부되고, 변환 가능한 값(`seq="0"`)은 reader가 읽는
    값으로 쓰인다. model이 받는 변경(dangling edge, stale trace status)은 structure를 검사하지 않으므로 그대로
    직렬화된다. 6000 step에서 0.15 s → 0.20 s.
  - `analyze()`·`check.evaluate()`·`build_incident_memo()`도 같은 복사본을 분석한다(PR #95). `analyze()`와 memo는
    구조가 맞는 non-canonical trace를 여전히 받고, `check.evaluate()`는 여전히 canonical 형식을 요구한다. 그 복사본은
    이제 Python-mode dump에서 만든다. JSON mode였을 때 `trace.run_id = nan`이 `null`로 바뀌어 `dumps()`와 writer가
    값을 지웠는데, 지금은 거부한다. `-W error`에서 변환 가능한 값이 거부되던 것도 없어졌다. 6000 step 기준
    비용: `analyze()` 0.25–0.27 s → 0.30–0.32 s, baseline 비교 0.57–0.58 → 0.69–0.70 s, `check.evaluate()`
    0.65–0.67 → 0.78–0.79 s, memo 0.016 → 0.044 s.
  - LangGraph: subgraph 주변에서 부모 그래프의 오류를 다시 기록하지 않는다(PR #91). 어떤 step이 error인지와
    disclosure가 바뀌므로, 해당 shape의 artifact digest가 바뀐다.
  - `query --limit`이 한 trace 안의 match를 모두 들고 있지 않는다(PR #92). 출력과 total은 그대로다.
  - 새 명령 `tracegraph summarize`(PR #99)와 새 JSON 계약 `tracegraph.motif-summary` schema v1.
- **열린 이슈 4개, 모두 이번 세션에 등록한 결함:** #100(`decimal.Overflow`), #101(`_safe`의 lone surrogate),
  #102(CLI 출력의 emoji 치환), #103(`_migrate_v1`의 `{exc}`). 소유자 결정을 기다리는 이슈는 없다.

### `361ad03` 이후 착지한 변경

| PR | merge | 내용 |
| --- | --- | --- |
| #89 | `8be2da8` | 0.5.0 릴리스 기록(문서만) |
| #91 | `aa769f6` | #85: subgraph의 `__start__`, persistent subgraph 재진입, subgraph 뒤의 부모 checkpoint가 이미 있던 오류를 다시 기록하지 않는다. fork copy는 오류를 기록하지 않는다. 남은 fork-of-input + resume 모양은 #90으로 등록 |
| #92 | `9d79a9e` | #79: `query --limit`이 trace 안에서도 cap 아래의 match만 남긴다(matcher를 generator로, `heapq.nsmallest`). total은 exact. 3.1M match 체인 `--limit 1`: 976 → 70 MiB(memory backend). LadybugDB Cypher는 결과를 엔진이 buffer해서 높게 남는다 |
| #94 | `2eaa403` | #82: `artifact.dumps()`가 model이 거부하는 입력에 `ValueError`만 낸다. model로 검증한 복사본을 직렬화하고 structure는 검사하지 않는다(`analyze()`가 non-canonical baseline을 digest하므로) |
| #95 | `a161122` | #93: `analyze()`·`check.evaluate()`·`build_incident_memo()`가 검증된 복사본을 분석해, model이 거부하는 입력에 `ValueError`만 낸다. model 검증을 통과한 입력의 `decimal.Overflow`는 #100이다. 복사본은 `model._validated_copy`로 옮겨 Python-mode dump + `warnings=False`로 만들고, dump와 validate 모두에서 다른 예외를 `ValueError`(type 이름만)로 바꾼다 |
| #97 | `f441388` | #90: 문서만. resume된 subgraph input의 fork copy가 오류를 다시 기록하는 것을 알려진 한계로 적었다 |
| #98 | `9354644` | #96: 문서만. 같은 한계의 `Send` payload 변형과 네 번째 시도가 실패한 이유 |
| #99 | `ebf09a2` | #60: `tracegraph summarize <preset> <artifacts...>`. preset match를 `motif-signature/v1`로 묶고 coverage를 항상 보인다. 인접 preset만 받는다 |

### 이번 세션의 결정(2026-10-19, PR #94~#99)

1. **#82:** 이슈의 옵션 1을 model 검사로만 좁혔다. `dumps()`에 `validate_normalized`까지 넣는 프로토타입은
   테스트 5개를 깨뜨렸다. 둘은 `analyze()`의 baseline digest 경로이고, 셋은 손상 fixture를 쓰는 reader 테스트다.
2. **#93:** 옵션 1에 memo를 더했다(소유자 승인). 실패 대부분이 structural gate 뒤의 분석 코드에서 나서 옵션 2로는 부족했다.
   공유 helper가 #94와 #95에 걸쳐 지적 5건을 모았고(실패하는 iterator, `__str__`, JSON-mode NaN, `-W error`,
   validate 단계 누출). 소유자가 대체안을 승인해(세션 기록) pydantic 호출 한 줄과 예외 정규화 지점 하나로 다시 썼다.
3. **#90과 #96: 고치지 않고 문서로 남겼다.** "이 fork가 input의 복사본인가"를 가리는 네 가지 방법이 모두
   공개 `Pregel` 그래프의 반례로 깨졌고, 매번 진짜 실패를 숨기는 방향이었다. content로 분류한 세 방법은
   `__start__` 값, `versions_seen`, `__start__` task id를 봤다. 네 번째는 원래 input과 verbatim으로 매칭하는 방법이었다.
   중복 기록이 실패를 숨기는 것보다 낫다는 판단이다. 더 좁은 경로(`schedule_graph`로 `StateGraph`를 받은 경우에만
   켜기)는 측정하지 않았다. #98 본문에 있다.
4. **#60:** 이슈가 "Later / validate demand first"를 권했지만 소유자가 지금 만들기로 했다. v1은 인접 preset만
   받는다(gap 경로의 edge 증거가 기록되지 않음). signature에 safe-v1 이름을 넣는다(경로 모양 이름도 `analyze`처럼
   통과). 리뷰에서 정한 규칙: `check`의 규칙으로 확정된 match만 묶는다. 모르는 필드는 `null`과 별도 `unknown`
   목록으로 적는다. coverage는 `check._could_be_hidden`을 재사용한다. `heuristic_edges`는 gap code에서 직접 분류한
   4-state disclosure다(`check._gap_hides`는 may-hide 규칙이라 맞지 않았다).
5. **#54:** 2026-10-18에 PR #87 본문의 "whether to close #54"가 closing keyword로 읽혀 의도치 않게 닫혔다.
   범위는 #80과 #92로 다 했으므로 닫힌 채로 두고, 경위를 코멘트로 남겼다.

### 검증 증거

환경: macOS/arm64. PR별 수치는 각 PR 본문의 그 PR head 결과다. 첫 행의 `ebf09a2` suite-run 결과만 이 문서를 위해 다시 돌렸다.

| 검증 | 결과 |
| --- | --- |
| 전체 테스트 `pytest` | PR별 1578(#91) → 2451(#92) → 2472(#94) → 2572(#95) → 2596(#99 생성 시) passed, 11 skipped. `ebf09a2`에서 suite-run **2598 passed, 11 skipped, CLEAN** |
| lint·타입 | 코드 PR마다 `uvx ruff@0.14.2 check src tests scripts examples`와 mypy를 CI 방식으로 돌려 통과 |
| 골든 하네스 | #91·#92·#94·#95·#99에서 직전 merge와 비교해 커밋된 fixture 출력 265개 중 다른 파일 0개. #94·#95 본문은 두 실행이 서로 다른 소스를 import했는지도 따로 확인했다고 적고 있다 |
| 회귀 테스트 판별력 | 코드 PR마다 변경을 하나씩 되돌리는 mutation으로 새 테스트가 빨개짐을 확인(각 PR 본문) |
| CI | #91은 check 17개, 나머지는 15개 모두 통과 |
| 리뷰 게이트 | #91 계획 2·commit 3(마지막 NEEDS-FIX는 #90으로 등록). #92 계획 2·diff 2(SHIP). #94 계획 2·구현 2·PR 1(SHIP). #95 계획 2·구현 2(SHIP), PR 리뷰 없이 소유자 merge. #97 계획 3(매번 기준이 깨짐)·문서 1·PR 1(SHIP). #98 계획 1(네 번째 접근을 깨뜨림), 문서만. #99 계획 3(NEEDS-FIX → DISCUSS → NEEDS-FIX)·구현 2(둘 다 NEEDS-FIX)·PR 3(셋 다 NEEDS-FIX, 마지막은 Minor 문서 1건). 마지막 지적을 고친 `6bf5c1d`는 다시 리뷰하지 않아 SHIP 판정이 없다 |

기록 위치: 결정 근거와 측정은 PR #91·#92·#94·#95·#97·#98·#99와 이슈 #90·#93·#96의 본문, #54의 코멘트에 있다.
리뷰 원문은 저장소에 보존하지 않았다. 이 세션의 임시 디렉터리(`/private/tmp/claude-501/` 아래 세션 `9275dc9c-…`의
`scratchpad/i82`·`i93`·`i90`·`i96`·`i60/logs/`)와 저장소의 `.dev-trio/log/default/`(PR 범위 리뷰)에 나뉘어 있다.
임시 경로이므로 오래 남는다고 가정하지 않는다.

### 남은 결정과 후속 항목

1. **등록만 한 결함 4개**(각 이슈 본문에 재현과 선택지가 있다):
   - #100: 모델 검증을 통과한 `total_cost="1e1000000"`이 `analyze`를 `diagnose.py:436`의 `decimal.Overflow`로 죽인다.
   - #101: `diagnose._safe`가 lone surrogate 이름에서 `UnicodeEncodeError`를 낸다(`analyze` exit 1).
   - #102: `query`·`inspect`·`explain`·`analyze`가 `:ok:` 같은 이름을 emoji로 출력한다. `summarize`만 `emoji=False`다.
   - #103: `artifact.from_obj()`의 v1 migration이 `{exc}`를 포맷하다 `ValueError`가 아닌 예외를 내보낼 수 있다.
2. **알려진 한계(문서화, 고치지 않음):** #90·#96. README와 CHANGELOG의 Known limitations에 있다.
3. **`summarize` 후속(등록 안 함):** gap preset 3개의 edge 증거를 구분하는 방법, `--backend ladybug`. 이슈의
   제품 검증 가설(time-to-prioritize 측정)은 하지 않았다.
4. **다음 릴리스:** `## Unreleased`가 쌓여 있다. 릴리스할지와 버전(새 명령과 새 JSON 계약이 있으므로 minor)은
   소유자가 정한다. 절차는 [릴리스 런북](releasing.md)이다.
5. **기존 보류 결정**(아래 2026-10-14 절의 5번, 2026-09-15 절의 3·4·5·7·8번)은 그대로다.

---

## 이전 전달 상태 — 2026-10-18, `361ad03` 기준 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp`의 최신 커밋. 제품 코드의 기준은 `361ad03`(PR #86 병합)이고,
  그 위의 `c4d3b7d`(PR #88)는 버전·`uv.lock`·CHANGELOG·이 문서만 바꿨고 제품 소스는 그대로다. 시작 전에
  `git switch tracegraph-mvp && git pull --ff-only`로 맞춘다.
- **0.5.0 공개 완료(2026-10-18).** `agent-tracegraph` 0.5.0이 PyPI 최신이다. `test-v0.5.0`과 `v0.5.0`이
  모두 `c4d3b7d`를 가리킨다. 0.4.0은 artifact v1~v5만 읽으므로 v7을 쓰는 이 릴리스는 minor다. 범위는
  `CHANGELOG.md`의 `0.5.0 - 2026-10-18`, 업그레이드 주의는
  [GitHub Release v0.5.0](https://github.com/memtomem/tracegraph/releases/tag/v0.5.0)의 "Before you upgrade"에 있다.
  릴리스 검증은 아래 "남은 결정과 후속 항목" 5번에 적었다. 그다음 릴리스에 들어갈 변경은 아직 없다.
- **0.5.0에서 놓치기 쉬운 동작 변경**(아래 `b18fd1a` 기준 절의 것에 더해). 전체 목록은
  `CHANGELOG.md` `0.5.0 - 2026-10-18`의 해당 항목이다:
  - library writer `artifact.save()`/`save_atomic()`가 reader가 거부할 trace를 쓰지 않고 `ValueError`
    (pydantic `ValidationError` 포함)를 낸다(PR #83). 잘못된 타입의 `seq`는 이전엔 raw `TypeError`였고,
    provenance 위반도 writer에서는 `check_provenance` 문구 대신 `ValidationError`로 나온다.
  - 새 capture gap 코드 4개(schema는 v7 그대로). 이런 입력에서는 artifact와 그 보고서 digest가 바뀐다.
    `check`에 주는 영향은 코드마다 다르다:
    - `otlp.unresolved_span_link`·`otlp.unscoped_span_link`(PR #84): `_MAY_OMIT_STEPS`. count rule은
      gap 쪽으로 one-sided가 되고, topology는 어느 쪽에 gap이 있어도 INSUFFICIENT_EVIDENCE다.
    - `otlp.foreign_trace_link`(PR #84): 판정에 영향 없음.
    - `langgraph.rewritten_error_channel`(PR #86): count rule만 one-sided, success 의존 pattern은
      INSUFFICIENT_EVIDENCE, topology는 영향 없음.
  - `query`에서 interrupt와 `MemoryError`는 읽던 artifact에서 바로 드러나고, `--limit` 밖 match는
    label을 만들지 않아 거기서 나던 label 오류도 더는 나지 않는다(PR #80).
  - `--strict`(PR #81)는 opt-in이라 기본 동작은 그대로다. 켜면 건너뛴 파일이 있을 때 exit 2이고 export는 아무것도 쓰지 않는다.
- **열린 이슈 5개:** 소유자 결정 대기 #79·#82·#85, 일부 해결 #54(닫을지 결정 필요), 기능 제안 #60.

### `b18fd1a` 이후 착지한 변경

| PR | merge | 내용 |
| --- | --- | --- |
| #77 | `06daf97` | `b18fd1a` 기준 절의 handoff 문서(문서만) |
| #78 | `66d66c9` | #52: `repeated-agent-failure`의 unbounded two-step 경로가 이름이 맞을 수 없는 endpoint를 순회 없이 건너뛰고, 역방향 탐색을 seq window로 제한. star 모양의 longest-path DP도 부모 edge 완화로 바꿨다. n=4000 distinct 이름 체인 5.821 s → 0.0024 s |
| #80 | `13a10b7` | #54의 corpus 절반: `query`가 artifact를 하나씩 읽고 출력할 match만 남긴다. 200개 artifact `--limit 1` 1370 MiB → 70 MiB. 출력과 exit는 옛 CLI와 154개 호출에서 같다. trace 안의 절반은 #79로 분리 |
| #81 | `f4f44a3` | #50: `query`·`export-review-candidates`에 opt-in `--strict`. 디렉터리에서 건너뛴 파일이 하나라도 있으면 exit 2, export는 아무것도 쓰지 않는다. 기본 동작은 그대로 |
| #83 | `737e889` | #67: 두 writer가 model round-trip → `validate_normalized` → 검증된 복사본 직렬화 순으로 확인한 뒤에만 쓴다. `dumps()`만 남은 `TypeError`는 #82로 등록 |
| #84 | `d5e31e4` | #49: OTLP importer가 cause로 기록하지 못한 span link를 종류별 stderr 한 줄과 capture gap으로 공시. 앞의 두 코드는 `check`의 `_MAY_OMIT_STEPS`에 들어간다 |
| #86 | `361ad03` | #47: 오류 채널이 이미 set인 동안 node가 그 채널을 다시 쓰면 공시만 한다(`langgraph.rewritten_error_channel`). 실패 기록 규칙은 그대로. `check`는 count rule을 gap 쪽으로 one-sided로, success 의존 pattern을 INSUFFICIENT_EVIDENCE로 읽는다. subgraph `__start__`의 중복 기록은 #85로 등록 |
| #87 | `9bb37a5` | 이 절의 handoff 문서(문서만) |
| #88 | `c4d3b7d` | 0.5.0 준비: 버전 0.4.0 → 0.5.0, CHANGELOG 날짜, 리뷰 3라운드가 찾은 릴리스 노트 오류 4건 수정 |

### 이번 세션의 결정(2026-10-18, PR #78~#86)

1. **#50:** 이슈의 옵션 1, opt-in `--strict`. `validate`는 이미 모든 무효 파일을 보고하고 exit 1이므로
   flag를 받지 않는다. 우선순위는 명시한 손상 파일 → strict 거부 → "no valid artifacts" → 첫 중복
   trace_id → `query`가 들고 있던 matching 실패.
2. **#67:** 두 writer 모두 쓰기 전에 검사. 처음 고른 `validate_structure`는 commit 리뷰가 store는
   거부하지만 이 검사는 통과하는 canonical 위반 4개를 보여 줘서 `validate_normalized`로 넓혔다
   (6000 step에서 +0.061 s).
3. **#49:** 옵션 1, `Disclosures.add`로 ingest 시 공시. 새 header 필드는 없고 `links_preserved`도
   바꾸지 않는다. `traceId`가 없는 link는 그 span이 local일 수 있어 foreign과 다른 코드로 나눴다.
4. **#47: 공시만 한다.** 반복 실패를 기록하려던 규칙(이슈의 version-only, 그 뒤 value-change
   `226d406`)은 리뷰 다섯 라운드 연속으로 정상 node에 실패를 지어내는 모양이 하나씩 더 나왔다
   (subgraph 경계, caller input, fork copy, success 의존 pattern, reducer). 소유자가 범위를 공시만으로
   좁혔다(`16d7b90`). checkpoint는 어떤 node가 raise했는지 기록하지 않으므로 이 범위로 다시 열지 않는다.
5. **#54 분리:** corpus 절반은 PR #80으로 처리했고, trace 안의 match 열거 제한은 #79로 옮겼다.
   #54는 열어 둔 채다.

### 검증 증거

환경: macOS/arm64. 수치는 각 PR 본문에 적은 그 PR head의 결과이고, 이 문서를 위해 다시 돌리지 않았다.

| 검증 | 결과 |
| --- | --- |
| 전체 테스트 `pytest` | PR별 1418(#78) → 1445(#80) → 1457(#81) → 1485(#83) → 1508(#84) → **1541**(#86) passed, 11 skipped. #86은 LangGraph 1.2.15 / checkpoint 4.3.0에서도 `-m "not perf"` 1533 passed |
| 성능 guard | #78이 `-m perf` doubling guard 4개를 더했다(옛 matcher에서 모두 실패) |
| lint·타입 | 모든 PR에서 ruff와 mypy를 CI 방식으로 돌려 통과 |
| 골든 하네스 | 모든 PR에서 직전 merge와 비교해 커밋된 fixture 출력 265개 중 다른 파일 0개. 대조군 `0bbe9e6`→`43f5ccb`는 39개가 달랐다(#78에서 확인). 절차는 아래 `b18fd1a` 기준 절에 있다 |
| 회귀 테스트 판별력 | PR마다 mutation을 넣어 새 테스트가 빨개짐을 확인(각 PR 본문의 Mutations 목록) |
| CI | #78·#80·#81·#83·#84는 check 15개, #86은 17개 모두 통과. merge 커밋 `361ad03`의 push `tests` 실행도 통과 |
| 리뷰 게이트 | #78 3라운드, #80 5라운드(계획 2, diff 2, commit 1), #81 2라운드, #83 4라운드, #84 4라운드(계획 2, diff·commit 각 1), #86 8라운드(계획 2, diff 2, commit 1, 최종 1, PR 범위 2). 마지막 라운드는 #81·#83·#84·#86이 SHIP이다. #78은 commit 라운드가 문서 문구 2건으로 NEEDS-FIX였고 `6ca360f`에서 고쳤으며, #80은 commit 라운드가 지적한 메모리 문구 2건을 `41bc87a`에서 고쳤다(판정은 본문에 없다). |

기록 위치: 결정 근거와 측정은 PR #78·#80·#81·#83·#84·#86의 본문과 이슈 #79·#82·#85의 본문에 있다.
이 PR들의 리뷰 원문은 저장소에 보존하지 않았다. 저장소의 `.dev-trio/log/default/`가 아니라 그 작업 세션의 임시
디렉터리(`/private/tmp/claude-501/` 아래 세션 `13efa5c6-…`의 `scratchpad/devtrio-log/default/`)에 있고, 임시 경로이므로
오래 남는다고 가정하지 않는다.

### 남은 결정과 후속 항목

1. **소유자 결정 대기:**
   - #79(trace 안의 match 열거 제한): `--limit`을 exact로 유지하며 materialize하지 않고 세기,
     total을 계산하지 않는 명시적 bounded 모드 추가, 또는 한계를 문서로만 두기. 선택지는 이슈 본문에 있다.
   - #82(`artifact.dumps()`의 `TypeError`): writer와 같은 round-trip을 `dumps()`에도 넣기, 또는
     `TypeError`만 `ValueError`로 바꿔 던지기, 또는 `dumps()`를 입력을 신뢰하는 serializer로 문서화하기
     (#67의 CHANGELOG 항목은 지금 `dumps()`를 model·structure 검사 없는 serializer로 적고 있다).
   - #85(subgraph `__start__`에서 전파된 오류를 다시 기록): #47 이전부터 있던 결함이다.
2. **#54 닫기:** corpus 절반은 PR #80으로 끝났고 남은 절반은 #79에 있다. 닫을지는 소유자가 정한다.
3. **기능 제안:** #60(명시한 trace 묶음의 반복 인과 패턴 요약). 이슈가 #54의 bounded query와 함께
   설계하라고 하므로 #79 결정 뒤가 맞다.
4. **기존 보류 결정**(아래 2026-10-14 절의 5번, 2026-09-15 절의 3·4·5·7·8번)은 그대로다.
5. **0.5.0 릴리스: 완료(2026-10-18).** [릴리스 런북](releasing.md)대로 했다.
   - 준비 PR #88의 로컬 게이트: `pytest` 1541 passed, `twine@7.0.0 check` 통과, sdist 38 members 모두 허용
     목록 안. 리뷰 3라운드가 PR마다 쌓인 CHANGELOG 문장 중 릴리스 전체로는 틀린 것 4개를 찾았다(#63의
     "still unbounded", `check`의 provenance 설명, 노트북의 "every core command", `inspect`의 "unchanged").
   - TestPyPI 예행: [run 39132870808](https://github.com/memtomem/tracegraph/actions/runs/39132870808).
     production: [run 39136213678](https://github.com/memtomem/tracegraph/actions/runs/39136213678).
   - wheel `c69a3080…f092`, sdist `56cbef8d…0371`. 예행 build, TestPyPI 파일, production build,
     PyPI 파일이 바이트 단위로 같고, 두 파일 모두 `release.yml`·`pypi` environment의 publish
     attestation이 있다.
   - TestPyPI와 PyPI에서 받은 wheel의 깨끗한 core·`[cypher]` 설치(Python 3.12.11)에서
     `scripts/verify_wheel.py` 통과(cypher backend parity 포함). PyPI description은 `v0.5.0`의
     `README.md`와 바이트 단위로 같고 페이지는 HTTP 200, `uvx --from agent-tracegraph==0.5.0`은 바로 설치됐다.
   - [GitHub Release v0.5.0](https://github.com/memtomem/tracegraph/releases/tag/v0.5.0)은 prerelease로
     만들었다. 노트는 claims 리뷰 2라운드를 거쳤다(12건, 7건. 2라운드 7건 중 3건은 1라운드 수정에서 나왔다).

---

## 이전 전달 상태 — 2026-10-18, `b18fd1a` 기준 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp` @ `06daf97`(이 절을 올린 docs PR #77의 병합). 제품 코드의
  기준은 `b18fd1a`(PR #76 병합)이고, 이 문서는 그 위에 문서만 더한다. 시작 전에
  `git switch tracegraph-mvp && git pull --ff-only`로 맞춘다. PyPI 최신은
  여전히 0.4.0이다. 0.4.0 이후 `tracegraph-mvp`에 들어간 변경(PR #41, PR #44, 그리고 아래 표)은 `CHANGELOG.md`의 Unreleased에
  있고, artifact가 v6·v7로 올라가며 새 보고서 계약 `tracegraph.check` v1이 생겼으므로 다음 릴리스는
  0.5.0이다(릴리스는 아직 소유자 지시 대기).
- **0.5.0 노트에 꼭 들어갈 동작 변경:** 아래 PR #70·#71로 artifact 입력에 다른 trace를 고르는
  `analyze --trace`/`--baseline-trace`와 `ingest-phoenix --trace`가 exit 0에서 exit 2가 됐다.
  PR #76으로 `phoenix doctor`/`diagnose`의 `px` stdout에도 `TRACEGRAPH_MAX_ARTIFACT_BYTES`가 적용된다.
- **열린 이슈 7개:** 소유자 결정 대기 #47·#49·#50·#67, 성능 #52·#54, 기능 제안 #60.

### 2026-10-14 이후 착지한 변경

| PR | merge | 내용 |
| --- | --- | --- |
| #48, #51 | `f6e90c4`, `b6b4480` | 한국어 getting-started 노트북 `examples/getting_started_ko.ipynb`와 리뷰 후속 |
| — | `609882f` | 구현·제품 사용성 리뷰 기록 `docs/reviews/2026-10-16-*.md`(이슈 #52~#60의 출처) |
| — | `1ee2cf3` | 이슈 #58: `analyze`·`phoenix diagnose` 뒤의 조사 안내(정확한 step id, `explain` 명령 또는 저장 템플릿). PR 없이 직접 커밋돼 이슈는 2026-10-18에 조건별 확인 댓글과 함께 닫았다 |
| #61, #62 | `81e2bfb`, `706b73d` | stdin·`/dev/fd`·`/proc/<pid>/fd` 같은 descriptor alias 입력은 재생 템플릿으로 안내. 디렉터리 descriptor로 가는 symlink는 남은 한계(결정 기록) |
| #63 | `0bbe9e6` | OTLP export를 `analyze`로 바로 진단, stdin도 `TRACEGRAPH_MAX_ARTIFACT_BYTES`로 제한(#53의 stdin 절반) |
| #65 | `43f5ccb` | capture provenance(`trace.capture.gaps`, step `unknown_fields`), artifact `schema_version` 7 |
| #66, #68 | `45a25e9`, `a86dde9` | `tracegraph check`(PASS 0 / FAIL 1 / INSUFFICIENT_EVIDENCE 3 / 사용 오류 2)와 step을 빠뜨릴 수 있는 capture gap의 일관 처리 |
| #70 | `3173c95` | #64: artifact 입력의 selector는 그 artifact의 trace id와 같아야 함(`analyze`, `check` 공용 loader) |
| #71 | `591fa81` | #69: 같은 규칙을 Phoenix artifact를 받은 `ingest-phoenix`에 적용(`_require_selected_trace`) |
| #72 | `cebe39a` | #56: 실행할 수 없는 `px`(권한·디렉터리·형식 오류)가 traceback 대신 `doctor`는 `FAIL`/exit 1, `diagnose`는 사용 오류/exit 2. 고정 문구에 OS 오류 부분만 `os.strerror(errno)`로 넣고, 실행 거부(`EACCES`/`ENOEXEC`)면 실행 파일 확인 안내를 덧붙인다 |
| #74 | `0e3f3a8` | #73: `killed_run` fixture가 sleep 대신 x의 write가 SQLite에 기록될 때까지 기다림(테스트만) |
| #75 | `3639633` | #55: `SECURITY.md`에 `ingest --graph` 신뢰 경계와 입력 세 종류 표, stdin 크기 제한 정정(문서만) |
| #76 | `b18fd1a` | #53: `px` stdout을 이름 없는 임시 파일로 받아 크기 polling, stderr 버림, UTF-8 아님 보고, 위치 읽기 |

### 이번 세션의 결정(2026-10-18)

1. **#64·#69 selector 규칙:** 옵션 1(일치하지 않으면 exit 2)을 골랐다. artifact에 selector를 아예
   거부하는 옵션 2는 `check`가 이미 받아들이던 일치 selector와 어긋나서 버렸다.
2. **#46 not planned:** `__pregel_tasks`가 Topic 채널 값이 된 것은 LangGraph `4e8fbe4525`
   (2025-05-25)부터이고, `Topic.checkpoint()`는 그보다 앞선 `f2ec99b9a8`(2024-08-27)부터 list를 쓴다.
   tuple 형태를 쓴 공식 버전이 없어서 닫았다. 옛 형태는 이슈가 말한 `(flag, packets)`가 아니라
   `(seen, values)`다. 아래 2026-10-14 절의 15번이 이 건이다.
3. **#53 `px` 출력:** stdout은 기존 `TRACEGRAPH_MAX_ARTIFACT_BYTES`(기본 256 MiB, `0`은 해제),
   stderr는 DEVNULL. 처음 계획한 pipe + reader 스레드 설계는 계획 리뷰 2라운드의 Major 4건이 모두
   reader에 몰려 버리고, 임시 파일 + 한 스레드 polling으로 바꿨다. 그 뒤 리뷰가 `px`의 자손 프로세스
   문제를 거듭 지적했고, 프로세스 그룹 kill은 이미 회수한 리더의 pgid가 재사용되면 무관한 그룹을
   죽일 수 있어 버렸다(macOS Python에는 회수 없이 종료를 확인할 `os.waitid`가 없다). **자손 격리는
   범위 밖**으로 정했다. `px`는 사용자가 설치한 신뢰 실행 파일이고(`--graph` 모듈과 같은 경계), #53
   이전의 `subprocess.run`도 자손을 가두지 않았다. 대신 캡처는 종료를 관측한 시점의 크기까지
   `os.pread`로 읽어(POSIX) 자손의 늦은 write가 덮어쓰지 못하게 했다. 이 범위로 다시 열지 않는다.
4. **#58 completed, #73 신규 등록 후 해결.** #73의 원인은 측정으로 확인했다(sleep 0이면 10/10 실패,
   CI의 `[]`와 같은 모양).

### 검증 증거

환경: macOS/arm64, Python 3.12.11, `b18fd1a`.

| 검증 | 결과 |
| --- | --- |
| 전체 테스트 `pytest -q -m "not perf"` | **1366 passed**, 11 skipped |
| 성능 guard `pytest -m perf` | **4 passed** |
| lint `uvx ruff@0.14.2 check src tests scripts examples` | 통과 |
| 타입 `mypy`(CI와 같은 `--extra cypher --group typing` 환경) | 0 errors |
| 골든 하네스 | 코드를 바꾼 PR #70·#71·#72·#76 모두 직전 merge와 비교해 커밋된 fixture 출력 265개 중 다른 파일 0개. **세션 중 PR 본문에 적은 비교는 무효였다**: 스크립트에 `<checkout>/src`를 넘겨 두 실행 모두 현재 checkout을 import한 자기 비교였다. 위 수치는 2026-10-18에 저장소 루트를 넘겨 다시 잰 것이고, 대조군 #65(`0bbe9e6`→`43f5ccb`, artifact v7)는 같은 방법으로 39개 파일이 달라 하네스가 변화를 잡는 것을 확인했다(잘못된 형태로는 0개). #74(테스트만)·#75(문서만)는 돌리지 않았다 |
| 회귀 테스트 판별력 | 테스트를 더한 PR마다 새 테스트가 수정 전 소스에서 실패함을 확인(seam이 옮겨져서만 실패하는 것은 PR 본문에 따로 적었다) |
| CI | PR #70·#71·#72·#74·#75·#76 모두 check 15개 통과(required 8개 포함), `b18fd1a`의 `tests` 통과 |
| 리뷰 게이트 | PR당 4~8라운드(계획, 구현, PR 범위). #75는 계획 없이 문서 리뷰 3라운드 + PR 범위 1라운드, #76은 설계 교체와 범위 결정을 거쳐 8라운드. 모든 지적을 반영하거나 결정으로 범위를 정했다 |

골든 비교 절차. 저장소 밖 스크립트 `~/.claude/plans/tracegraph-golden/golden.sh <checkout-root> <out-dir>`를
쓴다. **첫 인자는 checkout 루트이고 `src`가 아니다.** 스크립트가 `PYTHONPATH="$1/src"`를 만들기 때문에,
`.../src`를 넘기면 없는 경로가 되어 editable 설치된 현재 checkout을 import한다. 스크립트는 한쪽 출력만
만들고, out-dir을 지운 뒤 다시 쓴다. fixture와 Python은 현재 checkout의 것을 쓴다. 비교 결과가 모두 같으면
대조군 쌍부터 돌려 하네스를 의심한다:

```bash
git worktree add /tmp/tg-base <base-sha>
PYTHONPATH=/tmp/tg-base/src .venv/bin/python -c 'import tracegraph; print(tracegraph.__file__)'  # /tmp/tg-base 아래여야 한다
~/.claude/plans/tracegraph-golden/golden.sh /tmp/tg-base /tmp/g-base
~/.claude/plans/tracegraph-golden/golden.sh "$PWD" /tmp/g-new
diff -r /tmp/g-base /tmp/g-new && echo identical
git worktree remove --force /tmp/tg-base
# 대조군: 0bbe9e6 → 43f5ccb(artifact v7)는 39개 파일이 달라야 한다
```

기록 위치: 결정 근거는 PR #70·#71·#72·#74·#75·#76의 본문과 이슈 닫는 댓글(#46, #58)에 있다. 이번 세션의 계획·범위
결정·재현 스크립트는 저장소 밖 `~/.claude/plans/tracegraph-2026-10-18/`(`scope53.md`가 #53의
자손 범위 결정, `probes/`에 재현 스크립트)에 있다. 리뷰 원문은 커밋하지 않은 로컬
`.dev-trio/log/default/`(PR 범위 리뷰)에 있고, 세션 중의 계획·구현 리뷰 로그는 세션 임시 디렉터리에
있었으므로 남지 않는다.

### 남은 결정과 후속 항목

1. **소유자 결정 대기:** #47(오류 채널이 남아 있는 동안의 반복 실패), #49(사라진 span link의 무공시),
   #50(`query <dir>`이 손상 artifact를 건너뛰어 CI gate가 통과하고, 2026-10-16 댓글대로
   `export-review-candidates`도 손상 입력을 건너뛴 채 빈 보고서를 exit 0으로 낸다), #67(library writer가 CLI가 거부할
   artifact를 쓸 수 있음). 선택지는 각 이슈 본문에 있다.
2. **성능:** #52(이름이 맞을 수 없는데도 반복 agent 매칭이 이차), #54(`query --limit` 전에 모든 match를
   만듦). 소유자 결정 없이 진행할 수 있다.
3. **기능 제안:** #60(명시한 trace 묶음의 반복 인과 패턴 요약).
4. **기존 보류 결정**(아래 2026-10-14 절의 5번, 2026-09-15 절의 3·4·5·7·8번)은 그대로다.
5. **0.5.0 릴리스:** [릴리스 런북](releasing.md)대로. `scripts/verify_wheel.py`에는 새 ingest가
   v7과 `capture`를 선언하는지 보는 smoke가 이미 있으니, 공개된 wheel로 그 smoke가 통과하는지
   확인할 것.

---

## 이전 전달 상태 — 2026-10-14 (Asia/Seoul)

- **다음 세션 시작점:** `tracegraph-mvp` @ `3a955a1`(PR #44 병합), 작업 트리 깨끗. PyPI 최신은
  0.4.0이다. 그 뒤 착지한 미릴리스 변경은 PR #41(11번), PR #43(13번, 테스트만), PR #44(14번,
  artifact v6)다. 다음 릴리스는 artifact 계약이 바뀌므로 0.4.1이 아니라 0.5.0이 맞다(소유자 결정
  대기). 열린 이슈는 없다.
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
14. **이슈 #37 — 예약됐지만 기록이 없는 task 보고: 착지, 미릴리스(PR #44, merge `3a955a1`, 2026-10-14).**
   - `ingest --graph path/to/file.py:attr`(`langgraph.json` 형식) 또는 `pkg.module:attr`로 컴파일된
     그래프를 받아, LangGraph 공개 API `get_state`가 가장 최근 root checkpoint에서 예약하는 task 중
     write가 하나도 없는 것을 `unset` step(`source=scheduled`)으로 보고한다. `Send`는 packet마다 하나.
     끝남·실패·멈춤은 여전히 checkpoint의 write로 판정하므로 `task`·`interrupt` step과 겹치지 않는다.
   - **계약(소유자 결정):** 수용 기준을 "thread를 쓴 그래프 기준"으로 다시 정했다. head 하나를 보는
     consistency gate는 리뷰 2라운드에서 양방향으로 실패했다(과거 `versions_seen` 때문에 정상 그래프
     거부, trigger를 추가한 그래프 통과). 대신 그래프 구조 지문을 `trace.schedule_graph`에 남긴다
     (예약 task가 없어도). 새 `StepSource.SCHEDULED`, 내용 기준 artifact v6, trace status 유지.
     `--graph` 없이 만든 artifact와 출력은 바이트가 같다(커밋된 fixture 출력 252개 확인).
   - 비공개 `prepare_next_tasks`는 버렸다. migration·delta 채널 복원·null write를 다시 구현해야 했고,
     "사용자 코드 안 돌림"이라는 장점도 사실이 아니었다(managed value, 채널 `from_checkpoint`).
   - 신뢰 경계: 모듈 import(top-level 코드)와, 읽는 동안 그래프의 state callback(mapper·validator,
     reducer, custom channel, managed value, cache-key 함수)이 실행된다. 노드는 실행되지 않는다.
   - 공시만 하는 경우: head에 적용 안 된 `Command(goto)` 입력, 그래프에 없는 노드로 가는 `Send`.
   - 검증: 테스트 1033개(신규 96개, 1.2.2·1.2.15·`COLUMNS=40`), 의도적 회귀 8개 중 7개 검출(놓친
     1개는 명시적 head id가 같은 위험을 이중으로 막는 장치). 리뷰: 계획 2라운드, 구현 2라운드.
   - 재현 스크립트와 계획: `~/.claude/plans/p37-scheduled-tasks.md`, `~/.claude/plans/p37-probes/`
     (저장소 밖).
15. **범위 밖 기존 결함(미해결, 이슈 미등록).** 오류 task 경로의 `_undecodable_sends`
   (`adapters/langgraph_checkpoint.py`)가 tasks 채널을 평면 리스트로 읽는다. 옛 Topic 인코딩
   `(flag, packets)`에서는 packet 3개를 "2 Send packet(s) could not be decoded (langgraph is not
   installed)"로 잘못 공시한다. 오래된 checkpoint에만 해당한다. PR #44의 `_send_packets`처럼 그래프
   채널에 디코딩을 맡기면 되지만, 그 경로에는 그래프가 없으므로 별도 판단이 필요하다.

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
