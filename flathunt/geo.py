"""Розпізнавання вулиці з тексту оголошення та відстань до точки відліку.

OLX віддає координати центроїда району (17 унікальних точок на 80 оголошень,
у всіх show_detailed=false), тому його map використовувати не можна.
Єдиний надійний сигнал — назва вулиці в тексті.
"""
import math
import re
import time

import requests

from . import store

# «вул. Городоцька», «по вулиці Зерова», «просп. Свободи», «пл. Ринок»
STREET_RE = re.compile(
    r"(?:вул(?:иц[іяею]|\.)?|просп(?:ект|\.)?|пр\.|площ[аі]|пл\.)\s*\.?\s*"
    r"([А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ'’\-]+(?:\s+[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ'’.\-]+)?)"
)
# номер будинку одразу після назви — тоді точка набагато точніша
HOUSE_RE = re.compile(r"\s*,?\s*(\d{1,3}[А-ЯA-Zа-яa-z]?)\b")

# Межі Львова — відсікають промахи геокодера (Стрийська зі Львова
# інакше знаходиться під Дрогобичем)
LVIV_BBOX = (49.75, 49.92, 23.90, 24.16)

_last_request = [0.0]


def haversine(lat1, lon1, lat2, lon2):
    r = 6371.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def in_lviv(lat, lon):
    a, b, c, d = LVIV_BBOX
    return a < lat < b and c < lon < d


def extract_street(title, description):
    """Повертає (назва, номер_будинку|None, впевненість)."""
    text = f"{title} . {description or ''}"
    m = STREET_RE.search(text)
    if m:
        name = m.group(1).strip(" .,")
        tail = text[m.end():m.end() + 12]
        h = HOUSE_RE.match(tail)
        return name, (h.group(1) if h else None), "вул"
    return None, None, None


def geocode(conn, name, house, cfg):
    """Геокодує вулицю через Nominatim із кешем у БД."""
    query = f"вулиця {name}"
    if house:
        query += f", {house}"
    query += ", Львів, Україна"

    cached = store.geocache_get(conn, query)
    if cached is not None:
        lat, lon, _ = cached
        return (lat, lon) if lat is not None else None

    wait = cfg["geo"]["min_interval"] - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.time()

    try:
        r = requests.get(
            cfg["geo"]["nominatim_url"],
            params={"format": "json", "limit": 1, "q": query},
            headers={"User-Agent": f"flat-hunt/1.0 ({cfg['geo']['contact_email']})"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    if not data:
        store.geocache_put(conn, query, None, None, None)
        conn.commit()
        return None

    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    if not in_lviv(lat, lon):
        store.geocache_put(conn, query, None, None, "поза Львовом: " + data[0].get("display_name", ""))
        conn.commit()
        return None

    store.geocache_put(conn, query, lat, lon, data[0].get("display_name"))
    conn.commit()
    return (lat, lon)


DISTRICTS = ("Галицький", "Личаківський", "Залізничний", "Франківський",
             "Шевченківський", "Сихівський")


def district_conflict(conn, query, olx_district):
    """Nominatim іноді чіпляє не той будинок на довгій вулиці.

    Дешевий детектор: якщо геокодер каже один район Львова, а OLX — інший,
    точці довіряти не можна.
    """
    if not olx_district:
        return False
    cached = store.geocache_get(conn, query)
    if not cached:
        return False
    display = cached[2] or ""
    found = [d for d in DISTRICTS if d in display]
    return bool(found) and olx_district not in found


# Публічний демо-сервер OSRM тримає лише автомобільний профіль: запит
# на /foot/ мовчки обслуговується машиною, і тривалість виходить утричі
# заниженою. Відстань по дорогах при цьому адекватна, тож беремо її
# і рахуємо час самі за нормальною швидкістю ходьби.
OSRM = "https://router.project-osrm.org/route/v1/driving/{lon1},{lat1};{lon2},{lat2}"
WALK_KMH = 4.5


def walk_route(conn, lat, lon, cfg):
    """Скільки насправді йти пішки. Пряма лінія бреше: біля вокзалу
    колія перетворює 1 км прямою на добрих 25 хвилин ходу.

    Результат кешується в тій самій таблиці, що й геокодування,
    під ключем із координатами.
    """
    anchor = cfg["anchor"]
    key = f"walk:{lat:.5f},{lon:.5f}"
    cached = store.geocache_get(conn, key)
    if cached is not None:
        minutes, km, _ = cached
        return (int(minutes), km) if minutes is not None else None

    url = OSRM.format(lon1=lon, lat1=lat, lon2=anchor["lon"], lat2=anchor["lat"])
    try:
        r = requests.get(url, params={"overview": "false"},
                         headers={"User-Agent": f"flat-hunt/1.0 ({cfg['geo']['contact_email']})"},
                         timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    routes = data.get("routes") or []
    if not routes:
        store.geocache_put(conn, key, None, None, "маршруту немає")
        conn.commit()
        return None

    km = round(routes[0]["distance"] / 1000, 2)
    minutes = round(km / WALK_KMH * 60)
    store.geocache_put(conn, key, minutes, km, "osrm driving distance @ 4.5 км/год")
    conn.commit()
    return minutes, km


def locate(conn, ad, cfg):
    """Доповнює оголошення street/lat/lon/dist_km. Змінює ad на місці."""
    name, house, conf = extract_street(ad.get("title"), ad.get("description"))
    ad["street"] = f"{name}, {house}" if (name and house) else name
    ad["street_conf"] = ("вул+буд" if house else conf) if name else None

    if not name:
        return ad

    point = geocode(conn, name, house, cfg)

    # Якщо геокодер сів у інший район, ніж каже OLX — це промах по номеру
    # будинку на довгій вулиці. Відкочуємось на вулицю без номера.
    if point is not None and house:
        q = f"вулиця {name}, {house}, Львів, Україна"
        if district_conflict(conn, q, ad.get("district")):
            ad["street_conf"] = "сумнівно"
            point = geocode(conn, name, None, cfg)

    if point is None and house:
        point = geocode(conn, name, None, cfg)
        if point and ad["street_conf"] != "сумнівно":
            ad["street_conf"] = "вул"
    if point is None:
        return ad

    ad["lat"], ad["lon"] = point
    ad["dist_km"] = round(
        haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], point[0], point[1]), 2
    )

    walk = walk_route(conn, point[0], point[1], cfg)
    if walk:
        ad["walk_min"], ad["walk_km"] = walk
    return ad
