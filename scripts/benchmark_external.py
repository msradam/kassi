"""kassi-bench-ext: run kassi against a third-party app it never instrumented.

    docker run -d --name kassi-httpbin --platform linux/arm64 -p 8600:8080 ghcr.io/mccutchen/go-httpbin
    uv run python scripts/benchmark_external.py --reps 5

The point: kassi's diagnosis loop is not tied to its own demo apps. go-httpbin (a popular OSS
reimplementation of httpbin) is observed only through the generic access-log proxy
(scripts/access_proxy.py), which ships its traffic to Splunk the way a real gateway would. The
endpoints give app-intrinsic ground truth: /status/500 errors (5xx regression), /delay/2 is
genuinely slow, /get is healthy. The proxy sees status + latency only, not the app's internal error
string, so a 5xx reads as a generic "upstream returned 500". Writes docs/benchmark/external_results.json.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from benchmark import _verdict_class  # same dir on sys.path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "benchmark"
UPSTREAM = "http://localhost:8600"  # go-httpbin
PROXY_PORT = 8500
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"

TARGETS = [
    {"name": "httpbin /status/500", "path": "/status/500", "method": "get", "klass": "regression",
     "intent": "load test the status endpoint", "fault": "go-httpbin returns HTTP 500"},
    {"name": "httpbin /delay/2", "path": "/delay/2", "method": "get", "klass": "degradation",
     "intent": "load test the delayed endpoint", "fault": "go-httpbin sleeps 2s per request"},
    {"name": "httpbin /get", "path": "/get", "method": "get", "klass": "none",
     "intent": "load test the get endpoint", "fault": "healthy control"},
]  # fmt: skip


def _kill_port(port: int) -> None:
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True).stdout
    for pid in out.split():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid), signal.SIGKILL)


def _wait(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _spec(path: str, method: str) -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "go-httpbin (external)", "version": "1.0.0"},
        "paths": {path: {method: {"responses": {"200": {"description": "ok"}}}}},
    }


def _score(target: dict, report: dict) -> dict:
    findings = (report.get("correlation") or {}).get("findings") or {}
    verdict = report.get("verdict") or ""
    worst = (findings.get("worst_path") or {}).get("path")
    predicted = _verdict_class(verdict)
    control = target["klass"] == "none"
    localized = None if control else (worst == target["path"])
    class_ok = predicted == target["klass"]
    correct = (predicted == "none") if control else bool(class_ok and localized)
    return {
        "target": target["name"], "expected": target["klass"], "predicted": predicted,
        "verdict": verdict, "worst_path": worst,
        "server_errors": findings.get("server_errors"), "client_errors": findings.get("client_errors"),
        "p95_ms": findings.get("p95_ms"), "correct": correct, "localized": localized, "class_ok": class_ok,
    }  # fmt: skip


async def _run_one(target: dict, build_application) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "openapi.json").write_text(json.dumps(_spec(target["path"], target["method"])))
        try:
            application = build_application()
            _, _, state = await asyncio.wait_for(
                application.arun(
                    halt_after=["report"],
                    inputs={
                        "repo_path": tmp,
                        "intent": target["intent"],
                        "target_base_url": PROXY_URL,
                        "splunk_index": "web",
                    },
                ),
                timeout=240,
            )
            return _score(target, state["report"])
        except Exception as exc:  # noqa: BLE001
            return {"target": target["name"], "error": f"{type(exc).__name__}: {exc}"}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    args = ap.parse_args()

    if not _wait(f"{UPSTREAM}/get", timeout=5):
        print(
            f"go-httpbin not reachable at {UPSTREAM}. Start it:\n"
            "  docker run -d --name kassi-httpbin --platform linux/arm64 -p 8600:8080 ghcr.io/mccutchen/go-httpbin"
        )
        return

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"  # deterministic: model off
    os.environ.pop("KASSI_HEC_TOKEN", None)

    from theodosia import UpstreamManager, bind_upstream
    from theodosia.upstream import reset_upstream

    from kassi.app import build_application
    from kassi.upstream import k6_upstream_config, splunk_configured, splunk_upstream_config

    if not splunk_configured():
        print("Splunk not configured in .env. Aborting.")
        return

    # Start the access-log proxy in front of go-httpbin.
    _kill_port(PROXY_PORT)
    proxy = subprocess.Popen(
        ["uv", "run", "--with", "fastapi", "--with", "uvicorn", "--with", "httpx",
         "python", str(ROOT / "scripts" / "access_proxy.py"), "serve", "--port", str(PROXY_PORT)],
        cwd=str(ROOT), env={**os.environ, "UPSTREAM": UPSTREAM}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    if not _wait(f"{PROXY_URL}/healthz"):
        proxy.terminate()
        print("access-log proxy did not come up. Aborting.")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    runs: list[dict] = []
    print(f"kassi-bench-ext: kassi vs go-httpbin via the access-log proxy, {args.reps} reps\n")
    upstream = UpstreamManager({"k6": k6_upstream_config(), "splunk": splunk_upstream_config()})
    token = bind_upstream(upstream)
    try:
        for rep in range(args.reps):
            for target in TARGETS:
                t0 = time.time()
                rec = await _run_one(target, build_application)
                rec["rep"] = rep
                rec["seconds"] = round(time.time() - t0, 1)
                runs.append(rec)
                (OUT / "external_results.json").write_text(json.dumps(runs, indent=2))
                if "error" in rec:
                    print(f"  {target['name']:<22} ERROR {rec['error']}")
                else:
                    mark = "ok" if rec["correct"] else "XX"
                    print(
                        f"  {target['name']:<22} {mark}  expected={rec['expected']:<11} "
                        f"got={rec['predicted']:<11} worst={rec['worst_path']} ({rec['seconds']}s)"
                    )
    finally:
        await upstream.aclose()
        reset_upstream(token)
        proxy.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proxy.wait(timeout=5)
        _kill_port(PROXY_PORT)

    ok = [r for r in runs if "error" not in r]
    print(f"\n=== {len(ok)}/{len(runs)} runs scored ===")
    for target in TARGETS:
        rows = [r for r in ok if r["target"] == target["name"]]
        if rows:
            c = sum(r["correct"] for r in rows)
            print(f"  {target['name']:<22} {c}/{len(rows)} correct  (expected {target['klass']})")
    print(f"\noverall: {sum(r['correct'] for r in ok)}/{len(ok)} correct")
    print(f"wrote {OUT / 'external_results.json'}")


if __name__ == "__main__":
    asyncio.run(main())
