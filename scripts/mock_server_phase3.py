"""Phase-3 mock server: returns review or falsify output by schema name.

Run three instances on ports 8901/8902/8903 to simulate three vendors.
Each port returns the same finding (so they cluster); falsify (schema contains
'Falsify') returns a confirmed verdict so the finding survives as consensus.
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

REVIEW = {
    "summary": "mock review",
    "findings": [
        {
            "lens": "correctness",
            "category": "boundary",
            "severity": "P1",
            "claim": "空列表取首元素导致崩溃",
            "detail": "当 x 为空时，x[0] 抛 IndexError，接口返回 500。",
            "anchor": {"file": "m.py", "start_line": 3, "end_line": 3, "snippet": "    return x[0]\n"},
            "suggestion": {
                "diff": None,
                "explanation": "取首元素前先判空",
                "acceptance": "python -c 'import m; m.foo()' 不抛 IndexError",
            },
        }
    ],
}

FALSIFY_CONFIRM = {
    "verdict": "confirmed",
    "reason": "代码确实未判空，x=[] 会抛 IndexError",
    "counterexample": None,
    "broken_assumption": None,
}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        payload = json.loads(raw)
        # Detect schema by the response_format json_schema name.
        schema_name = ""
        rf = payload.get("response_format", {})
        if isinstance(rf, dict):
            schema_name = rf.get("json_schema", {}).get("name", "")
        result = FALSIFY_CONFIRM if "falsif" in schema_name.lower() else REVIEW
        body = {
            "choices": [
                {
                    "message": {"content": json.dumps(result, ensure_ascii=False)},
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
