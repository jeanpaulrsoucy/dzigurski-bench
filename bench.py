#!/usr/bin/env python3
import argparse
import base64
import csv
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
API = "https://openrouter.ai/api/v1/chat/completions"
PROMPT = "What film is this image from?"
MAX_TOOL_CALLS = 5
MAX_ROUNDS = 6
MODELS = [
    "deepseek/deepseek-v4.1-flash",
    "z-ai/glm-5.3-flash",
    "openai/gpt-6.1-sol",
    "openai/gpt-5.6-sol",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-opus-5.5",
    "moonshotai/kimi-k3",
    "google/gemini-3.8-flash",
    "google/gemini-4-argon",
    "qwen/qwen3.8-max-0902",
    "meta/muse-wonder",
    "x-ai/grok-4.7",
]
FIELDS = ["model", "condition", "status", "answer", "lens_calls", "web_searches",
          "cost_usd", "seconds", "trace"]
WEB = {"type": "openrouter:web_search", "parameters": {"engine": "exa", "max_results": 5, "max_total_results": 20}}
FETCH = {"type": "openrouter:web_fetch", "parameters": {"engine": "openrouter", "max_uses": 5, "max_content_tokens": 3000}}
LENS = {
    "type": "function",
    "function": {
        "name": "reverse_image_search",
        "description": "Find visually matching webpages for the supplied image.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}


def write_event(path, event, **details):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                            "event": event, **details}, ensure_ascii=False) + "\n")


def call_api(payload, key, trace):
    write_event(trace, "request", payload=payload)
    start = time.monotonic()
    try:
        r = requests.post(API, headers={"Authorization": f"Bearer {key}"},
                          json=payload, timeout=180)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        write_event(trace, "response", http_status=r.status_code,
                    seconds=round(time.monotonic() - start, 2), body=body,
                    request_id=r.headers.get("x-request-id"))
        r.raise_for_status()
        if not isinstance(body, dict) or body.get("error"):
            raise RuntimeError(f"Unexpected OpenRouter response: {str(body)[:500]}")
        return body
    except requests.RequestException as exc:
        write_event(trace, "http_error", error=str(exc))
        raise


def run_trial(model, condition, image_url, lens_result, key, trace):
    tools = [] if condition == "vision" else [WEB, FETCH]
    if condition == "web+lens":
        tools = tools + [LENS]
    messages = [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "image_url", "image_url": {"url": image_url}},
    ]}]
    row = {"model": model, "condition": condition, "status": "ok", "answer": "",
           "lens_calls": 0, "web_searches": 0,
           "cost_usd": 0.0, "seconds": 0.0, "trace": trace.name}
    start = time.monotonic()
    write_event(trace, "start", model=model, condition=condition)
    try:
        for _ in range(MAX_ROUNDS):
            payload = {"model": model, "messages": messages, "usage": {"include": True}}
            if tools:
                payload.update(tools=tools, tool_choice="auto", max_tool_calls=MAX_TOOL_CALLS)
            response = call_api(payload, key, trace)
            usage = response.get("usage") or {}
            row["cost_usd"] += float(usage.get("cost") or 0)
            server = usage.get("server_tool_use_details") or usage.get("server_tool_use") or {}
            row["web_searches"] += int(server.get("web_search_requests") or 0)
            message = response["choices"][0]["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                content = message.get("content") or ""
                row["answer"] = content if isinstance(content, str) else json.dumps(content)
                if not row["answer"]:
                    row["status"] = "empty"
                break
            messages.append({"role": "assistant", **message})
            for call in calls:
                name = call.get("function", {}).get("name")
                if name != "reverse_image_search":
                    raise ValueError(f"Unknown tool call: {name}")
                row["lens_calls"] += 1
                result = lens_result
                reply = {"role": "tool", "tool_call_id": call["id"],
                         "content": json.dumps(result, ensure_ascii=False)}
                write_event(trace, "tool_result", call=call, reply=reply)
                messages.append(reply)
        else:
            row["status"] = "limit"
    except Exception as exc:
        row["status"] = "error"
        write_event(trace, "error", type=type(exc).__name__, message=str(exc))
    row["seconds"] = round(time.monotonic() - start, 2)
    write_event(trace, "end", result=row)
    return row


def next_trace(folder, model, condition):
    stem = re.sub(r"[^a-zA-Z0-9.-]+", "_", model) + "_" + condition.replace("+", "-")
    path = folder / f"{stem}.jsonl"
    i = 2
    while path.exists():
        path = folder / f"{stem}_{i}.jsonl"
        i += 1
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--condition", required=True, choices=["vision", "web", "web+lens"])
    parser.add_argument("--models", nargs="+", default=MODELS)
    parser.add_argument("--run-dir", default="results")
    parser.add_argument("--rerun", action="store_true")
    args = parser.parse_args()

    image = (ROOT / "dzigurski.jpg").read_bytes()
    lens_bytes = (ROOT / "lens.json").read_bytes()
    lens = json.loads(lens_bytes)
    folder = Path(args.run_dir)
    if not folder.is_absolute():
        folder = ROOT / folder
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"prompt": PROMPT, "image_sha256": hashlib.sha256(image).hexdigest(),
                "lens_sha256": hashlib.sha256(lens_bytes).hexdigest()}
    manifest_path = folder / "manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text()) != manifest:
        parser.error("Input files or prompt changed. Use another --run-dir.")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    csv_path = folder / "results.csv"
    previous = []
    if csv_path.exists():
        with csv_path.open(newline="", encoding="utf-8") as f:
            previous = list(csv.DictReader(f))
    done = {(r["model"], r["condition"]) for r in previous if r["status"] == "ok"}
    key = os.environ.get("OPENROUTER_API_KEY")
    pending = [m for m in args.models if args.rerun or (m, args.condition) not in done]
    if pending and not key:
        parser.error("Set OPENROUTER_API_KEY")
    image_url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")

    for model in pending:
        trace = next_trace(folder, model, args.condition)
        row = run_trial(model, args.condition, image_url, lens, key, trace)
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            if f.tell() == 0:
                writer.writeheader()
            writer.writerow(row)
        print(f"{model}: {row['status']} | Lens={row['lens_calls']} | {row['answer'][:100]}", flush=True)
    print(f"{len(pending)} trials attempted. Results: {csv_path}")


if __name__ == "__main__":
    main()
