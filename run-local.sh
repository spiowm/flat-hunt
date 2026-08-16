#!/usr/bin/env bash
# Керування локальним збирачем. Потрібен, бо pgrep -f "flat-hunt watch"
# збігається з власною оболонкою, і скрипт кладе сам себе.
# Тому працюємо через pid-файл.
set -euo pipefail

cd "$(dirname "$0")"
PIDFILE=logs/watch.pid
LOGFILE=logs/watch.log
mkdir -p logs

alive() { [[ -f $PIDFILE ]] && kill -0 "$(cat $PIDFILE)" 2>/dev/null; }

case "${1:-status}" in
  start)
    if alive; then echo "вже працює, PID $(cat $PIDFILE)"; exit 0; fi
    shift || true
    setsid nohup env PYTHONUNBUFFERED=1 uv run flat-hunt watch "$@" \
      >> "$LOGFILE" 2>&1 < /dev/null &
    echo $! > $PIDFILE
    sleep 3
    if alive; then echo "запущено, PID $(cat $PIDFILE), лог $LOGFILE"
    else echo "не запустився, дивись $LOGFILE"; tail -5 "$LOGFILE"; exit 1; fi
    ;;
  stop)
    if ! alive; then echo "не працює"; rm -f $PIDFILE; exit 0; fi
    PID=$(cat $PIDFILE)
    pkill -TERM -P "$PID" 2>/dev/null || true
    kill -TERM "$PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$PID" 2>/dev/null; then kill -9 "$PID" 2>/dev/null || true; fi
    rm -f $PIDFILE
    echo "зупинено"
    ;;
  restart)
    "$0" stop; shift || true; "$0" start "$@"
    ;;
  status)
    if alive; then
      echo "працює, PID $(cat $PIDFILE)"
      tail -6 "$LOGFILE"
    else
      echo "не працює"
    fi
    ;;
  *)
    echo "використання: $0 {start|stop|restart|status} [аргументи watch]"
    exit 1
    ;;
esac
