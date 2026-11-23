"""gr681314: node-role labels for federation route hints.

The agent-facing runtime surface names the *role* that serves a kind
(``compute node``), never the hostname. Roles derive from the serving
``PRECIS_PROCESS`` name; several hosts under one role get stable numeric
suffixes ordered by host hash. See :func:`precis.utils.hostname.node_role_labels`.
"""

from __future__ import annotations

from precis.utils.hostname import node_role_labels, role_label_for_process


class TestRoleLabelForProcess:
    def test_worker_lane_prefix_becomes_role(self) -> None:
        assert role_label_for_process("precis-worker-compute") == "compute"

    def test_bare_worker(self) -> None:
        assert role_label_for_process("precis-worker") == "worker"

    def test_web_process_variants(self) -> None:
        assert role_label_for_process("precis-web") == "web"
        # A user-suffixed dev process must not leak the user name.
        assert role_label_for_process("web:someuser") == "web"

    def test_other_precis_process(self) -> None:
        assert role_label_for_process("asa-bot") == "asa-bot"
        assert role_label_for_process("precis-worker-drain-kimi") == "drain-kimi"

    def test_sanitization_and_fallback(self) -> None:
        assert role_label_for_process("Precis_Worker GPU!") == "gpu"
        assert role_label_for_process("Weird Name!") == "weird-name"
        assert role_label_for_process("") == "fleet"
        assert role_label_for_process("::: :::") == "fleet"


class TestNodeRoleLabels:
    def test_lone_host_is_just_the_role(self) -> None:
        labels = node_role_labels([("maple", "precis-worker-fetch")])
        assert labels == ["fetch node"]

    def test_multiple_roles_sorted(self) -> None:
        labels = node_role_labels(
            [
                ("maple", "precis-worker-compute"),
                ("cedar", "precis-web"),
            ]
        )
        assert labels == ["compute node", "web node"]

    def test_shared_role_gets_stable_suffixes(self) -> None:
        routes = [
            ("maple", "precis-worker-research"),
            ("cedar", "precis-worker-research"),
            ("birch", "precis-worker-research"),
        ]
        labels = node_role_labels(routes)
        assert labels == ["research-1 node", "research-2 node", "research-3 node"]
        # Suffix assignment is stable across calls regardless of input order.
        assert node_role_labels(list(reversed(routes))) == labels

    def test_same_host_two_processes_groups_by_host(self) -> None:
        """One host advertising the kind via two processes is one host."""
        labels = node_role_labels(
            [
                ("maple", "precis-worker"),
                ("maple", "precis-worker-fetch"),
            ]
        )
        assert labels == ["fetch node", "worker node"]

    def test_no_routes_no_labels(self) -> None:
        assert node_role_labels([]) == []

    def test_hostnames_never_appear(self) -> None:
        routes = [
            ("secret-hostname-alpha", "precis-worker-compute"),
            ("secret-hostname-beta", "precis-worker-compute"),
            ("secret-hostname-gamma", "precis-web"),
        ]
        for label in node_role_labels(routes):
            assert "secret" not in label
            assert "hostname" not in label
            assert "alpha" not in label
            assert "beta" not in label
            assert "gamma" not in label
