"""Картки у Telegram.

Розмітка — HTML, а не MarkdownV2. Обидві дають однаковий вигляд, але
MarkdownV2 вимагає екранувати півтора десятка символів, і будь-яка тире
чи дужка в назві вулиці ламає повідомлення цілком. У HTML екранувати
треба три символи, тож ризик зіпсувати картку через чужий текст нижчий.
"""
import html
import re

import requests

TIER_MARK = {"ideal": "🟢", "good": "🟡", "far": "🟠", "unknown": "⚪"}
RISK_MARK = {"high": "🔴", "medium": "🟠", "low": "🟡"}
# Всередині коду рівні англійською, а в картку йде українською —
# картку читає людина, логи читає розробник.
RISK_WORD = {"high": "високий", "medium": "середній", "low": "низький"}
ROOMS_WORD = {1: "1-кімнатна", 2: "2-кімнатна", 3: "3-кімнатна"}

KEYBOARD = {
    # Три кнопки в один ряд: із повними словами вони не вміщаються
    # у ширину телефона й переносяться, тому підписи короткі.
    # Старі варіанти лишаються в BUTTONS: клавіатура на телефоні
    # persistent і оновиться лише з наступним повідомленням.
    "keyboard": [[{"text": "🔎 1-кімнатні"}, {"text": "🔎 2-кімнатні"},
                  {"text": "🔎 3-кімнатні"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def _get(ad, key, default=None):
    """Працює і з sqlite3.Row, і зі звичайним dict."""
    try:
        value = ad[key]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


def _human_age(iso):
    """«11 років», «3 міс.», «14 днів» — щоб вік читався одразу."""
    if not iso:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days < 31:
        return f"{days} дн."
    if days < 365:
        return f"{days // 30} міс."
    years = days // 365
    return f"{years} р." if years < 5 else f"{years} років"


def _posted(days):
    if days is None:
        return None
    if days == 0:
        return "викладено сьогодні"
    if days == 1:
        return "викладено вчора"
    return f"висить {days} дн."


def _money(value):
    return f"{int(value):,}".replace(",", " ")


def _map_link(ad):
    """Посилання на карту є завжди, коли є хоч якась точка."""
    lat, lon = _get(ad, "lat"), _get(ad, "lon")
    if lat and lon:
        return f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
    return None


def card(ad, anchor=None):
    rooms = _get(ad, "rooms")
    street = _get(ad, "street") or _get(ad, "district") or "адреса не вказана"
    price = _get(ad, "price", 0)
    people = _get(ad, "people", 1) or 1

    head = ROOMS_WORD.get(rooms, f"{rooms}-кімнатна" if rooms else "квартира")
    # Для дво- й трикімнатної планування вирішує так само, як кімнатність:
    # прохідні кімнати означають, що жити по окремих не вийде. Тому
    # позначка стоїть у першому ж рядку, а не десь у зауваженнях.
    if (rooms or 0) >= 2:
        head += {"так": " · роздільні", "ні": " · ⚠ прохідні",
                 "мабуть ні": " · ⚠ мабуть прохідні"}.get(_get(ad, "layout_status"), "")
    lines = [f"🏠 <b>{head}</b> · {html.escape(str(street))}", ""]

    money = f"<b>{_money(price)} ₴</b>"
    if people > 1:
        money += f"   ·   {_money(round(price / people))} ₴ з кожного"
    lines.append(money)

    facts = []
    if _get(ad, "area"):
        facts.append(f"{_get(ad, 'area'):g} м²")
    if _get(ad, "floor") and _get(ad, "total_floors"):
        facts.append(f"{_get(ad, 'floor')}/{_get(ad, 'total_floors')} поверх")
    if _get(ad, "renovation"):
        facts.append(str(_get(ad, "renovation")).lower())
    if facts:
        lines.append(html.escape(" · ".join(facts)))
    lines.append("")

    # Відстань і карта — одним рядком, без окремого повідомлення з міткою
    walk = _get(ad, "walk_min")
    dist = _get(ad, "dist_km")
    mark = TIER_MARK.get(_get(ad, "tier"), "⚪")
    if _get(ad, "street_conf") == "район":
        where = f"⚪ приблизно <b>{dist} км</b> до Анни — адреси немає, точка за центром району"
    elif walk:
        where = f"{mark} <b>{walk} хв</b> пішки до Анни"
    elif dist is not None:
        where = f"{mark} близько <b>{dist} км</b> до Анни"
    else:
        where = "⚪ відстань невідома"
    link = _map_link(ad)
    if link:
        where += f'   ·   <a href="{link}">на карті</a>'
    lines.append(where)

    conf = _get(ad, "street_conf")
    if conf in ("вул", "модель"):
        lines.append("<i>номер будинку не вказано — точка приблизна</i>")
    elif conf == "сумнівно":
        lines.append("<i>геокодер розійшовся з районом OLX — адресу треба уточнити</i>")

    who = "👤 <b>Власник</b>" if not _get(ad, "is_business") else "🏢 Агенція"
    commission = _get(ad, "commission")
    if commission == "немає":
        who += "   ·   без комісії"
    elif commission:
        who += f"   ·   комісія {commission}"
    lines.append(who)

    extras = []
    if _get(ad, "utilities") == "входить":
        extras.append("комуналка входить")
    if _get(ad, "backup_power") == "є":
        extras.append("⚡ резервне живлення")
    if _get(ad, "deposit"):
        extras.append(f"депозит {_get(ad, 'deposit')} міс.")
    if extras:
        lines.append(html.escape(" · ".join(extras)))

    # Вік оголошення й вік акаунта поруч: разом вони дають картину довіри
    # одним поглядом. Свіже оголошення з дводенного акаунта і свіже
    # оголошення з акаунта на десять років — зовсім різні речі.
    # Телефон і склад фото приходять із ЛУНа. Телефон рятує від історії,
    # коли оголошення знімають і зв'язатись уже нема як; склад фото прямо
    # відповідає на питання «а чому кухню не сфоткали».
    phone = _get(ad, "phone")
    if phone:
        digits = re.sub(r"\D", "", str(phone))
        pretty = f"+{digits}" if digits.startswith("380") else str(phone)
        lines.append(f'📞 <a href="tel:{pretty}">{pretty}</a>')

    rooms_shot = _get(ad, "photo_rooms")
    if rooms_shot:
        missing = [w for w in ("кухня", "санвузол") if w not in rooms_shot]
        line = f"📷 на фото: {html.escape(str(rooms_shot))}"
        if missing:
            line += f"  ·  <b>немає: {', '.join(missing)}</b>"
        lines.append(line)

    timeline = [x for x in (_posted(_get(ad, "days_listed")),
                            (f"акаунт {_human_age(_get(ad, 'user_created'))}"
                             if _human_age(_get(ad, "user_created")) else None)) if x]
    if timeline:
        lines.append("📅 " + html.escape(" · ".join(timeline)))

    warns = []
    if _get(ad, "restrictions"):
        warns.append(str(_get(ad, "restrictions")))
    drop = _get(ad, "price_drop")
    if drop and drop > 0:
        warns.append(f"ціна впала на {_money(drop)} ₴")
    level = _get(ad, "risk_level")
    if level:
        warns.append(f"ризик {RISK_WORD.get(level, level)}: {_get(ad, 'risk_flags', '')}")
    if warns:
        lines.append("")
        lines.append("<blockquote>" + html.escape(" · ".join(warns)) + "</blockquote>")

    lines.append("")
    lines.append(f'⭐ {_get(ad, "score", 0)}/100   ·   '
                 f'<a href="{html.escape(str(_get(ad, "url", "")))}">відкрити оголошення</a>')
    return "\n".join(lines)


def digest(rows, title, warming=False):
    """Компактний список для кнопок: одне оголошення — два рядки."""
    if not rows:
        if warming:
            return (f"<b>{title}</b>\n\nЩе збираю дані — база наповнюється після запуску. "
                    "Спробуй за кілька хвилин.")
        return f"<b>{title}</b>\n\nЗараз нічого немає під фільтрами."
    out = [f"<b>{title}</b>", ""]
    for i, r in enumerate(rows, 1):
        street = _get(r, "street") or _get(r, "district") or "адреса не вказана"
        walk = _get(r, "walk_min")
        where = f"{walk} хв" if walk else "? хв"
        tags = []
        if not _get(r, "is_business"):
            tags.append("власник")
        if _get(r, "commission") == "немає":
            tags.append("без комісії")
        if _get(r, "risk_level"):
            tags.append(f"⚠{RISK_WORD.get(_get(r, 'risk_level'), _get(r, 'risk_level'))}")
        out.append(
            f'{i}. <b>{_money(_get(r, "price", 0))} ₴</b> · {_get(r, "area", 0):g} м² · {where}\n'
            f'    <a href="{html.escape(str(_get(r, "url", "")))}">{html.escape(str(street))}</a>'
            + (f' — {html.escape(" · ".join(tags))}' if tags else "")
            + f'  ⭐{_get(r, "score", 0)}'
        )
    return "\n".join(out)


def _post(cfg, method, payload, chat_id=None):
    token = cfg["notify"]["telegram_token"]
    chat_id = chat_id or cfg["notify"].get("telegram_chat_id")
    if not token or not chat_id:
        return False, "telegram не налаштовано"
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      json={"chat_id": chat_id, **payload}, timeout=20)
    ok = r.ok and r.json().get("ok")
    return bool(ok), ("" if ok else r.text[:200])


def send(cfg, text, chat_id=None, keyboard=False):
    payload = {"text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if keyboard:
        payload["reply_markup"] = KEYBOARD
    return _post(cfg, "sendMessage", payload, chat_id)


def send_ad(cfg, ad, chat_id=None):
    return send(cfg, card(ad, cfg.get("anchor")), chat_id, keyboard=True)
