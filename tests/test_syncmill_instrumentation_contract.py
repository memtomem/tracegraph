"""Executable producer-side contract for optional, fail-open SyncMill telemetry."""

from syncmill_instrumentation_contract import BestEffortTelemetry, run_with_telemetry


def test_disabled_instrumentation_is_a_noop():
    called = False

    def exporter(document):
        nonlocal called
        called = True

    result = run_with_telemetry(
        lambda: "business-result",
        BestEffortTelemetry(enabled=False, exporter=exporter),
        {"resourceSpans": []},
    )
    assert result == "business-result"
    assert called is False


def test_exporter_failure_never_replaces_business_result():
    def broken_exporter(document):
        raise OSError("collector unavailable")

    telemetry = BestEffortTelemetry(enabled=True, exporter=broken_exporter)
    assert telemetry.emit({"resourceSpans": []}) is False
    assert run_with_telemetry(lambda: 42, telemetry, {"resourceSpans": []}) == 42


def test_successful_export_is_reported_without_mutating_document():
    seen = []
    document = {"resourceSpans": []}
    telemetry = BestEffortTelemetry(enabled=True, exporter=seen.append)
    assert telemetry.emit(document) is True
    assert seen == [document]
