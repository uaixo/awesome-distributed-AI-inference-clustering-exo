"""What GET /instance/previews computes, and what it costs to compute it."""

import time

import pytest

from exo.api.placement_previews import build_placement_previews
from exo.master.tests.conftest import create_mesh_topology, create_node_memory
from exo.shared.constants import EXO_PLACEMENT_MAX_CYCLE_NODES
from exo.shared.models.model_cards import ModelCard, ModelId, ModelTask
from exo.shared.topology import Topology
from exo.shared.types.backends import Backend
from exo.shared.types.common import NodeId
from exo.shared.types.memory import Memory
from exo.shared.types.profiling import NetworkInterfaceInfo, NodeNetworkInfo
from exo.shared.types.state import State

# A ten-node mesh is 1.1 million simple cycles and a nine-node one 125,673, so a cluster
# this size is where enumerating every cycle stops being affordable at all.
BOUNDED_CLUSTER_NODES = 11
BOUNDED_CLUSTER_BUDGET_SECONDS = 10.0


@pytest.fixture
def model_card() -> ModelCard:
    return ModelCard(
        model_id=ModelId("test-model"),
        storage_size=Memory.from_kb(1000),
        n_layers=10,
        hidden_size=30,
        supports_tensor=True,
        tasks=[ModelTask.TextGeneration],
        backends=[Backend.MlxMetal],
    )


def _state(node_count: int) -> tuple[State, list[NodeId]]:
    topology, node_ids = create_mesh_topology(node_count)
    network = NodeNetworkInfo(
        interfaces=[
            NetworkInterfaceInfo(name="en0", ip_address=f"169.254.0.{index + 1}")
            for index in range(node_count)
        ]
    )
    return (
        State(
            topology=topology,
            node_memory={node_id: create_node_memory(10**9) for node_id in node_ids},
            node_network={node_id: network for node_id in node_ids},
            node_backends={node_id: [Backend.MlxMetal] for node_id in node_ids},
        ),
        node_ids,
    )


def test_a_cluster_with_no_nodes_has_nothing_to_preview(model_card: ModelCard):
    assert build_placement_previews(model_card, State()).previews == []


def test_the_topology_is_enumerated_once_for_the_whole_response(
    model_card: ModelCard, monkeypatch: pytest.MonkeyPatch
):
    state, node_ids = _state(4)
    calls = 0
    enumerate_cycles = Topology.get_cycles

    def counting_get_cycles(topology: Topology):
        nonlocal calls
        calls += 1
        return enumerate_cycles(topology)

    monkeypatch.setattr(Topology, "get_cycles", counting_get_cycles)

    build_placement_previews(model_card, state)

    # One preview is asked for per sharding, engine and ring size, which is 4N questions
    # for N nodes. Every one of them used to enumerate the topology again.
    assert calls == 1, f"enumerated {calls} times for {4 * len(node_ids)} previews"


def test_a_small_cluster_is_searched_exhaustively_and_says_so(model_card: ModelCard):
    state, _ = _state(3)

    response = build_placement_previews(model_card, state)

    assert response.max_cycle_nodes is None
    assert {
        (
            preview.sharding.value,
            preview.instance_meta.value,
            None
            if preview.instance is None
            else len(preview.instance.shard_assignments.node_to_runner),
        )
        for preview in response.previews
    } == {
        ("Pipeline", "MlxRing", 1),
        ("Pipeline", "MlxRing", 2),
        ("Pipeline", "MlxRing", 3),
        ("Pipeline", "MlxJaccl", None),
        ("Tensor", "MlxRing", 2),
        ("Tensor", "MlxRing", 3),
        ("Tensor", "MlxRing", 1),
        ("Tensor", "MlxJaccl", None),
    }


def test_required_nodes_appear_in_every_preview(model_card: ModelCard):
    state, node_ids = _state(4)
    required = {node_ids[1], node_ids[3]}

    response = build_placement_previews(model_card, state, required)

    placed = [preview for preview in response.previews if preview.instance is not None]
    assert placed
    for preview in placed:
        assert preview.instance is not None
        assert required <= set(preview.instance.shard_assignments.node_to_runner)


def test_a_cluster_too_large_to_enumerate_still_answers_within_a_budget(
    model_card: ModelCard,
):
    state, node_ids = _state(BOUNDED_CLUSTER_NODES)

    started = time.monotonic()
    response = build_placement_previews(model_card, state)
    elapsed = time.monotonic() - started

    # Enumerating every cycle of this mesh takes about a minute and allocates millions of
    # objects, so without a limit this request could not be answered at all.
    assert elapsed < BOUNDED_CLUSTER_BUDGET_SECONDS, f"took {elapsed:.1f}s"
    assert response.max_cycle_nodes == EXO_PLACEMENT_MAX_CYCLE_NODES
    assert response.previews
    placed_sizes = {
        len(preview.instance.shard_assignments.node_to_runner)
        for preview in response.previews
        if preview.instance is not None
    }
    assert placed_sizes
    assert max(placed_sizes) <= EXO_PLACEMENT_MAX_CYCLE_NODES
    assert len(node_ids) == BOUNDED_CLUSTER_NODES
