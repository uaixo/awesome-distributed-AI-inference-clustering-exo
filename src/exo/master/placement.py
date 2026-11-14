from collections import defaultdict
from collections.abc import Iterable, Mapping
from collections.abc import Set as AbstractSet
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence, final

from exo.master.placement_utils import (
    Cycle,
    filter_cycles_by_memory,
    get_mlx_jaccl_coordinators,
    get_mlx_jaccl_devices_matrix,
    get_mlx_ring_hosts_by_node,
    get_shard_assignments,
)
from exo.shared.constants import (
    EXO_PLACEMENT_MAX_CYCLE_NODES,
    EXO_PLACEMENT_MAX_CYCLES,
)
from exo.shared.models.model_cards import ModelId
from exo.shared.topology import Topology
from exo.shared.types.backends import Backend
from exo.shared.types.commands import (
    CancelDownload,
    DownloadCommand,
)
from exo.shared.types.common import NodeId
from exo.shared.types.events import (
    Event,
    InstanceDeleted,
)
from exo.shared.types.memory import Memory
from exo.shared.types.placement import PlacementRequest
from exo.shared.types.profiling import (
    MemoryUsage,
    NodeIdentity,
    NodeNetworkInfo,
    NodeRdmaCtlStatus,
)
from exo.shared.types.state import State
from exo.shared.types.worker.downloads import (
    DownloadCompleted,
    DownloadFailed,
    DownloadOngoing,
    DownloadPending,
    DownloadProgress,
)
from exo.shared.types.worker.instances import (
    Instance,
    InstanceId,
    InstanceMeta,
    InstanceRemovalReason,
    MlxJacclInstance,
    MlxRingInstance,
)
from exo.shared.types.worker.runners import (
    RunnerId,
    RunnerLoaded,
    RunnerLoading,
    RunnerReady,
    RunnerRunning,
    RunnerShutdown,
    RunnerShuttingDown,
    RunnerStatus,
    RunnerWarmingUp,
    ShardAssignments,
)
from exo.shared.types.worker.shards import (
    Sharding,
    ShardMetadata,
    TensorShardMetadata,
)
from exo.utils.ports import random_ephemeral_port
from exo.worker.engines.mlx.models.spec import PlacementRules
from exo.worker.engines.mlx.models.spec_registry import placement_rules_for_model_type

INSTANCE_META_BACKENDS: dict[InstanceMeta, list[Backend]] = {
    InstanceMeta.MlxRing: [Backend.MlxMetal, Backend.MlxCuda, Backend.MlxCpu],
    InstanceMeta.MlxJaccl: [Backend.MlxMetal],
}


def launch_refusal(instance: Instance, state: State) -> str | None:
    """Why the master refuses to launch the instance, if it does: a node isn't in the cluster,
    or can't hold its shard besides the instances already there (some perhaps still loading).

    A node outside the cluster: the master's next check would delete the instance at once, as
    one that lost a node. Refused instead: the API sends a launch again until it shows, by when
    a node that is coming back (a split healing, say) is in the cluster again. Memory: two
    launches sent at the same time were otherwise both placed in memory that holds one. The
    API checks a launch the same way before sending it, but against its own state, which may
    not show the other launch yet."""
    if outside := [
        node_id
        for node_id in instance.shard_assignments.node_to_runner
        if not state.topology.contains_node(node_id)
    ]:
        return f"node(s) {outside} aren't in the cluster"
    return instance_beyond_memory(instance, state)


def instance_beyond_memory(instance: Instance, state: State) -> str | None:
    """Why a node can't hold its shard of the instance besides the instances in the state (some
    of them perhaps still loading), or None if every node can. The master checks a launch
    with it against its own state: two launches sent at the same time were otherwise both
    placed in memory that holds one. The API checks a launch the same way before sending it,
    but against its own state, which may not show the other launch yet."""
    return shard_beyond_memory(
        instance.shard_assignments,
        usable_memory(
            memory_once_loaded(state.node_memory, state.instances, state.runners)
        ),
        state.node_identities,
    )


def _get_node_download_fraction(
    node_id: NodeId,
    model_id: ModelId,
    download_status: Mapping[NodeId, Sequence[DownloadProgress]],
) -> float:
    """Return the download fraction (0.0–1.0) for a model on a given node."""
    for progress in download_status.get(node_id, []):
        if progress.shard_metadata.model_card.model_id != model_id:
            continue
        match progress:
            case DownloadCompleted():
                return 1.0
            case DownloadOngoing():
                total = progress.download_progress.total.in_bytes
                return (
                    progress.download_progress.downloaded.in_bytes / total
                    if total > 0
                    else 0.0
                )
            case DownloadPending():
                total = progress.total.in_bytes
                return progress.downloaded.in_bytes / total if total > 0 else 0.0
            case DownloadFailed():
                return 0.0
    return 0.0


def _cycle_download_score(
    cycle: Cycle,
    model_id: ModelId,
    download_status: Mapping[NodeId, Sequence[DownloadProgress]],
) -> float:
    """Sum of download fractions across all nodes in a cycle."""
    return sum(
        _get_node_download_fraction(node_id, model_id, download_status)
        for node_id in cycle
    )


def _not_enough_memory(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    node_memory: Mapping[NodeId, MemoryUsage],
) -> str:
    reported = [cycle for cycle in cycles if all(node in node_memory for node in cycle)]
    if not reported:
        return "Waiting for the nodes to report their memory"
    free = {
        node_id: usage.ram_available.in_bytes for node_id, usage in node_memory.items()
    }
    most_free = Memory.from_bytes(
        max(sum(free[node] for node in cycle) for cycle in reported)
    )
    return (
        f"Not enough memory: {request.model_card.model_id} needs "
        f"{request.model_card.resident_size.in_gb:.1f} GB, but connected nodes can use at most "
        f"{most_free.in_gb:.1f} GB between them"
    )


def _node_name(node_id: NodeId, node_identities: Mapping[NodeId, NodeIdentity]) -> str:
    identity = node_identities.get(node_id)
    if identity is None or identity.friendly_name == NodeIdentity().friendly_name:
        return str(node_id)
    return identity.friendly_name


def _tensor_share_too_large(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    node_memory: Mapping[NodeId, MemoryUsage],
    node_identities: Mapping[NodeId, NodeIdentity],
) -> str:
    # Explain the group of nodes that comes closest to holding its shares
    cycle = max(
        cycles,
        key=lambda c: min(node_memory[n].ram_available for n in c).in_bytes * len(c),
    )
    smallest = min(cycle, key=lambda n: node_memory[n].ram_available)
    share = request.model_card.resident_size // len(cycle)
    return (
        f"Tensor sharding gives each of {len(cycle)} nodes {share.in_gb:.1f} GB of "
        f"{request.model_card.model_id}, but {_node_name(smallest, node_identities)} can "
        f"use only about {node_memory[smallest].ram_available.in_gb:.1f} GB. Use Pipeline "
        "sharding, which splits the model by each node's memory, or nodes with more memory"
    )


def shard_beyond_memory(
    shard_assignments: ShardAssignments,
    node_memory: Mapping[NodeId, MemoryUsage],
    node_identities: Mapping[NodeId, NodeIdentity],
) -> str | None:
    """Why a node can't hold its shard of the model, or None if every node can."""
    for node_id, runner_id in shard_assignments.node_to_runner.items():
        name = _node_name(node_id, node_identities)
        usage = node_memory.get(node_id)
        if usage is None:
            return f"Waiting for {name} to report its memory"
        shard = shard_assignments.runner_to_shard[runner_id]
        share = Memory.from_bytes(shard_bytes(shard))
        if share > usage.ram_available:
            return (
                f"Not enough memory: {name} can use only about "
                f"{usage.ram_available.in_gb:.1f} GB, but its shard of "
                f"{shard.model_card.model_id} needs {share.in_gb:.1f} GB"
            )
    return None


def usable_memory(
    node_memory: Mapping[NodeId, MemoryUsage],
) -> dict[NodeId, MemoryUsage]:
    """Each node's memory with `ram_available` cut to what a model can use there.

    A Mac's GPU can use only its working set (about 75% of the RAM), so an idle Mac has more
    memory free than a model can use. A runner whose shard needs more aborts while loading.
    """
    return {
        node_id: usage
        if usage.gpu_working_set is None or usage.gpu_working_set >= usage.ram_available
        else usage.model_copy(update={"ram_available": usage.gpu_working_set})
        for node_id, usage in node_memory.items()
    }


# Runners whose memory is already in their node's report (loaded), or about to be freed
_NOT_PENDING = (
    RunnerLoaded,
    RunnerWarmingUp,
    RunnerReady,
    RunnerRunning,
    RunnerShuttingDown,
    RunnerShutdown,
)


def shard_bytes(shard: ShardMetadata) -> int:
    """The memory a runner needs for its shard of the model."""
    storage = shard.model_card.resident_size.in_bytes
    if isinstance(shard, TensorShardMetadata):
        return storage // shard.world_size
    if shard.n_layers == 0:
        return storage
    return storage * (shard.end_layer - shard.start_layer) // shard.n_layers


def memory_once_loaded(
    node_memory: Mapping[NodeId, MemoryUsage],
    instances: Mapping[InstanceId, Instance],
    runners: Mapping[RunnerId, RunnerStatus],
) -> dict[NodeId, MemoryUsage]:
    """Each node's free memory once the runners placed on it have loaded.

    A node reports the memory its loaded models use. A runner that is still starting or loading
    (or failed, and will load again) hasn't taken its memory yet, so without this the next
    placement would see that memory as free and could put a second model in it. Of a runner
    that is loading, only the layers it hasn't loaded yet are counted: the node's report has the
    others. Counting the whole shard counted those layers twice, and refused a second model that
    fitted while the first one was finishing its load.
    """
    pending: defaultdict[NodeId, int] = defaultdict(int)
    for instance in instances.values():
        assignments = instance.shard_assignments
        for node_id, runner_id in assignments.node_to_runner.items():
            status = runners.get(runner_id)
            if isinstance(status, _NOT_PENDING):
                continue
            shard = shard_bytes(assignments.runner_to_shard[runner_id])
            if isinstance(status, RunnerLoading) and status.total_layers > 0:
                unloaded = max(0, status.total_layers - status.layers_loaded)
                shard = shard * unloaded // status.total_layers
            pending[node_id] += shard
    return {
        node_id: usage.model_copy(
            update={
                "ram_available": Memory.from_bytes(
                    max(0, usage.ram_available.in_bytes - pending[node_id])
                )
            }
        )
        for node_id, usage in node_memory.items()
    }


CyclesByLength = Mapping[int, Sequence[Cycle]]
"""Cycles grouped by node count, each group in the order the search produced them.

Placement only ever uses one group — the smallest whose cycles can hold the model — so
grouping lets it stop at that one instead of walking every cycle in the topology. The
order within a group is the enumeration order, which is what breaks ties between equally
good cycles, so grouping does not change which cycle is chosen.
"""


@final
@dataclass(frozen=True)
class CycleSearch:
    """The rings placement may choose from, and the ring size the search stopped at."""

    by_length: CyclesByLength
    """The cycles to place on, grouped by node count."""

    max_cycle_nodes: int | None
    """The largest ring enumerated, or None when every ring in the topology was."""

    in_search_order: Sequence[Cycle] | None = None
    """The same cycles ungrouped, in the order the search produced them, when it kept that.

    Only the wording of a failure depends on it: the reason no ring could be placed on
    names the first of equally good rings.
    """

    @property
    def cycles(self) -> list[Cycle]:
        """Every cycle found, shortest group first."""
        return [
            cycle
            for length in sorted(self.by_length)
            for cycle in self.by_length[length]
        ]


@final
class PlacementSearchTruncatedError(ValueError):
    """No ring could be placed on, and rings above ``max_cycle_nodes`` were not searched.

    Distinct from the plain ValueError placement raises so that a caller can tell "this
    cluster cannot run the model" from "the rings that could have run it were too
    expensive to enumerate". Only ``search_placement_cycles`` can produce a search this
    applies to.
    """

    def __init__(self, message: str, max_cycle_nodes: int) -> None:
        """Record the largest ring size that was searched alongside the message."""
        super().__init__(message)
        self.max_cycle_nodes = max_cycle_nodes


def group_cycles_by_length(cycles: Iterable[Cycle]) -> CyclesByLength:
    """Group cycles by node count, keeping the order they arrived in within each group."""
    grouped: dict[int, list[Cycle]] = {}
    for cycle in cycles:
        grouped.setdefault(len(cycle), []).append(cycle)
    return grouped


def search_placement_cycles(
    topology: Topology,
    max_cycles: int = EXO_PLACEMENT_MAX_CYCLES,
    max_cycle_nodes: int = EXO_PLACEMENT_MAX_CYCLE_NODES,
) -> CycleSearch:
    """Find the rings ``place_instance`` may choose from, within a cost it can afford.

    Enumerating every simple cycle is factorial in the node count: a fully meshed nine
    node cluster has 125,673 of them and an eleven node one eleven million, which takes
    about a minute. A topology holding no more than ``max_cycles`` is still enumerated in
    full, which is what placement has always done, so it chooses exactly what it always
    chose. Only past that budget is the search limited to rings of ``max_cycle_nodes``
    nodes: cheap, but placement can then no longer choose a longer ring, and the cycles
    come back in a different order and anchored at a different node, which moves shard
    ranks and ring wiring. ``CycleSearch.max_cycle_nodes`` reports the limit that was
    applied, so a caller can say a ring size was not searched rather than that nothing
    fits.

    The budget counts cycles rather than nodes because node count does not predict the
    cost: forty Macs in a Thunderbolt loop hold forty cycles and are enumerated exactly,
    while eleven in a mesh hold eleven million. Establishing that a topology is over
    budget costs about 25ms whatever its size, since the cycles are drained from a lazy
    iterator and the walk is abandoned once the budget is passed.

    Call this once per request and pass the result to every ``place_instance`` call that
    request makes: the enumeration does not depend on the request, so repeating it per
    candidate multiplies the cost by the number of candidates.
    """
    every_cycle = topology.get_cycles_within_budget(max_cycles)
    if every_cycle is not None:
        return CycleSearch(
            by_length=group_cycles_by_length(every_cycle),
            max_cycle_nodes=None,
            in_search_order=every_cycle,
        )
    short_cycles = topology.get_cycles_up_to(max_cycle_nodes)
    return CycleSearch(
        by_length=group_cycles_by_length(short_cycles),
        max_cycle_nodes=max_cycle_nodes,
        in_search_order=short_cycles,
    )


def _cycles_tensor_can_divide(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    placement_rules: PlacementRules,
) -> list[Cycle]:
    """Keep the cycles whose node count divides what tensor sharding splits."""
    # TODO: the condition here for tensor parallel is not correct, but it works good enough for now.
    splits_kv_heads = placement_rules.tensor_parallel_splits_kv_heads
    kv_heads = request.model_card.num_key_value_heads
    return [
        cycle
        for cycle in cycles
        if request.model_card.hidden_size % len(cycle) == 0
        and (not splits_kv_heads or kv_heads is None or kv_heads % len(cycle) == 0)
        and all(
            number % len(cycle) == 0
            for number in placement_rules.tensor_parallel_divides
        )
    ]


def _cycles_holding_tensor_shares(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    node_memory: Mapping[NodeId, MemoryUsage],
) -> list[Cycle]:
    """Keep the cycles whose every node can hold its equal share of the model.

    Tensor sharding splits every layer evenly, so each node needs an equal share: enough
    memory between the nodes is no use if one of them can't hold its share.
    """
    return [
        cycle
        for cycle in cycles
        if all(
            node_memory[node].ram_available
            >= request.model_card.resident_size // len(cycle)
            for node in cycle
        )
    ]


def _cycles_the_sharding_allows(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    node_memory: Mapping[NodeId, MemoryUsage],
    placement_rules: PlacementRules,
) -> list[Cycle]:
    """Keep the cycles the requested sharding can actually use."""
    if request.sharding == Sharding.Tensor:
        return _cycles_holding_tensor_shares(
            request,
            _cycles_tensor_can_divide(request, cycles, placement_rules),
            node_memory,
        )
    if (
        request.sharding == Sharding.Pipeline
        and placement_rules.multi_node_pipeline_error is not None
    ):
        return [cycle for cycle in cycles if len(cycle) == 1]
    return list(cycles)


def _reject_sharding_the_model_cannot_do(
    request: PlacementRequest, placement_rules: PlacementRules
) -> None:
    """Raise for a sharding this model never supports, whatever the topology offers."""
    if request.sharding == Sharding.Tensor and not request.model_card.supports_tensor:
        raise ValueError(
            f"Requested Tensor sharding but this model does not support tensor parallelism: {request.model_card.model_id}"
        )
    if request.sharding == Sharding.Pipeline:
        for model_id, error in placement_rules.pipeline_unsupported_model_ids:
            if request.model_card.model_id == ModelId(model_id):
                raise ValueError(error)


def _no_placement_reason(
    request: PlacementRequest,
    cycles: Sequence[Cycle],
    node_memory: Mapping[NodeId, MemoryUsage],
    required_nodes: set[NodeId] | None,
    node_identities: Mapping[NodeId, NodeIdentity],
    placement_rules: PlacementRules,
) -> str:
    """Why none of ``cycles`` can be placed on: the first obstacle, in the order a reader
    would check them.

    Only called once every group has been walked and none survived, so it can afford to
    look at every cycle: nodes at all, enough of them connected, the chosen nodes
    connected, memory between them, and then what the sharding can use.
    """
    if not cycles:
        return "No nodes are available"
    candidate_cycles = [cycle for cycle in cycles if len(cycle) >= request.min_nodes]
    if not candidate_cycles:
        largest = max(len(cycle) for cycle in cycles)
        return f"Needs {request.min_nodes} connected nodes, but at most {largest} are connected to each other"
    if required_nodes:
        candidate_cycles = [
            cycle
            for cycle in candidate_cycles
            if required_nodes.issubset(cycle.node_ids)
        ]
        if not candidate_cycles:
            return "The chosen nodes aren't all connected to each other"
    cycles_with_sufficient_memory = filter_cycles_by_memory(
        candidate_cycles, node_memory, request.model_card.resident_size
    )
    if not cycles_with_sufficient_memory:
        return _not_enough_memory(request, candidate_cycles, node_memory)
    if request.sharding == Sharding.Tensor:
        divisible_cycles = _cycles_tensor_can_divide(
            request, cycles_with_sufficient_memory, placement_rules
        )
        if not divisible_cycles:
            kv_heads = request.model_card.num_key_value_heads
            return (
                f"No tensor sharding found for model with "
                f"hidden_size={request.model_card.hidden_size}"
                f"{f', num_key_value_heads={kv_heads}' if kv_heads is not None else ''}"
                f" across candidate cycles"
            )
        return _tensor_share_too_large(
            request, divisible_cycles, node_memory, node_identities
        )
    if placement_rules.multi_node_pipeline_error is not None:
        return placement_rules.multi_node_pipeline_error
    raise AssertionError("every cycle that holds the model suits pipeline sharding")


def _smallest_placeable_cycles(
    request: PlacementRequest,
    cycle_search: CycleSearch,
    node_count: int,
    node_memory: Mapping[NodeId, MemoryUsage],
    required_nodes: set[NodeId] | None,
    allowed_nodes: AbstractSet[NodeId] | None,
    node_identities: Mapping[NodeId, NodeIdentity],
) -> list[Cycle]:
    """The smallest group of equal-length cycles this request can be placed on.

    Walks the groups shortest first and returns the first that survives the allowed and
    required nodes, the memory the model needs, and what the sharding can use. Stopping
    there is what filtering every cycle and then taking the shortest survivors amounts
    to, since every filter looks only at a cycle's nodes, so a longer group can never
    displace a shorter one that passed. The difference is cost: the groups past the
    answer are never walked, and on a fully meshed cluster they hold almost every cycle.

    Raises ValueError naming the first obstacle, in the order a reader would check them
    (see ``_no_placement_reason``). Raises ``PlacementSearchTruncatedError`` instead when
    the search was bounded and the cluster has more nodes than the bound, since then a
    ring that was never enumerated might have been placeable and no other message may
    claim the cluster cannot hold the model.
    """
    placement_rules = placement_rules_for_model_type(request.model_card.model_type)
    cycles_by_length = cycle_search.by_length
    checked_model = False
    for length in sorted(
        length for length in cycles_by_length if length >= request.min_nodes
    ):
        group: Sequence[Cycle] = cycles_by_length[length]
        if allowed_nodes is not None:
            group = [cycle for cycle in group if set(cycle.node_ids) <= allowed_nodes]
        if required_nodes:
            group = [
                cycle for cycle in group if required_nodes.issubset(cycle.node_ids)
            ]
        with_memory = filter_cycles_by_memory(
            list(group), node_memory, request.model_card.resident_size
        )
        if not with_memory:
            continue
        if not checked_model:
            # Only reachable once something fits in memory, so that a cluster too small
            # for the model still reports memory rather than the sharding.
            _reject_sharding_the_model_cannot_do(request, placement_rules)
            checked_model = True
        allowed = _cycles_the_sharding_allows(
            request, with_memory, node_memory, placement_rules
        )
        if allowed:
            return allowed

    every_cycle: Sequence[Cycle] = (
        cycle_search.cycles
        if cycle_search.in_search_order is None
        else cycle_search.in_search_order
    )
    if allowed_nodes is not None:
        every_cycle = [
            cycle for cycle in every_cycle if set(cycle.node_ids) <= allowed_nodes
        ]
    reason = _no_placement_reason(
        request,
        every_cycle,
        node_memory,
        required_nodes,
        node_identities,
        placement_rules,
    )
    max_cycle_nodes = cycle_search.max_cycle_nodes
    if max_cycle_nodes is not None and node_count > max_cycle_nodes:
        raise PlacementSearchTruncatedError(
            f"Rings of more than {max_cycle_nodes} nodes were not searched on this "
            f"{node_count} node cluster, because enumerating them costs more than the "
            f"placement budget allows. Within the rings that were searched: {reason}",
            max_cycle_nodes=max_cycle_nodes,
        )
    raise ValueError(reason)


def place_instance(
    request: PlacementRequest,
    topology: Topology,
    current_instances: Mapping[InstanceId, Instance],
    node_memory: Mapping[NodeId, MemoryUsage],
    node_network: Mapping[NodeId, NodeNetworkInfo],
    node_backends: Mapping[NodeId, list[Backend]],
    required_nodes: set[NodeId] | None = None,
    download_status: Mapping[NodeId, Sequence[DownloadProgress]] | None = None,
    node_rdma_ctl: Mapping[NodeId, NodeRdmaCtlStatus] | None = None,
    runners: Mapping[RunnerId, RunnerStatus] | None = None,
    node_identities: Mapping[NodeId, NodeIdentity] | None = None,
    allowed_nodes: AbstractSet[NodeId] | None = None,
    cycles: Sequence[Cycle] | None = None,
    *,
    cycle_search: CycleSearch | None = None,
) -> dict[InstanceId, Instance]:
    """Place an instance of the requested model on the best group of nodes.

    Pass the cluster's `runners` so that memory taken by instances that haven't loaded yet is
    counted as used; without them every instance is assumed loaded. `node_identities` name the
    nodes in errors. With `allowed_nodes`, only groups of those nodes are considered.
    `cycles` are the topology's, if the caller has them: finding them is the costly part, and
    grows quickly with the number of nodes.

    ``cycle_search`` is the same thing with a bound on its cost, and wins over `cycles`:
    ``search_placement_cycles`` produces it. It carries the ring size the search stopped
    at as well as the rings, so a failure can say a ring size was not searched rather than
    that nothing fits. A caller trying several requests against one topology searches once
    and this walks only the group it places on. With neither, every cycle of the topology
    is enumerated here.
    """
    if runners is not None:
        node_memory = memory_once_loaded(node_memory, current_instances, runners)
    node_memory = usable_memory(node_memory)
    if cycle_search is None:
        if cycles is None:
            cycles = topology.get_cycles()
        cycle_search = CycleSearch(
            by_length=group_cycles_by_length(cycles),
            max_cycle_nodes=None,
            in_search_order=cycles,
        )

    smallest_cycles = _smallest_placeable_cycles(
        request,
        cycle_search,
        len(list(topology.list_nodes())),
        node_memory,
        required_nodes,
        allowed_nodes,
        node_identities or {},
    )

    required_backends = set(INSTANCE_META_BACKENDS[request.instance_meta]) & set(
        request.model_card.backends
    )
    if not required_backends:
        raise ValueError(
            f"Model {request.model_card.model_id} backends "
            f"{sorted(b.value for b in request.model_card.backends)} cannot satisfy engine "
            f"{request.instance_meta.value} which requires "
            f"{sorted(b.value for b in INSTANCE_META_BACKENDS[request.instance_meta])}"
        )
    smallest_cycles = [
        cycle
        for cycle in smallest_cycles
        if all(
            set(node_backends.get(node_id, [])) & required_backends for node_id in cycle
        )
    ]
    if not smallest_cycles:
        raise ValueError(
            f"No cycle where every node supports a backend in "
            f"{sorted(b.value for b in required_backends)} for {request.model_card.model_id}"
        )

    rdma_ctl_status = node_rdma_ctl or {}

    def _all_rdma_ctl_enabled(cycle: Cycle) -> bool:
        return all(
            ((status := rdma_ctl_status.get(node_id)) is not None and status.enabled)
            for node_id in cycle
        )

    smallest_rdma_cycles = [
        cycle
        for cycle in smallest_cycles
        if topology.is_rdma_cycle(cycle) and _all_rdma_ctl_enabled(cycle)
    ]

    if request.instance_meta == InstanceMeta.MlxJaccl:
        if not smallest_rdma_cycles:
            raise ValueError(
                "Requested RDMA (MlxJaccl) but no RDMA-connected cycles available"
            )
        smallest_cycles = smallest_rdma_cycles

    cycles_with_leaf_nodes: list[Cycle] = [
        cycle
        for cycle in smallest_cycles
        if any(topology.node_is_leaf(node_id) for node_id in cycle)
    ]

    resolved_download_status = download_status or {}
    candidate_cycles = (
        cycles_with_leaf_nodes if cycles_with_leaf_nodes != [] else smallest_cycles
    )

    selected_cycle = max(
        candidate_cycles,
        key=lambda cycle: (
            _cycle_download_score(
                cycle, request.model_card.model_id, resolved_download_status
            ),
            sum(
                (node_memory[node_id].ram_available for node_id in cycle),
                start=Memory(),
            ),
        ),
    )

    # Single-node: force Pipeline/Ring (Tensor and Jaccl require multi-node)
    if len(selected_cycle) == 1:
        request = request.model_copy(
            update={
                "instance_meta": InstanceMeta.MlxRing,
                "sharding": Sharding.Pipeline,
            }
        )

    shard_assignments = get_shard_assignments(
        request.model_card, selected_cycle, request.sharding, node_memory
    )

    cycle_digraph: Topology = topology.get_subgraph_from_nodes(selected_cycle.node_ids)

    instance_id = InstanceId()
    target_instances = dict(deepcopy(current_instances))

    match request.instance_meta:
        case InstanceMeta.MlxJaccl:
            # TODO(evan): shard assignments should contain information about ranks, this is ugly
            def get_device_rank(node_id: NodeId) -> int:
                runner_id = shard_assignments.node_to_runner[node_id]
                shard_metadata = shard_assignments.runner_to_shard.get(runner_id)
                assert shard_metadata is not None
                return shard_metadata.device_rank

            zero_node_ids = [
                node_id
                for node_id in selected_cycle.node_ids
                if get_device_rank(node_id) == 0
            ]
            assert len(zero_node_ids) == 1
            coordinator_node_id = zero_node_ids[0]

            mlx_jaccl_devices = get_mlx_jaccl_devices_matrix(
                [node_id for node_id in selected_cycle],
                cycle_digraph,
            )
            mlx_jaccl_coordinators = get_mlx_jaccl_coordinators(
                coordinator=coordinator_node_id,
                coordinator_port=random_ephemeral_port(),
                cycle_digraph=cycle_digraph,
                node_network=node_network,
            )
            target_instances[instance_id] = MlxJacclInstance(
                instance_id=instance_id,
                shard_assignments=shard_assignments,
                jaccl_devices=mlx_jaccl_devices,
                jaccl_coordinators=mlx_jaccl_coordinators,
            )
        case InstanceMeta.MlxRing:
            ephemeral_port = random_ephemeral_port()
            hosts_by_node = get_mlx_ring_hosts_by_node(
                selected_cycle=selected_cycle,
                cycle_digraph=cycle_digraph,
                ephemeral_port=ephemeral_port,
                node_network=node_network,
            )
            target_instances[instance_id] = MlxRingInstance(
                instance_id=instance_id,
                shard_assignments=shard_assignments,
                hosts_by_node=hosts_by_node,
                ephemeral_port=ephemeral_port,
            )

    return target_instances


def removal_events(
    instance_id: InstanceId,
    reason: InstanceRemovalReason,
    at: datetime,
) -> Sequence[Event]:
    """Remove an instance. Its requests aren't in the cluster's state: the nodes serving them
    end them as the instance goes (exo.worker.serving)."""
    events: list[Event] = [
        InstanceDeleted(instance_id=instance_id, reason=reason, at=at.isoformat())
    ]
    return events


def cancel_unnecessary_downloads(
    instances: Mapping[InstanceId, Instance],
    download_status: Mapping[NodeId, Sequence[DownloadProgress]],
) -> Sequence[DownloadCommand]:
    commands: list[DownloadCommand] = []
    currently_downloading = [
        (k, v.shard_metadata.model_card.model_id)
        for k, vs in download_status.items()
        for v in vs
        if isinstance(v, (DownloadOngoing))
    ]
    active_models = set(
        (
            node_id,
            instance.shard_assignments.runner_to_shard[runner_id].model_card.model_id,
        )
        for instance in instances.values()
        for node_id, runner_id in instance.shard_assignments.node_to_runner.items()
    )
    for pair in currently_downloading:
        if pair not in active_models:
            commands.append(CancelDownload(target_node_id=pair[0], model_id=pair[1]))

    return commands
