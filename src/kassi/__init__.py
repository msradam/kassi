"""kassi — a diff-driven load-test agent built on Burr + theodosia.

The workflow is a Burr state machine served over MCP by theodosia: an agent
drives it one legal ``step`` at a time, every step and every refusal is recorded
to an immutable ledger, and all k6 work (validation + execution) is delegated to
the official Grafana k6 MCP server wired in as an upstream.
"""

from kassi.app import build_application

__all__ = ["build_application"]
