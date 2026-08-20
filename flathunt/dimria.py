"""DIM.RIA (dom.ria.com) as a source.

What it adds over the others: an exact house number and true building
coordinates (`radius_location = 0`, plus an OSM building id), where OLX
gives only a district centroid. A sample of five showed four listings
absent from every other source, one of them a kilometre from the anchor.

The API is a two-step affair and the free key is tight:

    search  ->  a bare list of ad ids, 100 per page
    info/id ->  the actual listing, one request each

**1000 requests a month, 30 an hour.** That shapes everything here. The
collector runs every ten minutes; calling this source on every pass
would spend the month in three days. So it runs on its own slow clock,
counts every request through `budget`, and — most importantly — never
fetches the same id twice: a listing already in the database is skipped
forever, because its location will not change. The one-time cost of
learning the current stock is paid once, and after that only genuinely
new ads cost anything.

Disappearance detection is free: the search step already returns the
full set of live ids, so no detail request is needed to notice that an
ad is gone.

Without a key there is a fallback: the catalogue page carries 20 full
listings in `window.__INITIAL_STATE__`. Pagination does not work there
and the order is not chronological, so it is weak for catching fresh
ads, but it costs nothing.
"""
import json
import os
import re
import time

from curl_cffi import requests as http

from . import budget, net

CATALOG_URL = "https://dom.ria.com/uk/arenda-kvartir/lvov/"
API = "https://developers.ria.com/dom"
SITE = "https://dom.ria.com/uk/"
STATE_KEY = "window.__INITIAL_STATE__="
SERVICE = "dimria"

# Довгострокова оренда квартир у Львові. Ідентифікатори перевірені на
# живому API: category=1 — саме квартири (485 із 879 оренд у Львові),
# а realty_type=1, який тут стояв раніше, давав нуль результатів.
OPERATION_LONG_RENT = 3
CATEGORY_FLAT = 1
CURRENCY_UAH = 3

# Львів. Питати їх щоразу — це два запити з тисячі на місяць за даними,
# які не змінюються. Гірше того: пошук за підрядком «Львів» знаходив
# першими «Брюховичі (Львів)» — приміське село з cityID 19191.
LVIV_STATE_ID = 5
LVIV_CITY_ID = 5

STREET_PREFIX = re.compile(r"^(вул|вулиця|просп|проспект|пров|провулок|пл|площа)\.?\s+", re.I)


def _session():
    return http.Session(impersonate="chrome", timeout=30)


# --------------------------------------------------------------------------
# Запити з обліком квоти
# --------------------------------------------------------------------------

class OutOfQuota(Exception):
    """Ліміт вичерпано — далі не ходимо, інакше ключ заблокують."""


class _Wallet:
    """Дозволяє рівно стільки запитів, скільки лишилось за обома лімітами."""

    def __init__(self, conn, cfg):
        ria = (cfg.get("dimria") or {})
        self.conn = conn
        self.monthly = ria.get("monthly_cap", 900)
        self.hourly = ria.get("hourly_cap", 25)
        self.left = budget.allowance(conn, SERVICE, self.monthly, self.hourly)

    def take(self):
        if self.left <= 0:
            raise OutOfQuota
        budget.charge(self.conn, SERVICE)
        self.left -= 1


def _api(session, path, key, wallet, **params):
    """Витрата записується ДО запиту: невдалий теж рахується на їхньому боці."""
    if wallet is not None:
        wallet.take()

    def once():
        r = session.get(f"{API}/{path}", params={"api_key": key, "lang_id": 4, **params})
        if r.status_code == 429:
            raise OutOfQuota("429 від RIA — ліміт вичерпано")
        r.raise_for_status()
        return r.json()

    return net.with_retry(once)


def search_ids(session, key, wallet, search):
    """Один запит на діапазон: фільтри за кімнатністю й ціною працюють.

    Без фільтрів вибірка — 485 оголошень, із ними 35 однокімнатних і
    65 двокімнатних, тобто рівно те, що нас цікавить, за один запит.
    """
    data = _api(
        session, "search", key, wallet,
        category=CATEGORY_FLAT,
        operation_type=OPERATION_LONG_RENT,
        state_id=LVIV_STATE_ID,
        city_id=LVIV_CITY_ID,
        rooms_count=search["rooms_count"],
        price_from=search["price_min"],
        price_to=search["price_max"],
        currency_type=CURRENCY_UAH,
        page=0,
    )
    return [str(i) for i in (data.get("items") or [])]


def fetch_one(session, key, wallet, realty_id):
    return _api(session, f"info/{realty_id}", key, wallet)


# --------------------------------------------------------------------------
# Режим без ключа: розбір стану сторінки каталогу
# --------------------------------------------------------------------------

def _extract_state(html):
    """Стан лежить прямо як JS-об'єкт, тож рахуємо дужки."""
    start = html.find(STATE_KEY)
    if start < 0:
        return None
    start += len(STATE_KEY)
    depth, i = 0, start
    while i < len(html):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                i += 1
                break
        i += 1
    try:
        return json.loads(html[start:i])
    except json.JSONDecodeError:
        return None


def scrape_catalog(session=None):
    session = session or _session()
    html = net.with_retry(lambda: session.get(CATALOG_URL).text)
    state = _extract_state(html)
    if not state:
        return []
    return state.get("catalog", {}).get("realtyForCatalog", []) or []


# --------------------------------------------------------------------------
# Нормалізація під спільну схему
# --------------------------------------------------------------------------

def _num(value):
    if value is None:
        return None
    m = re.search(r"[\d.]+", str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    return float(m.group()) if m else None


def _price_uah(item):
    """Ціна в гривні.

    `priceArr` тримає одну й ту саму ціну в трьох валютах, де ключ 3 —
    гривня. Це єдине надійне джерело: `price` подано у валюті оголошення,
    а поля `priceUAH`, на яке спирався попередній код, в API немає взагалі.
    """
    raw = (item.get("priceArr") or {}).get("3")
    if raw is not None:
        digits = re.sub(r"[^\d]", "", str(raw))
        if digits:
            return int(digits)
    if str(item.get("currency_type_id") or "") == str(CURRENCY_UAH):
        value = _num(item.get("price_total")) or _num(item.get("price"))
        return int(value) if value else None
    return None


def _street(item):
    """«вул. Шептицьких» + 40 -> («Шептицьких, 40», «вул+буд»).

    Тип вулиці тут відкидаємо свідомо: адреса потрібна для показу й
    дедуплікації, а геокодувати нічого — координати приходять точні.
    Формат збігається з ЛУНом, інакше та сама квартира з двох джерел
    не склеїлась би.
    """
    raw = (item.get("street_name_uk") or item.get("street_name") or "").strip()
    if not raw:
        return None, None
    name = STREET_PREFIX.sub("", raw).strip()
    if not name:
        return None, None
    # Номер будинку часто порожній у building_number_str, але лежить
    # у building_number_for_map — саме за ним будинок і показують на карті.
    house = str(item.get("building_number_str") or "").strip()
    if not house:
        house = str(item.get("building_number_for_map") or "").strip()
    if house and str(item.get("is_show_building_no", 1)) != "0":
        return f"{name}, {house}", "вул+буд"
    return name, "вул"


def _coords(item):
    """Координати беремо, лише коли вони точні.

    `radius_location` — радіус розмиття: нуль означає справжню точку
    будинку. Ненульовий радіус нічим не кращий за центроїд району,
    а видавати його за точну адресу небезпечно: саме через такі точки
    квартира отримує неправдиві хвилини ходьби.
    """
    if item.get("radius_location"):
        return None, None
    lat, lon = _num(item.get("latitude")), _num(item.get("longitude"))
    if lat and lon:
        return lat, lon
    return None, None


def _photo_url(item):
    """Фото лежить не за тим шляхом, що в `main_photo`.

    У полі стоїть внутрішній шлях виду dom/photo/34120/.../341207652.jpg,
    але CDN на нього відповідає 415. Реальне посилання збирається зі
    слаґа оголошення й номера знімка.
    """
    main = str(item.get("main_photo") or "")
    photo_id = re.search(r"(\d+)\.[a-z]+$", main)
    slug = re.sub(r"-\d+\.html$", "", re.sub(r"^realty-", "", item.get("beautiful_url") or ""))
    if not (photo_id and slug):
        return None
    return f"https://cdn.riastatic.com/photosnew/dom/photo/{slug}__{photo_id.group(1)}fl.jpg"


def normalize(item, search):
    """Приводить оголошення DIM.RIA до тієї ж схеми, що й OLX."""
    street, conf = _street(item)
    lat, lon = _coords(item)
    realty_id = item.get("realty_id")

    url = item.get("beautiful_url") or ""
    if url and not url.startswith("http"):
        url = SITE + url.lstrip("/")

    rooms = item.get("rooms_count") or item.get("roomsCount")
    description = re.sub(r"\s+", " ", item.get("description_uk") or item.get("description") or "")

    return {
        "id": f"dimria:{realty_id}",
        "source": "dimria",
        "ext_id": str(realty_id),
        "url": url,
        "title": item.get("realtyTitle") or " ".join(
            x for x in (f"{rooms}-кімнатна" if rooms else None, street) if x),
        "description": description[:4000],
        "search_key": search["key"],
        "people": search["people"],
        "price": _price_uah(item),
        "rooms": rooms,
        "area": _num(item.get("total_square_meters")),
        "floor": item.get("floor"),
        "total_floors": item.get("floors_count"),
        "house_type": item.get("wall_type_uk") or item.get("realty_type_name_uk"),
        "city": item.get("city_name_uk"),
        "district": item.get("district_name_uk"),
        # Адреса й координати приходять готовими, геокодувати не треба
        "street": street,
        "street_conf": conf,
        "lat": lat,
        "lon": lon,
        # Точка самого будинку, а не здогад із тексту
        "coords_from_source": True,
        "is_business": 1 if item.get("agency_id") else 0,
        "user_id": str(item.get("user_id")) if item.get("user_id") else None,
        # Дати реєстрації продавця DIM.RIA не віддає: об'єкт `user`
        # приходить порожнім. Головний антискам-сигнал тут недоступний.
        "created_time": item.get("publishing_date"),
        "photos": item.get("photos_count") or 0,
        "photo_url": _photo_url(item),
    }


def _matches(ad, search):
    if ad["rooms"] and ad["rooms"] != search["rooms_count"]:
        return False
    if not ad["price"]:
        return False
    return search["price_min"] <= ad["price"] <= search["price_max"]


def _stub(realty_id, search):
    """Позначка «оголошення ще у видачі» — без запиту деталей.

    Зникнення ловиться на етапі пошуку, який і так вертає всі живі
    ідентифікатори, тож платити за деталі заради цього не треба.
    """
    return {"id": f"dimria:{realty_id}", "source": "dimria", "search_key": search["key"]}


def collect(searches, cfg, conn=None):
    """Повертає (оголошення, живі ідентифікатори за конфігураціями).

    Деталі запитуються лише для тих оголошень, яких ще немає в базі:
    місце розташування квартири не змінюється, тож повторно платити за
    вже відоме безглуздо.
    """
    key = os.environ.get("RIA_API_KEY", "").strip()
    if not key or conn is None:
        return [_ad for _ad in _collect_without_key(searches)], {}

    ria = cfg.get("dimria") or {}
    session = _session()
    wallet = _Wallet(conn, cfg)
    delay = ria.get("delay", 1.5)
    per_pass = ria.get("per_pass_details", 20)

    known = {r["ext_id"] for r in conn.execute(
        "SELECT ext_id FROM ads WHERE source = 'dimria' AND ext_id IS NOT NULL")}

    out, live, pending = [], {}, []
    try:
        for search in searches:
            ids = search_ids(session, key, wallet, search)
            live[search["key"]] = [_stub(i, search) for i in ids]
            pending += [(i, search) for i in ids if i not in known]
            time.sleep(delay)

        for realty_id, search in pending[:per_pass]:
            item = fetch_one(session, key, wallet, realty_id)
            ad = normalize(item, search)
            if _matches(ad, search):
                out.append(ad)
            time.sleep(delay)
    except OutOfQuota:
        # Те, що встигли зібрати, лишається корисним
        pass

    return out, live


def _collect_without_key(searches):
    items = scrape_catalog()
    for search in searches:
        for item in items:
            ad = normalize(item, search)
            if _matches(ad, search):
                yield ad
