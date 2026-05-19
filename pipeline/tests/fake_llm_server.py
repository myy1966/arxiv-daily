#!/usr/bin/env python3
"""
fake_llm_server.py - A fake OpenAI-compatible LLM API server for testing arxiv_daily.py.

Simulates various error conditions to test the robustness of the client.

Usage:
    python fake_llm_server.py --port 8080
    python fake_llm_server.py --port 8080 --error-mode http_500
    python fake_llm_server.py --port 8080 --error-sequence http_500,http_500,none
    python fake_llm_server.py --port 8080 --error-mode invalid_json --error-rate 0.3
"""

import argparse
import json
import random
import re
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# ---------------------------------------------------------------------------
# Response generation helpers
# ---------------------------------------------------------------------------

def extract_paper_ids(messages: list[dict]) -> list[str]:
    """Extract paper IDs from the user message in the prompt."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            return re.findall(r"ID:\s*(\d+\.\d+)", content)
    return []


def generate_normal_response(paper_ids: list[str]) -> dict:
    """Generate a well-formed response matching arxiv_daily.py expectations."""
    papers = []
    for pid in paper_ids:
        papers.append({
            "id": pid,
            "summary": f"这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升。",
            "tags": ["LLM Reasoning", "AI Evaluation"],
            "venue": None,
        })
    return {"papers": papers, "new_tags": ["AI Evaluation"]}


def make_sse_chunks(content_json: str, model: str = "fake-model") -> list[str]:
    """Split a JSON string into SSE chunks simulating streaming output."""
    chunk_size = 64
    parts = [content_json[i:i + chunk_size] for i in range(0, len(content_json), chunk_size)]

    lines = []
    for i, part in enumerate(parts):
        chunk = {
            "id": f"chatcmpl-fake-{i}",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": part},
                "finish_reason": None,
            }],
        }
        lines.append(f"data: {json.dumps(chunk)}\n\n")

    # Final chunk with finish_reason
    final_chunk = {
        "id": f"chatcmpl-fake-{len(parts)}",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }
    lines.append(f"data: {json.dumps(final_chunk)}\n\n")

    # Usage chunk
    prompt_tokens = len(content_json) // 4
    completion_tokens = len(content_json) // 2
    usage_chunk = {
        "id": f"chatcmpl-fake-usage",
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    lines.append(f"data: {json.dumps(usage_chunk)}\n\n")
    lines.append("data: [DONE]\n\n")
    return lines


# ---------------------------------------------------------------------------
# Error mode handlers
# ---------------------------------------------------------------------------

def handle_none(handler, paper_ids, model):
    """Normal response."""
    response = generate_normal_response(paper_ids)
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_http_500(handler, paper_ids, model):
    """Return 500 Internal Server Error."""
    handler.send_response(500)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    body = json.dumps({"error": {"message": "Internal server error", "type": "server_error"}})
    handler.wfile.write(body.encode("utf-8"))


def handle_http_400(handler, paper_ids, model):
    """Return 400 Bad Request."""
    handler.send_response(400)
    handler.send_header("Content-Type", "application/json")
    handler.end_headers()
    body = json.dumps({"error": {"message": "Bad request", "type": "invalid_request_error"}})
    handler.wfile.write(body.encode("utf-8"))


def handle_http_429(handler, paper_ids, model):
    """Return 429 Too Many Requests with Retry-After header."""
    handler.send_response(429)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Retry-After", type(handler).retry_after_header)
    handler.end_headers()
    body = json.dumps({"error": {"message": "Rate limit exceeded", "type": "rate_limit_error"}})
    handler.wfile.write(body.encode("utf-8"))


def handle_timeout(handler, paper_ids, model):
    """Accept connection but never respond (hang until client times out)."""
    try:
        time.sleep(type(handler).timeout_sleep)
    except (BrokenPipeError, ConnectionResetError):
        pass


def handle_connection_reset(handler, paper_ids, model):
    """Start streaming then abruptly close the connection mid-stream."""
    response = generate_normal_response(paper_ids)
    content_json = json.dumps(response, ensure_ascii=False)
    chunks = make_sse_chunks(content_json, model)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    # Send only first few chunks then kill the connection
    send_count = max(1, len(chunks) // 3)
    for chunk in chunks[:send_count]:
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()
    # Force close
    handler.connection.close()


def handle_invalid_json(handler, paper_ids, model):
    """Return content that is not valid JSON."""
    invalid_content = '{"papers": [{"id": "' + (paper_ids[0] if paper_ids else "0000.00000") + '", "summary": "truncated...'
    _send_sse_response(handler, invalid_content, model)


def handle_partial_stream(handler, paper_ids, model):
    """Send SSE stream that stops partway through without [DONE]."""
    response = generate_normal_response(paper_ids)
    content_json = json.dumps(response, ensure_ascii=False)
    chunks = make_sse_chunks(content_json, model)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    # Send only content chunks, skip usage and [DONE]
    content_halfway = len(content_json) // 2
    partial_json = content_json[:content_halfway]
    partial_chunks = make_sse_chunks(partial_json, model)
    # Send all but the last two lines (usage + [DONE])
    for chunk in partial_chunks[:-2]:
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()


def handle_wrong_ids(handler, paper_ids, model):
    """Return papers with completely wrong (hallucinated) IDs."""
    papers = []
    for i, pid in enumerate(paper_ids):
        fake_id = f"9999.{99999 - i:05d}"
        papers.append({
            "id": fake_id,
            "summary": f"这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升。",
            "tags": ["LLM Reasoning", "Diffusion Models"],
            "venue": None,
        })
    response = {"papers": papers, "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_duplicate_ids(handler, paper_ids, model):
    """Return papers with duplicate ID entries."""
    papers = []
    for pid in paper_ids:
        papers.append({
            "id": pid,
            "summary": f"这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升。",
            "tags": ["LLM Reasoning", "Diffusion Models"],
            "venue": None,
        })
    # Duplicate the first paper
    if papers:
        papers.append(dict(papers[0]))
    response = {"papers": papers, "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_missing_fields(handler, paper_ids, model):
    """Return papers with missing tags and/or summary too short."""
    papers = []
    for i, pid in enumerate(paper_ids):
        if i % 2 == 0:
            # Missing tags
            papers.append({
                "id": pid,
                "summary": f"这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升。",
                "tags": [],
                "venue": None,
            })
        else:
            # Summary too short (< 50 chars)
            papers.append({
                "id": pid,
                "summary": "简短摘要",
                "tags": ["LLM"],
                "venue": None,
            })
    response = {"papers": papers, "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_no_period(handler, paper_ids, model):
    """Return papers whose summary does not end with a period/full stop."""
    papers = []
    for pid in paper_ids:
        papers.append({
            "id": pid,
            "summary": "这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升",
            "tags": ["LLM Reasoning", "Diffusion Models"],
            "venue": None,
        })
    response = {"papers": papers, "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_empty_papers(handler, paper_ids, model):
    """Return an empty papers list."""
    response = {"papers": [], "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_schema_missing_papers(handler, paper_ids, model):
    """Return a JSON object without the required papers list."""
    response = {"foo": "bar", "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_schema_bad_new_tags(handler, paper_ids, model):
    """Return a response with invalid new_tags type."""
    response = {"papers": [], "new_tags": "not a list"}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_markdown_json(handler, paper_ids, model):
    """Return JSON wrapped in markdown code fences."""
    response = generate_normal_response(paper_ids)
    inner_json = json.dumps(response, ensure_ascii=False)
    content = f"```json\n{inner_json}\n```"
    _send_sse_response(handler, content, model)


def handle_slow_stream(handler, paper_ids, model):
    """Stream response with long delays between chunks."""
    response = generate_normal_response(paper_ids)
    content_json = json.dumps(response, ensure_ascii=False)
    chunks = make_sse_chunks(content_json, model)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    for chunk in chunks:
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()
        time.sleep(type(handler).stream_delay)


def handle_reasoning_loop(handler, paper_ids, model, repeat_count: int = 20):
    """Simulate reasoning_content infinite loop (e.g., glm-5 token exhaustion)."""
    model = "fake-reasoning-model"
    req_id = "fake-reasoning-loop-001"
    loop_line = "Let me assume the ID is `2605.17108`? No.\n"

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    for _ in range(repeat_count):
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {
                    "content": None,
                    "reasoning_content": loop_line,
                },
                "flag": 0,
            }],
        }
        handler.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        handler.wfile.flush()

    final = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": ""},
            "finish_reason": "stop",
            "flag": 0,
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": repeat_count,
            "total_tokens": 100 + repeat_count,
        },
    }
    handler.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def handle_content_loop(handler, paper_ids, model, repeat_count: int = 20):
    """Simulate content field infinite loop."""
    model = "fake-content-loop-model"
    req_id = "fake-content-loop-001"
    loop_line = '"summary": "这是一段重复的摘要内容，模型陷入了循环。"\n'

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    for _ in range(repeat_count):
        chunk = {
            "id": req_id,
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{
                "index": 0,
                "delta": {"content": loop_line},
                "flag": 0,
            }],
        }
        handler.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        handler.wfile.flush()

    final = {
        "id": req_id,
        "object": "chat.completion.chunk",
        "model": model,
        "choices": [{
            "index": 0,
            "delta": {"content": ""},
            "finish_reason": "stop",
            "flag": 0,
        }],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": repeat_count,
            "total_tokens": 100 + repeat_count,
        },
    }
    handler.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
    handler.wfile.write(b"data: [DONE]\n\n")
    handler.wfile.flush()


def handle_versioned_ids(handler, paper_ids, model):
    """Return paper IDs with version suffixes (e.g., 2501.12345v2)."""
    papers = []
    for pid in paper_ids:
        versioned_id = f"{pid}v{random.randint(1, 5)}"
        papers.append({
            "id": versioned_id,
            "summary": f"这是一篇关于人工智能领域的研究论文，提出了一种新的方法来解决现有技术中的关键问题，并在多个基准数据集上取得了显著的性能提升。",
            "tags": ["LLM Reasoning", "Diffusion Models"],
            "venue": None,
        })
    response = {"papers": papers, "new_tags": []}
    content_json = json.dumps(response, ensure_ascii=False)
    _send_sse_response(handler, content_json, model)


def handle_malformed_sse(handler, paper_ids, model):
    """Send an SSE stream where one data line contains invalid JSON."""
    response = generate_normal_response(paper_ids)
    content_json = json.dumps(response, ensure_ascii=False)
    chunks = make_sse_chunks(content_json, model)

    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()

    inject_at = max(1, len(chunks) // 2)
    for i, chunk in enumerate(chunks):
        if i == inject_at:
            # Inject a malformed SSE data line
            handler.wfile.write(b"data: {invalid json here\n\n")
            handler.wfile.flush()
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()


def _send_sse_response(handler, content: str, model: str):
    """Helper: send a complete SSE streaming response."""
    chunks = make_sse_chunks(content, model)
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.end_headers()
    for chunk in chunks:
        handler.wfile.write(chunk.encode("utf-8"))
        handler.wfile.flush()


# ---------------------------------------------------------------------------
# Error mode registry
# ---------------------------------------------------------------------------

ERROR_HANDLERS = {
    "none": handle_none,
    "http_500": handle_http_500,
    "http_400": handle_http_400,
    "http_429": handle_http_429,
    "timeout": handle_timeout,
    "connection_reset": handle_connection_reset,
    "invalid_json": handle_invalid_json,
    "partial_stream": handle_partial_stream,
    "wrong_ids": handle_wrong_ids,
    "duplicate_ids": handle_duplicate_ids,
    "missing_fields": handle_missing_fields,
    "no_period": handle_no_period,
    "empty_papers": handle_empty_papers,
    "schema_missing_papers": handle_schema_missing_papers,
    "schema_bad_new_tags": handle_schema_bad_new_tags,
    "markdown_json": handle_markdown_json,
    "slow_stream": handle_slow_stream,
    "reasoning_loop": handle_reasoning_loop,
    "content_loop": handle_content_loop,
    "versioned_ids": handle_versioned_ids,
    "malformed_sse": handle_malformed_sse,
}


# ---------------------------------------------------------------------------
# HTTP Request Handler
# ---------------------------------------------------------------------------

class FakeLLMHandler(BaseHTTPRequestHandler):
    # Class-level config set by main()
    error_mode: str = "none"
    error_sequence: list[str] = []
    error_rate: float = 0.0
    request_counter: int = 0
    counter_lock = threading.Lock()
    timeout_sleep: float = 600
    stream_delay: float = 2
    retry_after_header: str = "2"

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode("utf-8"))
            return

        # Read request body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode("utf-8"))
            return

        messages = payload.get("messages", [])
        model = payload.get("model", "fake-model")
        paper_ids = extract_paper_ids(messages)

        # Determine error mode for this request
        mode = self._resolve_error_mode()

        self.log_message("mode=%s papers=%d ids=%s", mode, len(paper_ids),
                         ",".join(paper_ids[:5]) + ("..." if len(paper_ids) > 5 else ""))

        handler_fn = ERROR_HANDLERS.get(mode)
        if handler_fn is None:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Unknown error mode: {mode}"}).encode("utf-8"))
            return

        try:
            handler_fn(self, paper_ids, model)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _resolve_error_mode(self) -> str:
        """Determine which error mode to use for the current request."""
        cls = type(self)

        # Priority 1: request header
        header_mode = self.headers.get("X-Fake-Error")
        if header_mode and header_mode in ERROR_HANDLERS:
            return header_mode

        # Priority 2: sequence mode
        with cls.counter_lock:
            idx = cls.request_counter
            cls.request_counter += 1

        if cls.error_sequence:
            seq = cls.error_sequence
            if idx < len(seq):
                return seq[idx]
            # After sequence exhausted, fall through to default/rate

        # Priority 3: rate-based random error
        if cls.error_rate > 0 and random.random() < cls.error_rate:
            return cls.error_mode

        # Priority 4: global error mode (only if no rate is set)
        if cls.error_rate == 0:
            return cls.error_mode

        return "none"

    def log_message(self, format, *args):
        """Override to add a prefix."""
        sys.stderr.write(f"[FakeLLM] {self.address_string()} - {format % args}\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fake OpenAI-compatible LLM API server for testing arxiv_daily.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Available error modes: {', '.join(sorted(ERROR_HANDLERS.keys()))}",
    )
    parser.add_argument("--port", type=int, default=8080, help="Port to listen on (default: 8080)")
    parser.add_argument("--error-mode", default="none", choices=sorted(ERROR_HANDLERS.keys()),
                        help="Global error mode (default: none)")
    parser.add_argument("--error-sequence", default="",
                        help="Comma-separated sequence of error modes applied to successive requests")
    parser.add_argument("--error-rate", type=float, default=0.0,
                        help="Probability (0.0-1.0) of triggering --error-mode per request (default: 0.0)")
    args = parser.parse_args()

    # Validate sequence
    sequence = []
    if args.error_sequence:
        for mode in args.error_sequence.split(","):
            mode = mode.strip()
            if mode not in ERROR_HANDLERS:
                print(f"Error: unknown mode '{mode}' in --error-sequence", file=sys.stderr)
                print(f"Available modes: {', '.join(sorted(ERROR_HANDLERS.keys()))}", file=sys.stderr)
                sys.exit(1)
            sequence.append(mode)

    if not 0.0 <= args.error_rate <= 1.0:
        print("Error: --error-rate must be between 0.0 and 1.0", file=sys.stderr)
        sys.exit(1)

    # Configure handler class
    FakeLLMHandler.error_mode = args.error_mode
    FakeLLMHandler.error_sequence = sequence
    FakeLLMHandler.error_rate = args.error_rate
    FakeLLMHandler.request_counter = 0

    server = HTTPServer(("0.0.0.0", args.port), FakeLLMHandler)
    print(f"Fake LLM server listening on http://0.0.0.0:{args.port}", file=sys.stderr)
    print(f"  error-mode: {args.error_mode}", file=sys.stderr)
    if sequence:
        print(f"  error-sequence: {','.join(sequence)}", file=sys.stderr)
    if args.error_rate > 0:
        print(f"  error-rate: {args.error_rate}", file=sys.stderr)
    print(f"  Use: ARXIV_UPDATE_API=http://localhost:{args.port}/v1", file=sys.stderr)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
        server.server_close()


if __name__ == "__main__":
    main()
