from __future__ import annotations

import ast
import json
import operator
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.USub: operator.neg,
}


def safe_eval(expr: str) -> float:
    node = ast.parse(expr, mode="eval")

    def walk(n):
        if isinstance(n, ast.Expression):
            return walk(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in OPS:
            return OPS[type(n.op)](walk(n.left), walk(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in OPS:
            return OPS[type(n.op)](walk(n.operand))
        raise ValueError("unsupported expression")

    return walk(node)


def extract_text(messages: list[dict]) -> str:
    chunks: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    chunks.append(str(item.get("text") or ""))
    return "\n".join(chunks)


def solve(payload: dict) -> str:
    text = extract_text(payload.get("messages") or [])
    expr_match = re.search(r"(-?\d+(?:\s*[-+*/]\s*-?\d+)+)\s*=\s*\?", text)
    if not expr_match:
        return json.dumps({"options": [1], "reason": "fallback"}, ensure_ascii=False)

    value = safe_eval(expr_match.group(1))
    answer = str(int(value)) if float(value).is_integer() else str(value)

    options = re.findall(r"\[\s*(\d+)\s*,\s*\"([^\"]+)\"\s*\]", text)
    for idx, label in options:
        if label.strip() == answer:
            return json.dumps(
                {
                    "options": [int(idx)],
                    "reason": f"{expr_match.group(1)}={answer}",
                },
                ensure_ascii=False,
            )

    return answer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        length = int(self.headers.get("content-length") or "0")
        payload = json.loads(self.rfile.read(length) or b"{}")
        content = solve(payload)
        body = {
            "id": "local-math-solver",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
