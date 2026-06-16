"""Build a directed graph from an OpenAPI 3.x spec.

Nodes: endpoints, schemas, properties, parameters, security schemes.
Edges: RETURNS, ACCEPTS, HAS_PROPERTY, REFERENCES, REQUIRES_AUTH, HAS_PARAM.
"""

from __future__ import annotations

from .digraph import DiGraph


class OpenAPIGraph:
    """Deterministic knowledge graph built from an OpenAPI spec."""

    def __init__(self, graph: DiGraph):
        self.graph = graph

    @classmethod
    def from_spec(cls, spec: dict) -> OpenAPIGraph:
        G = DiGraph()
        schemas = spec.get("components", {}).get("schemas", {})
        security_schemes = spec.get("components", {}).get("securitySchemes", {})

        # Security scheme nodes
        for name, scheme in security_schemes.items():
            node_id = f"security:{name}"
            G.add_node(
                node_id, type="security", scheme_type=scheme.get("type", ""), scheme=scheme.get("scheme", "")
            )

        # Schema nodes + property nodes
        for schema_name, schema_def in schemas.items():
            required = schema_def.get("required", [])
            G.add_node(schema_name, type="schema", required=required)
            _add_properties(G, schema_name, schema_def, schemas)

        # Endpoint nodes
        for path, path_item in spec.get("paths", {}).items():
            for method in ("get", "post", "put", "patch", "delete"):
                if method not in path_item:
                    continue
                op = path_item[method]
                node_id = f"{method.upper()} {path}"
                G.add_node(
                    node_id,
                    type="endpoint",
                    method=method.upper(),
                    path=path,
                    summary=op.get("summary", ""),
                    operation_id=op.get("operationId", ""),
                )

                # Request body → ACCEPTS
                req_body = op.get("requestBody", {})
                for content in req_body.get("content", {}).values():
                    ref = _resolve_ref(content.get("schema", {}))
                    if ref and ref in schemas:
                        G.add_edge(node_id, ref, relation="ACCEPTS")

                # Responses → RETURNS
                for _status, resp in op.get("responses", {}).items():
                    for content in resp.get("content", {}).values():
                        ref = _resolve_ref(content.get("schema", {}))
                        if ref and ref in schemas:
                            G.add_edge(node_id, ref, relation="RETURNS")

                # Parameters → HAS_PARAM
                params = op.get("parameters", []) + path_item.get("parameters", [])
                for param in params:
                    param_id = f"{node_id}:param:{param['name']}"
                    G.add_node(
                        param_id,
                        type="parameter",
                        name=param["name"],
                        location=param.get("in", ""),
                        param_type=param.get("schema", {}).get("type", ""),
                        required=param.get("required", False),
                    )
                    G.add_edge(node_id, param_id, relation="HAS_PARAM")

                # Security → REQUIRES_AUTH
                security = op.get("security", [])
                for sec_req in security:
                    for sec_name in sec_req:
                        sec_node = f"security:{sec_name}"
                        if G.has_node(sec_node):
                            G.add_edge(node_id, sec_node, relation="REQUIRES_AUTH")

        return cls(G)

    def endpoints(self) -> list[str]:
        return [n for n, d in self.graph.nodes(data=True) if d["type"] == "endpoint"]

    def schemas(self) -> list[str]:
        return [n for n, d in self.graph.nodes(data=True) if d["type"] == "schema"]

    def properties_of(self, schema_name: str) -> list[str]:
        return [
            self.graph.nodes[v].get("name", v.split(".")[-1])
            for _, v, d in self.graph.edges(schema_name, data=True)
            if d.get("relation") == "HAS_PROPERTY"
        ]

    def has_node(self, node_id: str) -> bool:
        return self.graph.has_node(node_id)

    def stats(self) -> dict:
        endpoints = [n for n, d in self.graph.nodes(data=True) if d["type"] == "endpoint"]
        schemas = [n for n, d in self.graph.nodes(data=True) if d["type"] == "schema"]
        return {
            "endpoints": len(endpoints),
            "schemas": len(schemas),
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
        }

    def to_dict(self) -> dict:
        nodes = []
        for n, d in self.graph.nodes(data=True):
            nodes.append({"id": n, **d})
        edges = []
        for u, v, d in self.graph.edges(data=True):
            edges.append({"source": u, "target": v, **d})
        return {"nodes": nodes, "edges": edges}

    @classmethod
    def from_dict(cls, data: dict) -> OpenAPIGraph:
        G = DiGraph()
        for node in data["nodes"]:
            node_id = node.pop("id")
            G.add_node(node_id, **node)
        for edge in data["edges"]:
            G.add_edge(edge.pop("source"), edge.pop("target"), **edge)
        return cls(G)


def _extract_refs_from_properties(G: DiGraph, schema_name: str, properties: dict, all_schemas: dict) -> None:
    """Extract $ref edges from inline properties (e.g., inside allOf)."""
    for _prop_name, prop_def in properties.items():
        ref = _resolve_ref(prop_def)
        if ref and ref in all_schemas:
            G.add_edge(schema_name, ref, relation="REFERENCES")
        items = prop_def.get("items", {})
        item_ref = _resolve_ref(items)
        if item_ref and item_ref in all_schemas:
            G.add_edge(schema_name, item_ref, relation="REFERENCES")


def _resolve_ref(schema: dict) -> str | None:
    """Extract schema name from a $ref pointer like '#/components/schemas/Foo'."""
    ref = schema.get("$ref", "")
    if ref.startswith("#/components/schemas/"):
        return ref.split("/")[-1]
    return None


def _add_properties(G: DiGraph, schema_name: str, schema_def: dict, all_schemas: dict) -> None:
    """Add property nodes and REFERENCES edges for a schema."""
    properties = schema_def.get("properties", {})

    # Handle allOf (merge properties from all sub-schemas)
    for sub in schema_def.get("allOf", []):
        ref = _resolve_ref(sub)
        if ref and ref in all_schemas:
            G.add_edge(schema_name, ref, relation="REFERENCES")
            # Also pull properties from referenced schema
        elif "properties" in sub:
            properties = {**properties, **sub["properties"]}

    for prop_name, prop_def in properties.items():
        prop_node = f"{schema_name}.{prop_name}"
        prop_type = prop_def.get("type", "")
        if not prop_type and "anyOf" in prop_def:
            prop_type = "|".join(t.get("type", "unknown") for t in prop_def["anyOf"] if isinstance(t, dict))

        # Check if property references another schema
        ref = _resolve_ref(prop_def)
        if ref and ref in all_schemas:
            G.add_node(prop_node, type="property", name=prop_name, property_type=f"$ref:{ref}")
            G.add_edge(schema_name, prop_node, relation="HAS_PROPERTY")
            G.add_edge(schema_name, ref, relation="REFERENCES")
            continue

        # Check array items for $ref or allOf
        items = prop_def.get("items", {})
        item_ref = _resolve_ref(items)
        if item_ref and item_ref in all_schemas:
            G.add_node(prop_node, type="property", name=prop_name, property_type=f"array<{item_ref}>")
            G.add_edge(schema_name, prop_node, relation="HAS_PROPERTY")
            G.add_edge(schema_name, item_ref, relation="REFERENCES")
            continue

        # Handle array items with allOf (e.g., SearchResponse.results)
        if "allOf" in items:
            ref_names = []
            for sub in items["allOf"]:
                sub_ref = _resolve_ref(sub)
                if sub_ref and sub_ref in all_schemas:
                    G.add_edge(schema_name, sub_ref, relation="REFERENCES")
                    ref_names.append(sub_ref)
                elif "properties" in sub:
                    # Inline object inside allOf — recurse into its properties
                    _extract_refs_from_properties(G, schema_name, sub["properties"], all_schemas)
            if ref_names:
                G.add_node(
                    prop_node, type="property", name=prop_name, property_type=f"array<{'+'.join(ref_names)}>"
                )
                G.add_edge(schema_name, prop_node, relation="HAS_PROPERTY")
                continue

        G.add_node(
            prop_node,
            type="property",
            name=prop_name,
            property_type=prop_type,
            default=prop_def.get("default"),
        )
        G.add_edge(schema_name, prop_node, relation="HAS_PROPERTY")
