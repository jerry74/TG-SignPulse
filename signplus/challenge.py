from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ChallengeError(ValueError):
    """The challenge cannot be solved without guessing."""


@dataclass(frozen=True)
class ChallengeAnswer:
    expression: str
    value: str
    button: str


_OPERATORS = {
    ast.Add: lambda left, right: left + right,
    ast.Sub: lambda left, right: left - right,
    ast.Mult: lambda left, right: left * right,
    ast.Div: lambda left, right: left / right,
}
_EXPRESSION = re.compile(r"(?P<expr>[0-9\s()+\-*/.]+)\s*=\s*\?")


class CaptionArithmeticSolver:
    """Solve a bounded arithmetic expression and choose one exact button."""

    def solve(self, caption: str, buttons: tuple[str, ...]) -> ChallengeAnswer:
        match = _EXPRESSION.search(caption)
        if match is None:
            raise ChallengeError("CHALLENGE_EXPRESSION_NOT_FOUND")
        expression = match.group("expr").strip()
        value = self._evaluate(expression)
        normalized = self._normalize(value)
        matches = [button for button in buttons if self._normalize_text(button) == normalized]
        if len(matches) != 1:
            raise ChallengeError("CHALLENGE_ANSWER_NOT_UNIQUE")
        return ChallengeAnswer(expression=expression, value=normalized, button=matches[0])

    def _evaluate(self, expression: str) -> Decimal:
        if len(expression) > 128:
            raise ChallengeError("CHALLENGE_EXPRESSION_TOO_LONG")
        try:
            node = ast.parse(expression, mode="eval")
            return self._walk(node)
        except (SyntaxError, ValueError, TypeError, ZeroDivisionError, InvalidOperation):
            raise ChallengeError("CHALLENGE_EXPRESSION_INVALID") from None

    def _walk(self, node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return self._walk(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return Decimal(str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = self._walk(node.operand)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
            left = self._walk(node.left)
            right = self._walk(node.right)
            return _OPERATORS[type(node.op)](left, right)
        raise ChallengeError("CHALLENGE_EXPRESSION_UNSAFE")

    @staticmethod
    def _normalize(value: Decimal) -> str:
        if not value.is_finite():
            raise ChallengeError("CHALLENGE_RESULT_INVALID")
        normalized = value.normalize()
        return format(normalized, "f")

    @staticmethod
    def _normalize_text(value: str) -> str:
        try:
            return CaptionArithmeticSolver._normalize(Decimal(value.strip()))
        except InvalidOperation:
            return value.strip()
