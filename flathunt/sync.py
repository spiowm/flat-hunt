"""Спільний стан у MongoDB.

SQLite лишається робочим сховищем — швидким, локальним, без мережі.
Mongo тримає спільну копію, щоб збирач на VPS і будь-який локальний
запуск бачили одне й те саме: головне, щоб позначка «вже надіслано»
не загубилась і ти не отримав ту саму квартиру двічі.

Якщо Mongo недоступна, синхронізація мовчки пропускається — збирач
не має падати через це.
"""
import os
from pathlib import Path

from . import store

DB_NAME = "flat_hunt"


def load_env_uri():
    """Дістає URI з оточення, за потреби прочитавши .env самостійно.

    Без цього будь-який виклик у обхід cli.load_config — а саме так
    робитимуть локальні аналізатори — мовчки лишався без підключення.
    """
    uri = os.environ.get("MONGODB_URI")
    if uri:
        return uri
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        key, _, value = line.strip().partition("=")
        if key.strip() == "MONGODB_URI":
            return value.strip().strip('"').strip("'") or None
    return None


def configured():
    """Чи взагалі просили синхронізацію."""
    return bool(load_env_uri())


def client(uri=None):
    uri = uri or load_env_uri()
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

    # Саме ця таблиця, а не колонка ads.notified, вирішує, чи піде
    # картка ще раз. Розсилка персональна, тож ключ — пара
    # оголошення й chat_id.
    sent = [dict(r) for r in conn.execute("SELECT * FROM notifications")]
    if sent:
        db.notifications.bulk_write(
            [UpdateOne({"_id": f"{n['ad_id']}|{n['chat_id']}"},
                       {"$set": {**n, "_id": f"{n['ad_id']}|{n['chat_id']}"}}, upsert=True)
             for n in sent],
            ordered=False,
        )
    return len(ads), len(geo)


def pull(conn, cli):
    """Забирає з Mongo те, чого немає локально.

    Позначки розсилки об'єднуються: якщо картка вже пішла комусь
    із будь-якої машини, вдруге вона не піде.
    """
    db = cli[DB_NAME]
    merged_ads = merged_geo = 0

    # Головне, що треба забрати: кому вже пішла яка картка. Без цього
    # другий запуск на іншій машині надішле все вдруге.
    have_sent = {
        (r["ad_id"], r["chat_id"])
        for r in conn.execute("SELECT ad_id, chat_id FROM notifications")
    }
    for doc in db.notifications.find({}, {"ad_id": 1, "chat_id": 1, "ts": 1}):
        pair = (doc.get("ad_id"), doc.get("chat_id"))
        if pair[0] and pair[1] and pair not in have_sent:
            store.mark_sent(conn, pair[0], pair[1])
            merged_ads += 1

    # Оголошення теж треба забрати, інакше свіжий контейнер після
    # деплою починає з порожньої бази: наново скрейпить, наново ганяє
    # модель по сотні описів і кілька хвилин відповідає «нічого немає».
    # Наявні локально не чіпаємо — їх оновить сам збирач.
    known = {r["id"] for r in conn.execute("SELECT id FROM ads")}
    cols = store.columns(conn)
    restored = 0
    for doc in db.ads.find():
        ad_id = doc.get("id") or doc.get("_id")
        if not ad_id or ad_id in known:
            continue
        payload = {k: v for k, v in doc.items()
                   if k in cols and k not in ("first_seen", "last_seen")}
        payload["id"] = ad_id
        if not payload.get("source"):
            payload["source"] = "olx"
        if not payload.get("ext_id"):
            payload["ext_id"] = str(ad_id).split(":")[-1]
        try:
            store.upsert(conn, payload)
            restored += 1
        except Exception:
            continue
    if restored:
        conn.commit()
    merged_ads += restored

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
