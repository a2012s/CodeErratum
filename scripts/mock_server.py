"""Mock OpenAI-compatible server for local E2E verification (not shipped in package)."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

VALID = {
    "summary": "mock review",
    "findings": [
        {
            "lens": "correctness",
            "category": "boundary",
            "severity": "P1",
            "claim": "空列表取首元素导致崩溃",
            "detail": "当 items 为空时，items[0] 抛 IndexError，接口返回 500。",
            "anchor": {
                "file": "m.py",
                "start_line": 3,
                "end_line": 3,
                "snippet": "    return x[0]\n",
            },
            "suggestion": {
                "diff": None,
                "explanation": "取首元素前先判空或提供默认值",
                "acceptance": "python -c 'import m; m.foo()' 不抛 IndexError",
            },
        }
    ],
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        body = {
            "choices": [
                {
                    "message": {"content": json.dumps(VALID, ensure_ascii=False)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8899), Handler).serve_forever()
