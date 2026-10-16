# Tracegraph 서비스 차별화·실사용성 추가 리뷰

- 검토일: 2026-10-16 (Asia/Seoul)
- 기준: `b6b44805ce877eaab318f2cd8db37232a557479c`, macOS/arm64, Python 3.12.11
- 관련 문서: [성능·보안·사용성 구현 리뷰](2026-10-16-implementation-review.md)
- 결과: 개선 제안 [#57](https://github.com/memtomem/tracegraph/issues/57)–[#60](https://github.com/memtomem/tracegraph/issues/60) 등록. 제품 구현이나 설계 채택은 포함하지 않는다.

## 1. 차별화 방향

**추천하는 제품 가치는 “기존 agent trace에서 조사할 증거를 찾고, 수정 후 회귀 여부를 재현 가능한 규칙으로 확인하는 로컬 분석 도구”다.** 주요 사용자는 이미 LangGraph·OTLP·Phoenix 데이터를 가진 agent 개발자와 CI 담당자다. 이 대상 설정은 현재 제품 경계에서 도출한 가설이며 사용자 인터뷰로 검증한 결과는 아니다.

현재 강점은 raw 인과관계와 파생 트리의 구분, 결정적인 분석, portable artifact와 digest, 원문 본문을 제외하는 보고서다. 이를 사용자가 체감할 수 있도록 **입력 → 조사 → 비교·판정 → 반복 장애 우선순위화**를 연결할 필요가 있다. 그래프 저장소 선택이나 Cypher 지원만으로는 이 흐름의 편익을 설명하기 어렵다.

2026-10-16에 확인한 공식 문서상 Phoenix는 trace를 CLI로 가져오는 경로를 제공하고, LangSmith는 baseline 회귀 비교와 코드 기반 평가, trace 공유·로그 탐색을 제공한다. 따라서 “CLI가 있다”, “실행을 비교한다”, “결정적 규칙을 쓴다” 각각을 독점적 기능으로 표현하면 안 된다. 차별화는 기존 관측 도구와 함께 쓰면서 **인과 증거의 범위와 한계를 유지한 재현 가능한 작업 흐름**을 얼마나 적은 수작업으로 제공하는지에 대한 제품 가설이다. [Phoenix CLI 문서](https://arize.com/docs/phoenix/tracing/how-to-tracing/importing-and-exporting-traces/retrieve-traces-via-cli), [LangSmith 평가 유형](https://docs.langchain.com/langsmith/evaluation-types), [LangSmith trace 관리](https://docs.langchain.com/langsmith/manage-trace).

저장소 `docs/FEASIBILITY.md`의 경쟁 구도는 2026-05-28 기록이다. 이번 확인만으로 경쟁 제품에 raw DAG 분석 기능이 없다고 단정하지 않는다. 후속 포지셔닝 정비에서는 README의 “incumbents don't” 같은 포괄적 표현을 현재 검증 가능한 입력·출력·증거 계약 중심으로 좁히는 편이 타당하다. 해당 문서를 이번에 직접 개정하지는 않았다.

## 2. 실제 흐름에서 확인한 마찰

| 사용자 목적 | 현재 동작·근거 | 사용자에게 생기는 부담 |
| --- | --- | --- |
| 이미 가진 OTLP 파일을 진단한다 | `analyze`에 저장소 OTLP fixture를 주면 Phoenix 형식 오류로 exit 2. `ingest-otlp` → `analyze`는 지원 경로 | 먼저 adapter와 중간 artifact 개념을 알아야 함 |
| 진단에서 실패 step을 더 조사한다 | 터미널은 이름·kind·seq를 표시하지만 `explain`은 step ID를 요구. 합성 실패의 ID가 진단 출력에 없었음 | 별도 JSON/입력에서 ID를 찾아야 함 |
| 잘린 진단의 나머지를 확인한다 | renderer는 `--json-out` 요청 여부와 무관하게 “more ... in JSON report”라고 표시하는 코드 | 저장된 보고서가 있는 것처럼 이해할 수 있음 |
| 수정 후 회귀가 없는지 CI에서 판정한다 | 상태만 OK→ERROR로 바꾼 사례에서 `diff`, `analyze --baseline`, `query tool-failure`가 각기 다른 의미로 exit 0 | 사용자가 입력 검증·비교·종료 코드 해석을 직접 조합해야 함 |
| 반복 장애부터 처리한다 | `query`는 매치별 행과 총계를 출력. 별도 cohort 요약 CLI는 없음 | 매치 수와 영향받은 trace 수, 대표 증거를 수작업으로 정리해야 함 |

첫 번째·두 번째·네 번째는 CLI 실행 관찰, 세 번째·다섯 번째는 코드 확인이다. 기존 명령은 문서화된 계약대로 작동한다. 여기서의 지적은 “현재 API가 틀렸다”가 아니라 목적을 달성하는 데 드는 작업량이다.

### 재현 결과

모든 입력은 저장소 fixture 또는 임시 디렉터리의 합성 artifact였다. 실제 고객 데이터·외부 LLM·Phoenix 서버는 사용하지 않았다.

| 실행 | 결과 |
| --- | --- |
| `tracegraph analyze tests/fixtures/syncmill/route-fallback.otlp.json` | exit 2, `Phoenix export must be a trace object or an array of trace objects` |
| 합성 `current.json`의 `analyze` | 실패 표시, exit 0; `current-unique-tool-id`는 출력에 없음 |
| `analyze current.json --baseline baseline.json` | OK→ERROR 상태 변화 표시, exit 0 |
| `diff baseline.json current.json` | `IDENTICAL`, exit 0. 이름·트리 구조가 같기 때문에 정상 동작 |
| `query tool-failure current.json` | 실패 매치 표시, exit 0. “매치 있음” 의미로 정상 동작 |

합성 비교를 다시 실행하는 최소 코드:

```python
import tempfile
from pathlib import Path
from typer.testing import CliRunner
from tracegraph import artifact
from tracegraph.cli import app
from tracegraph.model import RawTrace, Trace, Step, StepKind, StepStatus
from tracegraph.normalize import normalize

with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    for name, status in (("baseline", StepStatus.OK), ("current", StepStatus.ERROR)):
        nt = normalize(RawTrace(
            trace=Trace(trace_id=name, source_kind="otlp", status=status),
            steps=[Step(step_id=f"{name}-unique-tool-id", trace_id=name, seq=0,
                        name="server::search", kind=StepKind.TOOL, status=status)],
            causal_edges=[],
        ))
        artifact.save_atomic(nt, root / f"{name}.json")
    before, after = str(root / "baseline.json"), str(root / "current.json")
    for args in (["analyze", after], ["analyze", after, "--baseline", before],
                 ["diff", before, after], ["query", "tool-failure", after]):
        result = CliRunner().invoke(app, args)
        print(args[0], result.exit_code, "current-unique-tool-id" in result.output)
        print(result.output)
```

## 3. 등록한 개선 제안과 추천 순서

### 먼저: 파일 하나로 시작하고 증거까지 이동하기

**[#57 — 주 분석 진입점의 OTLP 지원](https://github.com/memtomem/tracegraph/issues/57).** 기존 파일을 바로 분석하게 하되 형식이 모호하면 추측하지 않고 명시적 선택을 요구한다. 중간 raw artifact를 자동 저장하지 않는다. 직접 OTLP 분석과 기존 ingest→analyze의 증거·결과 동등성, 다중 trace 선택, offline 동작을 검증한다. 설치 사용자용 합성 walkthrough도 같은 흐름에서 제공하면 저장소 checkout 없이 가치를 경험할 수 있다. 새로운 수집 서버나 계정 도입은 필요하지 않다.

**[#58 — 진단에서 증거·다음 조사 단계로 연결](https://github.com/memtomem/tracegraph/issues/58).** 각 finding에 식별 가능한 step과 현재 입력에 맞는 다음 동작을 표시한다. artifact가 없는 Phoenix/in-memory 입력에서는 먼저 명시적으로 저장·변환하는 유효한 경로를 안내한다. 숨겨진 후보가 있으면 실제 보고서 저장 여부에 맞춰 안내한다. 근거, containment 맥락, 누락 데이터를 구분하며 원인을 단정하는 자동 수정 제안으로 확장하지 않는다.

기존 `safe-v1` 보고서는 익명화가 아니고 `inspect`·`explain`은 원시 오류를 표시할 수 있다. 다음 동작 안내와 공유 안내는 이 차이를 유지해야 한다. `export-incident-memo`의 더 엄격한 allowlist를 일반 분석 보고서와 동일시하지 않는다.

### 다음: 수정 효과를 검증하는 별도 회귀 판정

**[#59 — PASS / FAIL / INSUFFICIENT_EVIDENCE 회귀 검사](https://github.com/memtomem/tracegraph/issues/59).** 현재 명령의 exit 의미는 유지하고 명시적 current·baseline·규칙을 받는 별도 검사 경로를 제안한다. 최초 범위는 새로 관측된 실패·패턴과 사용자가 요청한 구조 규칙이다. 결과는 두 입력 digest와 규칙 버전에 연결하고 별도 versioned 계약으로 제공한다.

비교 가능한 source granularity, 상태 해석, graph/projection 맥락과 metric coverage를 규칙별로 판단해야 한다. 정보 부족을 0이나 성공으로 치환하지 않는다. baseline을 자동으로 “정상”이라고 선정하거나 한 쌍의 실행으로 일반적인 응답 품질·통계적 유의성을 주장하지 않는다. #50의 불완전 입력 처리가 선행 또는 포함되어야 한다. 실제 runtime 집행과 정책 변경은 Tracegraph의 역할에 추가하지 않는다.

### 수요 검증 후: 반복 장애를 처리할 순서 정하기

**[#60 — 명시적 trace 집합의 반복 motif 요약](https://github.com/memtomem/tracegraph/issues/60).** 처음에는 선택한 preset의 관측 패턴을 versioned 기준으로 묶고 대표 artifact digest·step으로 이동하게 한다. 매치 수, 영향받은 trace 수, 전체 검토 범위와 누락을 구분한다. 안정적인 signature에서 임의 trace ID를 제외하되 서로 다른 source/fidelity를 무작정 합치지 않는다.

같은 파생 트리나 패턴을 같은 근본 원인이라고 부르지 않는다. 본문을 모델에 보내는 의미 기반 군집화, 절감액 추정, hosted dashboard는 초기 범위에서 제외한다. #54의 제한 검색 설계와 연결하여 요약 때문에 전체 메모리 적재가 더 커지지 않게 한다.

## 4. 사용성 효과를 검증하는 방법

다음 수치는 **파일럿 수용 목표**다. 현재 달성률·고객 수요·MTTR 개선·경쟁 우위를 측정한 결과가 아니다. 먼저 agent 개발자 5명 정도가 같은 합성 사례를 수행하게 하고 실패 지점을 기록하는 것을 제안한다. 사용자 모집이나 측정은 이번에 실행하지 않았다.

| 작업 | 확인할 지표 | 제안 목표 |
| --- | --- | --- |
| 처음 받은 export 진단 | 첫 유효 진단까지 시간, 잘못된 명령 횟수 | 5분 이내, adapter 내부 문서 탐색 없이 완료 |
| 실패 증거 찾기 | 올바른 step까지 이동 횟수·시간 | 제공된 안내 한 단계로 올바른 증거에 도달 |
| 증거 해석 | containment를 원인 확정으로 오해하는지 | 사례별 증거 수준과 모르는 점을 설명 가능 |
| CI 적용 | 사용자 정의 JSON 파싱·exit 반전 코드 필요 여부 | 문서화된 한 검사 경로로 적용; 불충분 증거의 PASS 오분류 0건 |
| 반복 장애 선별 | 최다 관측 motif와 대표 trace 선택 시간 | 수동 집계 없이 올바른 대표 증거 선택 |

예상 효과의 크기보다 먼저 볼 것은 실패율과 해석 오류다. 잘못된 인과 단정이나 불완전 증거의 정상 판정이 늘어난다면 클릭 수가 줄어도 개선으로 보지 않는다. 제품 검증을 위한 별도 원격 사용량 수집을 자동 도입하지 않는다.

## 5. 검토·전달 경계

- 공식 경쟁 문서의 현재 기능을 읽었지만 경쟁 제품을 설치해 동일 corpus로 benchmark하지 않았다. 과거 FEASIBILITY의 시장 독점 주장을 재확인한 것이 아니다.
- 이번 추가 검토에서는 위 CLI 시나리오만 실행했다. 전체 테스트·실서버 E2E를 다시 실행하지 않았다. 앞선 구현 리뷰의 검증 수치는 그 문서에 별도로 보존한다.
- 신규 #57–#60은 enhancement 제안이다. #52–#56의 결함·자원 개선과 중복 등록하지 않았고, 필요한 의존 관계는 본문에 표시했다.
- 등록 후 GitHub API로 네 이슈의 제목·본문·OPEN 상태를 다시 읽어 준비한 내용과 일치함을 확인했다. 문서 내부 링크와 재현 코드 구문도 점검했다.
- 최종 인터페이스·정책을 채택한 것이 아니므로 ADR은 생성하지 않았다. 구현에 착수할 때 #59의 판정·호환성 계약 등 결정이 필요한 부분을 확정해야 한다.
- 제품 코드·테스트 수정, 커밋·push·배포는 수행하지 않았다. 이슈 등록 시점에 이 보고서와 앞선 보고서는 로컬 미커밋 문서였으며, 이슈는 GitHub에 실제 등록했다.
