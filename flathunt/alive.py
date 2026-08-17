"""Checking that a listing still exists at its source.

LUN's index lags behind. It keeps showing OLX ads that OLX has already
removed, so three dead links reached the user: all three answered 410
Gone while LUN still listed them as available. The miss counter cannot
help here — from LUN's point of view the ad is present.

So the link is verified against its own source, and a card is never sent
without that check. One request per delivered card is nothing compared
to sending someone after a flat that no longer exists.
"""
from curl_cffi import requests as http

# Тільки коди відповіді. Шукати в тексті фразу «оголошення більше не
# доступне» не можна: OLX вантажить у кожну сторінку повний словник
# перекладів інтерфейсу, тож ця фраза є і на живих сторінках. Перша
# версія перевірки через це оголосила мертвою найкращу двокімнатну.
DEAD_CODES = {404, 410}


def check(url, timeout=20):
    """True — живе, False — знято, None — не вдалося перевірити.

    Невідомість трактуємо як «живе»: краще показати зайве, ніж мовчки
    сховати нормальне оголошення через одну мережеву помилку.
    """
    if not url:
        return None
    try:
        r = http.get(url, impersonate="chrome", timeout=timeout, allow_redirects=False)
    except Exception:
        return None

    if r.status_code in DEAD_CODES:
        return False
    if r.status_code >= 400:
        return None
    return True


def prune(conn, rows, mark_gone):
    """Відсіює мертві оголошення зі списку, позначаючи їх у базі."""
    alive_rows, dead = [], 0
    for row in rows:
        if check(row["url"]) is False:
            mark_gone(conn, row["id"])
            dead += 1
        else:
            alive_rows.append(row)
    if dead:
        conn.commit()
    return alive_rows, dead
