"""Перцептивний хеш першого фото.

Ключ «ціна + площа + вулиця» не ловить усього: агенція може подати ту
саму квартиру з іншою ціною, округленою площею й без адреси. Фото при
цьому лишаються ті самі.

Другий зиск — ознака шахрайства: ті самі знімки в оголошеннях за
різними адресами означають, що фото десь узяті, а квартири може й не бути.

Хеш рахується різницевий (dHash), 64 біти: зменшуємо до 9x8 у сірому
й порівнюємо сусідні пікселі по рядку. Стійкий до перестиснення
й зміни розміру, чого нам і треба.
"""
import io

import requests

HASH_SIZE = 8


def dhash(image):
    """64-бітний різницевий хеш у вигляді hex-рядка."""
    small = image.convert("L").resize((HASH_SIZE + 1, HASH_SIZE))
    pixels = list(small.getdata())
    bits = 0
    index = 0
    for row in range(HASH_SIZE):
        base = row * (HASH_SIZE + 1)
        for col in range(HASH_SIZE):
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1 << index
            index += 1
    return f"{bits:016x}"


def hash_url(url, timeout=20):
    if not url:
        return None
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return dhash(Image.open(io.BytesIO(r.content)))
    except Exception:
        return None


def distance(a, b):
    """Скільки біт відрізняється. До 6 — практично те саме фото."""
    if not a or not b:
        return 64
    return bin(int(a, 16) ^ int(b, 16)).count("1")


SAME = 6


def fill_missing(conn, limit=60):
    """Рахує хеші там, де їх ще немає."""
    rows = conn.execute(
        """SELECT id, photo_url FROM ads
           WHERE photo_hash IS NULL AND photo_url IS NOT NULL
             AND rejected IS NULL LIMIT ?""",
        (limit,),
    ).fetchall()
    done = 0
    for row in rows:
        h = hash_url(row["photo_url"])
        if h:
            conn.execute("UPDATE ads SET photo_hash = ? WHERE id = ?", (h, row["id"]))
            done += 1
    conn.commit()
    return done


def group_by_photo(conn):
    """Знаходить оголошення з однаковими фото.

    Повертає (кількість позначених дублів, кількість підозр на крадені фото).
    """
    rows = conn.execute(
        """SELECT id, photo_hash, street, price, first_seen, is_business, risk_flags
           FROM ads WHERE photo_hash IS NOT NULL"""
    ).fetchall()

    clusters = []
    for row in rows:
        for cluster in clusters:
            if distance(row["photo_hash"], cluster[0]["photo_hash"]) <= SAME:
                cluster.append(row)
                break
        else:
            clusters.append([row])

    dups = suspicious = 0
    from . import store

    def really_different(cluster):
        """Чи це справді різні об'єкти, а не різне написання однієї адреси.

        «Крип'якевича» з іншим символом апострофа нормалізація склеює.
        А от «Шалом Алейхема» і «Шолома-Алейхема» — та сама вулиця, але
        нормалізація тут безсила. Рятує ціна: коли фото збігаються й ціна
        та сама до гривні, це одне оголошення, подане двічі. Крадій фото
        навпаки ставить свою ціну, зазвичай нижчу за ринок.
        """
        prices = {c["price"] for c in cluster if c["price"]}
        if len(prices) == 1:
            return False

        norm = {n for n in (store._norm_street(c["street"]) for c in cluster) if n}
        if len(norm) < 2:
            return False
        for a in norm:
            for b in norm:
                if a == b:
                    continue
                if a in b or b in a or a[:5] == b[:5]:
                    return False
        return True

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        cluster.sort(key=lambda r: (r["is_business"] or 0, r["first_seen"] or ""))
        canonical = cluster[0]

        if really_different(cluster):
            # Ті самі фото на різних адресах — фото звідкись узяті
            for row in cluster:
                flags = row["risk_flags"]
                mark = "ті самі фото на іншій адресі"
                if mark not in (flags or ""):
                    conn.execute(
                        "UPDATE ads SET risk_flags = ?, risk_level = 'високий', score = max(0, score - 20) WHERE id = ?",
                        ("; ".join(filter(None, [flags, mark])), row["id"]),
                    )
                    suspicious += 1
            continue

        for row in cluster[1:]:
            conn.execute("UPDATE ads SET dup_of = ? WHERE id = ?", (canonical["id"], row["id"]))
            dups += 1

    conn.commit()
    _ = store
    return dups, suspicious
