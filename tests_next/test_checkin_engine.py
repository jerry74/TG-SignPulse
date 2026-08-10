from pathlib import Path

import pytest

from signplus.checkin import (
    CheckInEngine,
    RunCommand,
    Step,
    StepKind,
    TaskDefinition,
)
from signplus.scenario import (
    ScenarioTelegramAdapter,
    TelegramTransientError,
    TelegramUnauthorizedError,
)


class BaselineFaultAdapter:
    def __init__(self, delegate: ScenarioTelegramAdapter, faults: list[Exception]) -> None:
        self.delegate = delegate
        self.faults = faults
        self.latest_calls = 0

    async def latest_message(self, chat_id: int | str, thread_id: int | None = None):
        self.latest_calls += 1
        if self.faults:
            raise self.faults.pop(0)
        return await self.delegate.latest_message(chat_id, thread_id)

    async def send_text(self, *args: object, **kwargs: object) -> None:
        await self.delegate.send_text(*args, **kwargs)

    async def send_dice(self, *args: object, **kwargs: object) -> None:
        await self.delegate.send_dice(*args, **kwargs)

    async def click_button(self, *args: object, **kwargs: object) -> None:
        await self.delegate.click_button(*args, **kwargs)

    async def wait_for_message(self, *args: object, **kwargs: object):
        return await self.delegate.wait_for_message(*args, **kwargs)


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
    challenge_event = next(
        event for event in result.events if event["type"] == "challenge_solved"
    )
    assert challenge_event["candidate_count"] == 3
    assert len(challenge_event["candidate_summary"]) == 3
    assert challenge_event["candidate_summary"] != ("8", "9", "10")
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
    result = await CheckInEngine(telegram, clock=telegram.clock).execute(
        RunCommand("r", "a", task)
    )
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


@pytest.mark.asyncio
async def test_baseline_cursor_transient_errors_are_retried_before_any_send() -> None:
    scenario = ScenarioTelegramAdapter(
        {"version": 1, "initial_messages": [], "interactions": [{
            "operation": {"type": "send_text", "value": "/checkin"},
            "emit": [{"id": 1, "text": "success", "buttons": []}],
        }]}
    )
    telegram = BaselineFaultAdapter(
        scenario,
        [TelegramTransientError("disconnect"), TelegramTransientError("disconnect")],
    )
    task = TaskDefinition(
        "baseline-retry", 1, (Step(StepKind.SEND_TEXT, "/checkin"),),
        ("success",), ("failed",),
    )

    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))

    assert result.code == "SUCCESS_CONFIRMED"
    assert telegram.latest_calls == 3
    assert scenario.operations == [("send_text", "/checkin")]


@pytest.mark.asyncio
async def test_baseline_cursor_unauthorized_is_a_terminal_result_without_send() -> None:
    scenario = ScenarioTelegramAdapter(
        {"version": 1, "initial_messages": [], "interactions": []}
    )
    telegram = BaselineFaultAdapter(
        scenario, [TelegramUnauthorizedError("revoked")]
    )
    task = TaskDefinition(
        "baseline-unauthorized", 1, (Step(StepKind.SEND_TEXT, "/checkin"),),
        ("success",), ("failed",),
    )

    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))

    assert result.code == "ACCOUNT_UNAUTHORIZED"
    assert telegram.latest_calls == 1
    assert scenario.operations == []


@pytest.mark.asyncio
async def test_caption_buttons_may_arrive_in_a_later_message_edit() -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/checkin"},
                    "emit": [
                        {"id": 1, "caption": "1 + 1 = ?", "buttons": []},
                        {
                            "id": 1,
                            "caption": "1 + 1 = ?",
                            "buttons": ["2", "3"],
                            "delay_seconds": 5,
                        },
                    ],
                },
                {
                    "operation": {"type": "click_button", "value": "2"},
                    "emit": [{"id": 2, "text": "success", "buttons": []}],
                },
            ],
        }
    )
    task = TaskDefinition(
        "late-buttons", 1,
        (
            Step(StepKind.SEND_TEXT, "/checkin"),
            Step(StepKind.SOLVE_CAPTION_ARITHMETIC),
        ),
        ("success",), ("failed",),
    )

    result = await CheckInEngine(telegram, clock=telegram.clock).execute(
        RunCommand("r", "a", task)
    )

    assert result.code == "SUCCESS_CONFIRMED"
    assert telegram.operations[-1] == ("click_button", "2")
    assert telegram.clock.monotonic() == 5
    telegram.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("match_mode", "pattern", "buttons", "clicked"),
    [
        ("exact", "Check in", ["Check", "Check in"], "Check in"),
        ("regex", r"^Check\s+in$", ["Other", "Check in"], "Check in"),
    ],
)
async def test_button_matching_is_unique_and_explicit(
    match_mode: str, pattern: str, buttons: list[str], clicked: str
) -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_text", "value": "/start"},
                    "emit": [{"id": 1, "text": "choose", "buttons": buttons}],
                },
                {
                    "operation": {"type": "click_button", "value": clicked},
                    "emit": [{"id": 2, "text": "success", "buttons": []}],
                },
            ],
        }
    )
    task = TaskDefinition(
        "button", 1,
        (
            Step(StepKind.SEND_TEXT, "/start"),
            Step(StepKind.CLICK_BUTTON, pattern, match_mode=match_mode),
        ),
        ("success",), ("failed",),
    )

    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))

    assert result.code == "SUCCESS_CONFIRMED"
    telegram.assert_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "task", "expected_code"),
    [
        (
            {
                "version": 1,
                "initial_messages": [],
                "interactions": [{
                    "operation": {"type": "send_text", "value": "/start"},
                    "emit": [{"id": 1, "text": "choose", "buttons": ["Other"]}],
                }],
            },
            TaskDefinition(
                "missing-button", 1,
                (
                    Step(StepKind.SEND_TEXT, "/start"),
                    Step(StepKind.CLICK_BUTTON, "Check in"),
                ),
                ("success",), ("failed",),
            ),
            "BUTTON_NOT_FOUND",
        ),
        (
            {
                "version": 1,
                "initial_messages": [],
                "interactions": [{
                    "operation": {"type": "send_text", "value": "/start"},
                    "emit": [],
                }],
            },
            TaskDefinition(
                "timeout", 1, (Step(StepKind.SEND_TEXT, "/start"),),
                ("success",), ("failed",),
            ),
            "TELEGRAM_TIMEOUT",
        ),
        (
            {
                "version": 1,
                "initial_messages": [],
                "interactions": [{
                    "operation": {"type": "send_text", "value": "/start"},
                    "emit": [{"id": 1, "text": "success", "thread_id": 22}],
                }],
            },
            TaskDefinition(
                "wrong-topic", 1, (Step(StepKind.SEND_TEXT, "/start"),),
                ("success",), ("failed",), thread_id=11,
            ),
            "TELEGRAM_TIMEOUT",
        ),
        (
            {
                "version": 1,
                "initial_messages": [],
                "interactions": [{
                    "operation": {"type": "send_text", "value": "/start"},
                    "emit": [{"id": 1, "text": "still processing"}],
                }],
            },
            TaskDefinition(
                "unconfirmed", 1, (Step(StepKind.SEND_TEXT, "/start"),),
                ("success",), ("failed",),
            ),
            "SUCCESS_NOT_CONFIRMED",
        ),
    ],
)
async def test_explicit_failure_outcomes(
    scenario: dict[str, object], task: TaskDefinition, expected_code: str
) -> None:
    telegram = ScenarioTelegramAdapter(scenario)

    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))

    assert result.code == expected_code
    assert result.success is False
    telegram.assert_complete()


@pytest.mark.asyncio
async def test_dice_action_and_flood_wait_retry_are_recorded() -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [],
            "interactions": [
                {
                    "operation": {"type": "send_dice", "value": "🎯"},
                    "fault": {"type": "flood_wait", "seconds": 1},
                },
                {
                    "operation": {"type": "send_dice", "value": "🎯"},
                    "emit": [{"id": 1, "text": "success"}],
                },
            ],
        }
    )
    task = TaskDefinition(
        "dice", 1, (Step(StepKind.SEND_DICE, "🎯"),),
        ("success",), ("failed",),
    )

    result = await CheckInEngine(telegram, clock=telegram.clock).execute(
        RunCommand("r", "a", task)
    )

    assert result.code == "SUCCESS_CONFIRMED"
    assert telegram.operations == [("send_dice", "🎯")] * 2
    assert telegram.clock.monotonic() == 1
    assert any(event.get("reason") == "flood_wait" for event in result.events)


@pytest.mark.asyncio
async def test_recovery_can_click_the_existing_latest_message_without_resending() -> None:
    telegram = ScenarioTelegramAdapter(
        {
            "version": 1,
            "initial_messages": [
                {"id": 10, "text": "choose", "buttons": ["✅ 簽到", "說明"]}
            ],
            "interactions": [
                {
                    "operation": {"type": "click_button", "value": "✅ 簽到"},
                    "emit": [{
                        "id": 11,
                        "caption": "2 + 3 = ?",
                        "buttons": ["4", "5", "6"],
                    }],
                },
                {
                    "operation": {"type": "click_button", "value": "5"},
                    "emit": [{"id": 12, "text": "success"}],
                },
            ],
        }
    )
    task = TaskDefinition(
        "continue-existing",
        1,
        (
            Step(StepKind.CLICK_BUTTON, "签到|簽到", match_mode="regex"),
            Step(StepKind.SOLVE_CAPTION_ARITHMETIC),
        ),
        ("success",),
        ("failed",),
    )

    result = await CheckInEngine(telegram).execute(RunCommand("r", "a", task))

    assert result.code == "SUCCESS_CONFIRMED"
    assert telegram.operations == [
        ("click_button", "✅ 簽到"),
        ("click_button", "5"),
    ]
    telegram.assert_complete()
