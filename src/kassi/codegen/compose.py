"""Compose a single self-contained k6 script from a Plan + OpenAPI spec.

No Jinja, no imported client, no aux files: the whole script is one string of
plain ``k6/http`` calls, ready to hand to the k6 MCP `run_script`/`validate_script`
tools. Sample request data is derived from the OpenAPI schemas (best-effort, just
enough to exercise the endpoint shape).
"""

from __future__ import annotations

import json
from typing import Any

from kassi.codegen.slots import Plan
from kassi.state import Endpoint

_DEFAULT_BASE_URL = "http://localhost:8000"

_P95_BY_TAXONOMY = {"smoke": 2000, "regression_comparison": 800, "load": 1500}

_OPTIONS_BY_TAXONOMY = {
    "smoke": "scenarios: {{ smoke: {{ executor: 'shared-iterations', vus: 1, iterations: 5 }} }}",
    "regression_comparison": (
        "scenarios: {{ regression: {{ executor: 'constant-arrival-rate', rate: 20, "
        "timeUnit: '1s', duration: '30s', preAllocatedVUs: 20, maxVUs: 30 }} }}"
    ),
    "load": (
        "scenarios: {{ load: {{ executor: 'ramping-vus', startVUs: 5, stages: ["
        "{{ duration: '10s', target: 5 }}, {{ duration: '20s', target: 30 }}, "
        "{{ duration: '10s', target: 0 }}] }} }}"
    ),
}

_HANDLE_SUMMARY = """
export function handleSummary(data) {
  const m = data.metrics || {};
  const val = (name, key) => {
    const x = m[name] && (m[name].values || m[name]);
    return x ? x[key] : undefined;
  };
  const reqs = val('http_reqs', 'count');
  const p95 = val('http_req_duration', 'p(95)');
  const failed = val('http_req_failed', 'rate');
  const checks = val('checks', 'rate');
  const lines = [
    'kassi summary',
    '  requests:       ' + (reqs != null ? reqs : 0),
    '  p(95) duration: ' + (p95 != null ? p95.toFixed(1) + ' ms' : 'n/a'),
    '  failure rate:   ' + (failed != null ? (failed * 100).toFixed(2) + '%' : 'n/a'),
    '  checks rate:    ' + (checks != null ? (checks * 100).toFixed(2) + '%' : 'n/a'),
  ];
  return { stdout: lines.join('\\n') + '\\n' };
}
"""


def _resolve_ref(spec: dict, ref: str) -> dict:
    node: Any = spec
    for part in ref.lstrip("#/").split("/"):
        if not isinstance(node, dict) or part not in node:
            return {}
        node = node[part]
    return node if isinstance(node, dict) else {}


def _sample_for_schema(spec: dict, schema: dict, depth: int = 0) -> Any:
    if depth > 4 or not isinstance(schema, dict):
        return None
    if "$ref" in schema:
        return _sample_for_schema(spec, _resolve_ref(spec, schema["$ref"]), depth + 1)
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]

    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])

    if t == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        return {
            name: _sample_for_schema(spec, sub, depth + 1) for name, sub in props.items() if name in required
        }
    if t == "array":
        return []
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return True
    if t == "string":
        fmt = schema.get("format")
        if fmt == "email":
            return "kassi@example.test"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "password":
            return "kassi-passw0rd"
        return "kassi"
    return None


def _operation_for(spec: dict, method: str, path: str) -> dict | None:
    op = (spec.get("paths", {}) or {}).get(path, {})
    if not isinstance(op, dict):
        return None
    found = op.get(method.lower())
    return found if isinstance(found, dict) else None


def _request_body_sample(spec: dict, op: dict) -> Any:
    content = ((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}
    schema = content.get("schema") or {}
    return _sample_for_schema(spec, schema) if schema else None


def _path_param_samples(spec: dict, op: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in op.get("parameters", []) or []:
        if isinstance(p, dict) and p.get("in") == "path":
            out[p.get("name", "")] = _sample_for_schema(spec, p.get("schema") or {}) or 1
    return out


def _js(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _fill_path(path: str, samples: dict[str, Any]) -> str:
    out = path
    for name, value in samples.items():
        out = out.replace("{" + name + "}", str(value))
    return out


def _spec_declares_auth(spec: dict) -> bool:
    if spec.get("components", {}).get("securitySchemes"):
        return True
    return bool(spec.get("security"))


def _find_auth_endpoints(spec: dict) -> tuple[str | None, str | None]:
    """Best-effort discovery of (register_path, login_path) by path keyword."""
    register = login = None
    for path, ops in (spec.get("paths", {}) or {}).items():
        if not isinstance(ops, dict) or "post" not in ops:
            continue
        low = path.lower()
        if register is None and any(k in low for k in ("register", "signup", "sign-up")):
            register = path
        if login is None and any(k in low for k in ("login", "signin", "sign-in", "token")):
            login = path
    return register, login


def _setup_block(spec: dict, base_url_var: str, register: str | None, login: str | None) -> str:
    if not login:
        return ""
    lines = [
        "export function setup() {",
        "  const json = { headers: { 'Content-Type': 'application/json' } };",
    ]
    if register:
        reg_op = _operation_for(spec, "POST", register) or {}
        reg_body = _request_body_sample(spec, reg_op) or {}
        lines.append(f"  http.post(`${{{base_url_var}}}{register}`, JSON.stringify({_js(reg_body)}), json);")
    login_op = _operation_for(spec, "POST", login) or {}
    login_body = _request_body_sample(spec, login_op) or {}
    lines += [
        f"  const res = http.post(`${{{base_url_var}}}{login}`, JSON.stringify({_js(login_body)}), json);",
        "  let token = '';",
        "  try {",
        "    const b = res.json();",
        "    token = b.access_token || b.token || b.jwt || (b.data && b.data.token) || '';",
        "  } catch (e) {}",
        "  return { token };",
        "}",
    ]
    return "\n".join(lines)


def _request_statement(spec: dict, ep: Endpoint, base_url_var: str) -> str | None:
    op = _operation_for(spec, ep.method, ep.path)
    samples = _path_param_samples(spec, op) if op else {}
    url = f"`${{{base_url_var}}}{_fill_path(ep.path, samples)}`"
    method = ep.method.upper()

    if method in ("POST", "PUT", "PATCH"):
        body = _request_body_sample(spec, op) if op else None
        fn = {"POST": "post", "PUT": "put", "PATCH": "patch"}[method]
        return f"http.{fn}({url}, JSON.stringify({_js(body or {})}), params)"
    if method == "DELETE":
        return f"http.del({url}, null, params)"
    if method == "GET":
        return f"http.get({url}, params)"
    return None


def compose(*, plan: Plan, openapi_spec: dict | None, endpoints: list[Endpoint], base_url: str) -> str:
    spec = openapi_spec or {}
    base_url = base_url or _DEFAULT_BASE_URL

    flagged_auth = any("auth_required" in ep.flags for ep in plan.endpoints)
    register, login = _find_auth_endpoints(spec)
    needs_auth = (flagged_auth or _spec_declares_auth(spec)) and bool(login)

    options_inner = _OPTIONS_BY_TAXONOMY[plan.test_taxonomy].format()
    p95 = _P95_BY_TAXONOMY[plan.test_taxonomy]
    options = (
        "export const options = {\n"
        f"  {options_inner},\n"
        "  thresholds: {\n"
        "    http_req_failed: ['rate<0.05'],\n"
        f"    http_req_duration: ['p(95)<{p95}'],\n"
        "  },\n"
        "};"
    )

    blocks: list[str] = []
    for ep in endpoints:
        stmt = _request_statement(spec, ep, "BASE_URL")
        if stmt is None:
            continue
        label = f"{ep.method.upper()} {ep.path}"
        blocks.append(
            f"  group({_js(label)}, function () {{\n"
            f"    const res = {stmt};\n"
            f"    check(res, {{ {_js(label + ' is 2xx')}: (r) => r.status >= 200 && r.status < 300 }});\n"
            f"  }});"
        )

    if not blocks:
        blocks.append("  // no resolvable endpoints; nothing to exercise")

    setup = _setup_block(spec, "BASE_URL", register, login) if needs_auth else ""
    token_line = "  const token = (data && data.token) || '';" if needs_auth else "  const token = '';"
    auth_header = "    ...(token ? { Authorization: `Bearer ${token}` } : {}),\n" if needs_auth else ""

    parts = [
        "import http from 'k6/http';",
        "import { check, group, sleep } from 'k6';",
        "",
        f"const BASE_URL = __ENV.TARGET_BASE_URL || {_js(base_url)};",
        "",
        options,
        "",
    ]
    if setup:
        parts += [setup, ""]
    parts += [
        "export default function (data) {",
        token_line,
        "  const params = {",
        "    headers: {",
        "      'Content-Type': 'application/json',",
        auth_header + "    },",
        "  };",
        "",
        *blocks,
        "  sleep(1);",
        "}",
        _HANDLE_SUMMARY,
    ]
    return "\n".join(parts) + "\n"
