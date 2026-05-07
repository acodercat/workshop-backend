#!/usr/bin/env bash
# Backend service control + full deploy. Subcommand layout mirrors the
# frontend's ``serve.sh`` so the operator's muscle memory carries
# across the two services.
#
# Subcommands:
#   start    start uvicorn (no-op if already running on :PORT)
#   stop     SIGTERM, escalate to SIGKILL after the shutdown timeout
#   restart  stop + start (does NOT sync deps or run migrations)
#   status   show whether :PORT is bound and by which PID(s)
#   logs     ``tail -f`` the uvicorn log
#   deploy   the full pipeline: stop + ``uv sync`` + ``alembic upgrade
#            head`` + start. Use this after pulling new code.
#
# Truth source for "is it running" is ``lsof -t -i:$PORT``, not a PID
# file — survives the bootstrap transition where the user previously
# started uvicorn manually with ``nohup``, and removes a class of
# "PID file stale vs port reality" inconsistency states. uvicorn's
# master process forwards SIGTERM to its workers, so a single
# ``kill -TERM`` on the master is enough; we issue it to every PID
# bound to the port for idempotency.
#
# Logs:
#   logs/uvicorn.log     uvicorn stdout/stderr, appended on every start
#                        so a startup banner per cycle doubles as a
#                        deploy timeline.
#   logs/<loguru>        application-level logs (set up in
#                        ``app/logging_config.py``); independent of
#                        this script.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(dirname "$HERE")"
cd "$APP_DIR"

PORT=8005
WORKERS=4
LOG_DIR="$APP_DIR/logs"
UVICORN_LOG="$LOG_DIR/uvicorn.log"

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

bound_pids() {
  lsof -t -i:"$PORT" 2>/dev/null || true
}

is_running() {
  [[ -n "$(bound_pids)" ]]
}

cmd_start() {
  if is_running; then
    log "already running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
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
      log "OK — bound :$PORT after ${i}s (PID $(bound_pids | tr '\n' ' '))"
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
    log "not running on :$PORT"
    return 0
  fi
  # shellcheck disable=SC2086  # word-splitting is intended for ``kill``.
  log "stopping PID(s): $(echo $pids | tr '\n' ' ')"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true

  for ((i=1; i<=SHUTDOWN_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if ! is_running; then
      log "stopped gracefully (${i}s)"
      return 0
    fi
  done

  local stragglers
  stragglers="$(bound_pids)"
  log "graceful timed out after ${SHUTDOWN_TIMEOUT_SECONDS}s; SIGKILL"
  if [[ -n "$stragglers" ]]; then
    echo "$stragglers" | xargs kill -9 2>/dev/null || true
  fi
}

cmd_restart() {
  cmd_stop
  cmd_start
}

cmd_status() {
  if is_running; then
    log "running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
  else
    log "not running on :$PORT"
  fi
}

cmd_logs() {
  exec tail -f "$UVICORN_LOG"
}

cmd_deploy() {
  cmd_stop
  # ``uv sync`` and ``alembic upgrade head`` run BEFORE the new
  # uvicorn starts: a broken pyproject.toml or pending-but-unrunnable
  # migration fails fast. Better a minute of "no service" than a
  # started-but-broken service.
  log "uv sync"
  uv sync --quiet
  log "alembic upgrade head"
  uv run alembic upgrade head
  cmd_start
}

case "${1:-}" in
  start)   cmd_start ;;
  stop)    cmd_stop ;;
  restart) cmd_restart ;;
  status)  cmd_status ;;
  logs)    cmd_logs ;;
  deploy)  cmd_deploy ;;
  *)
    cat <<EOF
usage: $0 {start|stop|restart|status|logs|deploy}

  start    start uvicorn (no-op if already running)
  stop     SIGTERM, escalate to SIGKILL after ${SHUTDOWN_TIMEOUT_SECONDS}s
  restart  stop + start (no deps/migration sync)
  status   show whether :$PORT is bound and by which PID(s)
  logs     tail -f $UVICORN_LOG
  deploy   stop + uv sync + alembic upgrade head + start
EOF
    exit 1
    ;;
esac
