"""Спільний стан у MongoDB.

SQLite лишається робочим сховищем — швидким, локальним, без мережі.
Mongo тримає спільну копію, щоб збирач на VPS і будь-який локальний
запуск бачили одне й те саме: головне, щоб позначка «вже надіслано»
не загубилась і ти не отримав ту саму квартиру двічі.

Якщо Mongo недоступна, синхронізація мовчки пропускається — збирач
не має падати через це.
"""
import os

from . import store

DB_NAME = "flat_hunt"


def client(uri=None):
    uri = uri or os.environ.get("MONGODB_URI")
    if not uri:
        return None
    try:
        from pymongo import MongoClient
    except ImportError:
        return None
    try:
        cli = MongoClient(uri, serverSelectionTimeoutMS=8000, connectTimeoutMS=8000)
        cli.admin.command("ping")
        return cli
    except Exception:
        return None


def push(conn, cli):
    """Виливає локальний стан у Mongo."""
    from pymongo import UpdateOne

    db = cli[DB_NAME]
    ads = [dict(r) for r in conn.execute("SELECT * FROM ads")]
    if ads:
        db.ads.bulk_write(
            [UpdateOne({"_id": a["id"]}, {"$set": {**a, "_id": a["id"]}}, upsert=True) for a in ads],
            ordered=False,
        )
    geo = [dict(r) for r in conn.execute("SELECT * FROM geocache")]
    if geo:
        db.geocache.bulk_write(
            [UpdateOne({"_id": g["q"]}, {"$set": {**g, "_id": g["q"]}}, upsert=True) for g in geo],
            ordered=False,
        )
    return len(ads), len(geo)


def pull(conn, cli):
    """Забирає з Mongo те, чого немає локально.

    Позначка notified об'єднується за АБО: якщо десь уже надіслано,
    вдруге не шлемо.
    """
    db = cli[DB_NAME]
    merged_ads = merged_geo = 0

    local_notified = {r["id"] for r in conn.execute("SELECT id FROM ads WHERE notified = 1")}
    for doc in db.ads.find({}, {"id": 1, "notified": 1, "first_seen": 1}):
        ad_id = doc.get("id") or doc.get("_id")
        if doc.get("notified") and ad_id not in local_notified:
            cur = conn.execute("UPDATE ads SET notified = 1 WHERE id = ?", (ad_id,))
            merged_ads += cur.rowcount

    have = {r["q"] for r in conn.execute("SELECT q FROM geocache")}
    for doc in db.geocache.find():
        q = doc.get("q") or doc.get("_id")
        if q not in have:
            store.geocache_put(conn, q, doc.get("lat"), doc.get("lon"), doc.get("display"))
            merged_geo += 1

    conn.commit()
    return merged_ads, merged_geo


def full_ads(cli):
    """Усі оголошення з Mongo — для локальних аналізаторів."""
    return list(cli[DB_NAME].ads.find())
