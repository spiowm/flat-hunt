"""Відповіді на натискання кнопок у Telegram.

Кнопки надсилають звичайний текст, а не callback_query: так простіше
й надійніше, бо не треба відповідати на запит у вікно в кілька секунд.
Опитування вбудоване в цикл збирача — окремого процесу не потрібно.
"""
from . import notify, store

OFFSET_KEY = "telegram_offset"

BUTTONS = {
    "🔎 однокімнатні": ("r1", "Однокімнатні — найкраще зараз"),
    "однокімнатні": ("r1", "Однокімнатні — найкраще зараз"),
    "/odna": ("r1", "Однокімнатні — найкраще зараз"),
    "🔎 двокімнатні": ("r2", "Двокімнатні — найкраще зараз"),
    "двокімнатні": ("r2", "Двокімнатні — найкраще зараз"),
    "/dvi": ("r2", "Двокімнатні — найкраще зараз"),
}


def _offset(conn):
    row = conn.execute("SELECT lat FROM geocache WHERE q = ?", (OFFSET_KEY,)).fetchone()
    return int(row["lat"]) if row and row["lat"] else 0


def _save_offset(conn, value):
    store.geocache_put(conn, OFFSET_KEY, float(value), None, "telegram update offset")
    conn.commit()


def top_for(conn, cfg, search_key, recipient, limit=8):
    """Найкраще під конкретну конфігурацію, з урахуванням фільтрів людини."""
    params = [search_key]
    sql = f"""SELECT * FROM ads
             WHERE search_key = ? AND rejected IS NULL AND dup_of IS NULL
               AND {store.gone_clause('ads')}
               AND ifnull(risk_level,'') != 'високий'"""
    cap = (recipient or {}).get("max_walk_min")
    if cap:
        sql += " AND (walk_min IS NULL OR walk_min <= ?)"
        params.append(cap)
    sql += " ORDER BY score DESC, walk_min IS NULL, walk_min LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def listen_forever(cfg, recipients, stop=None):
    """Окремий потік для кнопок.

    Опитування всередині циклу збору змушувало б чекати відповіді до
    десяти хвилин — для кнопки це неприйнятно. Тут довге опитування
    Telegram: запит висить до двадцяти пʼяти секунд і повертається
    одразу, щойно щось надходить, тож відповідь приходить за секунди
    й майже не витрачає трафіку.
    """
    import time

    from . import store as _store

    conn = _store.connect()
    while stop is None or not stop.is_set():
        try:
            poll(cfg, conn, recipients, timeout=25)
        except Exception:
            time.sleep(5)


def poll(cfg, conn, recipients, timeout=0):
    """Читає нові повідомлення й відповідає на відомі кнопки."""
    token = cfg["notify"].get("telegram_token")
    if not token:
        return 0

    import requests

    known = {str(r["chat_id"]): r for r in recipients}
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params={"offset": _offset(conn) + 1, "timeout": timeout},
                         timeout=timeout + 10)
        updates = r.json().get("result", []) if r.ok else []
    except Exception:
        return 0

    answered = 0
    last = 0
    for upd in updates:
        last = max(last, upd.get("update_id", 0))
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        text = (msg.get("text") or "").strip().lower()
        if chat_id not in known:
            continue          # чужим не відповідаємо
        target = BUTTONS.get(text)
        if not target:
            continue
        key, title = target
        rows = top_for(conn, cfg, key, known[chat_id])
        # Порожня база після запуску й порожній результат — різні речі,
        # і плутати їх у відповіді не можна.
        total = conn.execute("SELECT count(*) FROM ads WHERE search_key = ?", (key,)).fetchone()[0]
        notify.send(cfg, notify.digest(rows, title, warming=(total == 0)), chat_id, keyboard=True)
        answered += 1

    if last:
        _save_offset(conn, last)
    return answered
