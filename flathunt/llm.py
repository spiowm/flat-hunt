"""Розбір опису оголошення моделлю Gemini.

Структуровані параметри OLX не містять найважливішого для рішення:
чи входить комуналка в ціну, який депозит, скільки бере агенція,
чи можна з твариною, чи є генератор. Усе це живе у вільному тексті.

Модель викликається один раз на оголошення, результат зберігається,
тож ~10 нових на добу коштують копійки. Якщо ключа немає — модуль
мовчки нічого не робить, решта конвеєра працює як раніше.
"""
import json
import os

import requests

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SCHEMA = {
    "type": "object",
    "properties": {
        "street": {"type": "string", "description": "Назва вулиці українською без слова «вулиця», порожньо якщо не вказана"},
        "house": {"type": "string", "description": "Номер будинку, порожньо якщо не вказаний"},
        "landmark": {"type": "string", "description": "Орієнтир, якщо вулиці немає: ТРЦ, парк, вокзал"},
        "commission": {"type": "string", "enum": ["немає", "половина", "повна", "невідомо"]},
        "utilities": {"type": "string", "enum": ["входить", "окремо", "невідомо"]},
        "utilities_estimate": {"type": "integer", "description": "Сума комуналки в гривнях, 0 якщо не названа"},
        "deposit_months": {"type": "integer", "description": "Депозит у місячних платежах, 0 якщо не названий"},
        "min_term_months": {"type": "integer", "description": "Мінімальний термін оренди в місяцях, 0 якщо не названий"},
        "pets_allowed": {"type": "string", "enum": ["так", "ні", "невідомо"]},
        "kids_allowed": {"type": "string", "enum": ["так", "ні", "невідомо"]},
        "backup_power": {"type": "string", "enum": ["є", "немає", "невідомо"],
                          "description": "Генератор, інвертор, EcoFlow, зарядна станція, автономне живлення"},
        "separate_rooms": {"type": "string", "enum": ["так", "ні", "невідомо"],
                            "description": "Чи кімнати роздільні, тобто чи можна жити по кімнатах"},
        "red_flags": {"type": "string", "description": "Стисло: що в тексті насторожує, або порожньо"},
    },
    "required": ["street", "commission", "utilities", "backup_power", "separate_rooms"],
}

PROMPT = """Ти розбираєш оголошення про оренду квартири у Львові.
Витягни факти ЛИШЕ з тексту. Нічого не вигадуй і не додумуй:
якщо чогось не написано, став «невідомо» або порожнє значення.

Заголовок: {title}

Опис:
{description}
"""


def enabled(cfg):
    return bool(cfg.get("llm", {}).get("gemini_key"))


def analyse(cfg, title, description, is_business=None):
    """Повертає dict за схемою або None, якщо не вдалось."""
    key = cfg["llm"]["gemini_key"]
    model = cfg["llm"]["model"]
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(title=title or "", description=(description or "")[:4000])}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
            "temperature": 0,
            # Витяг фактів із короткого опису не потребує міркувань, а
            # рахуються вони як вихідні токени за найвищою ставкою: без
            # цього рядка кожен виклик витрачав від трьохсот до семисот
            # токенів думання і коштував вчетверо дорожче за той самий
            # результат.
            "thinkingConfig": {"thinkingLevel": "low"},
        },
    }
    try:
        r = requests.post(ENDPOINT.format(model=model), params={"key": key}, json=body, timeout=60)
        if not r.ok:
            return None
        payload = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        data = json.loads(payload)
        _grounded(data, f"{title} {description}", is_business)
        return data
    except Exception:
        return None


# Підтвердження в тексті для кожного твердження. Модель схильна
# підставляти правдоподібне замість «невідомо» — дешева особливо, — а
# вигадана «комісія: немає» одразу занижує розрахунок вартості й додає
# балів. Тому кожне твердження мусить мати опору в самому описі.
EVIDENCE = {
    ("commission", "немає"): ("без комісі", "без комиссии", "комісії нема", "безкомісійн",
                               "без посередник", "від власника", "власник здає"),
    ("commission", "повна"): ("комісі", "комиссия"),
    ("commission", "половина"): ("комісі", "комиссия"),
    ("utilities", "входить"): ("включен", "входить", "все включ", "разом з комунал"),
    ("utilities", "окремо"): ("комунал", "окремо", "додатково", "лічильник"),
    ("backup_power", "є"): ("генератор", "інвертор", "ecoflow", "резервн", "акумулятор",
                             "зарядна станц", "безперебійн", "без електроенерг",
                             "не залежить від світл", "при відключенн"),
    ("pets_allowed", "ні"): ("тварин", "котів", "собак", "без домашніх"),
    ("kids_allowed", "ні"): ("дітей", "дітьми", "діти"),
}


def _grounded(data, text, is_business=None):
    """Викидає твердження, яких немає на що сперти в тексті.

    Опора — не лише слова опису. Якщо оголошення подав власник, а не
    агенція, «комісії немає» це обґрунтований висновок, а не вигадка:
    власники комісії не беруть.
    """
    low = (text or "").lower()
    dropped = []
    if is_business == 0 and data.get("commission") == "немає":
        return dropped
    for (field, value), markers in EVIDENCE.items():
        if data.get(field) != value:
            continue
        if not any(m in low for m in markers):
            data[field] = "невідомо"
            dropped.append(f"{field}={value}")
    for field in ("deposit_months", "min_term_months"):
        if data.get(field) and not any(m in low for m in
                                       ("депозит", "застав", "останній міс", "термін", "міс", "рік")):
            data[field] = 0
            dropped.append(field)
    return dropped


def _clean(value):
    value = (value or "").strip()
    return None if value in ("", "невідомо", "0") else value


def apply_to(ad, data):
    """Розкладає відповідь моделі по полях оголошення."""
    if not data:
        return ad

    # Вулиця — тільки якщо регекс не впорався; він точніший там, де спрацював
    if not ad.get("street"):
        street = _clean(data.get("street"))
        if street:
            house = _clean(data.get("house"))
            ad["street"] = f"{street}, {house}" if house else street
            ad["street_conf"] = "модель+буд" if house else "модель"
        elif _clean(data.get("landmark")):
            ad["landmark"] = _clean(data.get("landmark"))

    ad["commission"] = _clean(data.get("commission"))
    ad["utilities"] = _clean(data.get("utilities"))
    ad["deposit"] = str(data["deposit_months"]) if data.get("deposit_months") else None
    ad["min_term"] = str(data["min_term_months"]) if data.get("min_term_months") else None
    ad["backup_power"] = _clean(data.get("backup_power"))

    limits = []
    if data.get("pets_allowed") == "ні":
        limits.append("без тварин")
    if data.get("kids_allowed") == "ні":
        limits.append("без дітей")
    if data.get("min_term_months", 0) >= 12:
        limits.append(f"мінімум {data['min_term_months']} міс.")
    if _clean(data.get("red_flags")):
        limits.append(data["red_flags"].strip())
    ad["restrictions"] = "; ".join(limits) or None

    ad["_utilities_estimate"] = data.get("utilities_estimate") or 0
    ad["_separate_rooms"] = data.get("separate_rooms")
    ad["llm_done"] = 1
    return ad


def enrich_pending(conn, cfg, limit=40):
    """Проганяє через модель усе, що ще не розбиралось."""
    if not enabled(cfg):
        return 0, 0

    rows = conn.execute(
        """SELECT id, title, description, street, is_business FROM ads
           WHERE ifnull(llm_done, 0) = 0 AND rejected IS NULL AND dup_of IS NULL
           ORDER BY score DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    done = found_street = 0
    for row in rows:
        data = analyse(cfg, row["title"], row["description"], row["is_business"])
        if data is None:
            conn.execute("UPDATE ads SET llm_done = -1 WHERE id = ?", (row["id"],))
            continue
        ad = {"id": row["id"], "street": row["street"]}
        apply_to(ad, data)
        if ad.get("street") and not row["street"]:
            found_street += 1
        conn.execute(
            """UPDATE ads SET street = ?, street_conf = coalesce(?, street_conf),
                   commission = ?, utilities = ?, deposit = ?, min_term = ?,
                   restrictions = ?, backup_power = ?, llm_done = 1
               WHERE id = ?""",
            (ad.get("street"), ad.get("street_conf"), ad.get("commission"), ad.get("utilities"),
             ad.get("deposit"), ad.get("min_term"), ad.get("restrictions"),
             ad.get("backup_power"), row["id"]),
        )
        done += 1
    conn.commit()
    return done, found_street
