"""Жорсткі відсіви та оцінка оголошення (0–100)."""
import re

from . import risk

GOOD_RENOVATION = ("євроремонт", "дизайнерськ", "авторськ", "після ремонту", "новий ремонт")
OK_RENOVATION = ("житловий стан", "косметичний")
WALKTHROUGH = ("прохідн", "суміжн", "смежн")
SOLO_ONLY = ("для однієї особи", "для 1 особи", "для одної особи", "тільки для дівчин",
             "тільки дівчатам", "для дівчини", "лише для дівчат")


def _text(ad):
    return f"{ad.get('title') or ''} {ad.get('description') or ''}".lower()


def reject_reason(ad, cfg):
    """Повертає причину відсіву або None."""
    text = _text(ad)
    area = " ".join(filter(None, [ad.get("district"), ad.get("street"), ad.get("title")])).lower()

    for bad in cfg["exclude_areas"]:
        if bad.lower() in area or bad.lower() in text:
            return f"район: {bad}"

    for bad in cfg["exclude_text"]:
        if bad.lower() in text:
            return f"текст: {bad}"

    dist = ad.get("dist_km")
    if dist is not None and dist > cfg["tiers"]["ok"]:
        return f"далеко: {dist} км"

    rooms = ad.get("rooms") or 0
    if rooms >= cfg["require_separate_layout_from_rooms"]:
        layout = (ad.get("layout") or "").lower()
        if any(w in layout for w in WALKTHROUGH) or any(w in text for w in WALKTHROUGH):
            return "прохідні кімнати"
        if any(w in text for w in SOLO_ONLY):
            return "тільки для однієї особи"

    if rooms == 1 and any(w in text for w in SOLO_ONLY) and (ad.get("people") or 1) > 1:
        return "тільки для однієї особи"

    return None


def tier_of(dist, cfg):
    if dist is None:
        return "невідомо"
    if dist <= cfg["tiers"]["ideal"]:
        return "ідеал"
    if dist <= cfg["tiers"]["good"]:
        return "добре"
    if dist <= cfg["tiers"]["ok"]:
        return "далекувато"
    return "поза"


def score(ad, cfg):
    """0–100. Головні ваги: відстань і ціна на особу."""
    pts = 0
    text = _text(ad)

    dist = ad.get("dist_km")
    if dist is None:
        pts += 12 if "центр" in text else 6
    elif dist <= cfg["tiers"]["ideal"]:
        pts += 40
    elif dist <= cfg["tiers"]["good"]:
        pts += 30
    else:
        pts += 15

    # ціна на особу відносно верхньої межі вилки
    search = next((s for s in cfg["searches"] if s["key"] == ad.get("search_key")), None)
    if search and ad.get("price"):
        per_person = ad["price"] / max(1, ad.get("people") or 1)
        ceiling = search["price_max"] / max(1, search["people"])
        ratio = per_person / ceiling if ceiling else 1
        pts += max(0, min(25, round((1.25 - ratio) * 60)))

    if not ad.get("is_business"):
        pts += 12

    floor, total = ad.get("floor"), ad.get("total_floors")
    if floor and total and 1 < floor < total:
        pts += 6
    elif floor == 1:
        pts -= 3

    reno = (ad.get("renovation") or "").lower()
    if any(w in reno or w in text for w in GOOD_RENOVATION):
        pts += 6
    elif any(w in reno for w in OK_RENOVATION):
        pts += 2

    if "меблям" in (ad.get("furniture") or "").lower() or "з меблями" in text:
        pts += 5

    if (ad.get("photos") or 0) >= 5:
        pts += 3

    if ad.get("street_conf") == "вул+буд":
        pts += 4

    return max(0, min(100, pts))


def evaluate(ad, cfg, median_ppm=None):
    """Проставляє rejected / tier / score / ознаки ризику."""
    ad["rejected"] = reject_reason(ad, cfg)
    ad["tier"] = tier_of(ad.get("dist_km"), cfg)
    ad["score"] = 0 if ad["rejected"] else score(ad, cfg)

    marks = risk.flags(ad, median_ppm)
    ad["risk_flags"] = "; ".join(marks) or None
    ad["risk_level"] = risk.level(marks)
    # Підозріле не відсіюємо — рішення за людиною, але вниз списку опускаємо
    if ad["risk_level"] == "високий":
        ad["score"] = max(0, ad["score"] - 20)
    elif ad["risk_level"] == "середній":
        ad["score"] = max(0, ad["score"] - 8)

    if ad.get("lat") and ad.get("lon"):
        ad["map_url"] = f"https://www.google.com/maps/search/?api=1&query={ad['lat']},{ad['lon']}"
    return ad
