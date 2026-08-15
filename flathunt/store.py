"""SQLite-сховище оголошень і кеш геокодування."""
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# У контейнері вказуємо FLAT_HUNT_DB=/data/flats.db, щоб база жила в томі
DB_PATH = Path(os.environ.get("FLAT_HUNT_DB")
                or Path(__file__).resolve().parent.parent / "flats.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS ads (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    ext_id        TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    description   TEXT,
    search_key    TEXT,
    people        INTEGER,
    price         INTEGER,
    rooms         INTEGER,
    area          REAL,
    floor         INTEGER,
    total_floors  INTEGER,
    layout        TEXT,
    renovation    TEXT,
    furniture     TEXT,
    house_type    TEXT,
    city          TEXT,
    district      TEXT,
    street        TEXT,
    street_conf   TEXT,
    lat           REAL,
    lon           REAL,
    dist_km       REAL,
    is_business   INTEGER,
    seller        TEXT,
    user_id       TEXT,
    user_created  TEXT,
    user_verified TEXT,
    risk_flags    TEXT,
    risk_level    TEXT,
    map_url       TEXT,
    created_time  TEXT,
    refresh_time  TEXT,
    photos        INTEGER,
    score         INTEGER,
    tier          TEXT,
    rejected      TEXT,
    dup_of        TEXT,
    first_seen    TEXT,
    last_seen     TEXT,
    notified      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ads_notified ON ads(notified, rejected);
CREATE INDEX IF NOT EXISTS idx_ads_fingerprint ON ads(price, area, floor, street);

CREATE TABLE IF NOT EXISTS geocache (
    q       TEXT PRIMARY KEY,
    lat     REAL,
    lon     REAL,
    display TEXT,
    ts      TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    ad_id   TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    ts      TEXT,
    PRIMARY KEY (ad_id, chat_id)
);

CREATE TABLE IF NOT EXISTS price_history (
    ad_id TEXT NOT NULL,
    price INTEGER,
    ts    TEXT,
    PRIMARY KEY (ad_id, price, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_ad ON price_history(ad_id, ts);

CREATE TABLE IF NOT EXISTS runs (
    ts        TEXT,
    source    TEXT,
    fetched   INTEGER,
    new_ads   INTEGER,
    notified  INTEGER,
    note      TEXT
);
"""

FIELDS = [
    "id", "source", "ext_id", "url", "title", "description", "search_key", "people",
    "price", "rooms", "area", "floor", "total_floors", "layout", "renovation",
    "furniture", "house_type", "city", "district", "street", "street_conf", "lat",
    "lon", "dist_km", "is_business", "seller", "user_id", "user_created",
    "user_verified", "risk_flags", "risk_level", "map_url", "created_time",
    "refresh_time", "photos", "score", "tier", "rejected", "dup_of",
]


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Доліває колонки, яких бракує у вже наявній базі."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(ads)")}
    for column, decl in (
        ("user_created", "TEXT"), ("user_verified", "TEXT"),
        ("risk_flags", "TEXT"), ("risk_level", "TEXT"), ("map_url", "TEXT"),
        ("source_group", "TEXT"), ("walk_min", "INTEGER"), ("walk_km", "REAL"),
        ("photo_hash", "TEXT"), ("photo_url", "TEXT"), ("commission", "TEXT"),
        ("utilities", "TEXT"), ("deposit", "TEXT"), ("min_term", "TEXT"),
        ("restrictions", "TEXT"), ("backup_power", "TEXT"), ("real_cost", "INTEGER"),
        ("llm_done", "INTEGER"), ("price_prev", "INTEGER"), ("price_drop", "INTEGER"),
        ("days_listed", "INTEGER"),
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE ads ADD COLUMN {column} {decl}")
    conn.commit()


def columns(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(ads)")}


def upsert(conn, ad):
    """Оновлює лише ті колонки, які реально прийшли в ad.

    Це важливо: збагачення від моделі, позначки дублів і час ходьби
    рахуються окремими проходами й не приходять зі збирача. Якби
    оновлювався весь набір колонок, кожен наступний прохід їх затирав би.
    """
    known = columns(conn)
    present = [k for k in ad if k in known and k != "id"]

    is_new = conn.execute("SELECT 1 FROM ads WHERE id = ?", (ad["id"],)).fetchone() is None
    if is_new:
        cols = ["id"] + present
        marks = ", ".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO ads ({', '.join(cols)}, first_seen, last_seen) VALUES ({marks}, ?, ?)",
            [ad["id"]] + [ad[k] for k in present] + [now(), now()],
        )
    else:
        sets = ", ".join(f"{k} = ?" for k in present)
        conn.execute(
            f"UPDATE ads SET {sets}, last_seen = ? WHERE id = ?",
            [ad[k] for k in present] + [now(), ad["id"]],
        )
    return is_new


def _norm_street(street):
    """«вул. Генерала Чупринки, 59» і «Чупринки 59» мають збігтися."""
    if not street:
        return None
    s = street.lower().replace("’", "'").replace("`", "'")
    for junk in ("вулиця ", "вул. ", "вул ", "проспект ", "просп. ", "пр. ", "площа ", "пл. "):
        s = s.replace(junk, "")
    for title in ("генерала ", "святого ", "св. ", "митрополита ", "академіка "):
        s = s.replace(title, "")
    return re.sub(r"[^a-zа-яіїєґ0-9]", "", s)


def _fingerprint(row):
    """Ключ об'єкта, стійкий до джерела й дрібних розбіжностей у ціні."""
    return (
        round((row["price"] or 0) / 500),      # крок 500 грн: агенції округлюють по-різному
        round(row["area"] or 0),
        row["floor"],
        _norm_street(row["street"]),
    )


def sweep_duplicates(conn):
    """Позначає повтори по всій базі, зокрема між різними джерелами.

    Канонічним лишається оголошення від власника — саме воно без комісії.
    Якщо власника серед клонів немає, беремо найстаріше.
    """
    rows = conn.execute(
        """SELECT id, price, area, floor, street, first_seen, is_business, source FROM ads
           WHERE street IS NOT NULL AND area IS NOT NULL"""
    ).fetchall()

    groups = {}
    for r in rows:
        key = _fingerprint(r)
        if key[3]:
            groups.setdefault(key, []).append(r)

    marked = 0
    for members in groups.values():
        # власник попереду, далі за давністю появи
        members.sort(key=lambda r: (r["is_business"] or 0, r["first_seen"] or ""))
        canonical = members[0]
        conn.execute("UPDATE ads SET dup_of = NULL WHERE id = ?", (canonical["id"],))
        sources = ",".join(sorted({m["source"] for m in members}))
        for other in members[1:]:
            conn.execute(
                "UPDATE ads SET dup_of = ?, source_group = ? WHERE id = ?",
                (canonical["id"], sources, other["id"]),
            )
            marked += 1
        if len(members) > 1:
            conn.execute("UPDATE ads SET source_group = ? WHERE id = ?", (sources, canonical["id"]))
    return marked


def geocache_get(conn, q):
    row = conn.execute("SELECT lat, lon, display FROM geocache WHERE q = ?", (q,)).fetchone()
    if row is None:
        return None
    return (row["lat"], row["lon"], row["display"])


def geocache_put(conn, q, lat, lon, display):
    conn.execute(
        "INSERT OR REPLACE INTO geocache (q, lat, lon, display, ts) VALUES (?, ?, ?, ?, ?)",
        (q, lat, lon, display, now()),
    )


def pending_for(conn, recipient, default_min_score):
    """Що ще не пішло саме цьому одержувачу, з урахуванням його фільтрів."""
    keys = recipient.get("searches") or []
    min_score = recipient.get("min_score", default_min_score)
    params = [recipient["chat_id"], min_score]

    sql = """SELECT a.* FROM ads a
             WHERE a.rejected IS NULL AND a.dup_of IS NULL
               AND NOT EXISTS (SELECT 1 FROM notifications n
                               WHERE n.ad_id = a.id AND n.chat_id = ?)
               AND a.score >= ?"""
    if keys:
        sql += " AND a.search_key IN (%s)" % ",".join("?" for _ in keys)
        params += list(keys)

    walk_cap = recipient.get("max_walk_min")
    if walk_cap:
        # невідомий час ходьби не відсіюємо — краще зайва картка, ніж проґавлений варіант
        sql += " AND (a.walk_min IS NULL OR a.walk_min <= ?)"
        params.append(walk_cap)

    sql += " ORDER BY a.score DESC"
    return conn.execute(sql, params).fetchall()


def mark_sent(conn, ad_id, chat_id):
    conn.execute(
        "INSERT OR IGNORE INTO notifications (ad_id, chat_id, ts) VALUES (?, ?, ?)",
        (ad_id, chat_id, now()),
    )


def seed_notifications(conn, chat_ids):
    """Позначає весь наявний бекленг як надісланий усім одержувачам."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM ads")]
    for ad_id in ids:
        for chat_id in chat_ids:
            mark_sent(conn, ad_id, chat_id)
    return len(ids) * max(1, len(chat_ids))


def record_price(conn, ad_id, price):
    """Знімок ціни. Повертає попередню ціну, якщо вона змінилась."""
    if price is None:
        return None
    prev = conn.execute(
        "SELECT price FROM price_history WHERE ad_id = ? ORDER BY ts DESC LIMIT 1", (ad_id,)
    ).fetchone()
    if prev is None:
        conn.execute("INSERT OR IGNORE INTO price_history (ad_id, price, ts) VALUES (?, ?, ?)",
                     (ad_id, price, now()))
        return None
    if prev["price"] != price:
        conn.execute("INSERT OR IGNORE INTO price_history (ad_id, price, ts) VALUES (?, ?, ?)",
                     (ad_id, price, now()))
        return prev["price"]
    return None


def refresh_market_signals(conn):
    """Проставляє падіння ціни й скільки днів висить — по всій базі.

    Довго висить = або переоцінене, або має ваду, яку не видно з фото.
    Падіння ціни навпаки: власник поступливий, є з чим іти торгуватись.
    """
    updated = 0
    for row in conn.execute("SELECT id, first_seen, created_time FROM ads").fetchall():
        drop, days = market_signals(conn, row["id"], row["created_time"] or row["first_seen"])
        conn.execute(
            "UPDATE ads SET price_drop = coalesce(?, price_drop), days_listed = ? WHERE id = ?",
            (drop, days, row["id"]),
        )
        updated += 1
    conn.commit()
    return updated


def market_signals(conn, ad_id, first_seen):
    """Сигнали з історії: падіння ціни й скільки висить."""
    from datetime import datetime, timezone
    rows = conn.execute(
        "SELECT price, ts FROM price_history WHERE ad_id = ? ORDER BY ts", (ad_id,)
    ).fetchall()
    drop = None
    if len(rows) > 1 and rows[0]["price"] and rows[-1]["price"]:
        diff = rows[0]["price"] - rows[-1]["price"]
        if diff > 0:
            drop = diff
    days = None
    if first_seen:
        try:
            seen = datetime.fromisoformat(first_seen)
            if seen.tzinfo is None:
                seen = seen.replace(tzinfo=timezone.utc)
            days = (datetime.now(timezone.utc) - seen).days
        except ValueError:
            pass
    return drop, days


def log_run(conn, source, fetched, new_ads, notified, note=""):
    conn.execute(
        "INSERT INTO runs (ts, source, fetched, new_ads, notified, note) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), source, fetched, new_ads, notified, note),
    )
