"""Надсилання карток у Telegram."""
import html

import requests

TIER_MARK = {"ідеал": "🟢", "добре": "🟡", "далекувато": "🟠", "невідомо": "⚪"}


def card(ad):
    """HTML-картка для Telegram: усе потрібне, щоб одразу дзвонити."""
    price = ad["price"] or 0
    people = ad["people"] or 1
    per_person = round(price / people)
    mark = TIER_MARK.get(ad["tier"], "⚪")

    where = ad["street"] or ad["district"] or "адреса не вказана"
    dist = f"{ad['dist_km']} км" if ad["dist_km"] is not None else "відстань невідома"

    lines = [
        f"{mark} <b>{html.escape(where)}</b> — {dist}",
        f"<b>{price:,} ₴</b>".replace(",", " ") + (f"  ·  {per_person:,} ₴/особу".replace(",", " ") if people > 1 else ""),
    ]

    facts = []
    if ad["rooms"]:
        facts.append(f"{ad['rooms']}-кімн.")
    if ad["area"]:
        facts.append(f"{ad['area']:g} м²")
    if ad["floor"] and ad["total_floors"]:
        facts.append(f"{ad['floor']}/{ad['total_floors']} поверх")
    if ad["layout"]:
        facts.append(str(ad["layout"]).lower())
    if ad["renovation"]:
        facts.append(str(ad["renovation"]).lower())
    if facts:
        lines.append(html.escape(" · ".join(facts)))

    lines.append("👤 <b>власник</b>" if not ad["is_business"] else "🏢 агенція (можлива комісія)")
    lines.append(f"⭐ {ad['score']}/100")
    lines.append(f'<a href="{html.escape(ad["url"])}">Відкрити оголошення</a>')

    return "\n".join(lines)


def send(cfg, text):
    token = cfg["notify"]["telegram_token"]
    chat_id = cfg["notify"]["telegram_chat_id"]
    if not token or not chat_id:
        return False, "telegram не налаштовано"
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    ok = r.ok and r.json().get("ok")
    return bool(ok), ("" if ok else r.text[:200])
