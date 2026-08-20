"""A hard spending ledger for rate-limited external APIs.

DIM.RIA's free key allows 1000 requests a month and 30 an hour, and
going over blocks the key. The collector wakes every ten minutes, so
without a ledger the two search calls alone would spend the monthly
allowance in three days and trip the hourly limit within the first hour.

The counter lives in the database and travels through Mongo, because a
local run and the VPS share one key: each has to see what the other
spent. Every request is recorded before it is made — a crashed request
still counted against the quota on their side.
"""
import uuid
from datetime import datetime, timedelta, timezone

# uid потрібен для синхронізації: локальний запуск і VPS ділять один
# ключ, і в Mongo їхні витрати мають скластися, а не перезаписати одна
# одну. Без унікального ідентифікатора рядка дві машини вважали б, що
# кожна має повний ліміт, і разом витратили б удвічі більше.
SCHEMA = """
CREATE TABLE IF NOT EXISTS api_spend (
    uid     TEXT PRIMARY KEY,
    service TEXT,
    ts      TEXT
);
CREATE INDEX IF NOT EXISTS api_spend_lookup ON api_spend(service, ts);
"""


def _now():
    return datetime.now(timezone.utc)


def _month_start(now=None):
    now = now or _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def spent(conn, service, since):
    row = conn.execute(
        "SELECT count(*) n FROM api_spend WHERE service = ? AND ts >= ?",
        (service, since.isoformat(timespec="seconds")),
    ).fetchone()
    return row["n"] if row else 0


def remaining(conn, service, monthly, hourly, now=None):
    """Скільки запитів ще можна витратити: за місяць і за годину.

    Годинне вікно ковзне, а не прив'язане до початку години — саме так
    його рахує RIA: перевищення о 10:15 знімається об 11:15.
    """
    now = now or _now()
    by_month = monthly - spent(conn, service, _month_start(now))
    by_hour = hourly - spent(conn, service, now - timedelta(hours=1))
    return max(0, by_month), max(0, by_hour)


def allowance(conn, service, monthly, hourly, now=None):
    """Скільки запитів дозволено просто зараз — менше з двох лімітів."""
    return min(*remaining(conn, service, monthly, hourly, now=now))


def charge(conn, service, count=1):
    """Записує витрату. Робити це треба ДО запиту, а не після."""
    ts = _now().isoformat(timespec="seconds")
    conn.executemany(
        "INSERT OR IGNORE INTO api_spend (uid, service, ts) VALUES (?, ?, ?)",
        [(uuid.uuid4().hex, service, ts) for _ in range(count)],
    )
    conn.commit()


def last_spend(conn, service):
    row = conn.execute(
        "SELECT max(ts) ts FROM api_spend WHERE service = ?", (service,)
    ).fetchone()
    if not row or not row["ts"]:
        return None
    return datetime.fromisoformat(row["ts"])


def due(conn, service, every_hours, now=None):
    """Чи минув інтервал від останнього звернення до сервісу."""
    if not every_hours:
        return True
    last = last_spend(conn, service)
    if last is None:
        return True
    return (now or _now()) - last >= timedelta(hours=every_hours)


def prune(conn, service, keep_days=70):
    """Записи, старші за два місяці, на ліміти вже не впливають."""
    cutoff = (_now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM api_spend WHERE service = ? AND ts < ?", (service, cutoff))
    conn.commit()
