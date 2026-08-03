# `/me/events` — In-Process Pub/Sub Fan-Out

This is the design doc for the per-user notification stream (`GET
/me/events`) — the lightweight SSE feed that keeps the sidebar's
RUNNING/COMPLETE/FAILED indicators in sync across a user's tabs and
sessions. Code lives in `app/services/user/hub.py` (the fan-out),
`app/services/user/events.py` (the SSE generator), `app/api/public/user.py`
(the endpoint), and the `USER_EVENTS_CHANNEL` constant in
`app/services/redis.py`.

> **Not** the same as `session_stream` (the token-by-token detail SSE in
> `app/services/session/streaming.py`). That one must stay durable,
> ordered, and replayable, so it keeps using a Redis **Stream** + cursor.
> This doc is only about the lightweight, idempotent notification feed.

---

## 1. TL;DR

- **One Redis `SUBSCRIBE` per backend process**, created at startup —
  **not** one per connection. Opening `/me/events` allocates only an
  in-memory `asyncio.Queue`; it never touches Redis.
- The worker `PUBLISH`es `{user_id, event}` to a single global channel
  (`user_events:bus`). Each process's `UserEventHub` receives every
  event and routes it in-memory to the queues of **that user's
  connections on this process**.
- Redis connections for this feed drop from **O(concurrent SSE)** to
  **O(processes)**. Natively multi-process / multi-node — the broadcast
  reaches whichever process holds the connection, so **no sticky
  sessions**.
- Delivery is **at-most-once** (Redis Pub/Sub, fire-and-forget). That's
  fine because `session.status_changed` is **idempotent state sync** (it
  sets the current status, not a delta): a dropped event self-heals on
  the next status change, or when the client reconnects and re-fetches
  `GET /sessions`.

This is the same architecture as the Socket.IO Redis adapter / Phoenix
PubSub / Django Channels — a deliberately minimal, hand-rolled version
scoped to one idempotent, low-volume event type.

---

## 2. The problem it replaced

The original design had **each** `/me/events` connection open its own
Redis connection and run a blocking `XREAD` on a per-user stream
(`user_events:{user_id}`). So *N* concurrent SSE connections held *N*
Redis connections. With the streaming pool capped at ~100/process, that
put a hard, low ceiling on concurrent sidebar listeners and coupled
connection count to Redis pool pressure.

---

## 3. Architecture

```
                  ┌──────────── Backend process (1 of N) ─────────────┐
 worker           │                                                    │
 PUBLISH ───────► │  ① ONE Redis SUBSCRIBE "user_events:bus"          │
 {user_id,event}  │       (created at startup — UserEventHub._reader)  │
                  │              │                                     │
                  │              ▼  ② _dispatch: route by user_id      │
                  │     _subscribers = { 42:{qA,qB}, 99:{qC}, ... }    │
                  │          │       │           │                     │
                  │          ▼       ▼           ▼                     │
                  │     ③ queueA  queueB      queueC  ← in-memory Queue │
                  │          │       │           │                     │
                  │          ▼       ▼           ▼                     │
                  │       SSE-A    SSE-B       SSE-C  ← browser conns   │
                  └────────────────────────────────────────────────────┘

 opening /me/events  =  ③ one more in-memory queue + ZERO Redis conns
```

**The single subscription** is owned by the app lifespan
(`app/main.py`): at startup it builds `app.state.user_event_hub =
UserEventHub(app.state.redis)` and calls `.start()`, which launches the
`_reader()` task that does `pubsub.subscribe(USER_EVENTS_CHANNEL)` once
and holds one connection for the life of the process. It is torn down in
the lifespan `finally`, before the Redis pools close.

**Connections are in-memory only.** `hub.subscribe(user_id)` (called by
`stream_user_events`) creates an `asyncio.Queue(maxsize=16)`, adds it to
`_subscribers[user_id]`, and returns it. `hub.unsubscribe` removes it on
disconnect. Neither touches Redis.

---

## 4. Request lifecycle

1. **Open.** Browser opens `GET /me/events`. The endpoint
   (`stream_current_user_events`) injects the process-wide hub via
   `Depends(get_user_event_hub)` and calls `stream_user_events(hub,
   user.id)`. That calls `hub.subscribe(user.id)` → a new in-memory
   queue → emits a one-shot `user.ready` handshake → then blocks on
   `await queue.get()`.
2. **Event.** The worker `PUBLISH`es `{user_id, event}` to
   `user_events:bus`. The process's single `_reader` receives it (Redis
   broadcasts to every subscribed process), `_dispatch` looks up
   `_subscribers[user_id]`, and `put_nowait`s the event into each
   matching queue.
3. **Deliver.** `queue.get()` wakes, the generator serializes the event
   to SSE and yields it to the browser.
4. **Close.** On client disconnect, sse-starlette cancels the generator
   task; the `CancelledError` unwinds through `await queue.get()` into
   the `finally`, which calls `hub.unsubscribe`.

---

## 5. Key design decisions

- **Live-only, no replay.** The stream emits only events that arrive
  after it opens. Initial sidebar state comes from the page's `GET
  /sessions` REST fetch — *not* from this stream. Replaying past
  `COMPLETE`/`FAILED` lines would wrongly flip a still-running session's
  indicator off. There is no `Last-Event-ID` gap recovery; a reconnect
  re-fetches the list, which reconciles anything missed.

- **At-most-once is acceptable because the payload is idempotent.**
  `session.status_changed` carries the *current* status, not a
  transition. A dropped event leaves the sidebar momentarily stale until
  the next status change or reconnect heals it. This is what lets the
  feed use the lightest broker primitive (Pub/Sub) instead of a durable
  Stream.

- **Bounded per-connection queue, drop-oldest** (`_QUEUE_MAXSIZE = 16`).
  This is the **depth of one connection's buffer**, *not* a cap on
  connections per process. A slow client (backgrounded tab, weak
  network) that can't drain in time drops its **oldest** queued event
  rather than stalling the shared reader or its peers. For idempotent
  status sync only the latest state matters, so dropping stale
  intermediate states is harmless. Connection count is bounded by file
  descriptors + per-connection memory, fully decoupled from Redis.

- **One global channel, route locally.** Every process receives every
  user's events and delivers only the ones it has local subscribers for
  (`_dispatch` early-returns when `_subscribers` has no entry). One
  subscription per process regardless of how many users it serves. The
  broadcast amplification is negligible for sparse `status_changed`
  traffic; at much higher event volume you would shard into per-room
  channels (see §8).

- **No sticky sessions.** Because Redis broadcasts to all processes,
  whichever process holds a user's connection also receives the event.
  Multi-process / multi-node is the native shape, not a special case.

---

## 6. Reader resilience (`_reader`)

- **Dead-connection detection.** The loop uses
  `pubsub.get_message(timeout=30)` rather than `listen()`. `listen()`
  blocks indefinitely and never re-checks an idle subscription;
  `get_message` with a timeout re-enters the pubsub health check each
  tick, so a silently-dropped subscription socket surfaces as an error
  and the outer loop resubscribes instead of hanging forever. Aligned
  with the pool's 30s `health_check_interval`.
- **Malformed-payload tolerance.** `_dispatch` is wrapped in
  `try/except`: one bad message is logged and dropped, rather than
  tearing down the reader (which would resubscribe and briefly stall
  every connection on the process).
- Events missed during a resubscribe window are tolerated — same
  idempotent-self-heal argument as §5.

---

## 7. Wire contract + deploy coupling

`USER_EVENTS_CHANNEL = "user_events:bus"` in `app/services/redis.py` is a
**wire contract mirrored** in `agent-worker/agent_worker/wire/keys.py`.
The worker's producer side `PUBLISH`es to this channel (it used to
`XADD` a per-user stream). Because both ends changed, **backend and
worker must deploy together** — otherwise one writes a stream while the
other subscribes a channel and the notification feed silently breaks.
(The old `USER_EVENTS_PREFIX` / `user_events_key()` were removed.)

---

## 8. When to evolve

The current shape is right for sparse, idempotent, low-volume status
events. Signals that you've outgrown it:

- **High event volume × many processes** → the every-process-gets-every-event
  broadcast amplifies. Shard into per-room/per-shard channels (cf.
  Socket.IO's sharded adapter) or move the broker to NATS/Kafka.
- **Need "never drop + replayable"** → switch this feed to a Redis
  Streams consumer group or Kafka. (You'd be giving up the simplicity
  that idempotency currently buys.)
- **Push becomes a first-class concern** → offload to a dedicated push
  server (Centrifugo, Mercure) instead of maintaining a hand-rolled hub.

Until then, keep this feed scoped to lightweight idempotent events;
anything that needs durability/ordering/replay belongs on
`session_stream`, not here.
