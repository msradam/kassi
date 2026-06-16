"""Tests for the OpenAPI GraphRAG module: graph builder and subgraph retriever."""

import json
from pathlib import Path

import pytest

from kassi.graphrag.builder import OpenAPIGraph
from kassi.graphrag.retriever import SubgraphRetriever

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def midas_spec():
    with open(FIXTURES / "midas-openapi.json") as f:
        return json.load(f)


@pytest.fixture
def calliope_spec():
    with open(FIXTURES / "calliope-openapi.json") as f:
        return json.load(f)


@pytest.fixture
def midas_graph(midas_spec):
    return OpenAPIGraph.from_spec(midas_spec)


@pytest.fixture
def calliope_graph(calliope_spec):
    return OpenAPIGraph.from_spec(calliope_spec)


@pytest.fixture
def midas_retriever(midas_graph):
    return SubgraphRetriever(midas_graph)


@pytest.fixture
def calliope_retriever(calliope_graph):
    return SubgraphRetriever(calliope_graph)


# ── Node creation ──


class TestNodeCreation:
    def test_endpoints_become_nodes(self, midas_graph):
        endpoint_nodes = midas_graph.endpoints()
        assert len(endpoint_nodes) >= 9
        assert "POST /api/auth/login" in endpoint_nodes
        assert "GET /api/health" in endpoint_nodes

    def test_schemas_become_nodes(self, midas_graph):
        schema_nodes = midas_graph.schemas()
        assert "RegisterRequest" in schema_nodes
        assert "AuthResponse" in schema_nodes
        assert "AccountOut" in schema_nodes

    def test_properties_become_nodes(self, midas_graph):
        props = midas_graph.properties_of("LoginRequest")
        assert "email" in props
        assert "password" in props

    def test_security_schemes_become_nodes(self, calliope_graph):
        assert calliope_graph.has_node("security:BearerAuth")

    def test_node_types(self, midas_graph):
        G = midas_graph.graph
        for node, data in G.nodes(data=True):
            assert "type" in data, f"Node {node} missing 'type' attribute"
            assert data["type"] in ("endpoint", "schema", "property", "security", "parameter")


# ── Edge creation ──


class TestEdgeCreation:
    def test_endpoint_returns_schema(self, midas_graph):
        G = midas_graph.graph
        assert G.has_edge("POST /api/auth/login", "AuthResponse")
        edge_data = G.edges["POST /api/auth/login", "AuthResponse"]
        assert edge_data["relation"] == "RETURNS"

    def test_endpoint_accepts_schema(self, midas_graph):
        G = midas_graph.graph
        assert G.has_edge("POST /api/auth/login", "LoginRequest")
        edge_data = G.edges["POST /api/auth/login", "LoginRequest"]
        assert edge_data["relation"] == "ACCEPTS"

    def test_schema_has_property(self, midas_graph):
        G = midas_graph.graph
        assert G.has_edge("LoginRequest", "LoginRequest.email")
        edge_data = G.edges["LoginRequest", "LoginRequest.email"]
        assert edge_data["relation"] == "HAS_PROPERTY"

    def test_schema_references_schema(self, midas_graph):
        G = midas_graph.graph
        assert G.has_edge("AuthResponse", "UserOut")
        edge_data = G.edges["AuthResponse", "UserOut"]
        assert edge_data["relation"] == "REFERENCES"

    def test_endpoint_requires_auth(self, calliope_graph):
        G = calliope_graph.graph
        assert G.has_edge("POST /api/books", "security:BearerAuth")
        edge_data = G.edges["POST /api/books", "security:BearerAuth"]
        assert edge_data["relation"] == "REQUIRES_AUTH"

    def test_endpoint_has_parameter(self, midas_graph):
        G = midas_graph.graph
        param_edges = [
            (u, v)
            for u, v, d in G.edges(data=True)
            if u == "GET /api/accounts/{account_id}" and d.get("relation") == "HAS_PARAM"
        ]
        assert len(param_edges) >= 1

    def test_unsecured_endpoint_no_auth_edge(self, midas_graph):
        G = midas_graph.graph
        auth_edges = [
            (u, v)
            for u, v, d in G.edges(data=True)
            if u == "GET /api/health" and d.get("relation") == "REQUIRES_AUTH"
        ]
        assert len(auth_edges) == 0


# ── Node attributes ──


class TestNodeAttributes:
    def test_endpoint_has_method_and_path(self, midas_graph):
        G = midas_graph.graph
        data = G.nodes["POST /api/auth/login"]
        assert data["method"] == "POST"
        assert data["path"] == "/api/auth/login"

    def test_endpoint_has_summary(self, midas_graph):
        G = midas_graph.graph
        data = G.nodes["POST /api/auth/login"]
        assert "summary" in data

    def test_property_has_type_info(self, midas_graph):
        G = midas_graph.graph
        data = G.nodes["LoginRequest.email"]
        assert data["property_type"] == "string"

    def test_schema_has_required_fields(self, midas_graph):
        G = midas_graph.graph
        data = G.nodes["LoginRequest"]
        assert "required" in data
        assert "email" in data["required"]
        assert "password" in data["required"]


# ── Cross-spec compatibility ──


class TestCalliopeSpec:
    def test_calliope_endpoints(self, calliope_graph):
        endpoints = calliope_graph.endpoints()
        assert "GET /api/books" in endpoints
        assert "GET /api/books/search" in endpoints
        assert "POST /api/books/{id}/reviews" in endpoints

    def test_calliope_nested_refs(self, calliope_graph):
        G = calliope_graph.graph
        refs = [
            v for u, v, d in G.edges(data=True) if u == "SearchResponse" and d.get("relation") == "REFERENCES"
        ]
        assert len(refs) >= 2

    def test_calliope_schema_count(self, calliope_graph):
        schemas = calliope_graph.schemas()
        assert len(schemas) >= 12


# ── Serialization ──


class TestSerialization:
    def test_to_dict_roundtrip(self, midas_graph):
        data = midas_graph.to_dict()
        assert "nodes" in data
        assert "edges" in data
        restored = OpenAPIGraph.from_dict(data)
        assert set(restored.endpoints()) == set(midas_graph.endpoints())
        assert set(restored.schemas()) == set(midas_graph.schemas())

    def test_stats(self, midas_graph):
        stats = midas_graph.stats()
        assert stats["endpoints"] >= 9
        assert stats["schemas"] >= 5
        assert stats["edges"] > stats["endpoints"]


# ── Endpoint-based retrieval ──


class TestEndpointRetrieval:
    def test_retrieve_single_endpoint(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        assert "LoginRequest" in context.schemas
        assert "AuthResponse" in context.schemas
        assert "POST /api/auth/login" in context.endpoints

    def test_retrieve_includes_nested_refs(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        assert "UserOut" in context.schemas

    def test_retrieve_includes_auth(self, calliope_retriever):
        context = calliope_retriever.for_endpoints(["POST /api/books"])
        assert context.requires_auth

    def test_retrieve_excludes_unrelated(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        assert "AccountOut" not in context.schemas
        assert "TransactionOut" not in context.schemas

    def test_retrieve_multiple_endpoints(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login", "GET /api/accounts"])
        assert "LoginRequest" in context.schemas
        assert "AccountOut" in context.schemas
        assert "AccountListResponse" in context.schemas

    def test_retrieve_with_parameters(self, midas_retriever):
        context = midas_retriever.for_endpoints(["GET /api/accounts/{account_id}"])
        assert len(context.parameters) >= 1
        param_names = [p["name"] for p in context.parameters]
        assert "account_id" in param_names


# ── Diff-based retrieval ──


class TestDiffRetrieval:
    def test_diff_identifies_changed_endpoints(self, midas_retriever):
        diff = """
diff --git a/demos/midas-bank/app.py b/demos/midas-bank/app.py
--- a/demos/midas-bank/app.py
+++ b/demos/midas-bank/app.py
@@ -280,6 +280,46 @@
+@app.post("/api/transactions/transfer", status_code=201, response_model=TransactionOut)
+def transfer(req: TransferRequest, user=Depends(get_current_user), db=Depends(get_db)):
+    if req.amount <= 0:
"""
        endpoints = midas_retriever.endpoints_from_diff(diff)
        assert "POST /api/transactions/transfer" in endpoints

    def test_diff_retrieval_end_to_end(self, midas_retriever):
        diff = """
+@app.post("/api/transactions/transfer", status_code=201)
+def transfer(req: TransferRequest, user=Depends(get_current_user)):
"""
        endpoints = midas_retriever.endpoints_from_diff(diff)
        assert len(endpoints) >= 1
        context = midas_retriever.for_endpoints(endpoints)
        assert len(context.schemas) >= 1

    def test_diff_with_express_routes(self, calliope_retriever):
        diff = """
+router.get('/api/books/search', async (req, res) => {
+  const { q, limit = 20, offset = 0 } = req.query;
"""
        endpoints = calliope_retriever.endpoints_from_diff(diff)
        assert any("books/search" in ep or "/api/books" in ep for ep in endpoints)


# ── Context serialization ──


class TestContextSerialization:
    def test_to_text_includes_schemas(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        text = context.to_text()
        assert "LoginRequest" in text
        assert "AuthResponse" in text
        assert "email" in text
        assert "password" in text

    def test_to_text_includes_endpoint_info(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        text = context.to_text()
        assert "POST" in text
        assert "/api/auth/login" in text

    def test_to_text_includes_auth_hint(self, calliope_retriever):
        context = calliope_retriever.for_endpoints(["POST /api/books"])
        text = context.to_text()
        assert "auth" in text.lower() or "bearer" in text.lower()

    def test_to_text_compact(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        text = context.to_text()
        assert len(text) < 2000

    def test_to_dict(self, midas_retriever):
        context = midas_retriever.for_endpoints(["POST /api/auth/login"])
        d = context.to_dict()
        assert "endpoints" in d
        assert "schemas" in d
        assert isinstance(d["schemas"], dict)


# ── Edge cases ──


class TestEdgeCases:
    def test_nonexistent_endpoint(self, midas_retriever):
        context = midas_retriever.for_endpoints(["GET /api/nonexistent"])
        assert len(context.schemas) == 0
        assert len(context.endpoints) == 0

    def test_empty_endpoint_list(self, midas_retriever):
        context = midas_retriever.for_endpoints([])
        assert len(context.schemas) == 0

    def test_health_endpoint_minimal_context(self, midas_retriever):
        context = midas_retriever.for_endpoints(["GET /api/health"])
        assert not context.requires_auth
        assert "HealthResponse" in context.schemas
        assert len(context.schemas) <= 2
