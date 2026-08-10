import pytest

from signplus.challenge import CaptionArithmeticSolver, ChallengeError


def test_overlong_caption_expression_is_rejected_before_evaluation() -> None:
    expression = "+".join("1" for _ in range(70))

    with pytest.raises(ChallengeError, match="CHALLENGE_EXPRESSION_TOO_LONG"):
        CaptionArithmeticSolver().solve(f"{expression} = ?", ("70",))


@pytest.mark.parametrize(
    ("expression", "button"),
    [
        ("1 + 2 * 3", "7"),
        ("(8 - 2) / 3", "2.0"),
        ("-4 + 1.5", "-2.5"),
        ("0.1 + 0.2", "0.3"),
    ],
)
def test_arithmetic_is_normalized_and_selects_one_exact_button(
    expression: str, button: str
) -> None:
    answer = CaptionArithmeticSolver().solve(f"請計算 {expression} = ?", ("0", button))
    assert answer.button == button


@pytest.mark.parametrize(
    ("caption", "buttons"),
    [
        ("__import__('os').system('echo unsafe') = ?", ("0",)),
        ("1 / 0 = ?", ("0",)),
        ("1 + 1 = ?", ("2", "2.0")),
        ("1 + 1 = ?", ("3",)),
        ("5 // 2 = ?", ("2",)),
        ("5 % 2 = ?", ("1",)),
    ],
)
def test_uncertain_or_unsafe_challenge_never_returns_a_button(
    caption: str, buttons: tuple[str, ...]
) -> None:
    with pytest.raises(ChallengeError):
        CaptionArithmeticSolver().solve(caption, buttons)
