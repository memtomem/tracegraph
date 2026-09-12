# Tracegraph 현재 구현 종합 리뷰

> 이 문서는 기준 SHA의 원검토 및 당시 개선 기록이다. F-01~10은 11절에서 해결됐다.
> 2026-09-08 CI import 문제와 후속 wheel 검증, 현재 전달 상태는
> [HANDOFF](../HANDOFF.md)에 기록한다. 아래의 과거 재현·미실행 상태를 현재 상태로 재사용하지 않는다.

- 검토일: 2026-09-07 (Asia/Seoul)
- 기준: `e1a4d44aad5dcd5da736d75f48974173241422e6`, 로컬 브랜치 `tracegraph-mvp`
- 범위: 전체 제품 코드 20개 Python 모듈, 관련 테스트·fixture, CLI, 공개 JSON 계약, 예제, 문서, 빌드 설정, 두 GitHub Actions workflow
- 방법: 코드·계약 대조, 로컬 테스트, 합성 입력 재현, 설치된 의존성 소스 확인, 기존 CI 조회
- 원검토 당시 변경: 이 보고서만 추가. 이후 승인된 개선 구현·검증 결과는 [11절](#11-개선-구현-결과--2026-09-07)에 기록했다. 커밋·배포·새 외부 E2E 실행은 수행하지 않음

## 1. 종합 판단

**핵심 그래프 모델과 정상 입력의 분석 경로는 잘 구성되어 있지만, 외부 입력의 파일 경로 처리와 개인정보 표시, 자동 원인 분류에 우선 수정할 문제가 있다.** 정상 경로 테스트와 기존 연동 CI의 성공만으로 이를 배제할 수 없다.

실행으로 확인한 구현 문제는 **9건**, 코드·문서 대조로 확인한 문서 불일치는 **1건**이다. 우선순위는 P1 3건, P2 6건, P3 1건으로 분류했다. 이와 별도로 기존 계약이 의도적으로 허용하는 한계와 검증 공백을 구분했다.

가장 먼저 대응할 항목은 다음과 같다.

1. 입력 trace ID가 기본 출력 경로로 사용되어 작업 디렉터리 밖의 파일을 덮어쓸 수 있다.
2. OTLP에서 가져온 자연어 span 이름이 분석 보고서에 남는데도 `privacy_profile=safe-v1`로 표시된다.
3. 중첩된 agent와 tool이 모두 오류이면, 호출 관계만으로 agent를 주요 실패로, tool을 전파된 실패로 분류한다.

강점도 명확하다. 원본 인과 그래프와 파생 트리를 분리하고, canonical 검증으로 artifact 변형을 거부하며, 선택적 LadybugDB와 Python matcher의 동등성을 실제 테스트한다. 정확한 입력 바이트의 digest에 묶인 검토 후보와 원자적 출력, 외부 실행 계층과의 책임 분리는 유지할 가치가 있다.

현재 결과는 통제된 입력의 MVP·개발 분석 경로가 작동한다는 증거다. 임의 export를 안전하게 취급하거나 자동 진단을 확정적 RCA로 사용하는 범위는 아래 문제를 해소한 후 다시 평가해야 한다.

## 2. 검증 환경과 실행 증거

### 2.1 로컬 환경

| 항목 | 확인값 |
| --- | --- |
| OS / 아키텍처 | macOS 26.6.2 / arm64 |
| Python | 3.12.11 |
| tracegraph / Pydantic | 0.1.0 / 2.13.4 |
| LangGraph / checkpoint | 1.2.2 / 4.1.1 |
| checkpoint-sqlite / LadybugDB | 3.1.0 / 0.18.1 |
| pytest | 9.0.3 |
| 검토 시작 worktree | 깨끗함 |

이 세션의 사전 검증에서 다음 명령을 실행했다. 코드 변경이 없고 기준 커밋이 동일함을 본 검토에서도 확인했다.

| 명령 | 결과와 해석 |
| --- | --- |
| `.venv/bin/python -m pytest -q -m 'not perf'` | 373 passed, 3 failed, 3 deselected. 실패 3개는 로컬 HTTP bind에 대한 샌드박스 `PermissionError` |
| `.venv/bin/python -m pytest -q tests/test_phoenix_cli_contract.py` | 샌드박스 밖에서 재실행: 3 passed. 위 실패는 환경 제한으로 확인 |
| `.venv/bin/python -m pytest -q -m perf` | 3 passed, 376 deselected |
| `uv build --out-dir /private/tmp/tracegraph-review-20260907-dist` | sdist와 wheel 빌드 성공 |
| 본문 부록의 재현 코드 | F-01~F-09 및 L-01/L-02의 관찰 결과 확인 |

따라서 기능 테스트 376개와 성능 테스트 3개의 통과를 확인했다. **단일 실행에서 379개가 모두 통과했다는 뜻은 아니다.** 추가 재현은 기존 테스트가 놓친 입력·사용 경계를 확인하는 별도 증거다. Ruff·mypy·coverage는 현재 환경에 설치되어 있지 않았으며, 이번 검토에서 실행하거나 커버리지 비율을 산출하지 않았다.

빌드된 wheel에는 제품 Python 모듈 20개가 포함되고 공개 JSON schema는 포함되지 않았다. sdist에는 두 schema가 포함됐다. 검토 후보 schema를 저장소에서 vendor하라는 README의 명시적 계약과 일치한다. 새 환경에 wheel을 설치한 smoke test나 의존성 취약점 DB 전수 조회는 수행하지 않았다.

### 2.2 기존 원격 CI

두 실행 모두 기준 커밋과 정확히 일치한다.

| 실행 | 결과 | 검증 범위 |
| --- | --- | --- |
| [일반 tests — 33600601243](https://github.com/memtomem/tracegraph/actions/runs/33600601243) | 2026-09-02, 성공 | pure-python core, Cypher extra, 성능 guard, Phoenix CLI 계약 4개 job |
| [Phoenix + SyncMill E2E — 34099825293](https://github.com/memtomem/tracegraph/actions/runs/34099825293) | 2026-09-07, scheduled, 성공 | 실제 Phoenix·px·SyncMill 연결, 명시적 재시도, 비교, 후보 import 경로 |

원격 성공 여부는 GitHub CLI로 run과 job 메타데이터를 조회했다. 새 E2E를 실행하거나 보관된 evidence artifact를 다운로드해 내용 전체를 재검증하지는 않았다. 저장소의 현재 pin은 Phoenix CLI `1.8.1`, 서버 `18.0.0`과 Docker SHA-256 digest다. E2E에서 실제 사용한 SyncMill SHA까지 이번 보고서가 독립적으로 검증한 것은 아니다.

## 3. 중요도별 발견 사항

P1은 보안·개인정보 경계 또는 핵심 진단에 큰 영향을 주어 우선 수정할 문제, P2는 특정 입력·사용에서 정확성·안정성이 깨지는 문제, P3는 문서·사용성 정합성 문제다. 아래 코드 줄 번호는 모두 기준 커밋에 대한 위치다.

| ID | 우선순위 | 관점 | 발견 | 검증 |
| --- | --- | --- | --- | --- |
| F-01 | P1 | 파일 안전성 | trace ID 기반 기본 출력 경로 이탈·덮어쓰기 | CLI 재현 |
| F-02 | P1 | 개인정보 | 본문이 남은 보고서에도 `safe-v1` 표시 | CLI 재현 |
| F-03 | P1 | RCA 정확성 | 중첩 span의 오류 전파 방향을 호출 관계로 단정 | Phoenix adapter → 분석 재현 |
| F-04 | P2 | 상태 정확성 | 명시적 OK가 exception 이벤트 때문에 ERROR로 바뀜 | adapter 재현·공식 사양 대조 |
| F-05 | P2 | 비교 안정성·성능 | 깊은 트리 diff에서 재귀 오류와 반복 비교 비용 | 1,000~8,000단계 재현 |
| F-06 | P2 | 비교 정확성 | 논리 키 충돌로 상태 변화가 누락됨 | 분석 재현 |
| F-07 | P2 | 입력 원본 보존 | SQLite ingest 실패 중 원본 DB 변경 | CLI 재현·의존성 소스 대조 |
| F-08 | P2 | 입력 검증 | 잘못된 중첩 값과 깊은 JSON이 traceback 경로로 탈출 | CLI 재현 |
| F-09 | P2 | 백엔드 계약 | InMemoryStore 반환값 수정이 원본·스토어를 함께 변경 | 양쪽 백엔드 비교 |
| F-10 | P3 | 문서 | 공식 retry v2와 과거 heuristic 설명·완료 상태가 혼재 | 코드·문서·CI 대조 |

### F-01. 입력 trace ID가 기본 출력 경로를 제어한다

**근거:** [cli.py](../../src/tracegraph/cli.py#L748) 748–749행, Phoenix 경로 763–764행. [artifact.py](../../src/tracegraph/artifact.py#L102) 102행 이후는 부모 디렉터리를 생성하고 `os.replace`로 목적지를 교체한다.

**조건과 실제 동작:** `traceId="../victim"`인 OTLP 파일을 `ingest-otlp --file input.json`으로 읽으면 기본 출력이 `../victim.json`이 된다. 부록 `path_traversal`은 임시 `work/` 밖에 만든 sentinel 파일이 교체되고 명령이 exit 0으로 끝나는 것을 확인했다. 식별자가 절대 경로를 포함하는 경우에도 같은 경로 생성 방식이 적용된다. Phoenix에서도 동일한 default-path 패턴이 있으나 이번 실행 재현은 OTLP로 수행했다.

**기대와 영향:** 외부 export의 식별자는 분석 대상 데이터여야 한다. 출력 위치를 지정하지 않은 사용자가 작업 위치 밖의 파일 변경까지 의도했다고 볼 수 없다. 현재는 프로세스가 쓸 수 있는 위치의 `.json` 파일을 덮어쓸 수 있다. 임의 확장자 쓰기나 코드 실행까지 입증한 것은 아니다.

**수정 방향:** 명시적 `--out`과 데이터에서 파생하는 기본 파일명을 구분한다. 기본 파일명은 경로 구분자·절대 경로·부모 이동을 거부하거나 안전한 digest 기반 이름으로 생성하고, 최종 경로가 출력 루트 안에 있는지 확인한다. 기존 파일 교체 정책도 명시한다.

**수용 기준:** `../`, 절대 경로, 경로 구분자를 포함한 ID가 출력 루트 밖을 바꾸지 않는다. 일반 ID와 명시적 `--out` 사용은 유지된다. 재현: 부록 `path_traversal`.

### F-02. OTLP 본문이 남은 분석 보고서에 safe-v1을 선언한다

**근거:** [otlp_spans.py](../../src/tracegraph/adapters/otlp_spans.py#L378) 378–380행의 이름·오류 저장, [diagnose.py](../../src/tracegraph/analysis/diagnose.py#L118) 118–125행과 230행의 이름 전달. Phoenix에만 [phoenix_export.py](../../src/tracegraph/adapters/phoenix_export.py#L58) 58행의 identifier-shaped 이름 필터가 있다.

**조건과 실제 동작:** OTLP span 이름을 `Prompt: REVIEW_PRIVATE_ACCOUNT_BODY`로 만들고 정규화한 artifact에 `analyze --json-out`을 실행하면 본문이 JSON에 남고 `privacy_profile`은 `safe-v1`이다. CLI는 exit 0이다. 원시 오류 메시지는 보고서에서 제거되지만 OTLP artifact에는 남는다.

**기대와 영향:** `safe-v1`이라는 출력 계약은 입력 어댑터에 따라 암묵적으로 달라져서는 안 된다. 사용자나 후속 시스템이 안전한 보고서로 간주해 공유·보관할 때 자연어 span 이름에 포함된 본문이 유출될 수 있다. Phoenix 전용 기존 redaction 테스트가 실패했다는 의미는 아니다.

**수정 방향:** 보고서 생성 경계에 공통 privacy 처리 또는 검증된 provenance를 둔다. 진단 step 이름, 패턴 label, baseline 변경 설명, 평가 텍스트의 허용 범위를 함께 정의한다. 원시 OTLP·LangGraph artifact 보존 정책은 별도로 표시하고, 안전성 검증 없이 `safe-v1`을 부여하지 않는다. Collector 예제도 span name 자체를 제거하지 않으므로 upstream attribute allowlist만으로 이 문제를 해결할 수 없다.

**수용 기준:** 동일한 민감 본문 fixture를 Phoenix·OTLP·기존 artifact 경로로 공급해도 `safe-v1` 출력에 남지 않는다. 허용된 구조 식별자와 qualified tool identity는 필요한 계약에 따라 유지한다. 재현: `privacy_profile`.

### F-03. 호출 계층의 부모 오류를 자식 오류의 원인으로 단정한다

**근거:** [diagnose.py](../../src/tracegraph/analysis/diagnose.py#L153) 153–171행. `has_error_ancestor`는 edge origin을 구분하지 않는다. [otlp_spans.py](../../src/tracegraph/adapters/otlp_spans.py#L638)의 fallback은 span parent를 같은 causal edge로 전달한다.

**조건과 실제 동작:** agent span 안에서 search tool span이 실행되고 두 span 모두 ERROR인 Phoenix 입력을 분석했다. agent는 1~4초, tool은 2~3초 구간이다. 결과는 `primary=[agent]`, `propagated=[tool]`이며 주요 실패의 인과 edge는 0개다.

**기대와 영향:** 부모 span은 자식 실행을 감싸는 범위이므로 그 부모의 최종 ERROR는 자식 실패의 전파 결과일 수도 있다. 현재 입력만으로 어느 오류가 다른 오류를 유발했는지 확정할 수 없는데도 tool을 전파된 오류로 배제한다. 중첩된 실패에서 사용자가 가장 먼저 조사할 구체적 tool이 주요 실패 목록에서 밀려난다. tool 자체가 모든 출력에서 사라지는 것은 아니며 패턴 목록에는 남을 수 있다.

**수정 방향:** 실행의 포함 관계와 명시적 데이터·재시도 인과관계에 동일한 오류 전파 규칙을 적용하지 않는다. `SPAN_PARENT_FALLBACK`만으로 오류 전파를 확정하지 말고 실패 후보와 불확실성을 보존한다. 모든 edge 방향을 일괄 반전하면 명시적 인과 DAG를 깨뜨리므로 피한다.

**수용 기준:** 중첩 agent/tool 오류, 부모만 오류, 독립적인 두 오류, 명시적 causal-link 전파를 구분하는 테스트를 둔다. 기존 합성 테스트의 `input → tool → agent` 구조뿐 아니라 실제 포함 구조 `agent → tool`도 검증한다. 재현: `nested_span_failure`.

해석 근거: OpenTelemetry는 span의 parent와 최종 status를 각각 제공한다. 이 두 필드만으로 오류 전파 방향을 확정한다는 규칙은 아니다. [OpenTelemetry Tracing API](https://opentelemetry.io/docs/specs/otel/trace/api/)

### F-04. 명시적 OK보다 exception 이벤트를 우선한다

**근거:** [otlp_spans.py](../../src/tracegraph/adapters/otlp_spans.py#L181) 181–189행.

**조건과 실제 동작:** `status.code=1`인 tool span에 처리된 과거 exception 이벤트가 남아 있으면 `_status()`가 ERROR를 반환한다. 재현 결과 `step_status=error`, `error_count=1`이다.

**기대와 영향:** OpenTelemetry의 명시적 OK는 최종 성공 상태이며 분석 도구가 관련 오류를 억제하도록 사양에서 설명한다. 기록된 exception과 최종 실패를 구분하지 않으면 복구된 호출을 실패 패턴으로 집계한다. qualified tool·run ID가 있는 경우 검토 후보의 입력 조건에도 영향을 준다. 실제 외부 board import까지 실행한 것은 아니다. [OpenTelemetry Set Status](https://opentelemetry.io/docs/specs/otel/trace/api/#set-status)

**수정 방향:** 지원하는 숫자·문자열 OK 표현을 먼저 처리한다. ERROR, UNSET, 누락 상태에서 이벤트를 어떻게 참고할지는 명시적으로 고정하고, unknown status를 임의의 OK로 취급하는 현재 fallback도 함께 검토한다.

**수용 기준:** OK+exception은 성공, ERROR는 실패로 유지된다. UNSET+exception 처리 정책을 테스트하고, Phoenix를 통한 공유 builder 경로도 확인한다. 재현: `explicit_ok_exception`.

### F-05. 반복문 기반 AHU 구현도 깊은 tuple 비교에서 재귀 오류가 난다

**근거:** [ahu.py](../../src/tracegraph/analysis/ahu.py#L73) 73–74행의 중첩 tuple, 143행의 동등 비교, 152–155행의 subtree hashing. [test_ahu.py](../../tests/test_ahu.py#L69) 기존 깊이 테스트는 3,000단계다.

**조건과 실제 동작:** 이름이 `node`인 선형 트리 두 개에서 마지막 노드 이름만 변경했다. 8,000단계에서 `diff()`는 143행의 tuple 비교 중 `RecursionError`로 종료한다. 순회 함수가 반복문이어도 Python tuple 비교 내부의 재귀는 남아 있다.

| 노드 수 | diff 3회 중앙값, 초 | 결과 |
| --- | --- | --- |
| 1,000 | 0.045488 | 정상 차이 판정 |
| 2,000 | 0.192358 | 정상 차이 판정 |
| 4,000 | 0.661320 | 정상 차이 판정 |
| 8,000 | 측정 중단 | RecursionError |

이는 해당 Mac·Python 환경의 측정이며 보편적 절대 성능값이나 모든 Python 버전의 실패 임계값은 아니다. 반복 subtree 비교·hashing이 깊이에 비례해 다시 발생하므로, 현 구현을 단순히 선형 시간이라고 설명할 근거도 부족하다.

**수정 방향:** 양쪽 트리가 공유하는 intern table에서 `(label, child canonical IDs)`를 정수 ID로 정규화해 깊은 중첩 tuple의 직접 비교·hashing을 제거한다. 순회뿐 아니라 divergence 정렬·짝짓기·출력 경로의 반복 비용도 확인한다. 공개 `canonical()` 반환 형태는 별도 호환성 검토 없이 바꾸지 않는다.

**수용 기준:** 독립적으로 생성한 8,000단계 이상의 동일·상이한 트리 비교, 여러 깊은 root의 정렬, 추가·삭제 subtree 렌더링을 검증한다. 크기별 scaling guard를 추가하되 환경 부하와 correctness를 구분한다. 재현: `deep_diff_performance`.

### F-06. 문자열 논리 키 충돌로 상태 변화가 누락된다

**근거:** [diagnose.py](../../src/tracegraph/analysis/diagnose.py#L336) 336–338행.

**조건과 실제 동작:** 이름 `a`인 root의 자식 `b`와, 이름이 `a#0/CHAIN:b`인 별도 root가 같은 키 `CHAIN:a#0/CHAIN:b#0`를 만든다. 3개 step이 2개 key로 줄어든다. 별도 root만 OK→ERROR로 바꾸면 `error_count=1`인데 `behavior_changes=[]`, `topology_identical=true`가 된다.

**기대와 영향:** 이름에 허용된 `/`, `:`, `#`가 내부 path 구분자와 충돌해서는 안 된다. 비교 결과의 상태 변경 목록을 소비하는 사용자는 실제 회귀를 놓친다. 전체 보고서의 error count까지 숨겨지는 것은 아니다.

**수정 방향:** 내부 키를 `(kind, name, occurrence)`의 tuple path나 충돌 없는 canonical 인코딩으로 만든다. 표시 문자열은 식별 키와 분리한다. `None`과 실제 이름 `-`를 같은 표시로 바꾸는 경계도 함께 검증한다.

**수용 기준:** 모든 step이 하나의 서로 다른 내부 키를 가지며 구분자·빈 이름·동명 sibling에서도 상태 변화가 누락되지 않는다. 재현: `logical_key_collision`.

### F-07. SQLite 읽기 경로가 입력 DB를 변경한다

**근거:** [cli.py](../../src/tracegraph/cli.py#L717) 717–718행. 설치된 `langgraph-checkpoint-sqlite==3.1.0`의 `sqlite/__init__.py` 121행은 일반 writable connect를 사용하고, 129–163행의 `setup()`은 WAL 설정과 schema 생성을 실행한다. `.list()`에서 사용하는 cursor가 이 초기화를 호출한다.

**조건과 실제 동작:** `unrelated` 테이블만 있는 임시 DB에 `ingest --sqlite source.db --thread missing`을 실행했다. thread가 없어 exit 1로 실패했지만, 원본에 `checkpoints`, `writes` 테이블이 추가되고 journal mode가 `wal`로 바뀌었다.

**기대와 영향:** 사후 분석 입력을 읽다가 잘못 지정한 DB나 보존용 snapshot을 변경해서는 안 된다. 명령 실패 후에도 원본 변화가 남는다. 실제 운영 checkpoint DB 손상이나 데이터 삭제를 입증한 것은 아니다.

**수정 방향:** SQLite 입력의 존재·schema를 먼저 확인하고, 원본에 setup/migration을 수행하지 않는 읽기 경계를 둔다. saver를 계속 사용할 경우 원본의 일관된 임시 snapshot에서 작업하거나 read-only 연결과 setup 분리 방식을 검증한다. 활성 WAL DB의 단순 파일 복사만으로 snapshot 일관성을 보장하지 않는다.

**수용 기준:** 없는 경로, 다른 schema, 정상 snapshot, 읽기 전용 파일에서 원본 byte/schema/journal 변경이 없다. 실패는 읽기 관련 진단으로 종료한다. 재현: `sqlite_source_mutation`.

### F-08. 중첩 입력 검증과 JSON 예외 처리가 경로마다 다르다

**근거:** [otlp_spans.py](../../src/tracegraph/adapters/otlp_spans.py#L128) 128–136행과 181–184행, [phoenix_export.py](../../src/tracegraph/adapters/phoenix_export.py#L183) 183–186행, [cli.py](../../src/tracegraph/cli.py#L183) 183–207행. artifact 전용 loader는 `RecursionError`를 변환하지만 분석 입력의 직접 `json.loads()`에는 같은 처리가 없다.

| 입력 | 실제 결과 |
| --- | --- |
| OTLP `attributes=[1]` | AttributeError, exit 1 |
| OTLP `status="ERROR"` | AttributeError, exit 1 |
| Phoenix `context="bad"` | AttributeError, exit 1 |
| Phoenix 입력 위치에 20,000단계 중첩 JSON | RecursionError, exit 1 |

**기대와 영향:** 잘못된 외부 입력은 안정적인 입력 오류로 거부해야 한다. 현재는 내부 예외 경로로 탈출하고 정상 CLI 사용 오류 안내가 나오지 않는다. CliRunner가 포착한 예외 종류로 확인했으며, 이 재현으로 임의 코드 실행을 주장하지 않는다.

**수정 방향:** 각 외부 경계에서 object/array/scalar 타입을 좁히고 JSON 깊이 오류를 통일된 입력 오류로 변환한다. 정상 입력에서 발생한 프로그래밍 오류까지 전역 `except Exception`으로 숨기지 않는다. status·events·attributes·context와 숫자 변환의 경계를 함께 점검한다.

**수용 기준:** 잘못된 각 입력은 body-free 진단과 일관된 사용 오류 종료 코드를 반환하고, traceback·부분 출력 파일을 남기지 않는다. 재현: `otlp_bad_attribute`, `otlp_bad_status`, `phoenix_bad_context`, `phoenix_deep_json`.

### F-09. InMemoryStore는 public read를 통해 내부 상태가 바뀐다

**근거:** [in_memory.py](../../src/tracegraph/store/in_memory.py#L35) 35–37행, 54–62행, 80행, 90–93행. Ladybug의 [trace()](../../src/tracegraph/store/ladybug.py#L281)와 ancestors는 반환 객체를 복사한다.

**조건과 실제 동작:** `store.trace().steps[0].name="MUTATED"` 이후 InMemoryStore의 다음 조회와 최초 입력 `nt`가 모두 변경됐다. 같은 조작에서 LadybugStore는 두 값 모두 바뀌지 않았다.

**기대와 영향:** 읽기 결과에 표시용 값을 붙이는 호출자가 기준 artifact 객체와 이후 검색 결과를 의도치 않게 변경할 수 있다. edge 객체를 직접 변경하면 미리 구성한 adjacency와 반환 trace 사이의 불일치까지 가능하다. 후자는 코드 구조상 영향이며 이번 실행은 이름 변경으로 증명했다. 디스크 파일이 자동으로 변경되는 것은 아니다.

**수정 방향:** 생성 입력과 조회 반환값의 소유권을 양쪽 backend에 동일하게 적용한다. deep copy 또는 불변 모델을 선택하고 비용을 측정한다. `upsert`의 교체·중복 의미도 별도로 명시한다. 현재 InMemory node upsert는 교체, Ladybug node upsert는 CREATE이므로 이름만으로 공통 갱신 동작을 기대하기 어렵다.

**수용 기준:** 입력 객체, `trace()`, `ancestors()`의 반환값 변경이 저장소와 다른 caller에게 전파되지 않는 공통 계약 테스트를 둔다. CLI 일회성 사용에는 드러나지 않더라도 라이브러리 API 검증에 포함한다. 재현: `store_mutation`.

### F-10. 공식 retry 계약과 완료 상태 설명이 서로 맞지 않는다

**근거:** [README](../../README.md#L153) 153–168행, 192–195행, 214행; [ecosystem integration plan](../ecosystem-integration-plan.md#L3) 3–4행과 156–158행; [현재 presets](../../src/tracegraph/analysis/patterns.py#L570).

공식 `tool-retry-failure@v2`는 tool → 명시적 `retry:` CHAIN → 같은 tool의 세 step 엄격한 인접 관계다. README 후반은 같은 대표 패턴을 기본 unbounded gap으로 설명하고, 예시는 두 tool만 보여 준다. `near` 설명도 v2 공식 계약과 과거 heuristic의 차이를 충분히 드러내지 않는다.

또한 문서에는 명시적 retry causality와 Phoenix streaming이 후속 작업이라고 남아 있지만, 현재 E2E script와 동일 커밋의 성공한 CI는 통제된 producer 경로에서 이를 검증한다. 일반 사용자·모든 producer의 지원 완료를 뜻하는 것은 아니므로, 단순히 모든 상태를 완료로 바꾸는 것도 부정확하다.

**수정 방향과 수용 기준:** README의 예시·preset 설명을 현재 v2 출력으로 갱신한다. heuristic은 query-only, 명시적 retry는 producer 계약이라는 구분을 유지하고, fixture 검증·통제된 real-server E2E·일반 운영 지원을 별도 상태로 적는다. 현재 CLI와 실행 증거로 문서 예시를 다시 확인한다. 검증 방식은 정적 대조이며 별도 실행 결함 수에는 넣지 않았다.

## 4. 설계상 한계와 검증 공백

아래는 위 결함 수와 별도로 관리해야 한다. 일부는 현재 테스트가 명시적으로 고정한 정책이다.

### L-01. LangGraph 기본 예외는 error state 채널과 다르다

[오류 감지](../../src/tracegraph/adapters/langgraph_checkpoint.py#L432)는 사용자 state의 지정된 `error_channel`이 처음 truthy가 되는지만 본다. 실제 LangGraph 노드에서 `RuntimeError`를 발생시킨 재현은 checkpoint `pending_writes`에 `__error__` 1개를 남겼지만, 정규화 결과는 `status=ok`, `error_count=0`이었다.

이는 소스에 문서화된 채널 기반 감지 범위의 한계다. 현재 예제는 예외를 throw하지 않고 `error` state 값을 반환하므로 기본 테스트가 통과한다. 일반적인 node exception까지 감지한다고 설명해서는 안 된다. pending error·중단·부분 실행을 별도 증거로 읽거나, 지원하지 않는 경우 성공 단정 대신 감지 범위를 경고하는 개선이 필요하다. 공식 persistence 문서도 checkpoint와 pending writes를 별도 개념으로 설명한다. [LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

재현: `langgraph_native_error`. 현재 버전의 실제 LangGraph와 in-memory SqliteSaver를 사용했으며 합성 checkpoint만으로 추정한 결과가 아니다.

### L-02. 누락·외부 span link를 버려도 fidelity 표시는 낮아지지 않는다

[OTLP link 처리](../../src/tracegraph/adapters/otlp_spans.py#L581)와 [기존 테스트](../../tests/test_otlp_adapter.py#L313)는 다른 trace 또는 존재하지 않는 span으로의 link를 버리도록 명시한다. 같은 trace의 `missing` span을 가리키는 link를 넣어도 `raw_edges=0`, `declared_dag`, `links_preserved=true`, `warnings=[]`다.

따라서 이를 기존 matcher 동작의 회귀로 분류하지 않았다. 다만 `links_preserved`는 실제로는 해석 가능한 trace 내부 link에 국한된다. 누락 개수·사유·외부 reference를 별도 provenance로 남기고, 부분 인과 그래프임을 표시하는 것이 적절하다. OpenTelemetry 자체는 동일 trace와 다른 trace 사이 link를 모두 허용한다. [OpenTelemetry Links](https://opentelemetry.io/docs/specs/otel/trace/api/#link)

### L-03. 파생 트리 동일성이 원본 DAG 동일성으로 읽힐 수 있다

`diff`가 TREE_PARENT만 비교하는 것은 의도된 계약이다. 그러나 CLI의 `IDENTICAL` 메시지와 분석 보고서는 projection loss를 항상 함께 보여 주지는 않는다.

별도 합성 재현에서 `b→a`, `c→a`인 원본에 `c→b`를 추가했다. c의 primary tree parent는 여전히 a여서 `diff`는 exit 0과 `IDENTICAL`을 출력했고, c는 `projection_lossy=true`였다. `analyze(..., baseline=...)`의 warnings도 비어 있었다. **diff의 boolean 자체는 설계대로 정확하다.** 비교 대상이 파생 트리라는 점과 양쪽의 손실 수를 결과에 표시해 해석 오류를 줄여야 한다.

### L-04. 지표 총합과 자원 한계는 제한된 의미를 가진다

[지표 합산](../../src/tracegraph/analysis/diagnose.py#L246)은 존재하는 값을 모두 더한다. 전부 누락이면 unavailable이고 혼합 통화는 합치지 않는 점은 좋다. 다만 부분 누락과 complete total을 구분하는 필드가 없고, 상위 span의 누적치와 하위 span의 개별치가 중복인지 판별할 provenance도 없다. 이런 producer가 실제 배포에 존재한다고 단정하지 않으며, 해당 계측 계약과 coverage를 확인하기 전까지 전체 실행 비용으로 과해석하면 안 된다.

[stdin 읽기](../../src/tracegraph/cli.py#L177)와 [px capture](../../src/tracegraph/cli.py#L210)는 파일 입력의 byte cap을 적용하지 않는다. `query --limit`은 전체 결과 생성 이후 표시만 자른다. 대규모 batch의 총 메모리·조합 수를 제한하는 기능은 아니며 이번 검토에서 메모리 고갈 부하 실험은 하지 않았다. 후속으로 batch budget, streaming, 결과 수 제한과 truncation provenance를 설계할 수 있다.

### L-05. JSON 계약과 E2E 성공은 세부 의미 정확성을 모두 검증하지 않는다

[analysis schema](../../contracts/analysis-report.schema.json)의 `comparison`은 object/null만 검사하며 내부 필수 필드와 타입을 고정하지 않는다. Pydantic 모델과 저장소-vendored schema를 함께 유지하므로 차이를 감지할 계약 테스트가 필요하다. 후보 schema의 additive field 허용은 명시된 호환성 정책이므로 그 자체를 결함으로 보지 않았다.

[E2E script](../../scripts/verify_phoenix_syncmill_e2e.py#L190) 190–220행은 자동·baseline 진단의 stdout에서 문구를 확인하지만 생성된 두 analysis JSON의 primary failure, 선택 trace ID, metric delta를 직접 검증하지 않는다. 후보 JSON과 반복 import는 별도로 확인한다. 따라서 E2E 성공과 F-03 같은 세부 분류 문제는 동시에 존재할 수 있다.

지원 Python 표기는 `>=3.12`지만 현재 일반 CI는 Python 3.12·Linux 중심이다. 이번 로컬 Mac 증거를 Windows·새 Python 버전의 지원 증거로 확장하지 않았다. 실제 Collector 설정 기동, 취약점 전수 감사, 다른 저장소 구현 전수 리뷰와 production acceptance는 미실행이다.

## 5. 관점별 평가와 유지할 설계

| 관점 | 유지할 부분 | 보완할 부분 |
| --- | --- | --- |
| 모델·정규화 | Raw/Normalized 구분, 원본·파생 edge 분리, 원본 불변 투영, canonical 재검증 | 데이터 흐름·포함 관계의 의미 구분, fidelity의 범위 명시 |
| 입력 adapter | 선언된 부모 보존, dangling parent 거부, clock skew에서 topological 정렬, trace 혼합 거부 | F-01/02/04/07/08, 기본 LangGraph 예외 감지 한계 |
| RCA·비교 | raw DAG 조회와 파생 tree 비교 분리, 구조 변경과 상태 변경 분리 | F-03/05/06, 손실 경고·비교 계약 |
| 패턴·governance | 명시적 retry v2, heuristic의 review 제외, versioned preset, exact-byte digest, 결정적 정렬·중복 제거 | 잘못된 upstream status가 후보로 전파되지 않도록 F-04 해결 |
| 스토어 | Ladybug의 선택적 설치, parameterized Cypher, 30-hop 초과 fallback, backend parity | F-09, 공개 mutation/소유권 계약 통일 |
| 자원·파일 | Ladybug constructor 실패 정리, idempotent close, 일괄 쓰기 rollback, atomic output | F-01/07, 다중 출력 경로 충돌·전체 batch budget의 추가 검토 |
| CLI·사용성 | 명시 파일은 strict, directory discovery는 경고 후 skip, suffix보다 exact ID 우선, px 오류 본문 비노출 | F-08, 일부 구형 render 경로의 Rich escape 일관성 검토, F-10 |
| 패키징·CI | locked dependency 설치, base/extra 분리, third-party action SHA pin, 별도 성능·real-server E2E | 넓은 Python 지원 범위 검증, wheel 설치 smoke, JSON 의미 assertion |

특히 `export-review-candidates`는 입력 artifact를 한 번 읽어 그 바이트의 digest와 분석 대상을 함께 고정한다. source JSON을 재직렬화한 값과 실제 파일 digest를 혼동하지 않는다는 점이 좋다. 후보가 인간 검토용이라는 경계와 Toolgraph 정책을 직접 바꾸지 않는 구조도 유지해야 한다.

보안 검토는 확인한 입력·출력 경계에 한정된다. 이 보고서의 문제 목록이 없어진다고 패키지 전체의 보안 보증이나 실제 운영 허가가 성립하는 것은 아니다.

## 6. 수정 순서와 후속 수용 테스트

| 순서 | 묶음 | 완료 증거 |
| --- | --- | --- |
| 1 | F-01, F-02 | 외부 ID가 출력 루트를 벗어나지 않음; 모든 지원 입력 경로의 safe-v1 canary 통과 |
| 2 | F-03, F-04, L-01 | 중첩 실패·복구된 exception·native LangGraph 실패를 서로 구분; 관련 패턴과 후보 회귀 테스트 통과 |
| 3 | F-05, F-06 | 깊은 트리 동일/상이 비교 성공; 이름·경로 충돌에도 상태 변경 보존 |
| 4 | F-07, F-08, F-09 | 입력 DB 불변, malformed 입력의 안정적 거부, 양쪽 backend의 read isolation |
| 5 | F-10, L-02~05 | 현재 계약에 맞는 문서, fidelity와 metric completeness 명시, JSON 중심 E2E assertion |

수정은 이 보고서의 범위에 포함되지 않았다. 후속 변경에서는 발견별 재현을 회귀 테스트로 옮기고, 관련 테스트를 먼저 통과시킨 뒤 core/extra CI를 확인한다. 성능은 기능 성공 여부와 별도로 기록하고, real-server E2E가 필요한 변경만 기존 운영 검증 경로에서 다시 확인한다.

## 7. 재현 코드와 관찰값

다음 코드는 저장소 루트의 현재 `.venv`에서 실행하는 독립 재현이다. `.venv`에 dev 의존성과 선택적 LadybugDB가 있는 환경을 사용했다. 로컬 파일 쓰기는 `TemporaryDirectory` 안에만 수행하고 실제 원본 DB·trace·설정을 읽거나 바꾸지 않는다. 임시 파일을 만드는 두 CLI 재현도 테스트가 만든 데이터만 대상으로 한다.

아래 Python block을 임시 파일에 저장하여 `.venv/bin/python <임시파일>`로 실행한다. 정상 구현의 정답 assertion을 나열한 테스트가 아니라 **기준 커밋에서 관찰한 결함을 드러내는 probe**다. 수정 후 출력이 달라지는 것이 의도된 결과다. 내부 `_logical_keys` 호출은 키 손실을 직접 확인하기 위한 검토용 계측이며 제품 API 변경 제안이 아니다.

핵심 관찰값:

```text
path_traversal: exit=0, outside_work_overwritten=true
privacy_profile: profile=safe-v1, body_in_report=true
nested_span_failure: primary=[agent], propagated=[tool]
explicit_ok_exception: step_status=error, error_count=1
logical_key_collision: steps=3, keys=2, behavior_changes=[]
deep_diff_performance: n=8000 -> RecursionError
sqlite_source_mutation: [unrelated, checkpoints, writes], journal_mode=wal
malformed inputs: AttributeError 또는 RecursionError
store_mutation: InMemoryStore changed=true; LadybugStore changed=false
langgraph_native_error: pending_errors=1, status=ok, error_count=0
dangling_local_link: raw_edges=0, links_preserved=true, warnings=[]
```

```python
import json
import os
import tempfile
import time
from pathlib import Path
from statistics import median

from typer.testing import CliRunner
from tracegraph import artifact
from tracegraph.cli import app
from tracegraph.adapters import OTLPSpanAdapter, PhoenixExportAdapter, LangGraphCheckpointAdapter
from tracegraph.analysis.diagnose import analyze, dumps, _logical_keys
from tracegraph.analysis.ahu import diff
from tracegraph.model import Edge, EdgeType, RawTrace, Step, StepKind, StepStatus, Trace
from tracegraph.normalize import normalize
from tracegraph.store import InMemoryStore
from tracegraph.store.ladybug import LadybugStore

os.environ['TERM'] = 'dumb'
runner = CliRunner()

def emit(case, **values):
    print(json.dumps({'case': case, **values}, ensure_ascii=False))

def doc(*spans):
    return {'resourceSpans': [{'scopeSpans': [{'spans': list(spans)}]}]}

def span(sid='s', tid='t', **kwargs):
    return {'traceId': tid, 'spanId': sid, 'name': 'operation', **kwargs}

def nt_from(*spans):
    return normalize(OTLPSpanAdapter(doc(*spans)).ingest(spans[0]['traceId']))

def chain(n, final='node'):
    return normalize(RawTrace(trace=Trace(trace_id='t', source_kind='review'),
        steps=[Step(step_id=str(i), trace_id='t', seq=i, name=final if i == n-1 else 'node') for i in range(n)],
        causal_edges=[Edge(type=EdgeType.CAUSED_BY, src=str(i), dst=str(i-1)) for i in range(1,n)]))

with tempfile.TemporaryDirectory(prefix='tracegraph-review-') as tmp:
    base = Path(tmp)
    work = base / 'work'
    work.mkdir()
    victim = base / 'victim.json'
    victim.write_text('REVIEW_SENTINEL')
    source = work / 'input.json'
    source.write_text(json.dumps(doc(span(tid='../victim'))))
    original_cwd = os.getcwd()
    try:
        os.chdir(work)
        result = runner.invoke(app, ['ingest-otlp', '--file', str(source)])
    finally:
        os.chdir(original_cwd)
    emit('path_traversal', exit=result.exit_code, outside_work_overwritten=victim.read_text() != 'REVIEW_SENTINEL', trace_id=json.loads(victim.read_text())['trace']['trace']['trace_id'])

    nt = nt_from(span(name='Prompt: REVIEW_PRIVATE_ACCOUNT_BODY', status={'code':2, 'message':'REVIEW_PRIVATE_ERROR'}))
    path = base / 'raw-normalized.json'
    artifact.save_atomic(nt, path)
    output = base / 'analysis.json'
    result = runner.invoke(app, ['analyze', str(path), '--json-out', str(output)])
    payload = json.loads(output.read_text())
    emit('privacy_profile', exit=result.exit_code, profile=payload['privacy_profile'], body_in_report='REVIEW_PRIVATE_ACCOUNT_BODY' in output.read_text(), error_in_report='REVIEW_PRIVATE_ERROR' in output.read_text(), raw_error_in_artifact='REVIEW_PRIVATE_ERROR' in path.read_text())

    phoenix = {'traceId':'t', 'spans':[
        {'context':{'span_id':'agent', 'trace_id':'t'}, 'name':'agent', 'span_kind':'AGENT', 'status_code':'ERROR', 'start_time':1000000000, 'end_time':4000000000},
        {'context':{'span_id':'tool', 'trace_id':'t'}, 'parent_id':'agent', 'name':'search', 'span_kind':'TOOL', 'status_code':'ERROR', 'start_time':2000000000, 'end_time':3000000000}]}
    report = analyze(normalize(PhoenixExportAdapter(phoenix).ingest('t')))
    emit('nested_span_failure', primary=[f.step.step_id for f in report.primary_failures], propagated=[f.step_id for f in report.propagated_failures], primary_cause_edges=[len(f.causal_edges) for f in report.primary_failures])

    nt = nt_from(span(links=[{'traceId':'t','spanId':'missing'}]))
    emit('dangling_local_link', raw_edges=len(nt.edges_of(EdgeType.CAUSED_BY)), fidelity=nt.trace.causal_fidelity.value, links_preserved=nt.trace.links_preserved, warnings=analyze(nt).warnings)

    steps=[Step(step_id='root',trace_id='t',seq=0,name='a'), Step(step_id='other',trace_id='t',seq=1,name='a#0/CHAIN:b'),Step(step_id='child',trace_id='t',seq=2,name='b')]
    before=normalize(RawTrace(trace=Trace(trace_id='t',source_kind='review'), steps=steps,causal_edges=[Edge(type=EdgeType.CAUSED_BY,src='child',dst='root')]))
    after=normalize(RawTrace(trace=before.trace,steps=[s.model_copy(update={'status':StepStatus.ERROR}) if s.step_id=='other' else s.model_copy() for s in before.steps], causal_edges=before.edges_of(EdgeType.CAUSED_BY)))
    report=analyze(after,baseline=before)
    emit('logical_key_collision', steps=len(after.steps), keys=len(_logical_keys(after)), behavior_changes=[c.model_dump() for c in report.comparison.behavior_changes], topology_identical=report.comparison.topology_identical, errors=report.error_count)

    bad_inputs={
        'otlp_bad_attribute':('ingest-otlp',json.dumps(doc(span(attributes=[1])))),
        'otlp_bad_status':('ingest-otlp',json.dumps(doc(span(status='ERROR')))),
        'phoenix_bad_context':('ingest-phoenix',json.dumps({'traceId':'t','spans':[{'context':'bad'}]})),
        'phoenix_deep_json':('ingest-phoenix','['*20000+'0'+']'*20000),
    }
    for name,(command,text) in bad_inputs.items():
        file=base/f'{name}.json'; file.write_text(text)
        result=runner.invoke(app,[command,'--file',str(file),'--out',str(base/'invalid-output.json')])
        emit(name, exit=result.exit_code, exception=type(result.exception).__name__ if result.exception else None, clean_diagnostic='Invalid value' in result.output)

    for cls in (InMemoryStore,LadybugStore):
        nt=chain(2)
        store=cls.from_trace(nt)
        try:
            store.trace().steps[0].name='MUTATED'
            emit('store_mutation',backend=cls.__name__, changed=store.trace().steps[0].name=='MUTATED', original_changed=nt.steps[0].name=='MUTATED')
        finally:
            if hasattr(store,'close'): store.close()

    for n in (1000,2000,4000,8000):
        a,b=chain(n),chain(n,'changed')
        times=[]
        try:
            for _ in range(3):
                start=time.perf_counter(); result=diff(a,b); times.append(time.perf_counter()-start)
            emit('deep_diff_performance',n=n,median_seconds=round(median(times),6),identical=result.identical)
        except RecursionError as exc:
            emit('deep_diff_performance',n=n,error=type(exc).__name__)

    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import START,END,StateGraph
    from typing import TypedDict
    class State(TypedDict):
        value: str
    def boom(state):
        raise RuntimeError('REVIEW_SYNTHETIC_NODE_FAILURE')
    graph=StateGraph(State); graph.add_node('call_tool',boom); graph.add_edge(START,'call_tool'); graph.add_edge('call_tool',END)
    with SqliteSaver.from_conn_string(':memory:') as saver:
        try: graph.compile(checkpointer=saver).invoke({'value':'test'},{'configurable':{'thread_id':'failed'}})
        except RuntimeError: pass
        tuples=list(saver.list({'configurable':{'thread_id':'failed'}}))
        native_errors=sum(w[1]=='__error__' for t in tuples for w in (t.pending_writes or []))
        nt=normalize(LangGraphCheckpointAdapter(saver).ingest('failed'))
        emit('langgraph_native_error',pending_errors=native_errors,status=nt.trace.status.value,error_count=analyze(nt).error_count)

# Additional ingress and status checks; all filesystem writes remain temporary.
import sqlite3
s=span(name='server::tool',status={'code':1},events=[{'name':'exception','attributes':[{'key':'exception.message','value':{'stringValue':'recovered error'}}]}],attributes={'openinference.span.kind':'TOOL','syncmill.run_id':'run'})
nt=nt_from(s)
emit('explicit_ok_exception',step_status=nt.steps[0].status.value,error_count=analyze(nt).error_count)
with tempfile.TemporaryDirectory(prefix='tracegraph-review-sqlite-') as tmp:
    p=Path(tmp)/'source.db'
    with sqlite3.connect(p) as c:
        c.execute('CREATE TABLE unrelated (value TEXT)')
    result=runner.invoke(app,['ingest','--sqlite',str(p),'--thread','missing','--out',str(Path(tmp)/'out.json')])
    with sqlite3.connect(p) as c:
        emit('sqlite_source_mutation',exit=result.exit_code,tables=[row[0] for row in c.execute('SELECT name FROM sqlite_master WHERE type="table"')],journal_mode=c.execute('PRAGMA journal_mode').fetchone()[0])
```

## 11. 개선 구현 결과 — 2026-09-07

이 절 이전의 발견·재현·라인 번호는 위 기준 커밋의 **원검토 기록**이다. 이후 사용자 승인 계획에 따라 아래 수정을 작업 트리에 구현했다. 과거 재현 코드는 수정 전 동작의 증거이며, 현재의 통과 기준은 회귀 테스트다. 커밋·push·배포는 수행하지 않았다.

### 항목별 해결 상태

| 항목 | 상태 | 구현 및 검증 근거 |
| --- | --- | --- |
| F-01 | 해결 | [artifact.py](../../src/tracegraph/artifact.py)의 공통 기본 파일명은 짧은 portable ID만 그대로 쓰고 나머지는 SHA-256 파일명으로 만든다. 기본 출력은 완성된 임시 파일을 hard link로 원자적으로 생성하여 기존 파일·symlink·동시 작성 충돌 시 덮어쓰지 않는다. 명시적 `--out`은 기존 atomic replace를 유지한다. [cli.py](../../src/tracegraph/cli.py)는 입력/출력 및 두 출력 간 실제 파일 alias를 거부한다. |
| F-02 | 해결 | [diagnose.py](../../src/tracegraph/analysis/diagnose.py)는 step/pattern/evaluation/source/currency/비교 표시값을 identifier 정책으로 정제하고 기타 텍스트를 결정적 alias로 바꾼다. 원시 이름으로 비교한 뒤 표시만 정제한다. OTLP·Phoenix·실제 LangGraph 실행 및 artifact/baseline canary로 본문 제거와 원본 digest 보존을 검증했다. `safe-v1`은 구조적 ID를 유지하며 익명화를 뜻하지 않는다. |
| F-03 | 해결 | containment/legacy/unknown 경로는 오류 후보를 숨기지 않는다. 모호한 부모·자식 오류를 모두 보존하고 깊은 후보를 먼저 표시한다. checkpoint/graph-parent/span-link로 이어진 명시적 오류 ancestry만 context 분류에 사용하며 예외 전파의 증명으로 표현하지 않는다. 기존 진단 fixture도 실제 의도에 맞는 명시적 origin으로 수정했다. |
| F-04 | 해결 | [OTLP adapter](../../src/tracegraph/adapters/otlp_spans.py)는 명시적 OK를 exception event보다 우선한다. ERROR는 유지하고 UNSET/누락만 exception으로 추론한다. 지원하지 않는 status code는 입력 오류로 처리한다. 기존 artifact의 status를 소급 변경하지 않는다. |
| F-05 | 해결 | [ahu.py](../../src/tracegraph/analysis/ahu.py)의 diff/isomorphism은 양쪽 트리가 공유하는 정수 canonical ID를 사용한다. 출력 path는 연결 구조로 보관하고 필요할 때만 문자열로 만든다. 독립적으로 만든 8천·1만6천 단계의 동일/변경 트리와 큰 unmatched subtree를 검증했다. `canonical()`의 nested tuple 반환 형식은 유지하며 외부 Python tuple equality의 깊이 제한은 문서화했다. |
| F-06 | 해결 | 논리 step identity는 `(parent ID, kind, raw name, occurrence)`를 공유 interning한다. 공개 표시 key는 타입·literal/redacted 구분을 포함한 JSON 경로다. delimiter 충돌, None/빈 문자열/하이픈, 같은 이름 형제 및 literal alias 충돌을 검증했다. |
| F-07 | 해결 | [sqlite_snapshot.py](../../src/tracegraph/sqlite_snapshot.py)는 `mode=ro` 연결에서 SQLite backup API로 private 임시 DB를 만든 뒤 SqliteSaver를 적용한다. 전체 backup deadline은 30초다. 정상 checkpoint, 없는 thread/잘못된 schema, 읽기 전용 파일, 활성 WAL의 committed checkpoint, deadline 초과에서 원본 데이터·schema·journal mode 보존을 검증했다. WAL reader의 shared-memory coordination까지 무변경이라고 주장하지 않는다. |
| F-08 | 해결 | [input_validation.py](../../src/tracegraph/input_validation.py)의 JSON 경계에서 과도한 깊이를 ValueError로 변환한다. 두 adapter의 attributes/status/events/context/links 관련 형태를 검사하고 잘못된 입력을 필드 경로와 기대 타입으로 설명한다. 광범위한 `except Exception`으로 버그를 숨기지 않는다. |
| F-09 | 해결 | [in_memory.py](../../src/tracegraph/store/in_memory.py)는 입력 Trace/Step/Edge 및 trace/ancestors 반환값을 deep copy한다. 중첩 evaluation과 edge, header 변경까지 원본·저장소가 격리됨을 두 backend에서 검증했다. 기존 backend별 upsert 의미는 바꾸지 않았다. |
| F-10 | 해결 | [README](../../README.md), [연계 계획](../ecosystem-integration-plan.md), near preset 설명을 strict retry v2와 query-only heuristic 계약에 맞췄다. fixture, 통제된 Phoenix·SyncMill E2E, 일반 운영 수용을 구분했다. |

추가로 [analysis report schema](../../contracts/analysis-report.schema.json)의 comparison 내부 필드와 타입을 명시했다. [E2E verifier](../../scripts/verify_phoenix_syncmill_e2e.py)는 stdout 외에 생성된 JSON의 선택 trace, retry 경로와 실패 후보, 정확한 baseline digest, behavior regression, 표시값 정제와 공개 schema를 검사한다. 잘못된 trace/baseline/body를 거부하는 verifier 자체의 로컬 테스트도 추가했다.

### 검증 결과

Python 3.12.11/macOS에서 잠금 파일로 만든 독립 환경을 사용했다. core 환경은 마지막에 최종 wheel 설치로 교체하여 전체 기능 테스트와 별도 CLI smoke를 실행했다.

| 검증 | 결과 |
| --- | --- |
| 독립 core 환경, 전체 `-m 'not perf'` | **391 passed, 3 skipped, 4 deselected**. 선택 Ladybug 의존성은 설치되지 않음. |
| 독립 Cypher 환경, 전체 `-m 'not perf'` | **432 passed, 4 deselected**. 로컬 HTTP 계약 테스트 포함. |
| core 성능 `-m perf` | **4 passed**. 기존 normalize/diagnose guard와 새 deep-diff 증가율 guard 통과. |
| [신규 리뷰 회귀 테스트](../../tests/test_review_regressions.py) + Phoenix adapter 테스트 | **60 passed**. 신규 회귀 사례는 56개. |
| [deep-diff 성능 guard](../../tests/test_perf_regressions.py) | 4천/8천/1만6천 단계, 각 3회 median. 두 배 크기의 허용치는 이전 시간의 3.5배 + 50ms. |
| wheel/sdist 빌드 | `uv build --out-dir /private/tmp/tracegraph-improvements-dist` 성공. |
| 설치된 wheel, 저장소 밖 실행 | ingest-otlp, validate, analyze+baseline, diff, console entrypoint 통과. 실행 코드가 임시 환경의 site-packages에 있으며 Ladybug 미설치 확인. |
| 변경 공백 검사 | `git diff --check` 통과. |
| 새 원격 CI / 실제 Phoenix·SyncMill 서버 E2E | **미실행**. 원검토의 기존 HEAD CI 성공은 이번 수정본의 원격 검증 증거가 아님. |

주요 재현 테스트 이름은 `test_default_filename_cannot_escape`, `test_atomic_create_race_and_symlink`, `test_sqlite_failed_ingestion_preserves_source`, `test_readonly_live_wal_checkpoint_snapshot`, `test_report_privacy_after_real_adapter_ingestion`, `test_containment_does_not_hide_child_failure`, `test_explicit_ok_beats_handled_exception`, `test_logical_key_delimiters_and_missing_names_do_not_collide`, `test_independent_deep_trees`, `test_store_owns_inputs_and_outputs`다.

### 유지한 계약과 남은 한계

- artifact v1/v2 읽기, artifact v2 쓰기, report v2, candidate v1, preset ID/version, exact-byte candidate digest를 유지했다. 새 OTLP ingest의 status 정정은 이후 후보 결과를 달라지게 할 수 있다.
- L-01: LangGraph native pending-write exception 지원은 이 리뷰 시점에는 추가하지 않았다. state-channel 관측 범위를 보고서 warning과 문서에 명시했다.
  **2026-09-12 해결**: pending write의 `__error__`를 읽어 실패 task마다 파생 step을 생성한다. 리뷰의 재현 사례는 이제 `status=error`, `error_count=1`을 보고하며 실패 노드 이름이 붙는다. 미완료 task 보고는 별도 후속 작업이다.
- L-02: link 보존은 유효한 in-trace link만 뜻한다. foreign/unresolved link 수집을 새로 구현하지 않았다.
- L-03: derived-tree 동일성은 raw DAG 동일성이 아니다. CLI와 보고서는 lossy step 수 및 baseline 한계를 표시한다.
- L-04: metric은 관측값 합계다. 부분 coverage·producer aggregation 및 stdin/subprocess/query materialization의 메모리 제약은 남아 있다.
- L-05: comparison schema와 E2E report assertions는 강화했지만 새 실제 서버 E2E, 다른 Python 버전·플랫폼, 일반 운영 수용은 검증하지 않았다.
