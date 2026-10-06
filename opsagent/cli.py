"""Command line: run a task, start the sandbox, run evals.

    python -m opsagent sandbox                       # start the mock company apps on :8765
    python -m opsagent run "Find the latest invoice from Acme Supplies Pvt Ltd ..." [--visible] [--faults ui_drift,session_expiry=2]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = "http://localhost:8765"


def parse_faults(spec: str) -> dict:
    faults: dict = {}
    for part in filter(None, (spec or "").split(",")):
        k, _, v = part.partition("=")
        if not v:
            faults[k] = True
        elif v.isdigit():
            faults[k] = int(v)
        else:
            faults[k] = v
    return faults


def ensure_sandbox(base_url: str) -> subprocess.Popen | None:
    try:
        httpx.get(base_url + "/__admin__/state", timeout=1.5)
        return None
    except httpx.HTTPError:
        pass
    port = base_url.rsplit(":", 1)[-1]
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", port, "--log-level", "warning"],
                            cwd=ROOT)
    for _ in range(40):
        try:
            httpx.get(base_url + "/__admin__/state", timeout=1)
            return proc
        except httpx.HTTPError:
            time.sleep(0.25)
    raise SystemExit("sandbox failed to start")


async def cmd_run(args) -> int:
    from .llm.gemini import GeminiLLM
    from .runtime.agent import Runtime

    os.environ["OPSAGENT_VERBOSE"] = "1"
    proc = ensure_sandbox(args.base_url)
    try:
        faults = parse_faults(args.faults)
        httpx.post(args.base_url + "/__admin__/reset", json={"faults": faults}, timeout=5)
        print(f"sandbox reset; faults: {faults or 'none'}")
        rt = Runtime(GeminiLLM(model=args.model), base_url=args.base_url, headless=not args.visible,
                     slow_mo=400 if args.visible else 0)
        res = await rt.run(args.task)
        print("\n" + "=" * 70)
        print(f"OUTCOME: {res.stop_reason.value}  (agent status: {res.final_status or '-'})")
        print(res.summary)
        for v in res.verdicts:
            print(f"  verify [{'PASS' if v['passed'] else 'FAIL'}] {v['claim']}: {v['detail']}")
        print(f"cost: {json.dumps(res.meter)}\nreport: {res.run_dir}/report.md")
        return 0 if res.done else 1
    finally:
        if proc:
            proc.terminate()


async def cmd_voice(args) -> int:
    from . import voice
    from .llm.base import LLMError
    from .llm.gemini import GeminiLLM

    llm = GeminiLLM(model=args.model)
    try:
        if args.audio:
            audio, mime = Path(args.audio).read_bytes(), voice.mime_for(args.audio)
        else:
            audio, mime = voice.record(args.seconds), "audio/wav"
        print("Transcribing...")
        text = voice.clean_transcript(await llm.transcribe(audio, mime))
    except LLMError as exc:
        print(f"voice input failed: {exc}")
        return 2
    if not text:
        print("No speech detected.")
        return 2
    task = voice.confirm(text)
    if not task:
        print("Cancelled; nothing was run.")
        return 0
    args.task = task
    return await cmd_run(args)


def _add_run_options(r) -> None:
    r.add_argument("--faults", default="", help="comma list: session_expiry=2,ui_drift,flaky_submit=after_save,preload_duplicate,mail_amount_mismatch")
    r.add_argument("--visible", action="store_true", help="show the browser window")
    r.add_argument("--model", default=None)
    r.add_argument("--base-url", default=DEFAULT_URL)


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(prog="opsagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a task end to end")
    r.add_argument("task")
    _add_run_options(r)
    v = sub.add_parser("voice", help="speak the request (or --audio file), confirm the transcript, then run it")
    v.add_argument("--audio", default=None, help="use an audio file instead of the microphone")
    v.add_argument("--seconds", type=float, default=None, help="record for N seconds (default: until Enter)")
    _add_run_options(v)
    s = sub.add_parser("sandbox", help="start the mock company apps")
    s.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    if args.cmd == "sandbox":
        os.chdir(ROOT)
        os.execvp(sys.executable, [sys.executable, "-m", "uvicorn", "sandbox.app:app", "--port", str(args.port)])
    raise SystemExit(asyncio.run(cmd_voice(args) if args.cmd == "voice" else cmd_run(args)))


if __name__ == "__main__":
    main()
