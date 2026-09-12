# exo Architecture — a high-level design guide

exo is one Python process you run on every Mac you own. The processes find each other over
IPv6 multicast, elect a leader in three seconds, agree on a shared view of the world through a
replicated event log, and cooperatively run a model whose weights fit on none of them alone.
**There is no server, no peer list, and no head node** — every process contains every part, and
which one is in charge right now is a runtime outcome, not a configuration.

This document describes the system as implemented, from reading the source. Where the
repository's own documentation disagrees with the code, the code wins; those cases are called
out in [§12](#12-sharp-edges).

| | |
|---|---|
| **Shape** | ~76k lines, 4 languages — Python control plane, Rust transport, Svelte dashboard, Swift menubar app |
| **Ports** | `52413` UDP discovery · `52414` zenoh TCP · `52415` HTTP API + dashboard. Each binds every interface; the HTTP API requires a key, zenoh authenticates nothing |
| **Coordination** | One elected master. No quorum, no lease, no consensus log — an epoch fence instead |
| **Inference** | MLX, two strategies — pipeline (split layers) or tensor (split every matrix) |

**Contents**

1. [Three networks, not one](#1-three-networks-not-one)
2. [What one exo process actually is](#2-what-one-exo-process-actually-is)
3. [Finding each other](#3-finding-each-other)
4. [Election, and the epoch it defines](#4-election-and-the-epoch-it-defines)
5. [The control cycle](#5-the-control-cycle)
6. [Workers converge; they are not commanded](#6-workers-converge-they-are-not-commanded)
7. [Placement: choosing the ring](#7-placement-choosing-the-ring)
8. [How the model is actually split](#8-how-the-model-is-actually-split)
9. [Life of a request](#9-life-of-a-request)
10. [Getting the weights onto the machines](#10-getting-the-weights-onto-the-machines)
11. [API, dashboard, and the macOS app](#11-api-dashboard-and-the-macos-app)
12. [Sharp edges](#12-sharp-edges)
13. [Platform reality](#13-platform-reality)

---

## 1. Three networks, not one

Almost everything surprising about exo becomes obvious once you notice that three completely
separate networks run between the same machines, carrying different payloads over different
transports with different topologies. They never touch.

<img src="imgs/architecture/01-three-planes.svg" alt="Three separate networks connect the same three exo nodes: a control plane broadcasting JSON over zenoh to all nodes, a tensor plane sending activations only between ring neighbours over MLX distributed, and a weights plane in which each node independently pulls the full model repository from huggingface.co over HTTPS." width="100%">

| Plane | Carries | Transport | Topology |
|---|---|---|---|
| **Control** | Events, commands, election ballots, telemetry, and every generated token | zenoh pub/sub over TCP `:52414`, JSON | Broadcast to all |
| **Tensor** | Layer activations; KV cache during prefill offload | MLX distributed — `ring` over TCP, or `jaccl` over RDMA / Thunderbolt 5 | Ring neighbours, or all-to-all |
| **Weights** | Model files | Plain HTTPS to `huggingface.co` | Each node independently |

Keeping the planes distinct explains an otherwise baffling design decision, which is worth
stating early because it colours the rest of the system:

> **Generated tokens travel on the control plane, not the tensor plane.** A token sampled
> inside a runner process is wrapped in an event, JSON-serialised, published to the elected
> master, appended to the master's on-disk event log, re-broadcast to the entire cluster,
> applied to three separate replicas of cluster state, and only then written to the HTTP
> socket. This happens for *every token*, and it happens even when the API, the master, the
> worker and the runner are all the same process on the same laptop.

That is not an oversight — it falls out of the architecture. exo has exactly one path for
information to move between components, and that path is the replicated event log. The benefit
is that any node can serve any request and every node's dashboard shows the same thing. The
cost is that the hot loop of an inference server runs through a leader-sequenced, disk-backed,
JSON log.

---

## 2. What one exo process actually is

There is no `--master` flag. Every process constructs every component at boot, including a
`Master`, and runs them as siblings under a single `anyio` task group. Three flags subtract
rather than add: `--no-worker` (coordinate but don't infer), `--no-api`, `--no-downloads`.

<img src="imgs/architecture/02-node-anatomy.svg" alt="One exo process contains a Router, EventRouter and Election that always run, plus an optional Master, Worker, API and DownloadCoordinator, all sitting above an embedded Rust layer that owns the zenoh session on its own tokio runtime. The Worker spawns separate runner OS processes that hold the model." width="100%">

Two details are worth carrying forward.

**The model never lives in the node process.** It lives in a `spawn`-mode child process,
connected by three `multiprocessing.Queue` channels (tasks down, cancellations down, events up)
and two pipes for stdout and stderr. That isolation is what lets a segfaulting MLX kernel be
reported as a typed failure rather than taking the cluster node down with it. The supervisor
also tees the child's stderr through a diagnostic collector that classifies known signatures —
Metal GPU timeout, ring socket errno, ring transport abort — and attaches the result to the
failure event, so a crash reason travels through the event log all the way to the HTTP client.

**Node identity is regenerated on every start** — `os.urandom(16).hex()`, with the persistence
implementation commented out behind a TODO. A machine that restarts is, to the cluster, a
brand-new member; the old identity lingers in state until a 30-second timeout reaps it.

---

## 3. Finding each other

Discovery is a small hand-written UDP protocol in Rust, not a library feature. Each node joins
the IPv6 multicast group `ff12::e0a1:de89` on port 52413 across every non-loopback interface
and, once a second, multicasts a `Hello` carrying a fresh nonce and an 8-byte namespace token.
A peer replies with a unicast `WhatsUp` carrying its zenoh id and TCP listen port. The
discoverer then dials — but only if the peer's id sorts *lower* than its own, so each pair
produces one dial attempt rather than two. From there zenoh's own gossip fills in the rest of
the mesh.

The namespace token is the first eight bytes of `blake3(namespace_string)`, and the default
namespace string is the exo package version.

> **Two nodes running different exo versions can never see each other.** Mismatched `Hello`
> packets are dropped with a trace-level log and nothing else. There is no IPv4 fallback (v4
> replies are explicitly discarded) and `--bootstrap-peers` now raises rather than working, so
> there is no manual join escape hatch either.

### A naming warning

The transport is zenoh. The Python-facing API is nevertheless called `gossipsub_subscribe` /
`gossipsub_publish`, the Rust module that implements it is headed *"Compat shim for the old
libp2p code"*, and the macOS app exposes a setting named `EXO_LIBP2P_NAMESPACE`. There is no
libp2p and no gossipsub anywhere in the repository. The names are archaeology from a previous
transport; read them as "publish" and "subscribe".

---

## 4. Election, and the epoch it defines

`AGENTS.md` calls this a bully algorithm. It isn't. There is exactly one wire message type, one
three-second timer, and a local `max()` over whatever ballots arrived. No `OK`/`COORDINATOR`
triad, no explicit states, no commit phase, no quorum.

| Ranking key | Meaning |
|---|---|
| `clock` | Lamport-style round counter, bumped on any connectivity change |
| `seniority` | The largest cluster this node has ever been elected master of. Never decays. `--force-master` simply sets it to `1_000_000`. |
| `commands_seen` | Prefer the better-informed node |
| `master_node_id` | Deterministic lexicographic tiebreak |

An election is triggered by *any* zenoh liveliness change. Not by a node timing out, not by a
failed inference, not by anything in cluster state — those are a completely separate, slower
mechanism. And the trigger carries almost no information: the peer's identity is discarded at
the Python/Rust boundary, so the signal reaching the election is a single boolean. A join and a
departure are indistinguishable.

The load-bearing optimisation is what an incumbent proposes. A node that already believes it is
master re-proposes its *existing* session identity rather than minting a fresh one. If it wins
again, the elected session equals the current session, `is_new_master` is false, and nothing at
all happens except the API un-pausing.

<img src="imgs/architecture/03-election.svg" alt="Comparison of two election outcomes. When a worker joins or leaves, the incumbent master re-proposes its existing session and wins, so nothing is torn down. When the master itself dies, no survivor holds its session, every node proposes a new one, and the entire cluster state is discarded." width="100%">

This asymmetry is the single most important operational fact about exo: churn among workers is
nearly free, and losing the master is a full cluster restart — including on machines that never
saw a fault.

Split brain is tolerated rather than prevented. An isolated node elects itself instantly (the
boot campaign uses a zero-second timeout). Containment is purely by tagging: a master discards
local events stamped with a foreign session, and every node discards global events that are not
from its own session's elected master. Two masters can coexist and simply ignore each other.
Commands, notably, carry no session at all and are processed by both.

---

## 5. The control cycle

Cluster state is a single frozen Pydantic object, and the only way to change it is to fold an
event into it with a pure reducer. Any component may *propose* an event; only the elected
master assigns it a global index. Every node then replays the indexed stream into its own
replica.

<img src="imgs/architecture/04-control-cycle.svg" alt="The event cycle. A component proposes an event through the EventRouter, which stamps it and publishes it on the local_events topic. The master sequencer assigns a global index, applies it to its own state and appends it to a disk log, then rebroadcasts on global_events. Every node's ordered buffer fans it to a Worker and API state replica, and the Worker's plan function turns state back into new proposals." width="100%">

Three things in that picture are easy to miss:

- **The master publishes its own decisions to itself.** A command handler's output is not in
  state until it has gone out on `local_events` and come back through the sequencer — which is
  why back-to-back commands can be planned against stale state.
- **The pure reducer asserts index contiguity rather than tolerating gaps.** All gap tolerance
  lives one layer up, in the ordered buffer and its nack.
- **Ordering is enforced twice with different keys** — per-producer sequence numbers at the
  master's ingress, then a single global index at every consumer's.

Delivery is at-least-once, and deliberately so. The inbound hop from Rust into Python is
**lossy by design**: the zenoh subscriber callback does a non-blocking send into a 1024-slot
channel and discards the result, so a burst larger than the buffer is dropped silently. That is
precisely why the event plane carries its own acknowledgement layer — an event is considered
delivered when it comes back on `global_events`, and anything unacknowledged after five seconds
is resent.

There is no snapshotting and no cross-session replay. A new master starts from an empty state
with an empty log; the previous log is rotated to a compressed archive that nothing ever reads
back. Cluster state after a failover is not *recovered*, it is *re-derived* — because workers
keep re-emitting their hardware telemetry every second, membership and topology rebuild
themselves within a few ticks. Placed model instances do not, because nothing re-emits them.

---

## 6. Workers converge; they are not commanded

There is no dispatcher telling nodes what to do. Every worker runs the same pure `plan()`
function against its own replica of global state, on a 100 ms tick, and it returns *at most
one* task. The function is a short-circuiting chain, so the ordering is the policy:

| Precedence | Condition | Emitted task |
|---|---|---|
| 1 | A task on this node was cancelled | `CancelTask` |
| 2 | Its instance is gone, or any sibling runner failed | `Shutdown` |
| 3 | This node owns a runner id with no live process | `CreateRunner` |
| 4 | Runner idle and the model has no download record | `DownloadModel` |
| 5 | Multi-node instance, all peers idle or connecting | `ConnectToGroup` |
| 6 | *Every* node in the instance reports the download complete | `LoadModel` |
| 7 | All peers loaded | `StartWarmup` |
| 8 | All peers ready, images assembled | the pending generation task |

Multi-node rendezvous is encoded as rank ordering over *observed statuses* rather than as
messages. To join the MLX group, ranks `0…N−2` issue `ConnectToGroup` as soon as they are idle,
and rank `N−1` waits until it can see every peer in the connecting state — listeners come up
before the last dialler. Warmup is the reverse: ranks above zero go first, rank zero last.
Nothing is negotiated; each node reads the same replicated state and independently reaches the
same conclusion.

This is also why a request "fans out" without any fan-out. The master creates *one* task; every
worker holding a shard of that instance sees it in its replica and dispatches it to its own
runner in parallel.

---

## 7. Placement: choosing the ring

Placement is the master's one genuinely interesting decision, and its candidate set is unusual:
not subsets of nodes, but **every simple cycle of the topology graph**, plus one artificial
singleton cycle per node. Cycles, because the MLX ring backend requires each rank to have a
real left and right neighbour — a set of machines that does not form a ring can never be
selected, no matter how much memory it has.

<img src="imgs/architecture/05-placement-funnel.svg" alt="A funnel of placement filters narrowing from all simple cycles in the topology graph down through minimum node count, aggregate memory, sharding shape divisibility, backend intersection and RDMA connectivity, to a final selection rule, producing either a ring instance or a jaccl RDMA instance." width="100%">

For pipeline sharding, layers are split in proportion to each node's observed free memory using
largest-remainder rounding, with a floor of one layer per node. Transport bootstrap is decided
here, centrally, and baked into the instance — the hostfile a runner later hands to MLX was
computed by the master before any node bound a socket.

Two things the objective function does *not* contain are worth knowing: link bandwidth is never
measured (a "fast link" is inferred from the interface name — Thunderbolt beats Ethernet beats
Wi-Fi, with an explicit TODO admitting this), and existing instances' memory consumption is not
accounted for, since node memory is whatever `psutil` last reported.

> **Placement is one-shot and never rebalances.** The master's background loop can only
> *delete* — it removes instances whose nodes vanished and times out silent nodes. There is no
> controller reconciling a desired state. Adding a machine to a running cluster does nothing
> until a human places a model again, and a node that dies takes its instance down permanently.

---

## 8. How the model is actually split

Once the weights are on disk, a runner loads the *full* checkpoint lazily and then rewrites the
loaded model graph in memory. Two mutually exclusive strategies exist over one
`mx.distributed` group, and they have almost nothing in common.

<img src="imgs/architecture/06-parallelism.svg" alt="Side-by-side comparison of pipeline and tensor parallelism. Under pipeline parallelism each rank keeps a contiguous slice of decoder layers and passes activations to the next rank, with an all-gather at the end of decode. Under tensor parallelism every rank keeps all layers but each weight matrix is split, requiring two all-reduces per dense layer per token." width="100%">

Pipeline sends one activation tensor per hop per token; tensor parallelism sends nothing
between layers but all-reduces twice inside each of them. Only tensor parallelism gets faster
as you add machines; pipeline mainly gets you more memory. Under pipeline sharding, per-node
memory is its layer share *plus* a full copy of the embedding table and head — the weight-size
calculation deliberately does not divide those.

### The trick that makes it work without a broadcast

Under pipeline parallelism, decode ends with `all_gather(output)[-B:]` in the last wrapped
layer. `all_gather` concatenates in rank order, so slicing off the final `B` rows yields the
last rank's hidden state — *on every rank*. Combined with the replicated norm and head, every
rank now materialises the same full-vocabulary logits. Seed the RNG identically and build the
sampler from the same replicated parameters, and all N machines independently sample the same
token. It is a broadcast implemented as an all-gather, and it means no logits and no tokens
ever go over the wire; the ranks stay in lockstep by determinism instead.

### Fighting the framework

Two non-obvious workarounds appear everywhere in this code and are worth recognising when you
read it:

- `mx.eval()` is placed around every distributed operation *on purpose* — it keeps the op on
  the CPU stream, which has no watchdog. A GPU command buffer waiting on a slow peer would hit
  Metal's timeout.
- `mx.depends(cache.keys, output)` chains sends and collectives into the KV cache so MLX's lazy
  evaluator cannot prune an operation whose result nothing reads. Remove it and the
  communication silently disappears.

Admission and cancellation are themselves collectives. Before running anything, ranks
all-gather their task ids and enqueue only the sorted *intersection*, because any disagreement
about which task runs, or in what order, deadlocks the whole collective sequence. A task that
never reaches consensus simply waits forever rather than hanging the group.

Tensor parallelism is an explicit allowlist twice over: the model card must declare
`supports_tensor`, and the sharding pass raises on any model class without a registered
per-architecture strategy.

---

## 9. Life of a request

<img src="imgs/architecture/07-request-lifecycle.svg" alt="Swimlane sequence for one streaming chat completion. The request travels down from the HTTP client through the API, the elected master and every worker to the runner processes; the generated chunk then travels back up the same lanes. The four return hops repeat for every single token." width="100%">

The only place a request is rejected for "model not loaded" is the API's admission check, which
requires an *instance* to exist. If one exists but its runners are still downloading or
loading, the master happily creates the task and the client waits — with no server-side
deadline anywhere in the path, held open by 10-second SSE keep-alive comments.

Four API dialects converge on one internal request type before any of this begins: OpenAI Chat
Completions, OpenAI Responses, Anthropic Messages, and Ollama. Adding a dialect means adding an
adapter, not touching the pipeline. exo's own telemetry rides back in SSE *comments* —
`: prefill_progress`, `: generation_stats`, `: keep-alive` — so third-party clients ignore it
automatically. Ollama is the exception in shape: newline-delimited JSON rather than SSE.

A small detail that shows how carefully this layer was tuned: the Anthropic adapter strips
`x-anthropic-*` telemetry headers out of the system prompt, because Claude Code prepends
per-request content hashes that would otherwise break the KV prefix cache after about twenty
tokens instead of matching thousands.

---

## 10. Getting the weights onto the machines

Downloads are the one plane with no coordination at all. The master issues no download commands
for a new instance; each worker notices from replicated state that its runner needs a model and
publishes a `StartDownload` addressed to itself. Every node's coordinator subscribes to the
same broadcast topic and discards anything not addressed to it — the only unicast in the entire
system, implemented as a filter on a payload field.

> **Every participating node downloads the entire repository, not its shard.** The function
> that computes download patterns returns `["*"]` before any shard logic runs, with a comment
> giving three reasons smart downloads are off: not all file kinds are handled, there are no
> sticky sessions, and tensor parallelism needs every file anyway. The layer-range-aware filter
> beneath it is unreachable code. Four nodes running a 400 GB model means four independent
> 400 GB pulls from `huggingface.co`.

Sharding therefore happens in memory at load time, never on disk. Placement does bias toward
nodes that already hold weights — download warmth is the first term of the objective — but
nothing relays bytes between peers, and nothing checks free disk until a download is already
failing.

Every model exo can run is described by a TOML model card: 123 of them for language models, 18
for image models, declaring layer count, hidden size, KV head count, tensor-parallel support,
storage size, supported backends, context length, and sampling defaults. An unknown Hugging
Face repository can be turned into a card on the fly by fetching its `config.json` and weight
index. Custom cards live in two places at once — on disk and in replicated state — reconciled
by a one-second worker loop, which means a master election wipes them from state and the
reconciler then deletes them from disk.

---

## 11. API, dashboard, and the macOS app

Every node runs its own full API by default, and it is a peer rather than a proxy: it mirrors
cluster state from the event stream and publishes commands onto the bus, so any node can serve
any request. The Svelte dashboard is compiled to static files and mounted at `/` by the same
FastAPI app, which is why exo ships as a single process serving both.

The dashboard has no push channel. It polls `GET /state` — a whole-cluster snapshot — once per
second, and derives everything else from that: topology graph, memory and temperature per
device, download bars, instance readiness, chat model list. Token streaming is the one
exception, arriving over SSE-in-fetch. There are no WebSockets anywhere in the codebase, and
the backend's `/events` endpoint is a dump of the on-disk log rather than a live stream.

The shipped macOS app is worth calling out separately, because it changes what "configuration"
means. It is a SwiftUI menu-bar agent that supervises a PyInstaller-frozen copy of the daemon —
and it launches that process with *zero command-line arguments*. Every flag documented for
`uv run exo` is unreachable in the packaged product; the only knobs that reach the process are
environment variables. The app also owns cluster networking concerns Python does not:
Thunderbolt bridge detection and configuration, the macOS local-network permission check
(without which UDP discovery silently fails), and a root LaunchDaemon that installs an "exo"
network location.

---

## 12. Sharp edges

None of these are bugs exactly; they are the visible consequences of the design decisions
above. They are the things that will surprise you first.

**1. Losing the master restarts the entire cluster.** Not a handover. New master, empty state,
rotated log, index back to zero, every node rebuilds its worker, every worker kills every
runner. Machines that never saw a fault unload their models too. All placements must be redone
by hand.

**2. There is no automatic re-placement.** The master's reconcile loop can only delete. A
crashed worker permanently takes its instance down; recovery requires a human or the dashboard
to place the model again.

**3. A dead node's runner entry is frozen forever.** Only a clean `RunnerShutdown` removes a
runner from state, and a dead node can never send one. The stale entry actively blocks the
survivors, whose group-join requires every runner in the instance to be idle or connecting — so
restart attempts burn through the retry budget until the instance is deleted.

**4. Whole-node crashes truncate the client stream silently.** The clean error path — an error
chunk carrying mined stderr diagnostics — only exists when the *local* runner crashes. When a
remote node dies, the API closes the request's queue on instance deletion and the stream just
ends: no error frame, no `[DONE]`.

**5. Command failures are invisible to the caller.** Every master command handler is wrapped in
catch-and-log. A rejected placement produces no event and no response, and
`POST /place_instance` returns success either way. Validate with the synchronous dry-run
endpoints (`/instance/placement`, `/instance/previews`) — they are the only place a placement
error surfaces.

**6. There is no authentication anywhere.** The API binds `0.0.0.0` with CORS set to `*` and
credentials allowed. Unauthenticated endpoints include cluster mutation (create/delete
instance, start download, add model) and full disclosure: `GET /state`, and `GET /events`,
which returns the entire on-disk event log — prompts, completions, and image chunks included.
The cluster bus itself carries all of that as cleartext JSON to every peer.

**7. Every join and every leave costs a three-second election.** Because the connectivity
signal is one boolean with the peer identity discarded, exo cannot tell whether the changed
peer mattered. A flapping link means repeated campaigns, each pausing new requests
cluster-wide.

**8. Nothing applies backpressure inside Python.** Every in-process channel is unbounded — the
routing layer accepts a `max_buffer_size` and ignores it. Publishers never block; a slow
consumer just grows its queue until memory runs out. Meanwhile the Rust-to-Python inbound hop
drops messages when its 1024-slot buffer fills, without a log.

**9. Version is identity on the network.** The discovery namespace defaults to the exo version,
so two builds never see each other — and in the packaged macOS app the namespace setting cannot
actually change it, since the daemon is launched without arguments.

**10. A failed download is sticky.** Failure is terminal for that model on that node: neither
the coordinator nor the planner will retry while the status is failed. It clears only when the
coordinator is rebuilt — which happens on a new-master election. In offline mode a missing
model fails immediately and blocks the load barrier for every peer, since loading requires
*every* node to report the download complete.

**11. Ports are guessed, not reserved.** The MLX ring port, the jaccl coordinator port and each
prefill server port are drawn at random from the ephemeral range and never bound or checked
first — with a single hardcoded carve-out to avoid the API's own port.

**12. The repository's own docs are stale in load-bearing ways.** `AGENTS.md` describes a
`system_custodian` Rust crate that does not exist (the Cargo workspace is exactly two crates,
`exo_rs` and `networking`), and calls the election a bully algorithm, which it is not.
`docs/api.md` points at a `src/exo/master/api.py` that does not exist and omits roughly a dozen
live endpoints. These are the first files a newcomer reads.

---

## 13. Platform reality

Support is deliberately asymmetric. Apple Silicon macOS is the shipped, tested, notarised
product — a DMG containing a SwiftUI menu-bar app with a PyInstaller-frozen Python runtime
inside it, updated through a signed Sparkle feed. Linux builds and is verified in CI but runs
inference on CPU. There is no Windows path.

Nix is the build system of record: CI runs `nix flake check` and builds every flake output
across three platforms, including a bespoke step that extracts Apple's Metal toolchain into the
Nix store because the MLX Metal backend cannot otherwise be built hermetically. The
`uv` + `cargo` + `npm` path is the developer convenience, not the source of truth.

One provenance fact matters for anyone reasoning about behaviour: exo does not run upstream
dependencies on its hot paths. The Cargo workspace patches roughly twenty-five zenoh crates to
a fork, and `mlx` and `mlx-lm` are both pinned to private branches — on top of which the runner
applies its own monkey-patches at import time. Behaviour you look up in upstream documentation
may not be the behaviour you get.

### Where to start reading

| If you want to understand… | Read |
|---|---|
| Composition and lifecycle | `src/exo/main.py` — the whole node is assembled here |
| What can happen to state | `src/exo/shared/apply.py` and `src/exo/shared/types/events.py` |
| Why a node is doing what it's doing | `src/exo/worker/plan.py` — the precedence chain *is* the policy |
| Why a placement failed | `src/exo/master/placement.py` and `placement_utils.py` |
| How the model is sharded | `src/exo/worker/engines/mlx/auto_parallel.py` |
| Message plumbing | `src/exo/routing/topics.py`, then `router.py`, then `event_router.py` |

---

*Diagram sources live in [`imgs/architecture/`](imgs/architecture/) as standalone SVGs; they
adapt to light and dark themes. [`audit.md`](audit.md) is the companion improvement audit — the
same source read for defects rather than for structure.*
