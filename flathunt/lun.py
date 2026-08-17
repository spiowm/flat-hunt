"""ЛУН as a source.

The most valuable source of the three. LUN aggregates other sites, and
roughly seventy percent of its inventory comes from rieltor.ua, which we
do not watch at all — that is why listings visible on LUN were missing
from our lists.

Its cards also carry what OLX hides: exact coordinates, a house number,
the seller phone, an owner flag, the commission rate and a price history.
Half the machinery built for OLX — geocoding, model parsing, heuristics —
is unnecessary here because the data arrives structured.

Listings live in the Next.js streaming payload (self.__next_f), not in
plain HTML, so the page has to be reassembled before parsing.
"""
import json
import re
import time

from curl_cffi import requests as http

from . import net

BASE = "https://lun.ua/uk/rent/lviv/flats"

# Шляхи за кімнатністю. Змішаний список довелося б гортати вдвічі
# довше заради того самого: за окремими шляхами лежить 358 однокімнатних
# і 355 двокімнатних, і жоден не вичерпується за пʼятнадцять сторінок.
ROOM_PATH = {1: "flats-odnokimnatni", 2: "flats-dvokimnatni", 3: "flats-trykimnatni"}
NBU_RATES = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json"
IMAGE_CDN = "https://market-images.lunstatic.net/lun-ua/720/960/images/{}.webp"

# ЛУН класифікує кожне фото за приміщенням. Це прямо відповідає на
# питання «а чому кухню не сфоткали»: видно, чого в оголошенні немає.
ROOM_WORD = {"kitchen": "кухня", "bathroom": "санвузол", "living": "вітальня",
             "bedroom": "спальня", "hall": "коридор", "balcony": "балкон",
             "plan": "план", "house": "будинок", "yard": "двір", "view": "вид"}


def photo_rooms(card):
    """Які приміщення показані на фото — і, за різницею, яких немає."""
    cats = [i.get("category") for i in (card.get("images") or []) if isinstance(i, dict)]
    seen = []
    for c in cats:
        word = ROOM_WORD.get(c, c)
        if word and word not in seen:
            seen.append(word)
    return seen

_rates = {"uah": 1.0}


def _session():
    return http.Session(impersonate="chrome", timeout=30)


def rates():
    """Курси НБУ. Ціни на ЛУНі бувають у доларах і євро."""
    if len(_rates) > 1:
        return _rates
    try:
        data = http.get(NBU_RATES, impersonate="chrome", timeout=20).json()
        for row in data:
            code = str(row.get("cc", "")).lower()
            if code in ("usd", "eur"):
                _rates[code] = float(row["rate"])
    except Exception:
        # Без курсу краще взяти грубу оцінку, ніж загубити оголошення
        _rates.setdefault("usd", 42.0)
        _rates.setdefault("eur", 46.0)
    return _rates


def _cards_from_html(html):
    """Збирає потоковий payload Next.js і дістає з нього масиви карток."""
    chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
    if not chunks:
        return []
    payload = "".join(json.loads(f'"{c}"') for c in chunks)

    out, pos = [], 0
    while True:
        i = payload.find('"cards":[', pos)
        if i < 0:
            break
        start = payload.index("[", i)
        depth, j = 0, start
        while j < len(payload):
            if payload[j] == "[":
                depth += 1
            elif payload[j] == "]":
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        try:
            out.extend(json.loads(payload[start:j]))
        except json.JSONDecodeError:
            pass
        pos = j
    return out


def fetch_page(session, page, path=None):
    base = f"https://lun.ua/uk/rent/lviv/{path}" if path else BASE
    url = base if page == 1 else f"{base}?page={page}"
    html = net.with_retry(lambda: session.get(url).text)
    return _cards_from_html(html)


STREET_SUFFIX = re.compile(r"\s+(вул|вулиця|просп|проспект|пров|провулок|пл|площа)\.?\s*$", re.I)


# У карток, що прийшли з OLX, header — це заголовок оголошення, а не
# адреса: «Вільна 2 кімнатна квартира». Тому адресою вважаємо лише те,
# що на неї схоже, інакше в поле вулиці потрапляє назва оголошення.
def _street(card):
    """«Варшавська вул., 201-А» -> («Варшавська, 201-А», точність).

    Приймаємо лише власний формат ЛУНа, де перша частина закінчується
    типом вулиці. Усе інше — це заголовок оголошення з іншого сайту,
    і назва квартири в полі адреси зіпсувала б дедуплікацію.
    """
    header = (card.get("header") or "").strip()
    parts = [p.strip() for p in header.split(",")]
    if not header or not STREET_SUFFIX.search(parts[0]):
        return None, None
    name = STREET_SUFFIX.sub("", parts[0]).strip()
    house = parts[1].strip() if len(parts) > 1 else None
    if house and re.match(r"^\d", house):
        return f"{name}, {house}", "вул+буд"
    return name or None, "вул" if name else None


def _first_image(card):
    images = card.get("images") or []
    if not images or not isinstance(images[0], dict):
        return None
    image_id = images[0].get("imageId")
    return IMAGE_CDN.format(image_id) if image_id else None


def _price_uah(card):
    value = card.get("price")
    if not value:
        return None
    cur = (card.get("currency") or "uah").lower()
    return int(round(float(value) * rates().get(cur, 1.0)))


def _commission(card):
    if card.get("withoutCommission"):
        return "немає"
    rate = card.get("commissionRate")
    if card.get("commissionType") == "%" and rate:
        if rate >= 90:
            return "повна"
        if rate >= 40:
            return "половина"
    return None


def normalize(card, search):
    loc = card.get("location") or []
    lat = lon = None
    if len(loc) == 2:
        lon, lat = float(loc[0]), float(loc[1])

    street, conf = _street(card)
    site = (card.get("site") or {}).get("displayName") or "lun.ua"
    phones = card.get("phones") or []

    return {
        "id": f"lun:{card.get('id')}",
        "source": site,
        "ext_id": str(card.get("id")),
        "url": card.get("urlRaw") or f"https://lun.ua/realty/{card.get('id')}",
        "title": (card.get("header") or "").strip(),
        "description": re.sub(r"\s+", " ", card.get("text") or "")[:4000],
        "search_key": search["key"],
        "people": search["people"],
        "price": _price_uah(card),
        "rooms": card.get("roomCount"),
        "area": card.get("areaTotal"),
        "floor": card.get("floor"),
        "total_floors": card.get("floorCount"),
        "renovation": None if card.get("withoutRenovation") is None else (
            "без ремонту" if card.get("withoutRenovation") else None),
        "house_type": card.get("houseTypeName"),
        "city": "Львів",
        "district": (card.get("geo") or "").split(",")[-3].strip() if (card.get("geo") or "").count(",") >= 3 else None,
        # Адреса й координати приходять готовими — геокодувати не треба
        "street": street,
        "street_conf": conf,
        "lat": lat,
        "lon": lon,
        "is_business": 0 if card.get("isOwner") else 1,
        "commission": _commission(card),
        "phone": phones[0] if phones else None,
        "backup_power": "є" if card.get("autonomyPower") else None,
        "created_time": card.get("insertTime"),
        "photos": len(card.get("images") or []),
        "photo_url": _first_image(card),
        "photo_rooms": ", ".join(photo_rooms(card)) or None,
        # Тут модель не потрібна: усе, що вона витягала з тексту, вже є полями
        "llm_done": 1,
    }


def _matches(ad, search):
    if ad["rooms"] and ad["rooms"] != search["rooms_count"]:
        return False
    if not ad["price"]:
        return False
    return search["price_min"] <= ad["price"] <= search["price_max"]


def collect(searches, cfg, max_pages=25, delay=None):
    """Прохід по ЛУНу окремо для кожної потрібної кімнатності.

    Гортаємо до вичерпання, а не до фіксованого ліміту: сторінка, яка
    не дала жодного нового оголошення, і є кінцем списку. Ліміт лишається
    лише запобіжником від нескінченного циклу.
    """
    session = _session()
    delay = cfg["collector"]["delay_between_requests"] if delay is None else delay
    out, seen = [], set()

    for search in searches:
        path = ROOM_PATH.get(search.get("rooms_count"))
        if not path:
            continue
        for page in range(1, max_pages + 1):
            try:
                batch = fetch_page(session, page, path)
            except Exception:
                break
            fresh = [c for c in batch if c.get("id") not in seen]
            if not fresh:
                break
            for card in fresh:
                seen.add(card["id"])
                ad = normalize(card, search)
                if _matches(ad, search):
                    out.append(ad)
            time.sleep(delay)
    return out
