#!/usr/bin/env bash
# Local collector control. Needed because pgrep -f "flat-hunt watch"
# also matches this script's own shell, so it would kill itself.
# Hence the pid file.
set -euo pipefail

cd "$(dirname "$0")"
PIDFILE=logs/watch.pid
LOGFILE=logs/watch.log
mkdir -p logs

alive() { [[ -f $PIDFILE ]] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; }

case "${1:-status}" in
  start)
    if alive; then echo "already running, PID $(cat $PIDFILE)"; exit 0; fi
    shift || true
    setsid nohup env PYTHONUNBUFFERED=1 uv run flat-hunt watch "$@" \
      >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > $PIDFILE
    sleep 3
    if alive; then echo "started, PID $(cat $PIDFILE), log $LOGFILE"
    else echo "failed to start, see $LOGFILE"; tail -5 "$LOGFILE"; exit 1; fi
    ;;
  stop)
    if ! alive; then echo "not running"; rm -f $PIDFILE; exit 0; fi
    PID=$(cat $PIDFILE)
    pkill -TERM -P "$PID" 2>/dev/null || true
    kill -TERM "$PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
    rm -f $PIDFILE
    echo "stopped"
    ;;
  restart)
    "$0" stop; shift || true; "$0" start "$@"
    ;;
  status)
    if alive; then
      echo "running, PID $(cat $PIDFILE)"
      tail -6 "$LOGFILE"
    else
      echo "not running"
    fi
    ;;
  *)
    echo "usage: $0 {start|stop|restart|status} [watch args]"
    exit 1
    ;;
esac
