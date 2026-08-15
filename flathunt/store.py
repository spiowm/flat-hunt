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
        ("source_group", "TEXT"),
    ):
        if column not in have:
            conn.execute(f"ALTER TABLE ads ADD COLUMN {column} {decl}")
    conn.commit()


def upsert(conn, ad):
    """Повертає True, якщо оголошення бачимо вперше."""
    cur = conn.execute("SELECT id FROM ads WHERE id = ?", (ad["id"],))
    is_new = cur.fetchone() is None
    cols = ", ".join(FIELDS)
    marks = ", ".join("?" for _ in FIELDS)
    values = [ad.get(f) for f in FIELDS]
    if is_new:
        conn.execute(
            f"INSERT INTO ads ({cols}, first_seen, last_seen) VALUES ({marks}, ?, ?)",
            values + [now(), now()],
        )
    else:
        # dup_of проставляється окремо і не приходить зі збирача —
        # інакше кожен наступний прохід затирав би позначки дублів
        upd = [f for f in FIELDS if f not in ("id", "dup_of")]
        sets = ", ".join(f"{f} = ?" for f in upd)
        conn.execute(
            f"UPDATE ads SET {sets}, last_seen = ? WHERE id = ?",
            [ad.get(f) for f in upd] + [now(), ad["id"]],
        )
    return is_new


def find_duplicate(conn, ad):
    """Той самий об'єкт від іншого продавця: збіг ціни, площі, поверху й вулиці."""
    if not ad.get("area") or not ad.get("street"):
        return None
    row = conn.execute(
        """SELECT id FROM ads
           WHERE id != ? AND dup_of IS NULL
             AND price = ? AND abs(area - ?) < 1.5
             AND ifnull(floor, -1) = ifnull(?, -1)
             AND street = ?
           LIMIT 1""",
        (ad["id"], ad.get("price"), ad.get("area"), ad.get("floor"), ad.get("street")),
    ).fetchone()
    return row["id"] if row else None


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


def pending_notifications(conn, min_score):
    return conn.execute(
        """SELECT * FROM ads
           WHERE notified = 0 AND rejected IS NULL AND dup_of IS NULL AND score >= ?
           ORDER BY score DESC""",
        (min_score,),
    ).fetchall()


def mark_notified(conn, ad_id):
    conn.execute("UPDATE ads SET notified = 1 WHERE id = ?", (ad_id,))


def log_run(conn, source, fetched, new_ads, notified, note=""):
    conn.execute(
        "INSERT INTO runs (ts, source, fetched, new_ads, notified, note) VALUES (?, ?, ?, ?, ?, ?)",
        (now(), source, fetched, new_ads, notified, note),
    )
