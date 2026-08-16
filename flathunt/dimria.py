"""Збирач DIM.RIA (dom.ria.com).

Цінність джерела — точна адреса з номером будинку і справжні координати
об'єкта, чого OLX не дає взагалі (там центроїд району).

Два режими:

1. Офіційне API, коли заданий RIA_API_KEY. Дає нормальну пагінацію
   й фільтри. Ключ безкоштовний, реєстрація на developers.ria.com.
2. Запасний, без ключа: сторінка каталогу віддає 20 повних оголошень
   у window.__INITIAL_STATE__. Пагінація там не працює, а сортування
   не хронологічне, тому для лову свіжого це слабко — але для розбору
   наявного фонду цілком годиться.
"""
import json
import os
import re
import time

from curl_cffi import requests as http

from . import net

CATALOG_URL = "https://dom.ria.com/uk/arenda-kvartir/lvov/"
API = "https://developers.ria.com/dom"
STATE_KEY = "window.__INITIAL_STATE__="

# Довгострокова оренда квартир у Львові
OPERATION_LONG_RENT = 3
CATEGORY_FLAT = 1


def _session():
    return http.Session(impersonate="chrome", timeout=30)


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
# Режим з ключем: офіційне API
# --------------------------------------------------------------------------

def _api(session, path, key, **params):
    def once():
        r = session.get(f"{API}/{path}", params={"api_key": key, "lang_id": 4, **params})
        r.raise_for_status()
        return r.json()

    return net.with_retry(once)


def resolve_city(session, key, state_name="Львівська", city_name="Львів"):
    """Ідентифікатори області й міста краще питати, ніж зашивати."""
    states = _api(session, "states", key)
    state_id = next(
        (s["stateID"] for s in states if state_name.lower() in str(s.get("name", "")).lower()), None
    )
    if state_id is None:
        return None, None
    cities = _api(session, f"cities/{state_id}", key)
    city_id = next(
        (c["cityID"] for c in cities if city_name.lower() in str(c.get("name", "")).lower()), None
    )
    return state_id, city_id


def api_search(session, key, state_id, city_id, price_min, price_max, page=0, limit=50):
    data = _api(
        session, "search", key,
        category=CATEGORY_FLAT,
        realty_type=CATEGORY_FLAT,
        operation_type=OPERATION_LONG_RENT,
        state_id=state_id,
        city_id=city_id,
        price_from=price_min,
        price_to=price_max,
        currency_type=1,          # гривня
        page=page,
        limit=limit,
    )
    return data.get("items", []) or data.get("result", {}).get("search_result", {}).get("items", [])


# --------------------------------------------------------------------------
# Нормалізація під спільну схему
# --------------------------------------------------------------------------

def _num(value):
    if value is None:
        return None
    m = re.search(r"[\d.]+", str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    return float(m.group()) if m else None


def normalize(item, search):
    """Приводить оголошення DIM.RIA до тієї ж схеми, що й OLX."""
    street = item.get("street_name_uk") or item.get("streetName") or ""
    house = item.get("building_number_str") or ""
    if street and house and str(item.get("is_show_building_no", 1)) != "0":
        full_street = f"{street.strip()}, {house.strip()}"
        conf = "вул+буд"
    else:
        full_street = street.strip() or None
        conf = "вул" if street else None

    price = _num(item.get("priceUAH")) or _num(item.get("price_total"))
    realty_id = item.get("realty_id")
    url = item.get("beautiful_url") or ""
    if url and not url.startswith("http"):
        url = "https://dom.ria.com/uk/" + url.lstrip("/")

    lat, lon = _num(item.get("latitude")), _num(item.get("longitude"))

    return {
        "id": f"dimria:{realty_id}",
        "source": "dimria",
        "ext_id": str(realty_id),
        "url": url,
        "title": (item.get("realtyTitle") or f"{street} {house}").strip(),
        "description": re.sub(r"\s+", " ", item.get("description_uk") or "")[:4000],
        "search_key": search["key"],
        "people": search["people"],
        "price": int(price) if price else None,
        "rooms": item.get("rooms_count") or item.get("roomsCount"),
        "area": _num(item.get("total_square_meters")),
        "floor": item.get("floor"),
        "total_floors": item.get("floors_count"),
        "house_type": item.get("realty_type_name_uk"),
        "city": item.get("city_name_uk"),
        "district": item.get("district_name_uk"),
        # Тут адреса приходить готовою, геокодувати не треба
        "street": full_street,
        "street_conf": conf,
        "lat": lat,
        "lon": lon,
        "is_business": 0 if not item.get("agency_id") else 1,
        "user_id": str(item.get("user_id")) if item.get("user_id") else None,
        "created_time": item.get("publishing_date"),
        "photos": item.get("photos_count") or 0,
        "photo_url": ("https://cdn.riastatic.com/photos/" + item["main_photo"])
                     if item.get("main_photo") else None,
    }


def _matches(ad, search):
    """Без ключа фільтрів на боці сайту немає — відсіюємо самі."""
    if ad["rooms"] and ad["rooms"] != search["rooms_count"]:
        return False
    if not ad["price"]:
        return False
    return search["price_min"] <= ad["price"] <= search["price_max"]


def collect(searches, cfg):
    """Повертає нормалізовані оголошення для всіх конфігурацій разом.

    DIM.RIA не розділяє запити за кімнатністю так само, як OLX, тому
    зручніше зібрати один раз і розкласти по конфігураціях самим.
    """
    key = os.environ.get("RIA_API_KEY", "").strip()
    session = _session()
    out = []

    if key:
        state_id, city_id = resolve_city(session, key)
        if city_id is None:
            return []
        for search in searches:
            page = 0
            while page < 6:
                items = api_search(session, key, state_id, city_id,
                                   search["price_min"], search["price_max"], page=page)
                if not items:
                    break
                for item in items:
                    ad = normalize(item, search)
                    if _matches(ad, search):
                        out.append(ad)
                page += 1
                time.sleep(cfg["collector"]["delay_between_requests"])
        return out

    # без ключа — те, що віддає каталог
    items = scrape_catalog(session)
    for search in searches:
        for item in items:
            ad = normalize(item, search)
            if _matches(ad, search):
                out.append(ad)
    return out
