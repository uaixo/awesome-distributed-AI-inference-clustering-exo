from exo.shared.topology import Topology
from exo.shared.types.common import NodeId
from exo.shared.types.multiaddr import Multiaddr
from exo.shared.types.profiling import (
    MemoryUsage,
    NetworkInterfaceInfo,
    NodeNetworkInfo,
)
from exo.shared.types.topology import Connection, RDMAConnection, SocketConnection


def create_node_memory(memory: int) -> MemoryUsage:
    return MemoryUsage.from_bytes(
        ram_total=1000,
        ram_available=memory,
        swap_total=1000,
        swap_available=1000,
    )


def create_node_network() -> NodeNetworkInfo:
    return NodeNetworkInfo(
        interfaces=[
            NetworkInterfaceInfo(name="en0", ip_address=f"169.254.0.{i}")
            for i in range(10)
        ]
    )


def create_socket_connection(ip: int, sink_port: int = 1234) -> SocketConnection:
    return SocketConnection(
        sink_multiaddr=Multiaddr(address=f"/ip4/169.254.0.{ip}/tcp/{sink_port}"),
    )


def create_rdma_connection(iface: int) -> RDMAConnection:
    return RDMAConnection(
        source_rdma_iface=f"rdma_en{iface}", sink_rdma_iface=f"rdma_en{iface}"
    )


def create_mesh_topology(node_count: int) -> tuple[Topology, list[NodeId]]:
    """A topology where every node reaches every other, the worst case for cycle count.

    The number of simple cycles in a mesh is factorial in the node count, so this is what
    a cluster of Macs on one LAN looks like to placement.
    """
    topology = Topology()
    node_ids = [NodeId(f"node-{index:02d}") for index in range(node_count)]
    for node_id in node_ids:
        topology.add_node(node_id)
    for index, source in enumerate(node_ids):
        for sink in node_ids:
            if source != sink:
                topology.add_connection(
                    Connection(
                        source=source,
                        sink=sink,
                        edge=create_socket_connection(index + 1),
                    )
                )
    return topology, node_ids
