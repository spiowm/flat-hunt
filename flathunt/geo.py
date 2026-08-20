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
    r"(?P<kind>вул(?:иц[іяею]|\.)?|просп(?:ект|\.)?|пр\.|площ[аі]|пл\.)\s*\.?\s*"
    r"(?P<name>[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ'’\-]+(?:\s+[А-ЯІЇЄҐ][А-ЯІЇЄҐа-яіїєґ'’.\-]+)?)"
)

# «пр. Шевченка» — це проспект у центрі, а «вул. Шевченка» тягнеться
# до Рясного за годину ходьби. Тип конче треба зберігати: без нього
# центральна квартира відсіювалась як така, що за сто шість хвилин.
KIND = {"просп": "проспект", "пр.": "проспект", "площ": "площа", "пл.": "площа"}


def _kind_of(raw):
    raw = raw.lower().rstrip(".")
    for prefix, name in KIND.items():
        if raw.startswith(prefix.rstrip(".")):
            return name
    return "вулиця"
# номер будинку одразу після назви — тоді точка набагато точніша
HOUSE_RE = re.compile(r"\s*,?\s*(\d{1,3}[А-ЯA-Zа-яa-z]?)\b")

# Межі Львова — відсікають промахи геокодера (Стрийська зі Львова
# інакше знаходиться під Дрогобичем)
LVIV_BBOX = (49.75, 49.92, 23.90, 24.16)

# Але сама рамка завелика: це прямокутник дев'ятнадцять на дев'ятнадцять
# кілометрів, і приміські села в нього влазять. «Вулиця Святого Теодора»
# знайшлась у Малечковичах за 10.7 км, і квартира з центру дістала
# 160 хвилин ходьби та відсів. Тому додатково міряємо від якоря.
#
# Вісім кілометрів: наш найдальший тір — 90 хвилин пішки, а це 6.75 км
# ДОРОГАМИ, тобто по прямій ще менше. Все, що далі за вісім, кандидатом
# бути не може за жодних умов, тож нічого потрібного межа не ріже. Зате
# хибна точка за десять кілометрів гірша за відсутність адреси: без неї
# спрацював би запасний варіант із центром району.
MAX_KM_FROM_ANCHOR = 8

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


# Титули у назвах вулиць пишуть скорочено, і регекс обривався на крапці:
# «вул.Кн.Ольги» давало назву «Кн». Одна така картка вже пішла в розсилку.
ABBREV = {
    "кн": "Княгині", "св": "Святого", "ген": "Генерала", "акад": "Академіка",
    "митр": "Митрополита", "проф": "Професора", "марш": "Маршала",
    "кор": "Короля", "єп": "Єпископа",
}
ABBREV_RE = re.compile(r"\b(" + "|".join(ABBREV) + r")\.\s*", re.IGNORECASE)

# Ініціал у кінці назви — це вже сусідня вулиця з переліку:
# «Кн.Ольги-Л. Кобилиці» це ріг двох вулиць, а не одна назва.
TRAILING_INITIAL = re.compile(r"[\s\-]+[А-ЯІЇЄҐ]\.?$")


def _expand(text):
    return ABBREV_RE.sub(lambda m: ABBREV[m.group(1).lower()] + " ", text)


def extract_street(title, description):
    """Повертає (назва, номер_будинку|None, впевненість, тип)."""
    text = _expand(f"{title} . {description or ''}")
    m = STREET_RE.search(text)
    if m:
        name = TRAILING_INITIAL.sub("", m.group("name").strip(" .,")).strip(" .,-")
        tail = text[m.end():m.end() + 12]
        h = HOUSE_RE.match(tail)
        return name or None, (h.group(1) if h else None), "вул", _kind_of(m.group("kind"))
    return None, None, None, "вулиця"


# В оголошеннях вулицю пишуть у місцевому відмінку: «на Садовій»,
# «по Замарстинівській». Геокодер таких форм не знає й повертає порожньо,
# тож квартира лишається без відстані. Відмінюємо назад у називний.
DECLENSION = (
    ("ській", "ська"), ("цькій", "цька"), ("зькій", "зька"),
    ("чній", "чна"), ("шній", "шня"), ("жній", "жня"),
    ("ній", "на"), ("вій", "ва"), ("бій", "ба"), ("дій", "да"),
    ("тій", "та"), ("лій", "ла"), ("рій", "ра"), ("мій", "ма"),
    ("ій", "а"), ("ої", "а"), ("ою", "а"),
)


def undecline(name):
    """«Садовій» -> «Садова». Повертає None, якщо форма й так називна."""
    if not name:
        return None
    head, _, last = name.rpartition(" ")
    for suffix, replacement in DECLENSION:
        if last.lower().endswith(suffix) and len(last) > len(suffix) + 1:
            fixed = last[: -len(suffix)] + replacement
            return f"{head} {fixed}".strip()
    return None


def geocode(conn, name, house, cfg, kind="вулиця"):
    """Геокодує вулицю через Nominatim із кешем у БД."""
    query = f"{kind} {name}"
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
    limit = cfg.get("max_km_from_anchor", MAX_KM_FROM_ANCHOR)
    too_far = haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], lat, lon) > limit
    if not in_lviv(lat, lon) or too_far:
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


def _district_fallback(conn, ad, cfg):
    """Остання спроба: прив'язати хоча б до центру району."""
    point = district_point(conn, ad.get("district"), cfg)
    if point is None:
        return ad
    ad["lat"], ad["lon"] = point
    ad["dist_km"] = round(haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], *point), 2)
    ad["street_conf"] = "район"
    walk = walk_route(conn, point[0], point[1], cfg)
    if walk:
        ad["walk_min"], ad["walk_km"] = walk
    return ad


def _split_stored(street):
    """Розбирає збережену адресу «Менцинського, 3» назад на назву й номер."""
    if not street:
        return None, None
    parts = [p.strip() for p in street.split(",")]
    name = parts[0]
    house = parts[1] if len(parts) > 1 and re.match(r"^\d", parts[1]) else None
    return (name or None), house


def district_point(conn, district, cfg):
    """Центр району як груба прив'язка, коли вулиці не знайшли.

    Краще показати «приблизно два кілометри, район Галицький», ніж
    порожнє місце: людина принаймні розуміє, у який бік дивитись.
    """
    if not district:
        return None
    query = f"{district} район, Львів, Україна"
    cached = store.geocache_get(conn, query)
    if cached is not None:
        return (cached[0], cached[1]) if cached[0] is not None else None

    wait = cfg["geo"]["min_interval"] - (time.time() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    _last_request[0] = time.time()
    try:
        r = requests.get(cfg["geo"]["nominatim_url"],
                         params={"format": "json", "limit": 1, "q": query},
                         headers={"User-Agent": f"flat-hunt/1.0 ({cfg['geo']['contact_email']})"},
                         timeout=20)
        data = r.json() if r.ok else []
    except Exception:
        return None
    if not data:
        store.geocache_put(conn, query, None, None, None)
        conn.commit()
        return None
    lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    store.geocache_put(conn, query, lat, lon, data[0].get("display_name"))
    conn.commit()
    return (lat, lon)


def _measure(conn, ad, cfg):
    """Відстань і хвилини ходьби від уже відомої точки."""
    lat, lon = ad["lat"], ad["lon"]
    ad["dist_km"] = round(haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], lat, lon), 2)
    walk = walk_route(conn, lat, lon, cfg)
    if walk:
        ad["walk_min"], ad["walk_km"] = walk
    return ad


def locate(conn, ad, cfg):
    """Доповнює оголошення street/lat/lon/dist_km. Змінює ad на місці."""
    # ЛУН і DIM.RIA віддають координати самого будинку. Витягувати
    # вулицю з тексту й геокодувати її поверх цього означає проміняти
    # точну адресу на здогад: на вибірці ЛУНа так з'їжджали дев'ять
    # карток із дванадцяти, найгірша на 3.6 км. Квартира отримувала
    # чужі хвилини ходьби й через них відсівалась.
    #
    # Найяскравіший приклад із DIM.RIA: в API стоїть «вул. Петра
    # Полтави», а в описі продавець написав розмовне «вул. Патона
    # ( Полтави )» — і регекс перемагав API.
    if ad.get("coords_from_source") and ad.get("lat") and ad.get("lon"):
        if not ad.get("street"):
            # Адреси джерело не дало: назву з тексту беремо лише як
            # підпис, точку не рухаємо.
            name, house, _, _ = extract_street(ad.get("title"), ad.get("description"))
            if name:
                ad["street"] = f"{name}, {house}" if house else name
        if not ad.get("street_conf"):
            ad["street_conf"] = "вул"
        return _measure(conn, ad, cfg)

    name, house, conf, kind = extract_street(ad.get("title"), ad.get("description"))

    if name:
        ad["street"] = f"{name}, {house}" if house else name
        ad["street_conf"] = ("вул+буд" if house else conf)
    else:
        # Регекс не впорався — але адресу міг знайти розбір опису моделлю,
        # і вона вже лежить у полі street. Саме заради таких випадків
        # модель і потрібна, тож ігнорувати її результат тут не можна.
        name, house = _split_stored(ad.get("street"))
        if not name:
            ad["street"] = None
            ad["street_conf"] = None
            return _district_fallback(conn, ad, cfg)
        kind = "вулиця"

    if not name:
        return ad

    point = geocode(conn, name, house, cfg, kind)

    # Не знайшлось — можливо, назва стоїть у місцевому відмінку
    if point is None:
        alt = undecline(name)
        if alt:
            point = geocode(conn, alt, house, cfg, kind)
            if point is None and house:
                point = geocode(conn, alt, None, cfg, kind)
            if point is not None:
                name = alt
                ad["street"] = f"{alt}, {house}" if house else alt

    # Перехресна перевірка району потрібна не лише для номера будинку:
    # довга вулиця з однойменним двійником на іншому кінці міста дає
    # такий самий промах, «Шевченка» без номера знаходилась у Рясному.
    #
    # Але сама по собі розбіжність району нічого не доводить: межі в
    # OpenStreetMap і в OLX не збігаються, і центральні вулиці на межі
    # дають хибне спрацювання. Перша версія цієї перевірки знеструмила
    # Рапапорту з її пʼятьма хвилинами ходьби. Тому розбіжність важить
    # лише разом із великою відстанню — тоді це справді інша вулиця.
    q = f"{kind} {name}" + (f", {house}" if house else "") + ", Львів, Україна"
    if point is not None and district_conflict(conn, q, ad.get("district")):
        far = haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], point[0], point[1]) > 3.0
        if far:
            ad["street_conf"] = "сумнівно"
            point = geocode(conn, name, None, cfg, kind) if house else None

    if point is None and house:
        point = geocode(conn, name, None, cfg, kind)
        if point and ad["street_conf"] != "сумнівно":
            ad["street_conf"] = "вул"
    if point is None:
        return _district_fallback(conn, ad, cfg)

    ad["lat"], ad["lon"] = point
    ad["dist_km"] = round(
        haversine(cfg["anchor"]["lat"], cfg["anchor"]["lon"], point[0], point[1]), 2
    )

    walk = walk_route(conn, point[0], point[1], cfg)
    if walk:
        ad["walk_min"], ad["walk_km"] = walk
    return ad
