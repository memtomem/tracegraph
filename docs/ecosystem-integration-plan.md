# toolgraph, tracegraph, syncmill 연계 계획

**상태:** 첫 통합 마일스톤 및 Tracegraph T3 완료 (2026-07-12)
**작성일:** 2026-07-11
**정본:** [전체 계획](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/integration-plan.md) · [구현 설계](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/implementation-design.md) · [smoke runbook](https://github.com/memtomem/syncmill/blob/main/docs/ecosystem/smoke-runbook.md)

> 실제 landed 순서와 설계 라벨은 정본에서 구분한다. P0/P1과 첫 마일스톤,
> P2와 실제 identity/qualified-tool advisory P3/Gate D는 완료됐다. Tracegraph T3의
> versioned review-candidate producer도 완료됐고, strict P3.1, Toolgraph G3와 전체 P4는
> 열려 있다.

**핵심 결정:** 첫 통합 마일스톤에서 syncmill이 `OTLPSpanAdapter`가 소비하는 세 속성
(`openinference.span.kind`, `graph.node.id`, `graph.node.parent_id` — 모두
OpenInference/graph 표준)을 직접 emit하므로 **첫 통합 마일스톤에서 tracegraph의 src
변경은 없었다**. 1~2단계 작업은 전부 fixture(`examples/syncmill_otlp_traces.py`)와
golden test였다. 후속 T3는 공통 상관 식별자인 allowlisted `syncmill.run_id` 하나만
generic `Trace.run_id`로 보존하고, 다른 syncmill 속성은 읽거나 core causal model에
저장하지 않는다.

## 요약

tracegraph는 세 프로젝트 중 실행 후 분석 계층이다. syncmill의 Supervisor 및 agent
subprocess 실행을 OpenInference/OTLP span 또는 portable trace artifact로 받아 인과관계,
구조적 회귀, 반복 실패 패턴을 분석한다. toolgraph 판정은 trace의 보조 provenance로
연결할 수 있지만 인과관계 자체를 대체하지 않는다.

| 프로젝트 | 연계 역할 |
| --- | --- |
| syncmill | run, strategy, agent phase와 결과 이벤트 생산 |
| tracegraph | 이벤트를 raw causal graph로 정규화하고 분석 |
| toolgraph | 실행 전에 사용된 정책 판정과 graph generation 제공 |

## 현재 기준선

- LangGraph checkpoint와 OpenInference/OTLP span adapter를 제공한다.
- raw multi-parent `CAUSED_BY` 그래프를 system of record로 유지한다.
- `explain`, AHU 기반 `diff`, cross-trace pattern query를 제공한다.
- portable JSON이 기준 artifact이고 Kuzu는 선택적 accelerator다.

## 목표

1. syncmill 실행을 기존 raw/normalized 모델로 손실 없이 가져온다.
2. strategy, phase, agent, tool, gate 결과를 명시적 인과 edge로 표현한다.
3. 동일한 작업의 orchestration 전략별 구조와 실패 패턴을 비교한다.
4. toolgraph preflight artifact를 분석 결과의 외부 근거로 연결한다.

## 비목표

- syncmill의 실행 엔진이나 memtomem을 tracegraph가 대체하는 것
- 모든 stdout, prompt, patch 또는 메모리 내용을 trace artifact에 복제하는 것
- tree projection을 RCA의 system of record로 사용하는 것
- Kuzu 또는 특정 observability vendor를 필수 저장소로 만드는 것

## 제안 trace 계약

필수 공통 속성:

| 속성 | 설명 |
| --- | --- |
| `run_id` | syncmill 실행 전체의 안정적 식별자 |
| `trace_id` / `span_id` | OpenInference/OTLP 상관관계 |
| `strategy` | route, pipeline, compete, council, decompose |
| `agent_id` | 실행 agent, Supervisor는 별도 id 사용 |
| `phase` | plan, execute, critique, synthesize, gate 등 |
| `worktree_slot` | 병렬 실행을 구분하는 불투명 식별자 — **concurrent 전략 전용 optional** (순차 전략에는 의미 있는 slot이 없어 필수로 두면 route fixture가 검증 불가) |
| `status` | success, error, timeout, cancelled |
| `artifact_digest` | patch/result를 복제하지 않고 참조하는 digest |

권장 causal edge:

```text
run -> strategy phase -> agent attempt -> gate -> result
                         |              -> retry
                         -> tool call
preflight artifact --------------------> run metadata
```

동시 실행은 순서를 인과관계로 오인하지 않는다. fan-out은 공통 부모를, fan-in은 소비한
결과를 복수 부모로 기록한다. 단, council/decompose의 cross-agent handoff는
relevance-ranked `shared` 검색이라 "실제 소비"를 증명할 수 없으므로, link 의미는
"phase 경계에 shared에 **게시된** 것"(선언된 가용성)으로 정의하고 근사임을 명시한다.

**span name은 diff-stable이어야 한다**: AHU `diff`의 라벨이 `step.name or kind`이므로
span name에는 agent id/phase/인덱스만 허용하고 uuid, timestamp, run_id를 넣지 않는다.
넣으면 모든 diff가 divergence로 판정된다. golden test에서 전략 간 `diff`는 exit 1이
**pass 조건**이다 (divergence가 정답).

## 단계별 계획

### 0단계: 모델 적합성 확인

- [ ] 대표 strategy별 최소 trace fixture를 정의한다.
- [ ] Supervisor, phase, agent attempt를 기존 step kind로 표현 가능한지 검토한다.
- [ ] fan-out/fan-in 및 cancellation의 parent 규칙을 고정한다.
- [ ] 민감 데이터 redaction과 attribute allowlist를 정의한다.

### 1단계: syncmill OTLP fixture

- [ ] 코드 결합 없이 합성 OTLP export fixture를 먼저 추가한다.
- [ ] 기존 `OTLPSpanAdapter`가 run/strategy/agent 관계를 보존하는지 테스트한다.
- [ ] `inspect`와 `explain`에서 timeout 및 gate failure를 역추적한다.
- [ ] compete의 병렬 완료 순서가 허위 causal ordering을 만들지 않는지 검증한다.

### 2단계: 선택적 계측 adapter

- [ ] syncmill 측에 vendor-neutral span naming convention을 제안한다.
- [ ] 계측 비활성 상태에서 syncmill 동작과 성능이 변하지 않게 한다.
- [ ] exporter 실패가 agent 실행 실패로 전파되지 않게 한다.
- [ ] result/patch는 digest와 경로만 기록하고 본문은 저장하지 않는다.

### 3단계: orchestration 분석

- [ ] strategy 간 normalized structure diff 예제를 추가한다.
- [ ] timeout, repeated-agent-failure, gate-failure-after-success preset을 검토한다.
- [ ] agent 이름이 같은 재시도와 다른 worktree slot을 구분한다.
- [x] cross-run query 결과에 pattern version을 기록한다.

### 4단계: toolgraph provenance 연결

- [ ] preflight artifact digest와 graph generation을 run metadata로 가져온다.
- [ ] policy verdict를 causal edge가 아닌 외부 decision evidence로 표현한다.
- [x] failure pattern을 versioned governance review candidate JSON으로 내보낸다.
- [x] tracegraph가 toolgraph manifest를 직접 수정하지 않는 경계를 테스트한다.

T3 producer는 `run_id`, `pattern_id`/`pattern_version`, qualified `tool_key`, 분석한
normalized artifact의 `sha256:` digest만 내보낸다. 후보는 사람이 검토할 evidence이며
Toolgraph intake/annotation(G3)과 SyncMill board 노출은 별도 후속 작업이다.

## 검증 기준

- 동일한 OTLP fixture가 안정적인 normalized artifact를 만든다.
- 모든 RCA는 raw `CAUSED_BY`를 사용하고 projection loss를 숨기지 않는다.
- 병렬 span의 timestamp 정렬이 인과 edge로 잘못 승격되지 않는다.
- core test는 Kuzu와 syncmill 설치 없이 통과한다.
- prompt, credential, 전체 patch 및 memtomem 내용이 trace에 포함되지 않는다.

## 주요 위험과 대응

| 위험 | 대응 |
| --- | --- |
| 관측 순서를 인과관계로 오인 | 명시적 parent/link만 causal edge로 사용 |
| strategy별 attribute가 모델을 오염 | 공통 core와 namespaced extension 분리 |
| 높은 trace volume | sampling과 payload allowlist, artifact 외부화 |
| exporter 장애가 실행에 영향 | 비동기 best-effort export와 fail-open |
| 정책 판정을 원인으로 오해 | toolgraph 결과를 decision evidence로 구분 |

## 완료 정의

route와 compete 실제 실행 trace를 ingest해 `inspect`, `explain`, `diff`를 수행하고,
병렬성 및 gate failure에 대한 golden test가 통과하면 첫 통합 마일스톤을 완료한 것으로
본다. 모든 strategy 지원과 운영용 trace backend는 후속 마일스톤이다.
