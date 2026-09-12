from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
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
    EXO_PLACEMENT_FULL_SEARCH_MAX_NODES,
    EXO_PLACEMENT_MAX_CYCLE_NODES,
)
from exo.shared.models.model_cards import ModelId
from exo.shared.topology import Topology
from exo.shared.types.backends import Backend
from exo.shared.types.commands import (
    CancelDownload,
    CreateInstance,
    DeleteInstance,
    DownloadCommand,
    PlaceInstance,
)
from exo.shared.types.common import NodeId
from exo.shared.types.events import (
    Event,
    InstanceCreated,
    InstanceDeleted,
    TaskStatusUpdated,
)
from exo.shared.types.memory import Memory
from exo.shared.types.profiling import MemoryUsage, NodeNetworkInfo, NodeRdmaCtlStatus
from exo.shared.types.tasks import Task, TaskId, TaskStatus
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
    MlxJacclInstance,
    MlxRingInstance,
)
from exo.shared.types.worker.shards import Sharding
from exo.utils.ports import random_ephemeral_port

INSTANCE_META_BACKENDS: dict[InstanceMeta, list[Backend]] = {
    InstanceMeta.MlxRing: [Backend.MlxMetal, Backend.MlxCuda, Backend.MlxCpu],
    InstanceMeta.MlxJaccl: [Backend.MlxMetal],
}


def add_instance_to_placements(
    command: CreateInstance,
    topology: Topology,
    current_instances: Mapping[InstanceId, Instance],
) -> Mapping[InstanceId, Instance]:
    # TODO: validate against topology

    return {**current_instances, command.instance.instance_id: command.instance}


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

    @property
    def cycles(self) -> list[Cycle]:
        """Every cycle found, shortest group first."""
        return [
            cycle
            for length in sorted(self.by_length)
            for cycle in self.by_length[length]
        ]


def group_cycles_by_length(cycles: Iterable[Cycle]) -> CyclesByLength:
    """Group cycles by node count, keeping the order they arrived in within each group."""
    grouped: dict[int, list[Cycle]] = {}
    for cycle in cycles:
        grouped.setdefault(len(cycle), []).append(cycle)
    return grouped


def search_placement_cycles(
    topology: Topology,
    full_search_max_nodes: int = EXO_PLACEMENT_FULL_SEARCH_MAX_NODES,
    max_cycle_nodes: int = EXO_PLACEMENT_MAX_CYCLE_NODES,
) -> CycleSearch:
    """Find the rings ``place_instance`` may choose from, within a cost it can afford.

    Enumerating every simple cycle is factorial in the node count: a fully meshed nine
    node cluster has 125,673 of them and an eleven node one eleven million, which takes
    about a minute. At or below ``full_search_max_nodes`` nodes every cycle is enumerated,
    which is what placement has always done, so it chooses exactly what it always chose.
    Past that the search is limited to rings of ``max_cycle_nodes`` nodes: cheap, but
    placement can then no longer choose a longer ring, and the cycles come back in a
    different order and anchored at a different node, which moves shard ranks and ring
    wiring. ``CycleSearch.max_cycle_nodes`` reports the limit that was applied, so a
    caller can say a ring size was not searched rather than that nothing fits.

    Call this once per request and pass the result to every ``place_instance`` call that
    request makes: the enumeration does not depend on the command, so repeating it per
    candidate multiplies the cost by the number of candidates.
    """
    node_count = len(list(topology.list_nodes()))
    if node_count <= full_search_max_nodes:
        return CycleSearch(
            by_length=group_cycles_by_length(topology.get_cycles()),
            max_cycle_nodes=None,
        )
    return CycleSearch(
        by_length=group_cycles_by_length(topology.get_cycles_up_to(max_cycle_nodes)),
        max_cycle_nodes=max_cycle_nodes,
    )


def _cycles_the_sharding_allows(
    command: PlaceInstance, cycles: Sequence[Cycle]
) -> list[Cycle]:
    """Keep the cycles whose node count the requested sharding can actually use."""
    if command.sharding == Sharding.Tensor:
        # TODO: the condition here for tensor parallel is not correct, but it works good enough for now.
        # DeepSeek V4 is MQA (num_key_value_heads=1) but its sharding strategy
        # head-parallelises wq_b/wo_a and shards MoE experts instead of splitting
        # KV heads, so the kv-head divisibility check doesn't apply.
        is_deepseek_v4 = command.model_card.base_model.startswith("DeepSeek V4")
        kv_heads = command.model_card.num_key_value_heads
        return [
            cycle
            for cycle in cycles
            if command.model_card.hidden_size % len(cycle) == 0
            and (is_deepseek_v4 or kv_heads is None or kv_heads % len(cycle) == 0)
        ]
    if (
        command.sharding == Sharding.Pipeline
        and command.model_card.base_model.startswith("Gemma 4")
    ):
        return [cycle for cycle in cycles if len(cycle) == 1]
    return list(cycles)


def _reject_sharding_the_model_cannot_do(command: PlaceInstance) -> None:
    """Raise for a sharding this model never supports, whatever the topology offers."""
    if command.sharding == Sharding.Tensor and not command.model_card.supports_tensor:
        raise ValueError(
            f"Requested Tensor sharding but this model does not support tensor parallelism: {command.model_card.model_id}"
        )
    if command.sharding == Sharding.Pipeline and command.model_card.model_id == ModelId(
        "mlx-community/DeepSeek-V3.1-8bit"
    ):
        raise ValueError(
            "Pipeline parallelism is not supported for DeepSeek V3.1 (8-bit)"
        )


def _smallest_placeable_cycles(
    command: PlaceInstance,
    cycles_by_length: CyclesByLength,
    node_memory: Mapping[NodeId, MemoryUsage],
    required_nodes: set[NodeId] | None,
) -> list[Cycle]:
    """The smallest group of equal-length cycles this command can be placed on.

    Walks the groups shortest first and returns the first that survives the required
    nodes, the memory the model needs, and what the sharding can use. Stopping there is
    what filtering every cycle and then taking the shortest survivors amounts to, since
    the three filters look only at a cycle's nodes, so a longer group can never displace
    a shorter one that passed. The difference is cost: the groups past the answer are
    never walked, and on a fully meshed cluster they hold almost every cycle.

    Raises ValueError naming the first obstacle, in the order a reader would check them:
    nothing with enough memory, then a sharding the model cannot do, then no cycle whose
    node count that sharding can use.
    """
    lengths = sorted(
        length for length in cycles_by_length if length >= command.min_nodes
    )
    checked_model = False
    for length in lengths:
        group: Sequence[Cycle] = cycles_by_length[length]
        if required_nodes:
            group = [
                cycle for cycle in group if required_nodes.issubset(cycle.node_ids)
            ]
        with_memory = filter_cycles_by_memory(
            list(group), node_memory, command.model_card.storage_size
        )
        if not with_memory:
            continue
        if not checked_model:
            # Only reachable once something fits in memory, so that a cluster too small
            # for the model still reports memory rather than the sharding.
            _reject_sharding_the_model_cannot_do(command)
            checked_model = True
        allowed = _cycles_the_sharding_allows(command, with_memory)
        if allowed:
            return allowed

    if not checked_model:
        raise ValueError("No cycles found with sufficient memory")
    if command.sharding == Sharding.Tensor:
        kv_heads = command.model_card.num_key_value_heads
        raise ValueError(
            f"No tensor sharding found for model with "
            f"hidden_size={command.model_card.hidden_size}"
            f"{f', num_key_value_heads={kv_heads}' if kv_heads is not None else ''}"
            f" across candidate cycles"
        )
    raise ValueError(
        "Pipeline parallelism is not supported for Gemma 4; use tensor parallelism instead."
    )


def place_instance(
    command: PlaceInstance,
    topology: Topology,
    current_instances: Mapping[InstanceId, Instance],
    node_memory: Mapping[NodeId, MemoryUsage],
    node_network: Mapping[NodeId, NodeNetworkInfo],
    node_backends: Mapping[NodeId, list[Backend]],
    required_nodes: set[NodeId] | None = None,
    download_status: Mapping[NodeId, Sequence[DownloadProgress]] | None = None,
    node_rdma_ctl: Mapping[NodeId, NodeRdmaCtlStatus] | None = None,
    *,
    cycles_by_length: CyclesByLength,
) -> dict[InstanceId, Instance]:
    """Build the instance this command places, or raise ValueError explaining why not.

    ``cycles_by_length`` must be the cycles of ``topology``, grouped by node count —
    ``search_placement_cycles`` produces exactly that. It is a parameter rather than
    something this function enumerates because enumerating is the expensive part and does
    not depend on ``command``, so a caller trying several commands against one topology
    enumerates once and this walks only the group it places on.
    """
    smallest_cycles = _smallest_placeable_cycles(
        command, cycles_by_length, node_memory, required_nodes
    )

    required_backends = set(INSTANCE_META_BACKENDS[command.instance_meta]) & set(
        command.model_card.backends
    )
    if not required_backends:
        raise ValueError(
            f"Model {command.model_card.model_id} backends "
            f"{sorted(b.value for b in command.model_card.backends)} cannot satisfy engine "
            f"{command.instance_meta.value} which requires "
            f"{sorted(b.value for b in INSTANCE_META_BACKENDS[command.instance_meta])}"
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
            f"{sorted(b.value for b in required_backends)} for {command.model_card.model_id}"
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

    if command.instance_meta == InstanceMeta.MlxJaccl:
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
                cycle, command.model_card.model_id, resolved_download_status
            ),
            sum(
                (node_memory[node_id].ram_available for node_id in cycle),
                start=Memory(),
            ),
        ),
    )

    # Single-node: force Pipeline/Ring (Tensor and Jaccl require multi-node)
    if len(selected_cycle) == 1:
        command = command.model_copy(
            update={
                "instance_meta": InstanceMeta.MlxRing,
                "sharding": Sharding.Pipeline,
            }
        )

    shard_assignments = get_shard_assignments(
        command.model_card, selected_cycle, command.sharding, node_memory
    )

    cycle_digraph: Topology = topology.get_subgraph_from_nodes(selected_cycle.node_ids)

    instance_id = InstanceId()
    target_instances = dict(deepcopy(current_instances))

    match command.instance_meta:
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


def delete_instance(
    command: DeleteInstance,
    current_instances: Mapping[InstanceId, Instance],
) -> dict[InstanceId, Instance]:
    target_instances = dict(deepcopy(current_instances))
    if command.instance_id in target_instances:
        del target_instances[command.instance_id]
        return target_instances
    raise ValueError(f"Instance {command.instance_id} not found")


def get_transition_events(
    current_instances: Mapping[InstanceId, Instance],
    target_instances: Mapping[InstanceId, Instance],
    tasks: Mapping[TaskId, Task],
) -> Sequence[Event]:
    events: list[Event] = []

    # find instances to create
    for instance_id, instance in target_instances.items():
        if instance_id not in current_instances:
            events.append(
                InstanceCreated(
                    instance=instance,
                )
            )

    # find instances to delete
    for instance_id in current_instances:
        if instance_id not in target_instances:
            for task in tasks.values():
                if task.instance_id == instance_id and task.task_status in [
                    TaskStatus.Pending,
                    TaskStatus.Running,
                ]:
                    events.append(
                        TaskStatusUpdated(
                            task_status=TaskStatus.Cancelled,
                            task_id=task.task_id,
                        )
                    )

            events.append(
                InstanceDeleted(
                    instance_id=instance_id,
                )
            )

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
