#!/usr/bin/env bash
# Backend service control + full deploy. Manages TWO siblings:
#
#  - uvicorn (web)        — the request/SSE tier on :PORT.
#  - background (process) — the backend's background-jobs process
#                           (``python -m app.workers.run``): credit-settle
#                           sweeper + subscriber, plus the payment reconciler.
#
# Top-level commands act on BOTH siblings, so one command manages the whole
# backend. ``deploy.sh background SUBCMD`` is the escape hatch that drives the
# background process alone (retry it, cycle it, tail its log).
#
# Top-level commands:
#   start    start web + background (each no-op if already running)
#   stop     stop both (SIGTERM, escalate to SIGKILL after the timeout)
#   restart  stop both + start both (does NOT sync deps or run migrations)
#   status   report state of BOTH web and background
#   logs     ``tail -f`` the uvicorn log (background log via ``background logs``)
#   deploy   the full pipeline: stop both + ``uv sync`` + ``alembic upgrade
#            head`` + start both. Use this after pulling new code.
#
# Background-only subcommands (``deploy.sh background ...``):
#   start | stop | restart | status | logs — same lifecycle, background process only.
#
# Web start is the critical path (fail-hard if the port won't bind); the
# background process is a backstop, so the combined start brings it up
# best-effort — a background hiccup (e.g. DB briefly unreachable) never holds
# the web tier down.
#
# Truth source for "is it running":
#   web         ``lsof -t -i:$PORT``  — survives bootstrap transitions where
#               uvicorn was once started manually with ``nohup``.
#   background  ``pgrep -f 'app.workers.run'`` — no port; the module path
#               is specific enough that no other process will share it on
#               the cmdline.
#
# Multi-node note: every host should run both web and the background process.
# The settle sweeper coordinates across hosts via Postgres advisory lock (see
# ``app/workers/settle_sweeper.py::_LOCK_KEY``); at most one host's sweeper
# actually polls per tick, the rest hit the busy lock and skip.
#
# Logs:
#   logs/uvicorn.log     uvicorn stdout/stderr, appended on every start
#                        so a startup banner per cycle doubles as a
#                        deploy timeline.
#   logs/background.log  background-process stdout/stderr, same pattern.
#   logs/<loguru>        application-level logs (set up in
#                        ``app/logging_config.py``); independent of
#                        this script.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"
cd "$APP_DIR"

PORT=8005
# uvicorn worker processes. Default tuned for an 8-core box: ~cores minus
# headroom for the background sibling + OS + bursts (async workers ≈ cores).
# Override per-host via the WORKERS env var, e.g. `WORKERS=12 bash scripts/deploy.sh restart`.
WORKERS="${WORKERS:-6}"
LOG_DIR="$APP_DIR/logs"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
BACKGROUND_LOG="$LOG_DIR/background.log"

# Cmdline pattern used by ``pgrep -f`` to find the background process. The
# python module path is distinctive enough that no other process should
# share it; we anchor with a backslash-dot so partial substring matches
# in other ``python -m`` invocations don't trip us.
BACKGROUND_PATTERN='app\.workers\.run'

# Health-check budget: total seconds we wait for the new uvicorn to
# bind the port. Covers ``uv`` re-resolving the lockfile + uvicorn's
# own startup. Polled at 1s — fine-grained enough for a manual deploy,
# integer-only so we don't depend on ``bc``.
HEALTH_TIMEOUT_SECONDS=10

# Graceful-shutdown budget: seconds we wait for SIGTERM'd workers to
# finish in-flight requests before escalating to SIGKILL.
SHUTDOWN_TIMEOUT_SECONDS=10

mkdir -p "$LOG_DIR"

log() { echo "[$(date +%H:%M:%S)] $*"; }

# ── Web detection ───────────────────────────────────────────────────────
bound_pids() {
  lsof -t -i:"$PORT" 2>/dev/null || true
}

is_running() {
  [[ -n "$(bound_pids)" ]]
}

# ── Background-process detection ───────────────────────────────────────
background_pids() {
  pgrep -f "$BACKGROUND_PATTERN" 2>/dev/null || true
}

background_is_running() {
  [[ -n "$(background_pids)" ]]
}

# ── Web commands ───────────────────────────────────────────────────────
cmd_start() {
  if is_running; then
    log "web already running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
    return 0
  fi
  log "starting uvicorn on :$PORT (workers=$WORKERS)"
  nohup uv run --no-sync uvicorn app.public_main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --workers "$WORKERS" \
    >> "$UVICORN_LOG" 2>&1 &
  disown

  for ((i=1; i<=HEALTH_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if is_running; then
      log "web OK — bound :$PORT after ${i}s (PID $(bound_pids | tr '\n' ' '))"
      log "tail logs: tail -f $UVICORN_LOG"
      return 0
    fi
  done

  log "FAILED: nothing bound :$PORT after ${HEALTH_TIMEOUT_SECONDS}s"
  log "last 40 lines of $UVICORN_LOG:"
  tail -40 "$UVICORN_LOG" || true
  return 1
}

cmd_stop() {
  local pids
  pids="$(bound_pids)"
  if [[ -z "$pids" ]]; then
    log "web not running on :$PORT"
    return 0
  fi
  # shellcheck disable=SC2086  # word-splitting is intended for ``kill``.
  log "stopping web PID(s): $(echo $pids | tr '\n' ' ')"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true

  for ((i=1; i<=SHUTDOWN_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if ! is_running; then
      log "web stopped gracefully (${i}s)"
      return 0
    fi
  done

  local stragglers
  stragglers="$(bound_pids)"
  log "web graceful timed out after ${SHUTDOWN_TIMEOUT_SECONDS}s; SIGKILL"
  if [[ -n "$stragglers" ]]; then
    echo "$stragglers" | xargs kill -9 2>/dev/null || true
  fi
}

cmd_logs() {
  exec tail -f "$UVICORN_LOG"
}

# ── Background-process commands ────────────────────────────────────────
cmd_background_start() {
  if background_is_running; then
    log "background already running (PID $(background_pids | tr '\n' ' '))"
    return 0
  fi
  log "starting background process"
  nohup uv run --no-sync python -m app.workers.run \
    >> "$BACKGROUND_LOG" 2>&1 &
  disown

  # The background process has no port to bind; readiness = "the process
  # still exists after ${HEALTH_TIMEOUT_SECONDS}s". Crash-on-startup
  # (missing config, DB unreachable) shows as "process gone".
  for ((i=1; i<=HEALTH_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if background_is_running; then
      log "background OK — PID $(background_pids | tr '\n' ' ') after ${i}s"
      log "tail logs: tail -f $BACKGROUND_LOG"
      return 0
    fi
  done

  log "FAILED: background not running after ${HEALTH_TIMEOUT_SECONDS}s"
  log "last 40 lines of $BACKGROUND_LOG:"
  tail -40 "$BACKGROUND_LOG" || true
  return 1
}

cmd_background_stop() {
  local pids
  pids="$(background_pids)"
  if [[ -z "$pids" ]]; then
    log "background not running"
    return 0
  fi
  # shellcheck disable=SC2086
  log "stopping background PID(s): $(echo $pids | tr '\n' ' ')"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true

  for ((i=1; i<=SHUTDOWN_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if ! background_is_running; then
      log "background stopped gracefully (${i}s)"
      return 0
    fi
  done

  local stragglers
  stragglers="$(background_pids)"
  log "background graceful timed out after ${SHUTDOWN_TIMEOUT_SECONDS}s; SIGKILL"
  if [[ -n "$stragglers" ]]; then
    echo "$stragglers" | xargs kill -9 2>/dev/null || true
  fi
}

cmd_background_restart() {
  cmd_background_stop
  cmd_background_start
}

cmd_background_status() {
  if background_is_running; then
    log "background running (PID $(background_pids | tr '\n' ' '))"
  else
    log "background not running"
  fi
}

cmd_background_logs() {
  exec tail -f "$BACKGROUND_LOG"
}

# ── Combined lifecycle (both siblings) ────────────────────────────────
cmd_start_all() {
  # Web first + fail-hard (a port that won't bind is a real failure that
  # should abort). The background process is a backstop, brought up
  # best-effort so its startup trouble can't hold the web tier down — the
  # ``|| log`` keeps ``set -e`` from aborting and points at the granular retry.
  cmd_start
  cmd_background_start \
    || log "WARNING: background failed to start; web is up. Retry: $0 background start"
}

cmd_stop_all() {
  cmd_stop
  cmd_background_stop
}

cmd_restart_all() {
  cmd_stop_all
  cmd_start_all
}

# ── Combined status / deploy ──────────────────────────────────────────
cmd_status() {
  if is_running; then
    log "web        running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
  else
    log "web        not running on :$PORT"
  fi
  if background_is_running; then
    log "background running (PID $(background_pids | tr '\n' ' '))"
  else
    log "background not running"
  fi
}

cmd_deploy() {
  # Stop both first so a uv sync / migration error surfaces as a hard
  # fail rather than a half-deployed mismatch, then bring both back up
  # through the same combined helpers the top-level commands use — web
  # fail-hard, background best-effort (a background startup hiccup must
  # not block the web deploy).
  cmd_stop_all
  # ``--locked`` asserts uv.lock matches pyproject.toml rather than quietly
  # re-resolving on a production box — a lockfile somebody forgot to commit
  # should fail the deploy, not become whatever resolves today.
  #
  # ``--no-dev``: production needs the app's dependencies, not the test and
  # lint tooling. Without it a deploy also downloads ruff and mypy, which is
  # tens of megabytes of nothing this box runs — and long enough that the
  # start below can time out waiting for a port that is still being unpacked.
  #
  # The start commands then use ``--no-sync``, not ``--no-dev``: ``uv run``
  # syncs before executing unless told not to, and a start that installs is
  # a start that can change what is installed. Installing is this step's
  # job; ``start`` runs what it left.
  log "uv sync"
  uv sync --locked --no-dev --quiet
  log "alembic upgrade head"
  uv run --no-sync alembic upgrade head
  cmd_start_all
}

# ── Dispatch ───────────────────────────────────────────────────────────
case "${1:-}" in
  start)   cmd_start_all ;;
  stop)    cmd_stop_all ;;
  restart) cmd_restart_all ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  deploy)  cmd_deploy ;;
  background)
    case "${2:-}" in
      start)   cmd_background_start ;;
      stop)    cmd_background_stop ;;
      restart) cmd_background_restart ;;
      status)  cmd_background_status ;;
      logs)    cmd_background_logs ;;
      *)
        cat <<EOF
usage: $0 background {start|stop|restart|status|logs}
EOF
        exit 1
        ;;
    esac
    ;;
  *)
    cat <<EOF
usage: $0 {start|stop|restart|status|logs|deploy|background SUBCMD}

  start    start web + background (each no-op if already running)
  stop     stop both (SIGTERM, escalate to SIGKILL after ${SHUTDOWN_TIMEOUT_SECONDS}s)
  restart  stop both + start both (no deps/migration sync)
  status   show state of BOTH web (:$PORT) and the background process
  logs     tail -f $UVICORN_LOG (background log: $0 background logs)
  deploy   stop both + uv sync + alembic upgrade head + start both

  background start | stop | restart | status | logs
           drive ONLY the background process (tail -f $BACKGROUND_LOG on logs)
EOF
    exit 1
    ;;
esac
