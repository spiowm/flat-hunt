"""Жорсткі відсіви та оцінка оголошення (0–100)."""

from . import layout, risk

GOOD_RENOVATION = ("євроремонт", "дизайнерськ", "авторськ", "після ремонту", "новий ремонт")
OK_RENOVATION = ("житловий стан", "косметичний")
WALKTHROUGH = ("прохідн", "суміжн", "смежн")
# Студія — це відкрите планування без окремих кімнат. Для спільної
# оренди, де кожен має свою кімнату, це майже завжди мимо. Але термін
# уживають вільно («квартира-студія» часто означає просто кухню-вітальню),
# тому не відсіюємо, а помітно знижуємо бал і пишемо в картку.
STUDIO = ("студія", "студіо", "-студія", "студийн", "відкрите планування")
SOLO_ONLY = ("для однієї особи", "для 1 особи", "для одної особи", "тільки для дівчин",
             "тільки дівчатам", "для дівчини", "лише для дівчат")
# Троє братів — не сім'я з дітьми. Такі оголошення відпадають одразу,
# інакше на них марнується дзвінок.
FAMILY_ONLY = ("тільки сімейні пари", "лише сімейні пари", "тільки сімейним",
               "лише сімейним", "сімейні пари з дітьми", "тільки для сім'ї",
               "лише для сім'ї", "тільки сім'ям", "для сімейної пари")

# «розглядають пару 1-особу» — двоє братів це не пара і не одна особа.
# Формулювань багато, і кожне наступне я знаходив уже після того, як
# воно з'їдало дзвінок.
COUPLE_OR_SOLO = ("пару 1-особу", "пару або 1 особу", "пара або одна особа",
                  "пару чи одну особу", "для пари або однієї особи",
                  "розглядають пару", "тільки пара", "лише пара",
                  "пара без дітей", "для пари")


def _text(ad):
    return f"{ad.get('title') or ''} {ad.get('description') or ''}".lower()


def reject_reason(ad, cfg):
    """Повертає причину відсіву або None."""
    text = _text(ad)

    # Звірка з поточним конфігом. Без неї база тримає оголошення,
    # зібрані за старими критеріями: у списку однокімнатних до
    # дванадцяти тисяч висіли варіанти по чотирнадцять, а трикімнатні
    # лишались живими після того, як конфігурацію прибрали.
    search = next((s for s in cfg["searches"] if s["key"] == ad.get("search_key")), None)
    if search is None:
        return "search config disabled"
    price = ad.get("price")
    if price is not None and not (search["price_min"] <= price <= search["price_max"]):
        return f"outside band {search['price_min']}-{search['price_max']}"
    if ad.get("rooms") and search.get("rooms_count") and ad["rooms"] != search["rooms_count"]:
        return "wrong room count"
    area = " ".join(filter(None, [ad.get("district"), ad.get("street"), ad.get("title")])).lower()

    for bad in cfg["exclude_areas"]:
        if bad.lower() in area or bad.lower() in text:
            return f"area: {bad}"

    for bad in cfg["exclude_text"]:
        if bad.lower() in text:
            return f"text: {bad}"

    walk = ad.get("walk_min")
    conf = ad.get("street_conf")
    # Точка за центром району нічого не доводить: Личаківський тягнеться
    # і на десять хвилин від Анни, і на годину. Така оцінка лише довідкова
    # й на відсів впливати не може.
    if walk is not None and conf != "район":
        exact = conf in ("вул+буд", "модель+буд")
        cap = cfg["walk_tiers"]["ok"] if exact else cfg["walk_tiers"].get("ok_street_only", 90)
        if walk > cap:
            return f"too far: {walk} min walk"
    dist = ad.get("dist_km")
    if walk is None and dist is not None and dist > cfg["tiers"]["ok"]:
        return f"too far: {dist} km"

    rooms = ad.get("rooms") or 0
    if rooms >= cfg["require_separate_layout_from_rooms"]:
        # Прохідна кімната більше не дискваліфікує. Вона незручна для
        # життя по окремих кімнатах, але це привід подивитись уважніше,
        # а не викинути: через цей відсів у топ не потрапила квартира
        # за двадцять одну хвилину від Анни, а таких там одиниці.
        if any(w in text for w in SOLO_ONLY):
            return "single occupant only"

    if rooms == 1 and any(w in text for w in SOLO_ONLY) and (ad.get("people") or 1) > 1:
        return "single occupant only"

    if any(w in text for w in FAMILY_ONLY):
        return "families with children only"

    if (ad.get("people") or 1) > 1 and any(w in text for w in COUPLE_OR_SOLO):
        return "couple or single person only"

    age_cfg = cfg.get("account_age") or {}
    floor_days = age_cfg.get("reject_below_days")
    if floor_days:
        age = risk._age_days(ad.get("user_created"))
        if age is not None and age < floor_days:
            return f"seller account {age} days old — too fresh"

    cap = cfg.get("max_days_listed")
    if cap and (ad.get("days_listed") or 0) > cap:
        return f"listed {ad['days_listed']} days — bait, not housing"

    # Те, що витягла модель з опису: гуртожиток квартирою не є
    haystack = f"{text} {(ad.get('restrictions') or '').lower()}"
    for bad in cfg.get("exclude_llm") or []:
        if bad.lower() in haystack:
            return f"description: {bad}"

    return None


def real_cost(ad, cfg):
    """Скільки це насправді коштує на особу щомісяця в перший рік.

    Оголошення «17 500 все включено» і «17 500 + комуналка + комісія»
    у списку виглядають однаково, хоча різниця відчутна.
    """
    price = ad.get("price")
    if not price:
        return None
    total = float(price)

    if (ad.get("utilities") or "") != "входить":
        table = cfg.get("utilities_estimate") or {}
        total += float(table.get(ad.get("rooms") or 1, 2000))

    # комісія розкидана на рік
    commission = ad.get("commission")
    if commission == "повна":
        total += price / 12
    elif commission == "половина":
        total += price / 24
    elif commission is None and ad.get("is_business"):
        total += price / 24      # агенція без явної згадки: припускаємо пів ставки

    return round(total / max(1, ad.get("people") or 1))


def tier_of(ad, cfg):
    """Тір за хвилинами ходьби, а якщо маршруту немає — за прямою лінією."""
    walk = ad.get("walk_min")
    if walk is not None:
        t = cfg["walk_tiers"]
        if walk <= t["ideal"]:
            return "ideal"
        if walk <= t["good"]:
            return "good"
        if walk <= t["ok"]:
            return "far"
        return "out"

    dist = ad.get("dist_km")
    if dist is None:
        return "unknown"
    t = cfg["tiers"]
    if dist <= t["ideal"]:
        return "ideal"
    if dist <= t["good"]:
        return "good"
    if dist <= t["ok"]:
        return "far"
    return "out"


def score(ad, cfg):
    """0–100. Головні ваги: відстань і ціна на особу."""
    pts = 0
    text = _text(ad)

    if ad.get("street_conf") == "район":
        # бал як за невідому адресу, а не як за точну далеку
        tier = "unknown"
    else:
        tier = tier_of(ad, cfg)
    pts += {"ideal": 40, "good": 30, "far": 15}.get(
        tier, 12 if "центр" in text else 6)

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

    if ad.get("street_conf") in ("вул+буд", "модель+буд"):
        pts += 4

    if (ad.get("backup_power") or "") == "є":
        pts += 4
    if (ad.get("commission") or "") == "немає":
        pts += 5
    if (ad.get("utilities") or "") == "входить":
        pts += 4

    if (ad.get("rooms") or 0) >= 2 and any(w in text for w in STUDIO):
        pts -= 12

    # Планування. Для трикімнатної це головне питання: сенс у ній лише
    # тоді, коли кімнати ізольовані. Здогад за площею карається легше
    # за доведену прохідність — він усе-таки здогад.
    verdict = layout.status(ad, cfg)
    if verdict == layout.NO:
        pts -= 20
    elif verdict == layout.MAYBE_NO:
        pts -= 10
    elif verdict == layout.YES and (ad.get("rooms") or 0) >= 3:
        pts += 8

    # Довго висить — ринок його вже подивився й не взяв
    days = ad.get("days_listed") or 0
    if days >= 30:
        pts -= 8
    elif days >= 14:
        pts -= 4
    if (ad.get("price_drop") or 0) > 0:
        pts += 5      # власник готовий рухатись у ціні

    haystack = f"{text} {(ad.get('restrictions') or '').lower()}"
    for phrase, penalty in (cfg.get("penalise_llm") or {}).items():
        if phrase.lower() in haystack:
            pts -= penalty

    return max(0, min(100, pts))


def evaluate(ad, cfg, median_ppm=None):
    """Проставляє rejected / tier / score / ознаки ризику."""
    ad["rejected"] = reject_reason(ad, cfg)
    ad["tier"] = tier_of(ad, cfg)
    ad["score"] = 0 if ad["rejected"] else score(ad, cfg)

    verdict = layout.status(ad, cfg)
    ad["layout_status"] = verdict
    if verdict in (layout.NO, layout.MAYBE_NO):
        note = layout.WORD[verdict]
        existing = ad.get("restrictions")
        if note not in (existing or ""):
            ad["restrictions"] = "; ".join(filter(None, [existing, note]))

    if (ad.get("rooms") or 0) >= 2 and any(w in _text(ad) for w in STUDIO):
        note = "студія — окремих кімнат може не бути"
        existing = ad.get("restrictions")
        if note not in (existing or ""):
            ad["restrictions"] = "; ".join(filter(None, [existing, note]))

    marks = risk.flags(ad, median_ppm)
    ad["risk_flags"] = "; ".join(marks) or None
    ad["risk_level"] = risk.level(marks)
    # Підозріле не відсіюємо — рішення за людиною, але вниз списку опускаємо
    if ad["risk_level"] == "high":
        ad["score"] = max(0, ad["score"] - 20)
    elif ad["risk_level"] == "medium":
        ad["score"] = max(0, ad["score"] - 8)

    ad["real_cost"] = real_cost(ad, cfg)

    if ad.get("lat") and ad.get("lon"):
        ad["map_url"] = f"https://www.google.com/maps/search/?api=1&query={ad['lat']},{ad['lon']}"
    return ad
