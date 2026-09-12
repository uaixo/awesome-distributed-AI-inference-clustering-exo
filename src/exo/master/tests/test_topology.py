import pytest

from exo.master.tests.conftest import create_mesh_topology
from exo.shared.topology import Topology
from exo.shared.types.common import NodeId
from exo.shared.types.multiaddr import Multiaddr
from exo.shared.types.topology import Connection, Cycle, SocketConnection


@pytest.fixture
def topology() -> Topology:
    return Topology()


@pytest.fixture
def socket_connection() -> SocketConnection:
    return SocketConnection(
        sink_multiaddr=Multiaddr(address="/ip4/127.0.0.1/tcp/1235"),
    )


def test_add_node(topology: Topology):
    # arrange
    node_id = NodeId()

    # act
    topology.add_node(node_id)

    # assert
    assert topology.node_is_leaf(node_id)


def test_add_connection(topology: Topology, socket_connection: SocketConnection):
    # arrange
    node_a = NodeId()
    node_b = NodeId()
    connection = Connection(source=node_a, sink=node_b, edge=socket_connection)

    topology.add_node(node_a)
    topology.add_node(node_b)
    topology.add_connection(connection)

    # act
    data = list(topology.list_connections())

    # assert
    assert data == [connection]

    assert topology.node_is_leaf(node_a)
    assert topology.node_is_leaf(node_b)


def test_remove_connection_still_connected(
    topology: Topology, socket_connection: SocketConnection
):
    # arrange
    node_a = NodeId()
    node_b = NodeId()
    conn = Connection(source=node_a, sink=node_b, edge=socket_connection)

    topology.add_node(node_a)
    topology.add_node(node_b)
    topology.add_connection(conn)

    # act
    topology.remove_connection(conn)

    # assert
    assert list(topology.get_all_connections_between(node_a, node_b)) == []


def test_remove_node_still_connected(
    topology: Topology, socket_connection: SocketConnection
):
    # arrange
    node_a = NodeId()
    node_b = NodeId()
    conn = Connection(source=node_a, sink=node_b, edge=socket_connection)

    topology.add_node(node_a)
    topology.add_node(node_b)
    topology.add_connection(conn)
    assert list(topology.out_edges(node_a)) == [conn]

    # act
    topology.remove_node(node_b)

    # assert
    assert list(topology.out_edges(node_a)) == []


def test_list_nodes(topology: Topology, socket_connection: SocketConnection):
    # arrange
    node_a = NodeId()
    node_b = NodeId()
    conn = Connection(source=node_a, sink=node_b, edge=socket_connection)

    topology.add_node(node_a)
    topology.add_node(node_b)
    topology.add_connection(conn)
    assert list(topology.out_edges(node_a)) == [conn]

    # act
    nodes = list(topology.list_nodes())

    # assert
    assert len(nodes) == 2
    assert all(isinstance(node, NodeId) for node in nodes)
    assert set(node for node in nodes) == set([node_a, node_b])


@pytest.mark.parametrize("node_count", [3, 4, 5, 6])
@pytest.mark.parametrize("max_nodes", [2, 3, 4, 5, 6])
def test_bounded_cycles_are_the_full_cycles_of_that_length(
    node_count: int, max_nodes: int
):
    topology, _ = create_mesh_topology(node_count)

    bounded = _canonical(topology.get_cycles_up_to(max_nodes))
    expected = _canonical(
        [cycle for cycle in topology.get_cycles() if len(cycle) <= max_nodes]
    )

    assert bounded == expected


def test_bounded_cycles_keep_one_singleton_per_node():
    topology, node_ids = create_mesh_topology(4)

    singletons = [cycle for cycle in topology.get_cycles_up_to(2) if len(cycle) == 1]

    assert sorted(str(cycle.node_ids[0]) for cycle in singletons) == sorted(
        str(node_id) for node_id in node_ids
    )


def test_bounded_cycles_never_enumerate_a_longer_ring():
    topology, _ = create_mesh_topology(6)

    assert max(len(cycle) for cycle in topology.get_cycles_up_to(3)) == 3


@pytest.mark.parametrize("max_nodes", [-1, 0, 1])
def test_a_bound_below_two_is_refused(max_nodes: int):
    # rustworkx reads a cutoff of 0 as unbounded and 1 as 2, so a smaller bound would
    # quietly enumerate more than the caller asked for.
    topology, _ = create_mesh_topology(3)

    with pytest.raises(ValueError, match="at least 2"):
        topology.get_cycles_up_to(max_nodes)


def _canonical(cycles: list[Cycle]) -> list[tuple[str, ...]]:
    """Cycles as rotation-independent keys, keeping direction, so two runs compare."""
    rotations: list[tuple[str, ...]] = []
    for cycle in cycles:
        node_ids = [str(node_id) for node_id in cycle.node_ids]
        rotations.append(
            min(
                tuple(node_ids[index:] + node_ids[:index])
                for index in range(len(node_ids))
            )
        )
    return sorted(rotations)
