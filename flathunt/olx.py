"""Збирач OLX.

Уся потрібна інформація лежить у __PRERENDERED_STATE__ сторінки списку:
опис, параметри, поверх, планування, фото, isBusiness, createdTime.
Тому одна сторінка = 40 повних оголошень, окремо кожне тягнути не треба.
"""
import json
import re
import time
import urllib.parse

# Cloudflare на OLX фільтрує за TLS-відбитком, а не за заголовками:
# з Fedora звичайний requests проходить, з debian-slim у контейнері —
# 403 навіть із повним набором браузерних заголовків. curl_cffi відтворює
# рукостискання справжнього Chrome і знімає питання на будь-якому хості.
try:
    from curl_cffi import requests as http

    IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # локально можна й без нього
    import requests as http

    IMPERSONATE = {}

BASE = "https://www.olx.ua/uk/nedvizhimost/kvartiry/dolgosrochnaya-arenda-kvartir/lvov/"
STATE_RE = re.compile(r'__PRERENDERED_STATE__\s*=\s*("(?:\\.|[^"\\])*")')

ROOMS_TO_INT = {"odnokomnatnye": 1, "dvuhkomnatnye": 2, "trehkomnatnye": 3}


def build_url(rooms, price_min, price_max, page=1):
    params = {
        "currency": "UAH",
        "search[order]": "created_at:desc",
        "search[filter_enum_number_of_rooms_string][0]": rooms,
        "search[filter_float_price:from]": price_min,
        "search[filter_float_price:to]": price_max,
    }
    if page > 1:
        params["page"] = page
    return BASE + "?" + urllib.parse.urlencode(params)


def fetch_state(url, user_agent, timeout=30):
    r = http.get(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "uk-UA,uk;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=timeout,
        **IMPERSONATE,
    )
    r.raise_for_status()
    m = STATE_RE.search(r.text)
    if not m:
        raise RuntimeError(f"не знайдено __PRERENDERED_STATE__ у {url}")
    payload = json.loads(m.group(1))
    try:
        payload = urllib.parse.unquote(payload)
    except Exception:
        pass
    return json.loads(payload)


def extract_ads(state):
    """Дістає масив оголошень із стану, де б він не лежав."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            ads = node.get("ads")
            if isinstance(ads, list) and ads and isinstance(ads[0], dict) and "title" in ads[0]:
                found.extend(ads)
                return
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(state)
    return found


def _params_map(ad):
    out = {}
    for p in ad.get("params") or []:
        out[p.get("key")] = p.get("value")
    return out


def _num(value):
    if value is None:
        return None
    m = re.search(r"[\d.]+", str(value).replace(",", ".").replace(" ", ""))
    return float(m.group()) if m else None


def normalize(ad, search):
    """Приводить оголошення OLX до внутрішньої схеми."""
    p = _params_map(ad)
    price = None
    price_obj = ad.get("price") or {}
    if isinstance(price_obj, dict):
        rv = price_obj.get("regularPrice") or {}
        price = rv.get("value") if isinstance(rv, dict) else None
    if price is None:
        price = _num(p.get("price"))

    loc = ad.get("location") or {}
    user = ad.get("user") or {}
    contact = ad.get("contact") or {}
    photos = ad.get("photos") or []

    return {
        "id": f"olx:{ad.get('id')}",
        "source": "olx",
        "ext_id": str(ad.get("id")),
        "url": ad.get("url"),
        "title": (ad.get("title") or "").strip(),
        "description": re.sub(r"\s+", " ", (ad.get("description") or "")).strip()[:4000],
        "search_key": search["key"],
        "people": search["people"],
        "price": int(price) if price else None,
        "rooms": ROOMS_TO_INT.get(search["rooms"]),
        "area": _num(p.get("total_area")),
        "floor": int(_num(p.get("floor"))) if _num(p.get("floor")) is not None else None,
        "total_floors": int(_num(p.get("total_floors"))) if _num(p.get("total_floors")) is not None else None,
        "layout": p.get("flat_planning") or p.get("planning"),
        "renovation": p.get("flat_renovation") or p.get("renovation"),
        "furniture": p.get("furnish") or p.get("furniture"),
        "house_type": p.get("property_type_appartments_sale"),
        "city": loc.get("cityName"),
        "district": loc.get("districtName"),
        "is_business": 1 if ad.get("isBusiness") else 0,
        "seller": contact.get("name") or user.get("name"),
        "user_id": str(user.get("id")) if user.get("id") else None,
        # Вік акаунта — найдешевший сигнал шахрайства, лежить прямо тут
        "user_created": user.get("created"),
        "user_verified": (user.get("verification") or {}).get("status"),
        "created_time": ad.get("createdTime"),
        "refresh_time": ad.get("lastRefreshTime"),
        "photos": len(photos),
        "photo_url": photos[0] if photos else None,
    }


def collect(search, cfg):
    """Повертає список нормалізованих оголошень для однієї пошукової конфігурації."""
    ua = cfg["collector"]["user_agent"]
    delay = cfg["collector"]["delay_between_requests"]
    out, seen = [], set()
    for page in range(1, cfg["collector"]["pages_per_search"] + 1):
        url = build_url(search["rooms"], search["price_min"], search["price_max"], page)
        state = fetch_state(url, ua)
        ads = extract_ads(state)
        if not ads:
            break
        for ad in ads:
            if ad.get("id") in seen:
                continue
            seen.add(ad.get("id"))
            out.append(normalize(ad, search))
        if len(ads) < 40:
            break
        time.sleep(delay)
    return out
