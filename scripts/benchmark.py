"""kassi-bench: a reproducible, ground-truth benchmark for change-induced performance regressions.

    uv run python scripts/benchmark.py --reps 10
    uv run python scripts/benchmark.py --reps 10 --scenarios petclinic,feed

Each fault scenario is a code change that introduces one known performance fault of a known class
(5xx regression, latency degradation, 4xx throttling, downstream cascade). The `*-ok` scenarios are
controls: the same apps under load on a healthy endpoint, where the right answer is "nothing wrong."
For each, the harness starts the target app, drives the whole kassi FSM against live k6 + live
Splunk, and scores kassi's verdict against the ground-truth label: did it detect the regression,
attribute it to the right endpoint, classify the failure mode, name the root cause where one
exists, and stay quiet on the controls.

By default the full pipeline runs (the configured model authors the k6 script, writes the analysis,
a guardian pass audits) with the load held at 25 VUs / 25s; --deterministic bypasses the model for a
controlled baseline. The model is whatever KASSI_LLM selects (a local 8B over Ollama, or a frontier
model over the Claude Agent SDK); the harness is the same. Writes docs/benchmark/results.json
incrementally and prints the accuracy table. Prereqs: local Splunk seeded (scripts/seed_splunk.py),
.env set, k6 2.0, and the configured model reachable for model-on.

Distinct from infra-fault RCA benchmarks (RCAEval, PetShop), which inject CPU/memory/network faults
and score over pre-recorded traces: here the fault is a real code change, exercised live, and the
diagnosis is read back from Splunk over the exact test window.
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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "benchmark"

# scenario -> ground truth. `endpoint` is the changed/flawed route; `klass` the failure mode
# ("none" for a healthy control); `cause` a substring that must appear in kassi's named server-side
# root cause (None when the class has no server error string, i.e. latency or client throttling).
LABELS: dict[str, dict] = {
    "petclinic": {
        "port": 8400, "intent": "load test recording a new visit",
        "endpoint": "/api/visits", "klass": "regression", "cause": "database is locked",
        "fault": "SQLite write-lock under concurrency",
    },
    "storefront": {
        "port": 8401, "intent": "load test the checkout endpoint placing an order",
        "endpoint": "/api/checkout", "klass": "degradation", "cause": None,
        "fault": "N+1 over a shared connection (server-side db_time, zero errors)",
    },
    "feed": {
        "port": 8402, "intent": "load test recording new activity events",
        "endpoint": "/api/events", "klass": "degradation", "cause": None,
        "fault": "unbounded recompute, latency rising over the run",
    },
    "gateway": {
        "port": 8403, "intent": "load test requesting a price quote",
        "endpoint": "/api/quote", "klass": "throttling", "cause": None,
        "fault": "token-bucket rate limit, 429 throttling under load",
    },
    "orders": {
        "port": 8404, "intent": "load test placing a new order",
        "endpoint": "/api/order", "klass": "regression", "cause": "timed out",
        "fault": "downstream timeout cascade (504 + slow 201)",
    },
    "petclinic-ok": {
        "app": "petclinic", "port": 8400, "intent": "load test listing the owners",
        "endpoint": "/api/owners", "klass": "none", "cause": None,
        "fault": "healthy GET (control: no regression to find)",
    },
    "storefront-ok": {
        "app": "storefront", "port": 8401, "intent": "load test listing the products",
        "endpoint": "/api/products", "klass": "none", "cause": None,
        "fault": "healthy GET (control)",
    },
    "gateway-ok": {
        "app": "gateway", "port": 8403, "intent": "load test the service status",
        "endpoint": "/api/status", "klass": "none", "cause": None,
        "fault": "healthy GET (control)",
    },
}  # fmt: skip


def _kill_port(port: int) -> None:
    out = subprocess.run(["lsof", "-ti", f"tcp:{port}"], capture_output=True, text=True).stdout
    for pid in out.split():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pid), signal.SIGKILL)


def _wait_for_app(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def _verdict_class(verdict: str) -> str:
    """The failure mode kassi declares in its verdict, its actual headline output."""
    v = (verdict or "").lower()
    if "regression" in v:
        return "regression"
    if "throttling" in v:
        return "throttling"
    if "degradation" in v:
        return "degradation"
    return "none"


def _score(label: dict, report: dict) -> dict:
    findings = (report.get("correlation") or {}).get("findings") or {}
    anomalies = report.get("anomalies") or {}
    verdict = report.get("verdict") or ""
    worst = (findings.get("worst_path") or {}).get("path")
    top_error = (findings.get("top_error") or {}).get("error_message") or ""
    control = label["klass"] == "none"

    predicted = _verdict_class(verdict)
    detected = predicted != "none"
    class_ok = predicted == label["klass"]
    localized = None if control else (worst == label["endpoint"])
    cause_ok = None if label["cause"] is None else label["cause"].lower() in top_error.lower()
    if control:
        correct = predicted == "none"  # a control is right only if kassi stays quiet
    else:
        correct = bool(detected and localized and class_ok and cause_ok is not False)
    return {
        "verdict": verdict,
        "worst_path": worst,
        "top_error": top_error or None,
        "server_errors": findings.get("server_errors"),
        "client_errors": findings.get("client_errors"),
        "p95_ms": findings.get("p95_ms"),
        "anomaly": {
            "available": anomalies.get("available"),
            "anomalous_buckets": anomalies.get("anomalous_buckets"),
            "forecast_p95_ms": anomalies.get("forecast_p95_ms"),
            "method": anomalies.get("method"),
        },
        "predicted_class": predicted,
        "detected": detected,
        "localized": localized,
        "class_ok": class_ok,
        "cause_ok": cause_ok,
        "correct": correct,
    }


async def _run_one(name: str, label: dict, build_application) -> dict:
    """Start the app, drive the FSM once, score it, tear the app down."""
    app_dir = ROOT / "examples" / label.get("app", name)
    url = f"http://127.0.0.1:{label['port']}"
    _kill_port(label["port"])
    env = {**os.environ, "SPLUNK_INDEX": "web", "KASSI_SPLUNK_INSECURE": "1"}
    proc = subprocess.Popen(
        ["uv", "run", "--with", "fastapi", "--with", "uvicorn", "--with", "httpx",
         "python", str(app_dir / "app.py"), "serve"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        if not _wait_for_app(url):
            return {"scenario": name, "error": "app did not come up"}
        with tempfile.TemporaryDirectory() as tmp:
            # A control must load exactly its healthy GET. Pointing kassi at the app's full spec lets
            # intent-matching pull in a parametrized sibling (/api/products vs /api/products/{sku})
            # whose sample value 404s, which kassi then correctly reads as 4xx. Trim to one endpoint.
            if label["klass"] == "none":
                (Path(tmp) / "openapi.json").write_text(
                    json.dumps(
                        {
                            "openapi": "3.0.0",
                            "info": {"title": name, "version": "1.0.0"},
                            "paths": {
                                label["endpoint"]: {"get": {"responses": {"200": {"description": "ok"}}}}
                            },
                        }
                    )
                )
                repo_path = tmp
            else:
                repo_path = str(app_dir)
            application = build_application()
            _, _, state = await asyncio.wait_for(
                application.arun(
                    halt_after=["report"],
                    inputs={
                        "repo_path": repo_path,
                        "intent": label["intent"],
                        "target_base_url": url,
                        "splunk_index": "web",
                    },
                ),
                timeout=240,
            )
        return {"scenario": name, **_score(label, state["report"])}
    except Exception as exc:  # noqa: BLE001 - record and continue the suite
        return {"scenario": name, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        _kill_port(label["port"])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--scenarios", default=",".join(LABELS))
    ap.add_argument(
        "--deterministic",
        action="store_true",
        help="force model-off (scaffold + deterministic analysis) for a controlled baseline",
    )
    args = ap.parse_args()
    names = [s.strip() for s in args.scenarios.split(",") if s.strip() in LABELS]

    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    # Default: the real pipeline. Granite authors the k6 script, writes the cited analysis, and
    # Guardian audits it, every run. --deterministic points Ollama at a closed port so every model
    # phase falls back to its scaffold/deterministic path, a controlled baseline that isolates the
    # correlation from the model.
    if args.deterministic:
        os.environ["OLLAMA_HOST"] = "http://127.0.0.1:1"
    os.environ.pop("KASSI_HEC_TOKEN", None)  # don't publish benchmark runs to the kassi_runs index

    from theodosia import UpstreamManager, bind_upstream
    from theodosia.upstream import reset_upstream

    from kassi.app import build_application
    from kassi.upstream import k6_upstream_config, splunk_configured, splunk_upstream_config

    if not splunk_configured():
        print("Splunk not configured in .env. Aborting.")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    results_path = OUT / "results.json"
    runs: list[dict] = []
    started = time.time()
    total = len(names) * args.reps
    model_name = os.environ.get("KASSI_MODEL", "granite4.1:8b")
    mode = (
        "model OFF (deterministic baseline)"
        if args.deterministic
        else f"model ON ({model_name} authors + analyzes, Guardian audits)"
    )
    print(f"kassi-bench: {len(names)} scenarios x {args.reps} reps = {total} runs, {mode}\n")

    upstream = UpstreamManager({"k6": k6_upstream_config(), "splunk": splunk_upstream_config()})
    token = bind_upstream(upstream)
    try:
        i = 0
        for rep in range(args.reps):
            for name in names:
                i += 1
                t0 = time.time()
                rec = await _run_one(name, LABELS[name], build_application)
                rec["rep"] = rep
                rec["seconds"] = round(time.time() - t0, 1)
                runs.append(rec)
                results_path.write_text(json.dumps(runs, indent=2))  # incremental save
                if "error" in rec:
                    print(f"[{i:>3}/{total}] {name:<14} ERROR {rec['error']}  ({rec['seconds']}s)")
                else:
                    status = "ok" if rec["correct"] else "XX"
                    print(
                        f"[{i:>3}/{total}] {name:<14} {status}  {rec['predicted_class']:<11} "
                        f"worst={rec['worst_path']}  ({rec['seconds']}s)"
                    )
    finally:
        await upstream.aclose()
        reset_upstream(token)

    _report(runs, round(time.time() - started))


def _rate(rows: list[dict], key: str) -> str:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return f"{round(100 * sum(vals) / len(vals))}%" if vals else "n/a"


def _report(runs: list[dict], secs: int) -> None:
    ok = [r for r in runs if "error" not in r]
    nerr = len(runs) - len(ok)
    print(f"\n=== kassi-bench: {len(ok)}/{len(runs)} runs scored ({nerr} errored) in {secs}s ===\n")
    faults = [n for n in LABELS if LABELS[n]["klass"] != "none"]
    controls = [n for n in LABELS if LABELS[n]["klass"] == "none"]

    fhdr = (
        f"{'fault scenario':<15}{'n':>3}  {'detect':>7}{'localize':>9}{'class':>7}{'cause':>7}{'correct':>9}"
    )
    print(fhdr)
    print("-" * len(fhdr))
    for name in faults:
        rows = [r for r in ok if r["scenario"] == name]
        if not rows:
            continue
        print(
            f"{name:<15}{len(rows):>3}  {_rate(rows, 'detected'):>7}{_rate(rows, 'localized'):>9}"
            f"{_rate(rows, 'class_ok'):>7}{_rate(rows, 'cause_ok'):>7}{_rate(rows, 'correct'):>9}"
        )
    frows = [r for r in ok if LABELS[r["scenario"]]["klass"] != "none"]
    print("-" * len(fhdr))
    print(
        f"{'ALL FAULTS':<15}{len(frows):>3}  {_rate(frows, 'detected'):>7}{_rate(frows, 'localized'):>9}"
        f"{_rate(frows, 'class_ok'):>7}{_rate(frows, 'cause_ok'):>7}{_rate(frows, 'correct'):>9}"
    )

    chrows = [r for r in ok if LABELS[r["scenario"]]["klass"] == "none"]
    if chrows:
        chdr = f"\n{'control (healthy)':<18}{'n':>3}  {'false-alarm':>12}{'correct':>9}"
        print(chdr)
        print("-" * (len(chdr) - 1))
        for name in controls:
            rows = [r for r in ok if r["scenario"] == name]
            if rows:
                print(f"{name:<18}{len(rows):>3}  {_rate(rows, 'detected'):>12}{_rate(rows, 'correct'):>9}")
        print("-" * (len(chdr) - 1))
        print(
            f"{'ALL CONTROLS':<18}{len(chrows):>3}  {_rate(chrows, 'detected'):>12}{_rate(chrows, 'correct'):>9}"
        )

    print(f"\noverall correctness: {_rate(ok, 'correct')}  (n={len(ok)})")
    print(f"wrote {OUT / 'results.json'}")


if __name__ == "__main__":
    asyncio.run(main())
