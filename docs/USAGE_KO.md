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
남습니다. prompt, message, tool 인자/결과, 검색 문서, raw error, 평가 explanation,
사용자·세션·프로젝트 식별자는 저장하지 않습니다.

Phoenix export는 parent 관계를 제공하지만 원래 OTLP span link는 복원하지 못합니다.
따라서 결과에 `parent_only`가 표시되며, 이는 “fan-in이 없다”가 아니라 “추가 원인이
export에서 유실됐을 수 있다”는 뜻입니다.

Phoenix에서 직접 내려받은 JSON도 사용할 수 있습니다.

```bash
uv run tracegraph ingest-phoenix --file phoenix-trace.json --out safe-trace.json
uv run tracegraph analyze phoenix-trace.json
px trace get <trace-id> --format raw --no-progress \
  | uv run tracegraph analyze -
```

## OTLP/OpenInference span export 사용하기

LangGraph checkpoint가 아니라 OTLP JSON 또는 Collector JSONL span export가 있다면
`ingest-otlp`를 씁니다.

```bash
uv run tracegraph ingest-otlp --file otlp_trace.json --out otlp.json
uv run tracegraph inspect otlp.json
```

파일 안에 trace가 여러 개 있으면 `--trace`로 trace id를 지정합니다.

```bash
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

1. `examples/tiny_agent.py`로 `A.json`, `B.json`을 만든다.
2. `inspect A.json`으로 에러 step을 찾는다.
3. `explain A.json <step_id>`로 실패 원인을 따라간다.
4. `analyze A.json --baseline B.json`으로 자동 진단과 동작 차이를 본다.
5. `diff A.json B.json`으로 정상 실행과 실패 실행의 구조 차이를 본다.
6. `query tool-failure A.json B.json`으로 여러 trace 검색을 해 본다.
7. Phoenix trace id로 `phoenix diagnose`를 실행한다.
8. `examples/otlp_agent_trace.py`로 fan-in 예제를 만들고 `lossy-projection`을 확인한다.
