# tracegraph 입문자용 사용 가이드

이 문서는 tracegraph를 처음 써 보는 사용자가 로컬에서 샘플 trace를 만들고,
원인 분석과 실행 구조 비교까지 한 번에 따라 해 볼 수 있도록 정리한 가이드입니다.

tracegraph는 LangGraph checkpoint나 OpenInference/OTLP span export를 읽어서
"무엇이 무엇 때문에 실행됐는가"를 causal graph로 정리합니다. 핵심 사용 흐름은
다음 네 단계입니다.

1. trace를 만든다.
2. tracegraph artifact JSON으로 변환한다.
3. `analyze` 또는 `inspect`, `explain`, `diff`로 분석한다.
4. 여러 artifact에서 `query`로 패턴을 찾는다.

같은 흐름을 실제 출력과 해석, 실무 응용 예시와 함께 직접 실행해 보려면 저장소 checkout에서
실습 노트북 [`examples/getting_started_ko.ipynb`](../examples/getting_started_ko.ipynb)를 여세요.

## 설치

개발 환경에서는 저장소 루트에서 `uv`를 사용합니다.

```bash
uv sync
```

선택 사항인 LadybugDB/Cypher 백엔드까지 확인하려면 extra를 포함합니다.

```bash
uv sync --extra cypher
```

CLI가 보이는지 확인합니다.

```bash
uv run tracegraph --help
```

## 빠른 시작: 샘플 LangGraph trace 만들기

저장소에는 작은 LangGraph agent 예제가 들어 있습니다. 이 예제는 두 개의 thread를
만듭니다.

- `A`: tool 단계에서 에러가 나고 `handle_error` 경로를 탑니다.
- `B`: 정상 경로로 바로 `respond`까지 갑니다.

```bash
uv run python examples/tiny_agent.py
```

그러면 현재 디렉터리에 `trace.db`가 생깁니다. 이 SQLite checkpoint DB에서 각
thread를 tracegraph artifact로 변환합니다.

```bash
uv run tracegraph ingest --sqlite trace.db --thread A --out A.json
uv run tracegraph ingest --sqlite trace.db --thread B --out B.json
```

artifact가 올바른지 먼저 검사할 수 있습니다.

```bash
uv run tracegraph validate A.json B.json
```

step id를 찾지 않고 바로 실패를 진단하려면 다음 명령을 사용합니다.

```bash
uv run tracegraph analyze A.json
uv run tracegraph analyze A.json --baseline B.json --json-out analysis.json
```

`analyze`는 실제 실패 step, 그 앞의 인과관계, 반복 tool 패턴, 시간·token·cost·평가
요약을 한 번에 보여 줍니다. 정상/실패 차이가 발견되어도 분석 자체가 성공하면 exit
code는 0이며, CI 판정은 기존 `diff`와 `query`를 계속 사용합니다.

### 진단에서 증거 조사로 이동하기

실패 후보에는 전체 `step_id`가 표시됩니다. 같은 이름의 tool이 여러 번 실행돼도
이 ID로 구분할 수 있습니다. `Next investigation steps`에는 다음 안내가 나옵니다.

| 분석 입력 | 다음 단계 |
| --- | --- |
| artifact 파일 | 해당 파일과 전체 step ID를 넣은 `tracegraph explain -- ...` 명령 |
| Phoenix export 파일 | 선택 trace를 `ingest-phoenix`로 저장한 뒤 `explain`하는 템플릿 |
| stdin의 artifact | 원래 입력을 선택한 파일로 다시 보존한 뒤 `explain`하는 템플릿 |
| stdin의 Phoenix export | 원래 입력을 다시 공급하여 `ingest-phoenix --file=-`로 저장하는 템플릿 |
| `phoenix diagnose --save-artifact ...` | 저장 성공 후 실제 artifact 경로로 `explain`하는 명령 |
| artifact를 저장하지 않은 `phoenix diagnose` | 이번에 선택된 trace ID와 project를 유지해 저장하는 재조회 템플릿 |

`CHOOSE_NEW_ARTIFACT_PATH`는 존재하는 파일명이 아니라 사용자가 새 경로로 바꿔야 하는
자리표시자입니다. 안내만으로 파일을 저장하거나 명령을 실행하지 않습니다. 이미 소비된 stdin은
복구할 수 없으며, Phoenix 재조회 시 데이터가 바뀌었다면 다시 진단하여 step ID를 확인해야 합니다.
`/dev/stdin`, `/dev/fd/...`, `/proc/<pid>/fd/...`처럼 프로세스의 파일 디스크립터를 가리키는
경로(그리고 그런 경로로 향하는 심볼릭 링크)와 pipe도 재입력이 필요한 스트림으로 안내합니다.
디렉터리 디스크립터(`/proc/self/fd/N/`)를 가리키는 심볼릭 링크 아래의 파일은 감지하지 않습니다.
복사 가능한 명령은 POSIX 셸용으로 인자를 인용합니다. 제어문자가 있는 인자는 실행용 명령 대신
이스케이프된 argv 값으로 표시합니다. ID·경로·명령의 `:ok:` 같은 문자열은 emoji로 바꾸지 않고,
좁은 터미널에서도 복사할 값 중간에 줄바꿈을 삽입하지 않습니다.

후보가 생략되면 전체 후보를 보는 데 필요한 `--limit` 값을 안내합니다. JSON 보고서를 저장하지
않았으면 `--json-out` 사용법을, 저장에 성공했으면 실제 경로를 안내합니다. ID는 익명화되지 않으며
`explain`은 artifact의 원시 이름과 오류를 표시할 수 있으므로 공유 전에 확인하세요. 표시된 선행
관계만으로 예외 전파나 근본 원인이 확정되는 것은 아닙니다.
직접 관측한 실패, edge origin에 따른 인과·containment 구분과 누락 telemetry 표시는 기존 분석
출력을 유지합니다. 새 조사 안내가 인과관계를 추가 추론하거나 근본 원인을 확정하지는 않습니다.

## trace 구조 보기: inspect

`inspect`는 trace의 실행 흐름을 트리 형태로 보여 줍니다.

```bash
uv run tracegraph inspect A.json
```

입문 단계에서는 다음만 보면 됩니다.

- 빨간색 또는 `error`: 실패한 단계입니다.
- `lossy-projection`: 원인이 여러 개인 step을 트리로 접으면서 일부 원인이 생략됐다는 표시입니다.
- 마지막 요약 줄: step 수, tool 수, error 수, lossy projection 수입니다.

주의할 점은 `inspect`가 보기 편한 트리 화면이라는 것입니다. 실제 원인 분석은
아래의 `explain`처럼 raw causal graph를 사용합니다.

## 실패 원인 따라가기: explain

`explain`은 특정 step에서 시작해 그 step의 실제 원인들을 거꾸로 따라갑니다.

먼저 `inspect` 출력이나 JSON에서 실패 step id를 확인한 뒤 실행합니다.

```bash
uv run tracegraph explain A.json <step_id>
```

step id 전체가 길다면 유일한 suffix만 넣어도 됩니다.

```bash
uv run tracegraph explain A.json <step_id_suffix>
```

출력의 `←` 방향은 "이전 원인"을 뜻합니다. 예를 들어 `call_tool ← plan ← input`
처럼 보이면 `call_tool` 실패가 `plan`, `input`에서 이어진 실행 경로 위에 있다는
뜻입니다.

## 두 실행 비교하기: diff

`diff`는 두 artifact의 실행 구조가 같은지 비교합니다. 샘플에서는 `A`가 에러 처리
분기를 타고, `B`는 정상 경로를 타기 때문에 구조가 다릅니다.

```bash
uv run tracegraph diff A.json B.json
```

결과가 다르면 `NOT IDENTICAL`과 함께 어디서 갈라졌는지 보여 줍니다. 이 명령은
구조가 다를 때 exit code `1`로 종료하므로 CI에서 회귀 감지용으로 쓰기 좋습니다.

label은 무시하고 topology만 비교하려면 `--structure`를 붙입니다.

```bash
uv run tracegraph diff --structure A.json B.json
```

## 변경을 baseline과 검사하기: check

`check`는 지정한 현재 실행 하나를 지정한 baseline 하나와, 지정한 규칙으로 판정합니다.
기본 규칙은 없고 baseline도 대신 고르지 않습니다.

```bash
uv run tracegraph check B.json --baseline A.json \
  --rule failures-not-increased --rule pattern-not-increased:tool-failure --json-out check.json
```

exit code가 판정입니다. `0`은 PASS, `1`은 FAIL(규칙 위반이 확실함: 실패·매치 증가 또는 트리
변경), `3`은 INSUFFICIENT_EVIDENCE(판정할 증거가
부족하고 FAIL은 없음), `2`는 잘못된 입력·설정이며 이때는 보고서를 쓰지 않습니다.

- `failures-not-increased`: step의 `(kind, name)`별 오류 수와 전체 오류 수가 늘지 않았는지 봅니다.
- `pattern-not-increased:<preset>`: preset의 확정된 매치 수가 늘지 않았는지 봅니다.
- `topology-unchanged` / `topology-unchanged:shape`: 파생 트리를 label 포함 / 모양만 비교합니다.

실행 간에 "이 step이 저 step"이라는 기록은 어느 source에도 없으므로 개수로 비교합니다. 같은
이름의 step 둘이 실패를 서로 바꾼 경우는 잡지 못하며, 위치 기준 비교는 `analyze --baseline`이
보여 줍니다. 모르는 증거는 0으로 세지 않습니다. 선택한 규칙이 세는 step의 `unset` status, adapter가
대신 채운 값(`unknown_fields`)이나 없는 이름, step을 artifact에서 빠뜨렸을 수 있는 capture gap이 현재 쪽에 있으면
PASS를, baseline 쪽에 있으면 FAIL을 낼 수 없어 INSUFFICIENT_EVIDENCE가 됩니다. topology 규칙은 개수가
아니라 트리를 비교하므로, step을 빠뜨렸을 수 있는 gap이 어느 쪽에 있든 관측된 트리가 같아도 판정 불가입니다. 규칙이 셀 수 없는
미지는 판정을 막지 않습니다. 예를 들어 `unset` CHAIN은 `pattern-not-increased:tool-failure`를
막지 않고, 보관하지 않은 오류 메시지는 `timeout`만 막습니다. source가 다르거나 수집 provenance가
없는(artifact v7 이전) 쌍은 모든 규칙에서, 인과 설정이 다르거나 ingest 때 인과 edge를 추정으로
고른 쌍은 인과 구조를 읽는 규칙(여러 step preset, topology)에서 판정 불가입니다. 이름이 없는 step은
이름을 비교하는 곳에서 모두 미지로 다루고, LangGraph checkpoint 이름은 추론값이므로 LangGraph
실행에서는 label 포함 topology 규칙이 판정 불가이고 `:shape`은 판정합니다. 입력은 파일·stdin 하나씩이며 디렉터리는 받지 않습니다. 입력이 그 실행의 완전한
기록이라고 가정하므로, 실패한 span이 빠진 export는 정상 실행과 구별되지 않습니다.
`--json-out` 보고서는 `tracegraph.check` v1(`contracts/check-report.schema.json`)이며 두 입력의
정규화 artifact digest와 규칙 id·버전·인자를 담습니다.

## 여러 trace에서 패턴 찾기: query

먼저 기본 제공 preset을 확인합니다.

```bash
uv run tracegraph presets
```

가장 많이 쓰는 시작점은 tool 실패 찾기입니다.

```bash
uv run tracegraph query tool-failure A.json B.json
```

디렉터리를 넘기면 그 안의 `*.json` artifact를 함께 읽습니다.

```bash
uv run tracegraph query tool-failure traces/
```

디렉터리 안의 `*.json` 중 artifact가 아닌 파일은 경고만 남기고 건너뜁니다. 그래서 실패가 담긴
artifact가 깨져 있으면 "매치 없음"(exit 1)으로 끝날 수 있습니다. CI에서 "이 패턴이 없어야 한다"를
검사할 때는 `--strict`를 붙이세요. 건너뛴 파일이 하나라도 있으면 exit 2로 실패합니다.
`export-review-candidates`도 같은 옵션을 받습니다.

```bash
uv run tracegraph query tool-failure --strict traces/
```

결과가 너무 많으면 `--limit`으로 개수를 제한합니다.

```bash
uv run tracegraph query tool-failure --limit 10 traces/
```

패턴에 걸린 step의 원인까지 같이 보고 싶으면 `--explain`을 붙입니다.

```bash
uv run tracegraph query tool-failure --explain A.json B.json
```

## Phoenix trace를 한 명령으로 진단하기

Phoenix는 trace 화면과 평가를 담당하고, tracegraph는 그 trace에서 실패 인과관계와
반복 동작을 추가로 분석합니다. 먼저 공식 안내에 따라 LangGraph OpenInference 계측과
Phoenix CLI인 `px`를 설정합니다.

- [Phoenix LangGraph 계측](https://arize.com/docs/phoenix/integrations/python/langgraph/langgraph-tracing)
- [Phoenix CLI](https://arize.com/docs/phoenix/sdk-api-reference/typescript/arizeai-phoenix-cli)

`px` 1.0.4 이상이 PATH에 있고 Phoenix 인증이 설정돼 있다면 먼저 연결 상태를
확인하고, trace id 없이도 최근 실패를 진단할 수 있습니다.

```bash
uv run tracegraph phoenix doctor
uv run tracegraph phoenix diagnose
uv run tracegraph phoenix diagnose --project my-agent
uv run tracegraph phoenix diagnose <trace-id>
```

trace id를 생략하면 최근 20개 중 가장 최신 실패 trace를 선택합니다. 실패 trace가
없으면 그 사실을 알리고 가장 최신 정상 trace를 진단합니다. 특정 trace를 지정하면
최근 목록 조회 없이 해당 trace만 가져옵니다. Phoenix project를 명시할 때는
`--project`를 사용하고, endpoint와 인증정보는 `px` profile 또는 환경 변수에서
관리합니다. tracegraph는 API key를 인자로 받거나 출력하지 않습니다.

자동 retry 진단은 upstream producer가 기록한 `retry:` CHAIN marker만 신뢰합니다.
SyncMill의 명시적 retry telemetry는 이 계약을 기록하지만, 일반 OTLP 또는 LangGraph
checkpoint producer가 marker를 기록하지 않으면 반복된 tool 호출을 retry로 자동 표시하지
않습니다. 이 경우 `tracegraph query tool-repeat-failure-heuristic <artifact>`로 추론 신호를
직접 조회할 수 있습니다. 이 heuristic 결과는 governance review candidate로 내보낼 수
없습니다.

정상 실행을 명시적으로 비교하려면 baseline trace id를 넘깁니다. 진단 대상은 자동으로
고를 수 있지만 baseline은 의미가 같은 실행인지 추측하지 않고 항상 명시적으로 받습니다.

```bash
uv run tracegraph phoenix diagnose <failed-trace-id> --baseline <successful-trace-id>
```

재현 가능한 body-free 결과를 남기려면 다음 옵션을 사용합니다.

```bash
uv run tracegraph phoenix diagnose <trace-id> \
  --save-artifact safe-trace.json \
  --json-out analysis.json
```

Phoenix raw export에는 prompt와 output이 포함될 수 있지만 `phoenix diagnose`는 이를
메모리에서 바로 제거하며 raw 응답을 저장하지 않습니다. 조회는 annotation을 포함한
read-only `px trace list/get`만 사용하고 Phoenix 데이터를 수정하는 annotate, note,
delete 명령은 호출하지 않습니다. artifact와 report에는 구조
식별자, identifier 형태의 step 이름, kind/status/time, token, 명시적 cost, 평가의 name/label/score만
남습니다. 평가 name과 label도 step 이름처럼 identifier 형태일 때만 남기고, 자유 텍스트 label은
버립니다. name이 identifier 형태가 아닌 평가는 통째로 버립니다. prompt, message, tool 인자/결과, 검색 문서, raw error, 평가 explanation,
사용자·세션·프로젝트 식별자는 저장하지 않습니다.

Phoenix export는 parent 관계를 제공하지만 원래 OTLP span link는 복원하지 못합니다.
따라서 결과에 `parent_only`가 표시되며, 이는 “fan-in이 없다”가 아니라 “추가 원인이
export에서 유실됐을 수 있다”는 뜻입니다.

status가 UNSET이고 exception event가 없는 span도 경로마다 다르게 읽습니다(message나 type이
있는 exception event가 붙으면 두 경로 모두 `error`). `ingest-otlp`는 이를 `ok`로
기록합니다. UNSET은 OpenTelemetry의 기본 status이고 SyncMill도 성공 span을 UNSET으로 남기기
때문입니다. Phoenix 경로는 `unset`을 그대로 둡니다. trace status는 같지만 step status는
표시되는 곳마다 다르고, `ok` step을 요구하는 preset은 `unset` step에 맞지 않습니다. 그래서
UNSET으로 남은 agent 시도가 실패한 `gate:` tool의 직접 원인인 SyncMill 실행은
`ingest-otlp`에서는 `gate-failure-after-success`로 보고되고 Phoenix에서는 보고되지 않습니다.
baseline은 같은 경로로 읽은 실행과 비교하십시오. 섞으면 경로 때문에만 status가 다른 step이
behavior change(`ok`→`unset` 또는 그 반대)로 보고될 수 있습니다.

Phoenix에서 직접 내려받은 JSON도 사용할 수 있습니다.

```bash
uv run tracegraph ingest-phoenix --file phoenix-trace.json --out safe-trace.json
uv run tracegraph analyze phoenix-trace.json
px trace get <trace-id> --format raw --no-progress \
  | uv run tracegraph analyze -
```

## OTLP/OpenInference span export 사용하기

LangGraph checkpoint가 아니라 OTLP JSON 또는 Collector JSONL span export가 있다면
`analyze`에 바로 넘겨 진단할 수 있고, artifact로 보관하려면 `ingest-otlp`를 씁니다.

```bash
uv run tracegraph analyze otlp_trace.json
uv run tracegraph ingest-otlp --file otlp_trace.json --out otlp.json
uv run tracegraph inspect otlp.json
```

`analyze`는 입력을 형태로만 판별합니다. `schema_version`이 있으면 artifact, `resourceSpans`가
있으면 OTLP, `spans`(또는 최상위 `traces`/`data`)가 있으면 Phoenix입니다. 두 형태를 함께 가진
입력은 추측하지 않고 거절합니다. 단일 객체라면 각 형식을 명시적으로 읽는
`ingest-otlp`/`ingest-phoenix`를 안내하고, 리스트·JSONL·Phoenix envelope라면 한 형식의
레코드만 별도 파일로 추출하라고 안내합니다. 레코드가 유효한지는 adapter가 판정하므로 직접
진단과 명시적 ingest 명령이 받아들이는 입력은 같습니다. 직접 진단은 `ingest-otlp`와 같은 adapter 설정을 쓰므로 보고서가 artifact를 거친
결과와 바이트 단위로 같고, `--json-out`을 요청하지 않으면 파일을 쓰지 않습니다. OTLP artifact가
오류 문자열을 그대로 보존한다는 점은 변하지 않습니다(보고서는 `safe-v1`).

파일 안에 trace가 여러 개 있으면 `--trace`로 trace id를 지정합니다. 지정하지 않으면 발견된
trace id 목록과 함께 종료 코드 2로 끝납니다. artifact는 trace를 하나만 담으므로, artifact에
`--trace`(baseline이면 `--baseline-trace`)를 주면 그 artifact의 trace id와 같아야 합니다. 다르면
보고서나 artifact를 쓰지 않고 종료 코드 2로 끝납니다(`analyze`, `check`, `ingest-phoenix` 공통).

```bash
uv run tracegraph analyze otlp_trace.json --trace agent-trace-1
uv run tracegraph ingest-otlp --file otlp_trace.json --trace agent-trace-1 --out otlp.json
```

저장소의 샘플 OTLP 파일은 다음 명령으로 만들 수 있습니다.

```bash
uv run python examples/otlp_agent_trace.py
uv run tracegraph ingest-otlp --file otlp_trace.json --out otlp.json
```

OTLP 예제는 원인이 여러 개인 fan-in 구조를 포함하므로 `lossy-projection` 표시와
raw causal graph 기반 `explain`의 차이를 확인하기 좋습니다.

Phoenix와 tracegraph가 같은 실행을 받으면서 span link까지 보존해야 한다면
`examples/otel-collector-phoenix-tracegraph.yaml`의 이중 pipeline 예제를 사용합니다.
Phoenix pipeline은 정상 관찰 데이터를 받고, tracegraph pipeline은 body allowlist를
적용한 뒤 JSONL로 저장합니다.

## LadybugDB/Cypher 백엔드 사용하기

기본 분석은 pure Python in-memory 백엔드로 동작합니다. 선택 설치한 LadybugDB 백엔드는
패턴 검색과 원인 추적을 가속하는 선택 백엔드입니다.

```bash
uv sync --extra cypher
uv run tracegraph query tool-failure --backend ladybug A.json B.json
uv run tracegraph explain --backend ladybug A.json <step_id>
```

중요한 점은 JSON artifact가 항상 원본이라는 것입니다. LadybugDB DB는 다시 만들 수 있는
캐시/가속기이고, 분석 의미는 기본 백엔드와 같아야 합니다.

## 리뷰 후보·인시던트 메모 내보내기

두 명령 모두 artifact를 읽기만 하고, 원본 대신 정해진 필드만 담은 파일을 새로 씁니다.

```bash
# 거버넌스 검토 후보 (일치하는 경우가 없어도 정상 보고서입니다)
uv run tracegraph export-review-candidates tool-retry-failure traces/ -o candidates.json

# 장기 기억 저장소용 인시던트 사후 분석 메모 (Markdown)
uv run tracegraph export-incident-memo A.json -o incident.md
```

`export-review-candidates`는 후보마다 `run_id`, `pattern_id`, `pattern_version`,
`tool_key`, `artifact_digest` 다섯 필드만 씁니다. `run_id`가 없거나 도구 이름이
`server::tool` 형태가 아닌 artifact가 하나라도 일치하면 전체를 실패시키고 기존 출력
파일을 건드리지 않습니다.

`export-incident-memo`는 `safe-v1` 보고서보다 더 좁습니다. step 이름, 오류 메시지,
평가 label을 쓰지 않고, step은 `step_<번호>_<종류>` 별칭으로, run id는 잘린 SHA-256
`run_digest`로만 나타냅니다. 나머지는 trace 상태, causal fidelity, step·오류 수, 프리셋
패턴 id와 버전, edge origin, fidelity 공시 같은 구조 메타데이터입니다. 포함 관계만 보여 주는 edge나 출처를 모르는 edge는
"caused by"가 아니라 "preceded by"로 적습니다. 메모에는 입력 artifact 파일 바이트의
SHA-256이 함께 기록됩니다.

## 자주 헷갈리는 개념

### artifact

`ingest`나 `ingest-otlp`가 만드는 JSON 파일입니다. tracegraph의 시스템 오브
레코드입니다. 다른 저장소 백엔드는 이 파일에서 다시 만들 수 있어야 합니다.

### raw causal graph

실제 인과관계를 모두 보존하는 graph입니다. 한 step에 원인이 여러 개면 여러 개를
그대로 둡니다. `explain`과 `query`는 이 계층을 봅니다.

### derived tree

사람이 보기 좋고 구조 비교가 쉬운 단일 부모 트리입니다. 원인이 여러 개인 step은
하나의 부모만 고르므로 정보가 줄어들 수 있습니다. 이런 step에는
`projection_lossy=True`가 붙습니다.

### interrupt step

LangGraph 노드가 `interrupt()`로 멈춘 뒤 resume도 결과도 기록되지 않았다면, thread의 가장
최근 root checkpoint 아래에 source가 `interrupt`이고 status가 `unset`인 step이 생깁니다.
이 step은 "기록된 이력 안에서 멈춘 채 끝났다"는 뜻이며, 지금 LangGraph가 이 task를 재개할
것이라는 뜻은 아닙니다. interrupt와 함께 resume이나 오류가 기록되었는데 결과가 없으면
checkpoint만으로는 아직 멈춰 있는지 알 수 없으므로 step을 만들지 않고 ingest 때 공시합니다.
recursion limit, 프로세스 종료, `interrupt_before`/`interrupt_after` breakpoint처럼 예약된
task가 아무것도 기록하지 않은 경우는 그래프 없이는 보고되지 않습니다(아래 scheduled step).
interrupt step이 있는 artifact는 `schema_version` 5를 선언하므로 0.3.1 이하 버전은 이 파일을
읽지 않습니다.

### scheduled step

`ingest`에 thread를 쓴 컴파일된 그래프를 `--graph`로 넘기면, 예약됐지만 아무것도 기록하지
않은 task를 보고합니다.

```bash
uv run tracegraph ingest --sqlite trace.db --thread A --out A.json --graph path/to/agent.py:graph
uv run tracegraph ingest --sqlite trace.db --thread A --out A.json --graph pkg.module:graph
```

LangGraph의 `get_state`가 그 그래프로 thread의 가장 최근 root checkpoint에서 예약하는 task를
알려 주고, 그중 그 checkpoint에 write가 하나도 없는 task가 source `scheduled`, status `unset`인
step이 됩니다. 끝났는지·실패했는지·멈췄는지는 여전히 checkpoint의 write로 판정하므로 `task`나
`interrupt` step과 겹치지 않습니다. 실행 도중 죽은 task도 아무것도 기록하지 않으므로, 이 step은
"시작하지 않았다"는 뜻이 아닙니다.

- **결과는 넘긴 그래프에 대한 답입니다.** 다른 그래프는 같은 checkpoint에서 다른 task를 예약할
  수 있고, checkpoint 하나로는 어느 그래프가 썼는지 증명할 수 없습니다. 그래서 artifact에
  그래프의 구조 지문(`trace.schedule_graph`)을 남깁니다. 예약된 task가 없어도 남기므로 "확인했고
  남은 일이 없음"과 "확인하지 않음"이 구분됩니다.
- **공시만 하는 경우:** 그 checkpoint에 저장됐지만 아직 적용되지 않은 `Command(goto=...)` 입력과,
  그래프에 없는 노드로 가는 `Send` packet은 LangGraph 자신의 답에서 빠지므로 ingest 때 경고로
  알립니다.
- **코드가 실행됩니다.** 모듈을 import하므로 top-level 코드가 돌고, checkpoint를 읽는 동안 그래프의
  state callback(schema mapper·validator, reducer, custom channel, managed value, cache-key 함수)이
  실행됩니다. 노드는 실행되지 않습니다. `--graph`를 직접 줄 때만 쓰며 추론하지 않습니다.
- `--graph`로 읽은 artifact는 0.4.x 이하 버전이 읽지 않습니다. 내용상 `schema_version` 6이
  필요하고, 이 릴리스의 새 ingest는 아래 수집 provenance 때문에 7을 선언합니다. trace status는
  그대로입니다.

### 수집 provenance (capture)

새로 ingest한 artifact는 adapter가 관측하지 못한 것을 함께 기록하고 `schema_version` 7을
선언합니다. 그 이전 릴리스가 만든 artifact는 읽고 다시 저장해도 이전 릴리스가 쓰던 그대로입니다
(v2–v6은 바이트까지 같고, v1은 지금처럼 v2로 migration됩니다). provenance가 새로 붙지는 않습니다.

- `trace.capture.gaps`: ingest 경고 하나마다 고정 코드와 개수가 남습니다. 읽을 때는 형식에 맞는
  코드라면 처음 보는 코드도 그대로 보존합니다(예:
  `langgraph.malformed_pending_writes`, `otlp.graph_parent_by_timing`). 경고 문장은 지금처럼
  stderr에만 나가고, 코드에는 checkpoint id·task id·producer 값이 들어가지 않습니다. 놓친 것이
  없으면 `{"gaps": []}`로 남으므로 "기록했고 공시할 것이 없었음"과 "기록하지 않음"이
  구분됩니다. step 하나의 값을 대신 채운 경우는 gap이 아니라 아래 `unknown_fields`에 남습니다.
- `steps[].unknown_fields`: adapter가 값을 대신 채운 필드입니다. `kind`·`name`(인식되는
  OpenInference kind나 쓸 수 있는 이름이 없는 span, 식별자 형태가 아니라 버려진 Phoenix 이름,
  귀속하지 못한 LangGraph task), `error_msg`(OTLP의 `"error"` 대체 문구, 보관하지 않은 메시지,
  LangGraph의 "직렬화되지 않은 예외" 표시)가 있습니다.
- LangGraph checkpoint는 자기를 쓴 노드를 기록하지 않습니다. 이름은 부모 checkpoint의 routing
  채널에서 추론하므로(병렬이면 여러 후보 중 첫 번째, `Send`·update면 없음) input checkpoint와 그
  입력을 적용하는 loop checkpoint를 뺀 모든 checkpoint step에 이름이 있든 없든 `kind`·`name`이
  표시됩니다. 표시된 이름은 화면용으로 그대로 남습니다. LangGraph task id로 이름을 확인한
  task·interrupt·scheduled step은 표시하지 않습니다.
- OTLP의 `graph.node.parent_id`를 시작 시각으로 골랐거나 따르지 못하고 `parentSpanId`를 쓴
  경우에도 gap으로 남고, stderr 경고가 새로 한 줄 나옵니다.
- LangGraph 오류 채널이 이미 오류를 담고 있는데 노드가 그 채널을 다시 쓰면
  `langgraph.rewritten_error_channel` gap이 됩니다. 다시 실패한 것인지, state 전체를 돌려주는 노드가
  값을 넘긴 것인지(reducer가 있으면 이때도 값이 바뀝니다) checkpoint만으로는 구분할 수 없어서
  실패로 기록하지 않습니다. `check`는 이 gap이 있는 쪽의 개수 규칙을 한쪽으로만 판정하고, 성공을
  조건으로 하는 pattern(`gate-failure-after-success` 등)은 어느 쪽에 gap이 있든 판정 불가로 둡니다.
  topology 규칙은 영향을 받지 않습니다. 모든 시도를 기록하려면 재시도 전에 오류 키를 비우세요
  (`{"error": None}`). 다만 채널에 reducer가 있으면 `None`으로 비워지는지는 그 reducer에 달려
  있습니다.
- OTLP span link 중 원인으로 기록하지 못한 것도 종류별로 gap이 됩니다. 이 trace를 가리키지만
  export에 없는 span을 가리키는 link는 `otlp.unresolved_span_link`, trace를 지정하지 않은 link는
  `otlp.unscoped_span_link`, 다른 trace를 가리키는 link는 `otlp.foreign_trace_link`입니다. 앞의
  두 개는 이 trace의 span이 export에서 빠졌을 수 있다는 뜻이라 `check`에서 step이 빠졌을 수 있는
  gap과 똑같이 다룹니다. trace를 넘는 원인은 어차피 비교하지 않으므로 마지막 것은 판정에
  영향을 주지 않습니다.
- 새로 ingest한 artifact의 digest와, 그것으로 만든 분석 보고서·incident memo의 digest가
  바뀝니다. 보고서·memo 형식, 그 밖의 CLI 출력, exit code는 그대로입니다.

### step id

각 실행 단계의 고유 id입니다. `explain`에는 전체 id 또는 유일하게 구분되는 suffix를
넣을 수 있습니다.

## 외부 실행 근거 연결

Toolgraph preflight를 함께 계측하는 경우 `toolgraph.preflight.artifact_digest`,
`toolgraph.graph_generation`, `toolgraph.preflight.verdict`를 한 묶음으로 기록합니다.
tracegraph는 이를 외부 판정 근거로
보존하지만 실패 원인 edge로 승격하지 않습니다. 실행 결과나 patch는 본문이나 로컬
경로 대신 `syncmill.artifact_digest=sha256:...`만 기록합니다.

SyncMill exporter는 선택 기능이며 비활성 상태에서는 no-op이어야 합니다. Collector나
파일 exporter가 실패해도 agent 실행 결과를 실패로 바꾸지 않는 reference 구현은
`examples/syncmill_instrumentation_contract.py`에서 확인할 수 있습니다.

## 문제 해결

### `validate`가 실패합니다

artifact가 tracegraph의 canonical form과 다르다는 뜻입니다. 손으로 JSON을 수정했거나
다른 버전/도구가 일부 edge를 누락했을 수 있습니다. 가능한 경우 원본 checkpoint나
OTLP export에서 다시 `ingest`하세요.

### `query`가 exit code 1로 끝납니다

에러가 아니라 "매치가 없다"는 의미입니다. CI에서 "특정 실패 패턴이 없어야 한다"는
조건을 확인할 때 이 동작을 활용할 수 있습니다.

### `--backend ladybug`가 동작하지 않습니다

선택 dependency가 설치되지 않았을 가능성이 큽니다.

```bash
uv sync --extra cypher
```

그래도 안 되면 기본 백엔드로 먼저 같은 artifact가 분석되는지 확인하세요.

```bash
uv run tracegraph query tool-failure A.json B.json
```

## 추천 학습 순서

`examples/getting_started_ko.ipynb`의 저장된 CLI 출력은 이전 버전입니다.
현재는 `analyze` 출력의 `Next investigation steps`에 있는 `explain` 명령을 복사해 사용하세요.
`step_id:` 줄은 표시용 JSON 문자열이며 셸용 인용이 아닙니다. 이를 명령에 그대로 붙이면
`$HOME` 같은 문자열이 셸에서 확장될 수 있으므로, 인자가 안전하게 인용된 안내 명령을 사용하세요.

1. `examples/tiny_agent.py`로 `A.json`, `B.json`을 만든다.
2. `inspect A.json`으로 에러 step을 찾는다.
3. `explain A.json <step_id>`로 실패 원인을 따라간다.
4. `analyze A.json --baseline B.json`으로 자동 진단과 동작 차이를 본다.
5. `diff A.json B.json`으로 정상 실행과 실패 실행의 구조 차이를 본다.
6. `query tool-failure A.json B.json`으로 여러 trace 검색을 해 본다.
7. Phoenix trace id로 `phoenix diagnose`를 실행한다.
8. `examples/otlp_agent_trace.py`로 fan-in 예제를 만들고 `lossy-projection`을 확인한다.
