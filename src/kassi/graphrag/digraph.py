"""Minimal directed graph — drop-in replacement for the subset of NetworkX we use.

Zero external dependencies. Implements only what OpenAPIGraph and SubgraphRetriever need:
DiGraph with add_node, add_edge, has_node, nodes(data), edges(data), number_of_nodes/edges.
"""

from __future__ import annotations


class _NodeView:
    """Mimics networkx NodeView: G.nodes[x] for attr access, G.nodes(data=True) for iteration."""

    def __init__(self, nodes: dict[str, dict]):
        self._nodes = nodes

    def __call__(self, data: bool = False):
        if data:
            return list(self._nodes.items())
        return list(self._nodes.keys())

    def __getitem__(self, key):
        return self._nodes[key]

    def __iter__(self):
        return iter(self._nodes)

    def __len__(self):
        return len(self._nodes)

    def __contains__(self, key):
        return key in self._nodes


class _EdgeView:
    """Mimics networkx EdgeView: G.edges[u, v] for attr access, G.edges(node, data=True) for iteration."""

    def __init__(self, adj: dict[str, dict[str, dict]]):
        self._adj = adj

    def __call__(self, node=None, data: bool = False):
        if node is not None:
            adj = self._adj.get(node, {})
            if data:
                return [(node, target, attrs) for target, attrs in adj.items()]
            return [(node, target) for target in adj]
        result = []
        for source, targets in self._adj.items():
            for target, attrs in targets.items():
                if data:
                    result.append((source, target, attrs))
                else:
                    result.append((source, target))
        return result

    def __getitem__(self, key):
        source, target = key
        return self._adj[source][target]


class DiGraph:
    """Minimal directed graph backed by plain dicts."""

    def __init__(self):
        self._nodes: dict[str, dict] = {}
        self._adj: dict[str, dict[str, dict]] = {}  # source -> {target -> edge_data}

    def add_node(self, node_id: str, **attrs):
        if node_id in self._nodes:
            self._nodes[node_id].update(attrs)
        else:
            self._nodes[node_id] = attrs
            self._adj.setdefault(node_id, {})

    def add_edge(self, source: str, target: str, **attrs):
        self._adj.setdefault(source, {})[target] = attrs
        self._nodes.setdefault(source, {})
        self._nodes.setdefault(target, {})
        self._adj.setdefault(target, {})

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def has_edge(self, source: str, target: str) -> bool:
        return target in self._adj.get(source, {})

    def successors(self, node: str):
        return list(self._adj.get(node, {}).keys())

    def number_of_nodes(self) -> int:
        return len(self._nodes)

    def number_of_edges(self) -> int:
        return sum(len(targets) for targets in self._adj.values())

    @property
    def nodes(self) -> _NodeView:
        return _NodeView(self._nodes)

    @property
    def edges(self) -> _EdgeView:
        return _EdgeView(self._adj)

    def subgraph(self, nodes) -> DiGraph:
        """Return a new DiGraph containing only the specified nodes and edges between them."""
        node_set = set(nodes)
        sub = DiGraph()
        for n in node_set:
            if n in self._nodes:
                sub.add_node(n, **self._nodes[n])
        for source in node_set:
            for target, attrs in self._adj.get(source, {}).items():
                if target in node_set:
                    sub.add_edge(source, target, **attrs)
        return sub
