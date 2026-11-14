"""Building the placement previews the dashboard shows, away from the event loop.

The previews endpoint asks placement the same question once per candidate ring size and
engine, which is 4N questions for an N-node cluster. Enumerating the topology's cycles
is the expensive part of answering one and does not depend on the question, so this
enumerates once and reuses it, and it is a plain synchronous function so a caller can
run the whole batch in a thread instead of blocking the node's only event loop.
"""

from exo.api.types.api import PlacementPreview, PlacementPreviewResponse
from exo.master.placement import (
    CycleSearch,
    PlacementSearchTruncatedError,
    place_instance,
    search_placement_cycles,
    shard_bytes,
)
from exo.shared.constants import (
    EXO_PLACEMENT_MAX_CYCLE_NODES,
    EXO_PLACEMENT_MAX_CYCLES,
)
from exo.shared.models.model_cards import ModelCard
from exo.shared.topology import Topology
from exo.shared.types.common import NodeId
from exo.shared.types.placement import PlacementRequest
from exo.shared.types.state import State
from exo.shared.types.worker.instances import InstanceId, InstanceMeta
from exo.shared.types.worker.shards import Sharding

PREVIEW_SHARDINGS: tuple[Sharding, ...] = (Sharding.Pipeline, Sharding.Tensor)
"""The shardings previewed, in the order the previews are returned."""

PREVIEW_INSTANCE_METAS: tuple[InstanceMeta, ...] = (
    InstanceMeta.MlxRing,
    InstanceMeta.MlxJaccl,
)
"""The engines previewed, in the order the previews are returned."""

_NO_PLACEMENT = 0
"""The node count a failed preview is recorded under, so each failure is reported once."""

_NOT_SEARCHED = -1
"""The node count a preview whose ring sizes were never enumerated is recorded under.

Separate from ``_NO_PLACEMENT`` so that "no ring of any searched size fits" and "the rings
that might have fitted were not searched" are both reported, instead of whichever the
loop reached first standing for both.
"""


def build_placement_previews(
    model_card: ModelCard,
    state: State,
    required_nodes: set[NodeId] | None = None,
    max_cycles: int = EXO_PLACEMENT_MAX_CYCLES,
    max_cycle_nodes: int = EXO_PLACEMENT_MAX_CYCLE_NODES,
    *,
    topology: Topology | None = None,
) -> PlacementPreviewResponse:
    """Place ``model_card`` every way the cluster allows and report each distinct result.

    One preview per (sharding, engine, node count) placement can produce, and one
    carrying the error for each (sharding, engine) it cannot produce at all. Returns no
    previews for a topology with no nodes.

    ``state`` must be a single snapshot, because every preview in one response is
    computed against it: a caller running this in a thread reads ``self.state`` once
    before the call rather than once per preview. The limits are parameters so a test can
    reach the bounded search without building a topology large enough to need it.

    ``topology`` is what to place on when that is not all of ``state.topology``: the API
    passes the state's topology without the nodes it cannot reach now, since a model
    could not load there.
    """
    if topology is None:
        topology = state.topology
    node_ids = list(topology.list_nodes())
    if not node_ids:
        return PlacementPreviewResponse(previews=[])

    cycle_search = search_placement_cycles(topology, max_cycles, max_cycle_nodes)
    current_instance_ids = set(state.instances.keys())

    seen: set[tuple[Sharding, InstanceMeta, int]] = set()
    previews: list[PlacementPreview] = []

    for sharding in PREVIEW_SHARDINGS:
        for instance_meta in PREVIEW_INSTANCE_METAS:
            for min_nodes in range(1, len(node_ids) + 1):
                preview, not_searched = _preview(
                    model_card,
                    state,
                    topology,
                    cycle_search,
                    current_instance_ids,
                    required_nodes,
                    sharding,
                    instance_meta,
                    min_nodes,
                )
                if preview.instance is not None:
                    placed_nodes = len(
                        preview.instance.shard_assignments.node_to_runner
                    )
                elif not_searched:
                    placed_nodes = _NOT_SEARCHED
                else:
                    placed_nodes = _NO_PLACEMENT
                if (sharding, instance_meta, placed_nodes) not in seen:
                    previews.append(preview)
                seen.add((sharding, instance_meta, placed_nodes))

    return PlacementPreviewResponse(
        previews=previews, max_cycle_nodes=cycle_search.max_cycle_nodes
    )


def _preview(
    model_card: ModelCard,
    state: State,
    topology: Topology,
    cycle_search: CycleSearch,
    current_instance_ids: set[InstanceId],
    required_nodes: set[NodeId] | None,
    sharding: Sharding,
    instance_meta: InstanceMeta,
    min_nodes: int,
) -> tuple[PlacementPreview, bool]:
    """Place one candidate, reporting the reason as the preview's error if it cannot be.

    The flag says whether the failure was that the rings were never enumerated, which the
    caller keeps distinct from a ring size that was searched and did not fit.
    """
    try:
        placements = place_instance(
            PlacementRequest(
                model_card=model_card,
                sharding=sharding,
                instance_meta=instance_meta,
                min_nodes=min_nodes,
            ),
            topology,
            state.instances,
            state.node_memory,
            state.node_network,
            state.node_backends,
            required_nodes=required_nodes,
            download_status=state.downloads,
            node_rdma_ctl=state.node_rdma_ctl,
            runners=state.runners,
            node_identities=state.node_identities,
            cycle_search=cycle_search,
        )
    except ValueError as exc:
        return (
            PlacementPreview(
                model_id=model_card.model_id,
                sharding=sharding,
                instance_meta=instance_meta,
                instance=None,
                error=str(exc),
            ),
            isinstance(exc, PlacementSearchTruncatedError),
        )

    new_instances = [
        instance
        for instance_id, instance in placements.items()
        if instance_id not in current_instance_ids
    ]
    if len(new_instances) != 1:
        return (
            PlacementPreview(
                model_id=model_card.model_id,
                sharding=sharding,
                instance_meta=instance_meta,
                instance=None,
                error="Expected exactly one new instance from placement",
            ),
            False,
        )

    instance = new_instances[0]
    shard_assignments = instance.shard_assignments
    # Each node's share: pipeline gives a node layers in proportion to its memory. Keys
    # are ``NodeId`` strings because the field crosses the HTTP boundary.
    memory_delta_by_node = {
        str(node_id): shard_bytes(shard_assignments.runner_to_shard[runner_id])
        for node_id, runner_id in shard_assignments.node_to_runner.items()
    }
    return (
        PlacementPreview(
            model_id=model_card.model_id,
            sharding=sharding,
            instance_meta=instance_meta,
            instance=instance,
            memory_delta_by_node=memory_delta_by_node or None,
            error=None,
        ),
        False,
    )
