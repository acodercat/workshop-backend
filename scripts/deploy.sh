#!/usr/bin/env bash
# Backend service control + full deploy. Manages TWO siblings:
#
#  - uvicorn (web)              — top-level commands (start/stop/...).
#  - credits-janitor (background) — ``deploy.sh janitor SUBCMD``.
#
# Subcommand layout mirrors the frontend's ``serve.sh`` so the operator's
# muscle memory carries across services.
#
# Web subcommands:
#   start    start uvicorn (no-op if already running on :PORT)
#   stop     SIGTERM, escalate to SIGKILL after the shutdown timeout
#   restart  stop + start (does NOT sync deps or run migrations)
#   status   report state of BOTH web and janitor
#   logs     ``tail -f`` the uvicorn log
#   deploy   the full pipeline: stop everything + ``uv sync`` + ``alembic
#            upgrade head`` + start both. Use this after pulling new code.
#
# Janitor subcommands (``deploy.sh janitor ...``):
#   start    start the credits-janitor process
#   stop     SIGTERM the janitor; escalate to SIGKILL after the shutdown timeout
#   restart  stop + start
#   status   report whether the janitor process is running
#   logs     ``tail -f`` the janitor log
#
# Truth source for "is it running":
#   web      ``lsof -t -i:$PORT``  — survives bootstrap transitions where
#            uvicorn was once started manually with ``nohup``.
#   janitor  ``pgrep -f 'app.workers.run'`` — the janitor has no port;
#            the module path is specific enough that no other process
#            will share it on the cmdline.
#
# Multi-node note: every host should run both web and janitor. The
# janitor coordinates across hosts via Postgres advisory lock (see
# ``app/workers/credits_janitor.py::_LOCK_KEY``); at most one host's
# janitor actually sweeps per tick, the rest hit the busy lock and skip.
#
# Logs:
#   logs/uvicorn.log     uvicorn stdout/stderr, appended on every start
#                        so a startup banner per cycle doubles as a
#                        deploy timeline.
#   logs/janitor.log     credits-janitor stdout/stderr, same pattern.
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
JANITOR_LOG="$LOG_DIR/janitor.log"

# Cmdline pattern used by ``pgrep -f`` to find the janitor process. The
# python module path is distinctive enough that no other process should
# share it; we anchor with a backslash-dot so partial substring matches
# in other ``python -m`` invocations don't trip us.
JANITOR_PATTERN='app\.workers\.run'

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

# ── Janitor detection ──────────────────────────────────────────────────
janitor_pids() {
  pgrep -f "$JANITOR_PATTERN" 2>/dev/null || true
}

janitor_is_running() {
  [[ -n "$(janitor_pids)" ]]
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

# ── Janitor commands ───────────────────────────────────────────────────
cmd_janitor_start() {
  if janitor_is_running; then
    log "janitor already running (PID $(janitor_pids | tr '\n' ' '))"
    return 0
  fi
  log "starting credits-janitor"
  nohup uv run python -m app.workers.run \
    >> "$JANITOR_LOG" 2>&1 &
  disown

  # Janitor has no port to bind; readiness = "the process still
  # exists after ${HEALTH_TIMEOUT_SECONDS}s". Crash-on-startup
  # (missing config, DB unreachable) shows as "process gone".
  for ((i=1; i<=HEALTH_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if janitor_is_running; then
      log "janitor OK — PID $(janitor_pids | tr '\n' ' ') after ${i}s"
      log "tail logs: tail -f $JANITOR_LOG"
      return 0
    fi
  done

  log "FAILED: janitor not running after ${HEALTH_TIMEOUT_SECONDS}s"
  log "last 40 lines of $JANITOR_LOG:"
  tail -40 "$JANITOR_LOG" || true
  return 1
}

cmd_janitor_stop() {
  local pids
  pids="$(janitor_pids)"
  if [[ -z "$pids" ]]; then
    log "janitor not running"
    return 0
  fi
  # shellcheck disable=SC2086
  log "stopping janitor PID(s): $(echo $pids | tr '\n' ' ')"
  echo "$pids" | xargs kill -TERM 2>/dev/null || true

  for ((i=1; i<=SHUTDOWN_TIMEOUT_SECONDS; i++)); do
    sleep 1
    if ! janitor_is_running; then
      log "janitor stopped gracefully (${i}s)"
      return 0
    fi
  done

  local stragglers
  stragglers="$(janitor_pids)"
  log "janitor graceful timed out after ${SHUTDOWN_TIMEOUT_SECONDS}s; SIGKILL"
  if [[ -n "$stragglers" ]]; then
    echo "$stragglers" | xargs kill -9 2>/dev/null || true
  fi
}

cmd_janitor_restart() {
  cmd_janitor_stop
  cmd_janitor_start
}

cmd_janitor_status() {
  if janitor_is_running; then
    log "janitor running (PID $(janitor_pids | tr '\n' ' '))"
  else
    log "janitor not running"
  fi
}

cmd_janitor_logs() {
  exec tail -f "$JANITOR_LOG"
}

# ── Combined status / deploy ──────────────────────────────────────────
cmd_status() {
  if is_running; then
    log "web      running on :$PORT (PID $(bound_pids | tr '\n' ' '))"
  else
    log "web      not running on :$PORT"
  fi
  if janitor_is_running; then
    log "janitor  running (PID $(janitor_pids | tr '\n' ' '))"
  else
    log "janitor  not running"
  fi
}

cmd_deploy() {
  # Stop everything first so any new code on uv sync / migration error
  # surfaces as a hard fail rather than a half-deployed mismatch.
  cmd_stop
  cmd_janitor_stop
  log "uv sync"
  uv sync --quiet
  log "alembic upgrade head"
  uv run alembic upgrade head
  # Start order: janitor first, then web. Either order works (no
  # dependency), but janitor-first means by the time web accepts
  # traffic the background reconciliation is already running.
  cmd_janitor_start
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
  janitor)
    case "${2:-}" in
      start)   cmd_janitor_start ;;
      stop)    cmd_janitor_stop ;;
      restart) cmd_janitor_restart ;;
      status)  cmd_janitor_status ;;
      logs)    cmd_janitor_logs ;;
      *)
        cat <<EOF
usage: $0 janitor {start|stop|restart|status|logs}
EOF
        exit 1
        ;;
    esac
    ;;
  *)
    cat <<EOF
usage: $0 {start|stop|restart|status|logs|deploy|janitor SUBCMD}

  start    start uvicorn (no-op if already running)
  stop     SIGTERM, escalate to SIGKILL after ${SHUTDOWN_TIMEOUT_SECONDS}s
  restart  stop + start (no deps/migration sync)
  status   show state of BOTH web (:$PORT) and credits-janitor
  logs     tail -f $UVICORN_LOG
  deploy   stop all + uv sync + alembic upgrade head + start all

  janitor start | stop | restart | status | logs
           same lifecycle commands targeting the credits-janitor process
           (tail -f $JANITOR_LOG on logs)
EOF
    exit 1
    ;;
esac
