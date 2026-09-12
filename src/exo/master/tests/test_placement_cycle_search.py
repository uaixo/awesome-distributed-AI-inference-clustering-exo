"""How placement finds the rings it may use, and what it costs."""

from collections.abc import Sequence
from typing import final, override

import pytest

from exo.master.placement import (
    CycleSearch,
    PlacementSearchTruncatedError,
    group_cycles_by_length,
    place_instance,
    search_placement_cycles,
)
from exo.master.tests.conftest import (
    create_mesh_topology,
    create_node_memory,
    create_ring_topology,
)
from exo.shared.models.model_cards import ModelCard, ModelId, ModelTask
from exo.shared.topology import Topology
from exo.shared.types.backends import Backend
from exo.shared.types.commands import CommandId, PlaceInstance
from exo.shared.types.common import NodeId
from exo.shared.types.memory import Memory
from exo.shared.types.profiling import NodeNetworkInfo
from exo.shared.types.topology import Cycle
from exo.shared.types.worker.instances import InstanceMeta
from exo.shared.types.worker.shards import Sharding


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


def test_a_cluster_within_the_budget_is_searched_exhaustively():
    topology, _ = create_mesh_topology(5)

    search = search_placement_cycles(topology, max_cycles=_simple_cycle_count(topology))

    assert search.max_cycle_nodes is None
    # Group for group and cycle for cycle: the groups are what placement reads and their
    # order is what breaks its ties.
    assert search.by_length == group_cycles_by_length(topology.get_cycles())
    assert max(search.by_length) == 5


def test_a_cluster_over_the_budget_is_searched_up_to_the_ring_limit():
    topology, _ = create_mesh_topology(6)

    search = search_placement_cycles(topology, max_cycles=10, max_cycle_nodes=3)

    assert search.max_cycle_nodes == 3
    assert max(search.by_length) == 3
    # Reporting the limit is what stops a missing four-node ring reading as "does not
    # fit" when it only means "not searched".
    assert len(search.cycles) < len(topology.get_cycles())


def test_a_sparse_cluster_is_searched_exhaustively_however_many_nodes_it_has():
    # Forty Macs in a Thunderbolt loop: far more nodes than any mesh this budget would
    # admit, but only 42 cycles, so the exact answer is still affordable. Budgeting by
    # node count instead of by cycles would needlessly bound this one, and bounding it
    # would rewire the forty node ring that is the whole point of the topology.
    topology, node_ids = create_ring_topology(40)

    search = search_placement_cycles(topology)

    assert search.max_cycle_nodes is None
    assert search.by_length == group_cycles_by_length(topology.get_cycles())
    assert max(search.by_length) == len(node_ids)


def test_the_budgeted_enumeration_matches_the_unbounded_one_exactly():
    topology, _ = create_mesh_topology(5)
    every_cycle = topology.get_cycles()
    exactly_enough = _simple_cycle_count(topology)

    # Element for element, not as sets: shard ranks, ring neighbours and the tie-break
    # between equally good rings all follow from the order and the anchor node.
    assert topology.get_cycles_within_budget(exactly_enough) == every_cycle
    assert topology.get_cycles_within_budget(exactly_enough + 1) == every_cycle


def test_a_topology_over_the_budget_yields_nothing_rather_than_a_partial_answer():
    topology, _ = create_mesh_topology(5)

    # Half a length group would silently shrink the set placement breaks ties over, for a
    # ring size that still looks fully searched.
    assert topology.get_cycles_within_budget(_simple_cycle_count(topology) - 1) is None
    assert topology.get_cycles_within_budget(1) is None


def _simple_cycle_count(topology: Topology) -> int:
    """How many cycles the budget counts: everything but the per-node singletons."""
    return len(topology.get_cycles()) - len(list(topology.list_nodes()))


def test_a_budget_no_topology_could_satisfy_is_refused():
    topology, _ = create_mesh_topology(3)

    for max_cycles in (-1, 0):
        with pytest.raises(ValueError, match="at least 1"):
            topology.get_cycles_within_budget(max_cycles)


def test_grouping_keeps_the_order_that_breaks_ties_between_equal_cycles():
    topology, _ = create_mesh_topology(4)
    cycles = topology.get_cycles()

    grouped = group_cycles_by_length(cycles)

    for length, group in grouped.items():
        assert list(group) == [cycle for cycle in cycles if len(cycle) == length]


def test_placement_stops_at_the_first_ring_size_that_fits(
    model_card: ModelCard,
):
    topology, node_ids = create_mesh_topology(3)
    node_memory = {node_id: create_node_memory(10**6) for node_id in node_ids}
    grouped = dict(group_cycles_by_length(topology.get_cycles()))
    longest = max(grouped)
    grouped[longest] = _RefusesToBeRead()

    placements = place_instance(
        PlaceInstance(
            command_id=CommandId(),
            model_card=model_card,
            sharding=Sharding.Pipeline,
            instance_meta=InstanceMeta.MlxRing,
            min_nodes=1,
        ),
        topology,
        {},
        node_memory,
        {node_id: NodeNetworkInfo() for node_id in node_ids},
        {node_id: [Backend.MlxMetal] for node_id in node_ids},
        cycle_search=CycleSearch(by_length=grouped, max_cycle_nodes=None),
    )

    # No single node holds this model but a pair does, so the search passes over the
    # one-node group and stops at the two-node one. Reading the three-node group would
    # have failed the test, and on a real mesh that group is most of the topology.
    instance = next(iter(placements.values()))
    assert len(instance.shard_assignments.node_to_runner) == 2


def test_a_ring_size_nothing_offers_reports_memory_rather_than_reading_further(
    model_card: ModelCard,
):
    topology, node_ids = create_mesh_topology(3)
    node_memory = {node_id: create_node_memory(10**6) for node_id in node_ids}

    with pytest.raises(ValueError, match="No cycles found with sufficient memory"):
        place_instance(
            PlaceInstance(
                command_id=CommandId(),
                model_card=model_card,
                sharding=Sharding.Pipeline,
                instance_meta=InstanceMeta.MlxRing,
                min_nodes=len(node_ids) + 1,
            ),
            topology,
            {},
            node_memory,
            {node_id: NodeNetworkInfo() for node_id in node_ids},
            {node_id: [Backend.MlxMetal] for node_id in node_ids},
            cycle_search=search_placement_cycles(topology),
        )


@final
class _RefusesToBeRead(Sequence[Cycle]):
    """A cycle group that fails the test if placement reads it at all."""

    @override
    def __len__(self) -> int:
        return 1

    @override
    def __getitem__(self, index: object) -> Cycle:  # pyright: ignore[reportIncompatibleMethodOverride]
        raise AssertionError("placement read a cycle group longer than the one it used")

    @override
    def __iter__(self) -> "NodeId":  # pyright: ignore[reportIncompatibleMethodOverride]
        raise AssertionError("placement read a cycle group longer than the one it used")


def _place(
    model_card: ModelCard,
    node_count: int = 3,
    node_ram: int = 10**6,
    sharding: Sharding = Sharding.Pipeline,
    min_nodes: int = 1,
):
    topology, node_ids = create_mesh_topology(node_count)
    return place_instance(
        PlaceInstance(
            command_id=CommandId(),
            model_card=model_card,
            sharding=sharding,
            instance_meta=InstanceMeta.MlxRing,
            min_nodes=min_nodes,
        ),
        topology,
        {},
        {node_id: create_node_memory(node_ram) for node_id in node_ids},
        {node_id: NodeNetworkInfo() for node_id in node_ids},
        {node_id: [Backend.MlxMetal] for node_id in node_ids},
        cycle_search=search_placement_cycles(topology),
    )


def test_gemma_4_places_on_the_one_node_that_holds_it(model_card: ModelCard):
    gemma = model_card.model_copy(update={"base_model": "Gemma 4 27B"})

    placements = _place(gemma, node_ram=10**9)

    instance = next(iter(placements.values()))
    assert len(instance.shard_assignments.node_to_runner) == 1


def test_gemma_4_is_refused_when_no_single_node_holds_it(model_card: ModelCard):
    gemma = model_card.model_copy(update={"base_model": "Gemma 4 27B"})

    # Two nodes together hold it, which pipeline sharding could normally use, but Gemma 4
    # can only be placed on one node.
    with pytest.raises(ValueError, match="not supported for Gemma 4"):
        _place(gemma, node_ram=600_000)


def test_deep_seek_v3_1_8bit_refuses_pipeline_sharding(model_card: ModelCard):
    deepseek = model_card.model_copy(
        update={"model_id": ModelId("mlx-community/DeepSeek-V3.1-8bit")}
    )

    with pytest.raises(ValueError, match="DeepSeek V3.1"):
        _place(deepseek, node_ram=10**9)


def test_a_model_without_tensor_support_refuses_tensor_sharding(model_card: ModelCard):
    no_tensor = model_card.model_copy(update={"supports_tensor": False})

    with pytest.raises(ValueError, match="does not support tensor parallelism"):
        _place(no_tensor, node_ram=10**9, sharding=Sharding.Tensor)


def test_a_cluster_too_small_reports_memory_rather_than_the_sharding(
    model_card: ModelCard,
):
    no_tensor = model_card.model_copy(update={"supports_tensor": False})

    # Both would raise. Memory comes first, because a cluster that cannot hold the model
    # at all has not got as far as choosing how to shard it.
    with pytest.raises(ValueError, match="No cycles found with sufficient memory"):
        _place(no_tensor, node_ram=1, sharding=Sharding.Tensor)


def test_a_ring_size_that_was_never_searched_is_not_reported_as_a_memory_problem(
    model_card: ModelCard,
):
    topology, node_ids = create_mesh_topology(6)
    # Every node holds the whole model many times over, so memory is not the obstacle.
    node_memory = {node_id: create_node_memory(10**12) for node_id in node_ids}
    search = search_placement_cycles(topology, max_cycles=10, max_cycle_nodes=3)

    with pytest.raises(PlacementSearchTruncatedError) as raised:
        place_instance(
            PlaceInstance(
                command_id=CommandId(),
                model_card=model_card,
                sharding=Sharding.Pipeline,
                instance_meta=InstanceMeta.MlxRing,
                min_nodes=5,
            ),
            topology,
            {},
            node_memory,
            {node_id: NodeNetworkInfo() for node_id in node_ids},
            {node_id: [Backend.MlxMetal] for node_id in node_ids},
            cycle_search=search,
        )

    assert raised.value.max_cycle_nodes == 3
    assert "were not searched" in str(raised.value)


def test_a_searched_ring_size_that_does_not_fit_still_reports_memory(
    model_card: ModelCard,
):
    topology, node_ids = create_mesh_topology(3)
    node_memory = {node_id: create_node_memory(1) for node_id in node_ids}

    # The cluster was searched in full, so nothing was withheld and the honest answer is
    # that it cannot hold the model.
    with pytest.raises(
        ValueError, match="No cycles found with sufficient memory"
    ) as raised:
        place_instance(
            PlaceInstance(
                command_id=CommandId(),
                model_card=model_card,
                sharding=Sharding.Pipeline,
                instance_meta=InstanceMeta.MlxRing,
                min_nodes=1,
            ),
            topology,
            {},
            node_memory,
            {node_id: NodeNetworkInfo() for node_id in node_ids},
            {node_id: [Backend.MlxMetal] for node_id in node_ids},
            cycle_search=search_placement_cycles(topology),
        )

    assert not isinstance(raised.value, PlacementSearchTruncatedError)
