"""Subgraph retrieval — extract relevant API context for a set of endpoints or a diff."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .builder import OpenAPIGraph


@dataclass
class RetrievedContext:
    """The subgraph context retrieved for a set of endpoints."""

    endpoints: list[str] = field(default_factory=list)
    schemas: dict[str, dict] = field(default_factory=dict)
    parameters: list[dict] = field(default_factory=list)
    requires_auth: bool = False
    _graph: OpenAPIGraph | None = field(default=None, repr=False)

    def to_text(self) -> str:
        """Serialize to compact text for LLM context injection."""
        lines = []
        for ep in self.endpoints:
            G = self._graph.graph if self._graph else None
            if G and G.has_node(ep):
                data = G.nodes[ep]
                lines.append(f"## {data.get('method', '')} {data.get('path', '')}")
                if data.get("summary"):
                    lines.append(f"Summary: {data['summary']}")
            else:
                lines.append(f"## {ep}")

        if self.requires_auth:
            lines.append("\nAuthentication: Bearer token required")

        if self.parameters:
            lines.append("\nParameters:")
            for p in self.parameters:
                req = " (required)" if p.get("required") else ""
                lines.append(f"  - {p['name']}: {p.get('type', 'string')} in {p.get('in', 'query')}{req}")

        if self.schemas:
            lines.append("\nSchemas:")
            for name, schema_info in self.schemas.items():
                lines.append(f"\n### {name}")
                for prop in schema_info.get("properties", []):
                    req_mark = " *" if prop.get("required") else ""
                    lines.append(f"  - {prop['name']}: {prop['type']}{req_mark}")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "endpoints": self.endpoints,
            "schemas": self.schemas,
            "parameters": self.parameters,
            "requires_auth": self.requires_auth,
        }


class SubgraphRetriever:
    """Retrieve relevant API context by traversing the OpenAPI knowledge graph."""

    def __init__(self, graph: OpenAPIGraph):
        self.graph = graph

    def for_endpoints(self, endpoint_ids: list[str]) -> RetrievedContext:
        """Given endpoint node IDs, traverse the graph to collect all related context."""
        G = self.graph.graph
        found_endpoints = []
        all_schemas: dict[str, dict] = {}
        all_params: list[dict] = []
        needs_auth = False

        for ep_id in endpoint_ids:
            if not G.has_node(ep_id):
                # Try fuzzy match — endpoint might be specified as path only
                matched = self._fuzzy_match_endpoint(ep_id)
                if not matched:
                    continue
                ep_id = matched

            found_endpoints.append(ep_id)

            # BFS from endpoint, collecting related nodes
            for _, neighbor, edge_data in G.edges(ep_id, data=True):
                rel = edge_data.get("relation", "")

                if rel == "REQUIRES_AUTH":
                    needs_auth = True

                elif rel == "HAS_PARAM":
                    param_data = G.nodes[neighbor]
                    param_name = param_data.get("name", "")
                    if param_name.lower() == "authorization" and param_data.get("location", "") == "header":
                        needs_auth = True
                    all_params.append(
                        {
                            "name": param_name,
                            "type": param_data.get("param_type", "string"),
                            "in": param_data.get("location", "query"),
                            "required": param_data.get("required", False),
                        }
                    )

                elif rel in ("ACCEPTS", "RETURNS"):
                    # Collect the schema and traverse its references (depth 2)
                    self._collect_schema(neighbor, all_schemas, depth=2)

        return RetrievedContext(
            endpoints=found_endpoints,
            schemas=all_schemas,
            parameters=all_params,
            requires_auth=needs_auth,
            _graph=self.graph,
        )

    def endpoints_from_diff(self, diff: str) -> list[str]:
        """Extract endpoint IDs from a unified diff by matching route patterns.

        Only examines added lines (starting with '+') to identify endpoints
        that were actually changed, not just present in the diff context.
        Falls back to full diff if no added lines match.
        """
        G = self.graph.graph
        known_endpoints = self.graph.endpoints()
        known_paths = {G.nodes[ep]["path"]: ep for ep in known_endpoints}

        # Extract only added lines from the diff for targeted matching
        added_lines = "\n".join(
            line[1:] for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )

        # Try matching added lines first, fall back to full diff
        for text in [added_lines, diff]:
            found = self._match_endpoints_in_text(text, known_paths)
            if found:
                return list(found)

        return []

    def _match_endpoints_in_text(self, text: str, known_paths: dict[str, str]) -> set[str]:
        """Match API endpoint patterns in text against known OpenAPI paths."""
        found = set()

        # Match FastAPI/Flask decorators: @app.get("/api/foo")
        for match in re.finditer(r'@app\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)', text):
            method = match.group(1).upper()
            path = match.group(2)
            ep_id = f"{method} {path}"
            if ep_id in known_paths.values():
                found.add(ep_id)
            else:
                for known_path, known_ep in known_paths.items():
                    if _paths_match(path, known_path):
                        found.add(known_ep)

        # Match Express/Hono/generic: router.get('/api/foo', ...) or app.get('/api/foo', ...)
        for match in re.finditer(r'(?:router|app)\.(get|post|put|patch|delete)\(\s*["\']([^"\']+)', text):
            method = match.group(1).upper()
            path = match.group(2)
            for known_path, known_ep in known_paths.items():
                if _paths_match(path, known_path):
                    found.add(known_ep)

        # Fallback: match any quoted path that looks like an API route
        if not found:
            for match in re.finditer(r'["\'](/api/[^"\']+)["\']', text):
                path = match.group(1)
                for known_path, known_ep in known_paths.items():
                    if _paths_match(path, known_path):
                        found.add(known_ep)

        return found

    def _collect_schema(self, schema_name: str, collected: dict, depth: int) -> None:
        """Recursively collect a schema and its referenced schemas."""
        if schema_name in collected or depth <= 0:
            return

        G = self.graph.graph
        if not G.has_node(schema_name) or G.nodes[schema_name].get("type") != "schema":
            return

        schema_data = G.nodes[schema_name]
        properties = []
        required_fields = schema_data.get("required", [])

        for _, neighbor, edge_data in G.edges(schema_name, data=True):
            rel = edge_data.get("relation", "")

            if rel == "HAS_PROPERTY":
                prop_data = G.nodes[neighbor]
                prop_name = prop_data.get("name", neighbor.split(".")[-1])
                properties.append(
                    {
                        "name": prop_name,
                        "type": prop_data.get("property_type", ""),
                        "required": prop_name in required_fields,
                    }
                )

            elif rel == "REFERENCES":
                self._collect_schema(neighbor, collected, depth - 1)

        collected[schema_name] = {"properties": properties}

    def _fuzzy_match_endpoint(self, query: str) -> str | None:
        """Try to match a partial endpoint identifier."""
        for ep in self.graph.endpoints():
            if query in ep:
                return ep
        return None


def _paths_match(actual: str, template: str) -> bool:
    """Check if an actual path matches an OpenAPI template path.

    Handles:
    - Direct match: '/api/books' == '/api/books'
    - Express params: '/api/books/:id/reviews' matches '/api/books/{id}/reviews'
    - Concrete values: '/api/accounts/1' matches '/api/accounts/{account_id}'

    Does NOT match named path segments against {param} placeholders —
    e.g., '/api/books/suggestions' will NOT match '/api/books/{id}'
    because 'suggestions' looks like a distinct route, not a parameter value.
    """
    actual = actual.rstrip("/")
    template = template.rstrip("/")

    # Normalize Express :param to OpenAPI {param}
    actual = re.sub(r":(\w+)", r"{\1}", actual)

    if actual == template:
        return True

    # Segment-level comparison to avoid false positives
    actual_parts = actual.split("/")
    template_parts = template.split("/")

    if len(actual_parts) != len(template_parts):
        return False

    for a, t in zip(actual_parts, template_parts, strict=False):
        if a == t:
            continue
        if t.startswith("{") and t.endswith("}"):
            # Only match concrete values (numeric, UUIDs) or Express/OpenAPI params
            # against template params. Named segments like 'suggestions' are
            # distinct routes, not parameter values.
            if a.startswith("{") and a.endswith("}"):
                continue  # Both are params — match
            if re.fullmatch(r"[\d]+", a):
                continue  # Numeric ID — match
            if re.fullmatch(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a, re.IGNORECASE
            ):
                continue  # UUID — match
            return False  # Named segment like 'suggestions' — NOT a match
        else:
            return False  # Literal mismatch

    return True
