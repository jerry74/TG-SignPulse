from pathlib import Path

import pytest

from signplus.checkin import (
    CheckInEngine,
    RunCommand,
    Step,
    StepKind,
    TaskDefinition,
)
from signplus.scenario import ScenarioTelegramAdapter


@pytest.mark.asyncio
async def test_user_can_complete_caption_challenge_and_confirm_success() -> None:
    telegram = ScenarioTelegramAdapter.from_path(
        Path(__file__).parent / "scenarios" / "happy_path.json"
    )
    task = TaskDefinition(
        name="daily-check-in",
        chat_id=10001,
        steps=(
            Step(kind=StepKind.SEND_TEXT, value="/checkin"),
            Step(kind=StepKind.CLICK_BUTTON, value="签到"),
            Step(kind=StepKind.SOLVE_CAPTION_ARITHMETIC),
        ),
        success_patterns=(r"签到成功",),
        failure_patterns=(r"签到失败",),
    )

    result = await CheckInEngine(telegram).execute(
        RunCommand(run_id="run-1", account_name="test-account", task=task)
    )

    assert result.success is True
    assert result.code == "SUCCESS_CONFIRMED"
    assert telegram.operations == [
        ("send_text", "/checkin"),
        ("click_button", "签到"),
        ("click_button", "9"),
    ]
    telegram.assert_complete()


@pytest.mark.asyncio
async def test_user_can_confirm_success_from_an_edited_message() -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [{"id": 20, "text": "old", "buttons": []}],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 21, "text": "处理中", "buttons": ["确认"]}],
                },
                {
                    "operation": {"type": "click_button", "value": "确认"},
                    "emit": [{"id": 21, "text": "签到成功", "buttons": []}],
                },
            ],
        }
    )
    task = TaskDefinition(
        name="edited-message",
        chat_id=10002,
        steps=(
            Step(kind=StepKind.SEND_TEXT, value="/checkin"),
            Step(kind=StepKind.CLICK_BUTTON, value="确认"),
        ),
        success_patterns=(r"签到成功",),
        failure_patterns=(r"签到失败",),
    )

    result = await CheckInEngine(telegram).execute(
        RunCommand(run_id="run-edited", account_name="test-account", task=task)
    )

    assert result.success is True
    assert result.code == "SUCCESS_CONFIRMED"
    telegram.assert_complete()


@pytest.mark.asyncio
async def test_transient_telegram_errors_are_retried_at_most_three_attempts() -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "fault": {"type": "transient"},
                },
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "fault": {"type": "transient"},
                },
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [{"id": 1, "text": "签到成功", "buttons": []}],
                },
            ],
        }
    )
    task = TaskDefinition(
        name="retry",
        chat_id=10003,
        steps=(Step(kind=StepKind.SEND_TEXT, value="/checkin"),),
        success_patterns=(r"签到成功",),
        failure_patterns=(r"签到失败",),
    )

    result = await CheckInEngine(telegram).execute(
        RunCommand(run_id="run-retry", account_name="test-account", task=task)
    )

    assert result.success is True
    assert telegram.operations == [("send_text", "/checkin")] * 3


@pytest.mark.asyncio
async def test_failure_rule_has_priority_when_same_message_also_matches_success() -> None:
    telegram = ScenarioTelegramAdapter(
        {"version": 1, "initial_messages": [], "interactions": [{
            "operation": {"type": "send_text", "value": "/checkin"},
            "emit": [{"id": 1, "text": "签到成功，但签到失败", "buttons": []}],
        }]}
    )
    task = TaskDefinition(
        name="priority", chat_id=1,
        steps=(Step(StepKind.SEND_TEXT, "/checkin"),),
        success_patterns=("签到成功",), failure_patterns=("签到失败",),
    )
    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))
    assert (result.success, result.code) == (False, "FAILURE_CONFIRMED")


@pytest.mark.asyncio
async def test_unauthorized_terminates_without_retry() -> None:
    telegram = ScenarioTelegramAdapter(
        {"version": 1, "initial_messages": [], "interactions": [{
            "operation": {"type": "send_text", "value": "/checkin"},
            "fault": {"type": "unauthorized"},
        }]}
    )
    task = TaskDefinition(
        name="unauthorized", chat_id=1,
        steps=(Step(StepKind.SEND_TEXT, "/checkin"),),
        success_patterns=("ok",), failure_patterns=("failed",),
    )
    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))
    assert result.code == "ACCOUNT_UNAUTHORIZED"
    assert telegram.operations == [("send_text", "/checkin")]
