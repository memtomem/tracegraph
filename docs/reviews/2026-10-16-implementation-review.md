# Tracegraph 성능·보안·사용성 구현 리뷰

- 검토·등록일: 2026-10-16 (Asia/Seoul)
- 기준 커밋: `b6b44805ce877eaab318f2cd8db37232a557479c` (`tracegraph-mvp`, 0.4.0)
- 로컬 환경: macOS / arm64, Python 3.12.11, 저장소 `.venv`
- 작업 시작 시 worktree는 깨끗했으며 등록 직전에도 기준 커밋이 같았다.
- 결과: **신규 이슈 5건 등록, 기존 #50에 재현 근거 추가**. 제품 수정은 포함하지 않는다.

## 1. 검토 범위와 판단

CLI 입력·출력과 Phoenix subprocess 경계, 패턴 검색·자동 진단, 정규화·artifact 계약, 저장소 조회 경로, graph reference 로딩, 보안 정책과 사용 문서, 관련 회귀·성능 테스트를 검토했다. 전체 비성능 테스트를 실행했지만 모든 코드 경로에 대한 보안 감사나 의존성 취약점 전수 조사를 의미하지는 않는다.

기존 회귀 테스트와 현재 HEAD의 CI는 통과 근거를 갖고 있다. 그와 별개로, 결과가 없는 패턴 검색의 불필요한 계산 비용, 입력·결과 전체 적재에 따른 자원 사용, 보안 문서의 실행 경계 누락, CLI 실행 오류 안내에 개선할 부분이 있다. 손상된 입력을 건너뛴 후보 내보내기가 정상적인 빈 결과로 전달되는 문제도 재현했다.

원본 `CAUSED_BY`와 파생 `TREE_PARENT`의 구분, 원본 artifact와 `safe-v1` 보고서의 개인정보 경계, 정확한 artifact 바이트의 digest 계약은 개선 과정에서 보존해야 한다. 기존 한계가 문서화되어 있다는 사실과 이를 개선할 필요가 있다는 판단을 구분했다.

## 2. 등록 내역

우선순위는 제안이며 GitHub priority label을 새로 만들거나 적용하지 않았다. P2는 계획적인 성능·안전성·신뢰 경계 개선, P3는 제한된 환경에서의 오류 안내 개선으로 사용한다. 취약점 심각도나 CVSS 등급이 아니다.

| ID | 우선순위 | 관점 | 발견 및 분류 | 등록 |
| --- | --- | --- | --- | --- |
| R-01 | P2 | 성능 | 결과 0건인 반복 agent 실패 검색에서도 노드 수 두 배에 처리 시간이 약 네 배 증가 — 실행 재현 | [#52](https://github.com/memtomem/tracegraph/issues/52) |
| R-02 | P2 | 자원 보호 | stdin 및 Phoenix 출력의 무제한 버퍼링 — 문서화된 한계의 개선 | [#53](https://github.com/memtomem/tracegraph/issues/53) |
| R-03 | P2 | 성능·사용성 | `query --limit` 전에 전체 입력·검색 결과 적재 — 현재 계약을 확장하는 개선 | [#54](https://github.com/memtomem/tracegraph/issues/54) |
| R-04 | P2 | 보안 문서 | SECURITY.md에 명시적 `--graph` 코드 실행 경계 누락 — 문서 정합성 | [#55](https://github.com/memtomem/tracegraph/issues/55) |
| R-05 | P3 | 사용성 | Phoenix 실행 권한 오류가 처리되지 않은 예외로 탈출 — 통제된 오류 주입 재현 | [#56](https://github.com/memtomem/tracegraph/issues/56) |
| R-06 | P2 | 증거 완전성·사용성 | 손상된 파일을 건너뛴 후보 내보내기가 빈 보고서를 저장하고 성공 — 기존 이슈 범위 보강 | [#50 댓글](https://github.com/memtomem/tracegraph/issues/50#issuecomment-6211989989) |

각 이슈와 댓글에는 기준 SHA, 고정 커밋의 코드 링크, 재현 방법 또는 코드 관찰 방법, 기대·실제 동작, 영향과 수용 기준을 담았다. 등록 후 GitHub API로 제목·본문·OPEN 상태를 다시 읽고 준비한 내용과 일치함을 확인했다. #50 댓글도 본문과 URL을 재확인했다.

### R-01. 결과가 없어도 반복하는 조상 탐색

[`_match_two_step_unbounded`](https://github.com/memtomem/tracegraph/blob/b6b44805ce877eaab318f2cd8db37232a557479c/src/tracegraph/analysis/patterns.py#L340)는 각 실패 endpoint의 조상을 순회·정렬한 다음 이름 back-reference를 검사한다. 모든 이름이 다른 실패 AGENT 체인에서는 매치가 있을 수 없는데도 조상 탐색을 반복한다. `repeated-agent-failure`는 자동 진단의 preset에도 포함된다.

정규화를 제외한 `find_matches` 실행 시간, 크기별 3회 median:

| 노드 수 | 매치 수 | 시간 |
| --- | --- | --- |
| 500 | 0 | 0.07486초 |
| 1,000 | 0 | 0.30580초 |
| 2,000 | 0 | 1.27080초 |

출력 자체가 많아서 생기는 불가피한 비용으로 설명할 수 없는 사례다. 다만 이 수치는 위 합성 입력과 로컬 환경의 측정이며 전체 `analyze` 시간이나 모든 DAG의 복잡도 측정은 아니다.

수용 기준은 이름·predicate 후보 필터링 등으로 불필요한 순회를 줄이되 결과 순서, 중복 제거, longest-path lower bound, null-name 의미를 유지하는 것이다. 참조 matcher와의 차등 검증 및 결과 0건 입력의 증가율 검증이 필요하다. 재현 코드는 #52에 포함했다.

### R-02. 파일과 스트림의 자원 제한 차이

[`_read_source`](https://github.com/memtomem/tracegraph/blob/b6b44805ce877eaab318f2cd8db37232a557479c/src/tracegraph/cli.py#L207)는 stdin에 무제한 `read()`를 사용한다. 동일한 779바이트 합성 artifact에 `TRACEGRAPH_MAX_ARTIFACT_BYTES=16`을 적용했을 때 파일 경로는 exit 2, stdin 경로는 exit 0이었다.

[`_px_run`](https://github.com/memtomem/tracegraph/blob/b6b44805ce877eaab318f2cd8db37232a557479c/src/tracegraph/cli.py#L240)은 stdout과 stderr를 모두 메모리에 캡처한다. timeout은 시간 제한이며 바이트 제한이 아니다. Phoenix 부분은 코드 확인이며 실제 서버의 대용량 응답을 요청하거나 OOM을 유발하지 않았다.

파일에만 제한이 있다는 사실은 이미 SECURITY.md에 공개되어 있다. 새로운 원격 공격이나 현재 계약 위반으로 분류하지 않았다. stdin·stdout·stderr의 실제 읽기 제한, 설정의 적용 범위, overflow/timeout 시 자식 프로세스 정리와 body-free 오류를 수용 기준으로 등록했다. 읽기 제한만으로 JSON 분석 전체가 streaming 방식이 되는 것은 아니다.

### R-03. 출력 제한 전에 전체 결과 적재

[`query`](https://github.com/memtomem/tracegraph/blob/b6b44805ce877eaab318f2cd8db37232a557479c/src/tracegraph/cli.py#L1111)는 `_load_many`와 `_search_with_backend`를 완료한 다음 `matches[:limit]`를 적용한다. `--explain` 저장소는 이미 지연 생성하지만, 그 이전의 입력·매치 적재 비용은 남는다.

현재 도움말은 tail truncation을 설명하므로 오동작으로 등록하지 않았다. 제한 실행을 도입할 때에는 정확한 총건수를 알 수 없는 경우의 표시, exhaustive 결과의 결정적 prefix와의 동등성, backend fallback을 함께 정의해야 한다. 조기 종료 때문에 뒤쪽 파일의 검증·중복 trace ID 확인을 생략하거나 #50을 악화시켜서는 안 된다. 이 항목의 RSS 실측은 수행하지 않았다.

### R-04. `--graph` 신뢰 경계의 문서 누락

[`graph_ref.py`](https://github.com/memtomem/tracegraph/blob/b6b44805ce877eaab318f2cd8db37232a557479c/src/tracegraph/graph_ref.py#L1), README와 CLI 도움말은 사용자가 명시한 모듈의 top-level 코드와 graph state callback이 실행될 수 있음을 설명한다. 반면 SECURITY.md는 이 경계를 설명하지 않고 checkpoint를 유일한 실행 가능 입력 형식으로 서술한다.

명시적으로 지정한 코드 실행 자체는 의도된 동작이다. 보안 정책에 일반 JSON 파싱, checkpoint 역직렬화 hardening, 명시적 Python graph 코드 실행을 구분하고, 노드는 실행하지 않더라도 callback은 실행하며 serializer allowlist가 graph 코드를 sandbox하지 않는다는 점을 설명하도록 등록했다. CLI에 새로운 확인 절차를 추가하는 제안은 아니다.

### R-05. Phoenix 실행 오류 안내

`subprocess.run`에 `PermissionError(13, "synthetic permission denied")`를 주입하고 실제 Typer 명령 `phoenix doctor`를 호출했다. 결과는 exit 1, 예외형 `PermissionError`, 출력에 `FAIL` 없음이었다. 실제 설치된 `px`의 권한을 변경하거나 특정 운영 설치의 장애를 재현한 것은 아니다.

실행 권한과 executable format 등 관련 OS 오류를 `_PxError`로 변환하고 기존 doctor/diagnose 종료 규약에 맞춰 안내하도록 등록했다. 기존 missing executable·timeout·nonzero exit 안내와 민감 출력 비노출 조건을 보존해야 한다. 비교용 decode 오류는 doctor에서 이미 처리되어 신규 문제에 포함하지 않았다.

### R-06. 불완전한 입력에서 정상 빈 후보 보고서 생성

정상적인 no-match artifact 하나와 내용이 `{`인 손상 JSON 하나를 임시 디렉터리에 만들고 `export-review-candidates tool-failure <directory> -o <report>`를 실행했다.

| 관찰 항목 | 결과 |
| --- | --- |
| 종료 코드 | 0 |
| 보고서 생성 | 예 |
| 후보 수 | 0 |
| skipped 경고 | 있음 |

이 재현은 입력 범위가 불완전하다는 것을 증명한다. 손상 파일이 원래 매칭 실패를 담고 있었다는 사실까지 증명하지는 않는다. 원래 #50은 후보 export 경로를 미확인으로 남겨 두었으므로 새 이슈 대신 댓글로 보강했다.

엄격한 export 경로에서는 입력 누락을 출력 발행 전에 거부하고 기존 목적지를 보존하는 검증이 필요하다. 모두 검증된 입력에서 매치가 없는 경우의 정상 빈 보고서는 계속 허용해야 한다. permissive 동작을 유지한다면 자동화가 불완전성을 구분할 수 있어야 한다. strict flag나 종료 코드의 최종 선택은 이슈에서 결정할 후속 설계이며 여기서 채택하지 않았다.

## 3. 실행 증거와 한계

아래 검증은 같은 대화의 계획 수립 단계에서 기준 SHA에 실행했다. 등록 단계에서 SHA·작업 트리와 원격 CI 메타데이터를 재확인했다. 문서 작성만을 위해 제품 테스트를 반복하지 않았다.

| 검증 | 결과 및 해석 |
| --- | --- |
| `.venv/bin/python -m pytest -q tests/test_perf_regressions.py tests/test_validation.py tests/test_review_regressions.py` | **154 passed, 1 skipped**, 3.38초. 기존 성능 guard 4개 포함. |
| `.venv/bin/python -m pytest -q -m 'not perf'` | **975 passed, 7 skipped, 4 failed, 4 deselected**, 8.22초. 실패는 노트북 커널 1건과 로컬 HTTP 계약 3건의 포트 사용 제한. |
| `.venv/bin/python -m pytest -q tests/test_notebooks.py tests/test_phoenix_cli_contract.py` — 샌드박스 밖 재실행 | **5 passed**, 9.24초. 위 실패 4건과 노트북의 별도 정적 검사 1건 포함. |
| 기존 원격 tests CI | [38567402957](https://github.com/memtomem/tracegraph/actions/runs/38567402957), `success`, head SHA가 기준 커밋과 정확히 일치. 등록 시 API 재확인. |
| 합성 재현 | R-01 timing, R-02 파일/stdin 비교, R-05 OS 오류 주입, R-06 후보 export 수행. 코드와 기대 출력은 등록 이슈·댓글에 포함. |

위 숫자를 합산해 단일 전체 실행이 성공했다고 표현하지 않는다. skip은 성공으로 바꾸지 않았으며, 기존 CI 성공은 별도의 원격 증거다. 새 실서버 Phoenix·SyncMill E2E, 설치 wheel 재검증, 의존성 취약점 DB 전수 조회, 다른 Python·OS 조합, 전체 경로의 peak RSS 측정은 수행하지 않았다.

## 4. 중복 처리와 후속 작업 경계

- [#46](https://github.com/memtomem/tracegraph/issues/46): tuple Topic encoding에서 실패한 Send task 이름 누락 — 기존 이슈 유지.
- [#47](https://github.com/memtomem/tracegraph/issues/47): 이전 error channel 값이 유지될 때 반복 실패 미기록 — 기존 이슈 유지.
- [#49](https://github.com/memtomem/tracegraph/issues/49): foreign/missing OTLP link 누락 고지 — 기존 이슈 유지.
- [#50](https://github.com/memtomem/tracegraph/issues/50): 디렉터리 입력의 불완전성 — 후보 export 근거만 추가.
- [2026-09-07 리뷰](2026-09-07-implementation-review.md)의 해결된 F-01~F-10을 다시 신규 결함으로 등록하지 않았다. 과거 성능·CI 결과를 현재 검증으로 재사용하지 않았다.

현재 결과물은 결함과 개선 제안의 등록이다. 최종 설계가 합의되지 않아 ADR을 생성하지 않았으며, 필요하면 #53·#54·#50의 설정·호환성 정책 결정 후 제안 ADR로 구체화할 수 있다. 공개 이슈에는 합성 데이터와 이미 공개된 동작 경계만 포함했다.

이 구현 리뷰의 최초 등록 단계에서 저장소 변경은 이 보고서 한 파일이었다. 제품 코드·테스트 수정, 이슈 종료, 커밋·push·PR·배포는 수행하지 않았다. 신규 이슈 5건과 #50 댓글은 GitHub에 실제 등록됐으며, 이 보고서 파일은 이슈 등록 시점에 로컬 미커밋 상태였다.

후속 요청에 따른 서비스 차별화·실사용성 검토와 신규 개선 제안 #57–#60은 [추가 리뷰](2026-10-16-product-usability-review.md)에 별도로 기록했다.
