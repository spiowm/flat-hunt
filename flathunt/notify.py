"""Надсилання карток у Telegram.

Крім тексту йде нативна мітка на карті. Точність кола відображає те,
наскільки ми впевнені в адресі: якщо відомий номер будинку — коло
маленьке, якщо тільки вулиця — велике, і по ньому одразу видно,
що точку ще треба уточнити в продавця.
"""
import html

import requests

TIER_MARK = {"ідеал": "🟢", "добре": "🟡", "далекувато": "🟠", "невідомо": "⚪"}
RISK_MARK = {"високий": "🔴", "середній": "🟠", "низький": "🟡"}

# Радіус кола на карті, метрів: наскільки насправді відома точка
ACCURACY = {"вул+буд": 80.0, "вул": 600.0, "сумнівно": 1400.0}


def _get(ad, key, default=None):
    """Працює і з sqlite3.Row, і зі звичайним dict."""
    try:
        value = ad[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def card(ad, anchor=None):
    price = _get(ad, "price", 0)
    people = _get(ad, "people", 1) or 1
    per_person = round(price / people)
    mark = TIER_MARK.get(_get(ad, "tier"), "⚪")

    where = _get(ad, "street") or _get(ad, "district") or "адреса не вказана"
    dist_km = _get(ad, "dist_km")
    dist = f"{dist_km} км до Анни" if dist_km is not None else "відстань невідома"

    lines = [
        f"{mark} <b>{html.escape(str(where))}</b> — {dist}",
        f"<b>{price:,} ₴</b>".replace(",", " ")
        + (f"  ·  {per_person:,} ₴/особу".replace(",", " ") if people > 1 else ""),
    ]

    facts = []
    if _get(ad, "rooms"):
        facts.append(f"{_get(ad, 'rooms')}-кімн.")
    if _get(ad, "area"):
        facts.append(f"{_get(ad, 'area'):g} м²")
    if _get(ad, "floor") and _get(ad, "total_floors"):
        facts.append(f"{_get(ad, 'floor')}/{_get(ad, 'total_floors')} поверх")
    if _get(ad, "layout"):
        facts.append(str(_get(ad, "layout")).lower())
    if _get(ad, "renovation"):
        facts.append(str(_get(ad, "renovation")).lower())
    if facts:
        lines.append(html.escape(" · ".join(facts)))

    lines.append("👤 <b>власник</b>" if not _get(ad, "is_business") else "🏢 агенція (можлива комісія)")

    level = _get(ad, "risk_level")
    if level:
        flags = _get(ad, "risk_flags", "")
        lines.append(f"{RISK_MARK.get(level, '🟡')} <b>ризик {level}:</b> {html.escape(str(flags))}")

    conf = _get(ad, "street_conf")
    if conf == "вул":
        lines.append("<i>номер будинку не вказано — точка приблизна, спитай адресу</i>")
    elif conf == "сумнівно":
        lines.append("<i>геокодер і OLX не зійшлись у районі — адресу треба уточнити</i>")

    lines.append(f"⭐ {_get(ad, 'score', 0)}/100")

    links = [f'<a href="{html.escape(str(_get(ad, "url", "")))}">Оголошення</a>']
    lat, lon = _get(ad, "lat"), _get(ad, "lon")
    if lat and lon:
        links.append(f'<a href="https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}">Карта</a>')
        if anchor:
            links.append(
                f'<a href="https://www.google.com/maps/dir/?api=1&origin={lat},{lon}'
                f'&destination={anchor["lat"]},{anchor["lon"]}&travelmode=walking">Пішки до Анни</a>'
            )
    lines.append(" · ".join(links))

    return "\n".join(lines)


def _post(cfg, method, payload):
    token = cfg["notify"]["telegram_token"]
    chat_id = cfg["notify"]["telegram_chat_id"]
    if not token or not chat_id:
        return False, "telegram не налаштовано"
    payload = {"chat_id": chat_id, **payload}
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload, timeout=20)
    ok = r.ok and r.json().get("ok")
    return bool(ok), ("" if ok else r.text[:200])


def send(cfg, text):
    return _post(cfg, "sendMessage", {
        "text": text, "parse_mode": "HTML", "disable_web_page_preview": False,
    })


def send_ad(cfg, ad):
    """Картка + мітка на карті з колом реальної точності."""
    anchor = cfg.get("anchor")
    ok, err = send(cfg, card(ad, anchor))
    if not ok:
        return ok, err

    lat, lon = _get(ad, "lat"), _get(ad, "lon")
    if lat and lon:
        _post(cfg, "sendLocation", {
            "latitude": lat,
            "longitude": lon,
            "horizontal_accuracy": ACCURACY.get(_get(ad, "street_conf"), 900.0),
        })
    return True, ""
