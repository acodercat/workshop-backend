#!/usr/bin/env bash
# Backend service control + full deploy. Manages TWO siblings:
#
#  - uvicorn (web)              — top-level commands (start/stop/...).
#  - background-settle (background) — ``deploy.sh settle SUBCMD``.
#
# Subcommand layout mirrors the frontend's ``serve.sh`` so the operator's
# muscle memory carries across services.
#
# Web subcommands:
#   start    start uvicorn (no-op if already running on :PORT)
#   stop     SIGTERM, escalate to SIGKILL after the shutdown timeout
#   restart  stop + start (does NOT sync deps or run migrations)
#   status   report state of BOTH web and settle
#   logs     ``tail -f`` the uvicorn log
#   deploy   the full pipeline: stop everything + ``uv sync`` + ``alembic
#            upgrade head`` + start both. Use this after pulling new code.
#
# Settle subcommands (``deploy.sh settle ...``):
#   start    start the background-settle process
#   stop     SIGTERM the process; escalate to SIGKILL after the shutdown timeout
#   restart  stop + start
#   status   report whether the process is running
#   logs     ``tail -f`` the settle log
#
# Truth source for "is it running":
#   web      ``lsof -t -i:$PORT``  — survives bootstrap transitions where
#            uvicorn was once started manually with ``nohup``.
#   settle   ``pgrep -f 'app.workers.run'`` — no port; the module path
#            is specific enough that no other process will share it on
#            the cmdline.
#
# Multi-node note: every host should run both web and background-settle.
# The sweeper coordinates across hosts via Postgres advisory lock (see
# ``app/workers/settle_sweeper.py::_LOCK_KEY``); at most one host's
# sweeper actually polls per tick, the rest hit the busy lock and skip.
#
# Logs:
#   logs/uvicorn.log     uvicorn stdout/stderr, appended on every start
#                        so a startup banner per cycle doubles as a
#                        deploy timeline.
#   logs/settle.log     background-settle stdout/stderr, same pattern.
#   logs/<loguru>        application-level logs (set up in
#                        ``app/logging_config.py``); independent of
#                        this script.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"
cd "$APP_DIR"

PORT=8005
# uvicorn worker processes. Default tuned for an 8-core box: ~cores minus
# headroom for the settle sibling + OS + bursts (async workers ≈ cores).
# Override per-host via the WORKERS env var, e.g. `WORKERS=12 bash scripts/deploy.sh restart`.
WORKERS="${WORKERS:-6}"
LOG_DIR="$APP_DIR/logs"
UVICORN_LOG="$LOG_DIR/uvicorn.log"
SETTLE_LOG="$LOG_DIR/settle.log"

# Cmdline pattern used by ``pgrep -f`` to find the settle process. The
# python module path is distinctive enough that no other process should
# share it; we anchor with a backslash-dot so partial substring matches
# in other ``python -m`` invocations don't trip us.
SETTLE_PATTERN='app\.workers\.run'

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

# ── Settle detection ──────────────────────────────────────────────────
settle_pids() {
  pgrep -f "$SETTLE_PATTERN" 2>/dev/null || true
}

settle_is_running() {
  [[ -n "$(settle_pids)" ]]
}

# ── Web commands ───────────────────────────────────────────────────────
cmd_start() {
  if is_running; then
    log "web already running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
    return 0
  fi
  log "starting uvicorn on :$PORT (workers=$WORKERS)"
  nohup uv run uvicorn app.main:app \
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

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_logs() {
  exec tail -f "$UVICORN_LOG"
}

# ── Settle commands ───────────────────────────────────────────────────
cmd_settle_start() {
  if settle_is_running; then
    log "settle already running (PID $(settle_pids | tr '\n' ' '))"
    return 0
  fi
  log "starting background-settle"
  nohup uv run python -m app.workers.run \
    >> "$SETTLE_LOG" 2>&1 &
  disown

  # Settle has no port to bind; readiness = "the process still
  # exists after ${HEALTH_TIMEOUT_SECONDS}s". Crash-on-startup
  # (missing config, DB unreachable) shows as "process gone".
  for ((i=1; i<=HEALTH_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if settle_is_running; then
      log "settle OK — PID $(settle_pids | tr '\n' ' ') after ${i}s"
      log "tail logs: tail -f $SETTLE_LOG"
      return 0
    fi
  done

  log "FAILED: settle not running after ${HEALTH_TIMEOUT_SECONDS}s"
  log "last 40 lines of $SETTLE_LOG:"
  tail -40 "$SETTLE_LOG" || true
  return 1
}

cmd_settle_stop() {
  local pids
  pids="$(settle_pids)"
  if [[ -z "$pids" ]]; then
    log "settle not running"
    return 0
  fi
  # shellcheck disable=SC2086
  log "stopping settle PID(s): $(echo $pids | tr '\n' ' ')"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true

  for ((i=1; i<=SHUTDOWN_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if ! settle_is_running; then
      log "settle stopped gracefully (${i}s)"
      return 0
    fi
  done

  local stragglers
  stragglers="$(settle_pids)"
  log "settle graceful timed out after ${SHUTDOWN_TIMEOUT_SECONDS}s; SIGKILL"
  if [[ -n "$stragglers" ]]; then
    echo "$stragglers" | xargs kill -9 2>/dev/null || true
  fi
}

cmd_settle_restart() {
  cmd_settle_stop
  cmd_settle_start
}

cmd_settle_status() {
  if settle_is_running; then
    log "settle running (PID $(settle_pids | tr '\n' ' '))"
  else
    log "settle not running"
  fi
}

cmd_settle_logs() {
  exec tail -f "$SETTLE_LOG"
}

# ── Combined status / deploy ──────────────────────────────────────────
cmd_status() {
  if is_running; then
    log "web      running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
  else
    log "web      not running on :$PORT"
  fi
  if settle_is_running; then
    log "settle  running (PID $(settle_pids | tr '\n' ' '))"
  else
    log "settle  not running"
  fi
}

cmd_deploy() {
  # Stop everything first so any new code on uv sync / migration error
  # surfaces as a hard fail rather than a half-deployed mismatch.
  cmd_stop
  cmd_settle_stop
  log "uv sync"
  uv sync --quiet
  log "alembic upgrade head"
  uv run alembic upgrade head
  # Start order: settle first, then web. Either order works (no
  # dependency), but settle-first means by the time web accepts
  # traffic the background reconciliation is already running.
  cmd_settle_start
  cmd_start
}

# ── Dispatch ───────────────────────────────────────────────────────────
case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  deploy)  cmd_deploy ;;
  settle)
    case "${2:-}" in
      start)   cmd_settle_start ;;
      stop)    cmd_settle_stop ;;
      restart) cmd_settle_restart ;;
      status)  cmd_settle_status ;;
      logs)    cmd_settle_logs ;;
      *)
        cat <<EOF
usage: $0 settle {start|stop|restart|status|logs}
EOF
        exit 1
        ;;
    esac
    ;;
  *)
    cat <<EOF
usage: $0 {start|stop|restart|status|logs|deploy|settle SUBCMD}

  start    start uvicorn (no-op if already running)
  stop     SIGTERM, escalate to SIGKILL after ${SHUTDOWN_TIMEOUT_SECONDS}s
  restart  stop + start (no deps/migration sync)
  status   show state of BOTH web (:$PORT) and background-settle
  logs     tail -f $UVICORN_LOG
  deploy   stop all + uv sync + alembic upgrade head + start all

  settle start | stop | restart | status | logs
           same lifecycle commands targeting the background-settle process
           (tail -f $SETTLE_LOG on logs)
EOF
    exit 1
    ;;
esac
