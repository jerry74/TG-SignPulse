import pytest

from signplus.scenario import (
    ScenarioMismatch,
    ScenarioTelegramAdapter,
    TelegramTimeout,
    TelegramTransientError,
)


def test_scenario_rejects_secret_like_fields() -> None:
    with pytest.raises(ValueError, match="secret-like field"):
        ScenarioTelegramAdapter(
            {"version": 1, "token": "must-not-be-stored", "interactions": []}
        )


@pytest.mark.asyncio
async def test_scenario_mismatch_reports_expected_and_actual_operation() -> None:
    adapter = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [{
                "operation": {"type": "send_text", "value": "/expected"}
            }],
        }
    )

    with pytest.raises(ScenarioMismatch, match="expected.*got"):
        await adapter.send_text(1, "/actual")


@pytest.mark.asyncio
async def test_duplicate_and_out_of_order_events_do_not_cross_the_cursor() -> None:
    adapter = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [{"id": 10, "text": "old"}],
            "interactions": [{
                "operation": {"type": "send_text", "value": "/start"},
                "emit": [
                    {"id": 10, "text": "old"},
                    {"id": 9, "text": "older"},
                    {"id": 11, "text": "new"},
                ],
            }],
        }
    )
    baseline = await adapter.latest_message(1)
    await adapter.send_text(1, "/start")

    message = await adapter.wait_for_message(1, baseline, 30)

    assert message.id == 11


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "exception"),
    [("disconnect", TelegramTransientError), ("timeout", TelegramTimeout)],
)
async def test_versioned_faults_are_explicit(
    fault: str, exception: type[Exception]
) -> None:
    adapter = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [{
                "operation": {"type": "send_text", "value": "/start"},
                "fault": {"type": fault},
            }],
        }
    )

    with pytest.raises(exception):
        await adapter.send_text(1, "/start")


@pytest.mark.asyncio
async def test_unknown_fault_cannot_silently_pass() -> None:
    adapter = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [{
                "operation": {"type": "send_text", "value": "/start"},
                "fault": {"type": "made_up"},
            }],
        }
    )

    with pytest.raises(ScenarioMismatch, match="unsupported fault"):
        await adapter.send_text(1, "/start")
