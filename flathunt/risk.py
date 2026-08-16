"""Ознаки можливого шахрайства.

Нічого не відсіює — тільки маркує. Рішення за людиною, але у картці
має бути видно, на що дивитись перед тим, як переказувати завдаток.
"""
import re
from datetime import datetime, timezone

YOUNG_ACCOUNT_DAYS = 90        # «акаунту лише кілька місяців»
VERY_YOUNG_ACCOUNT_DAYS = 21

# Класика оренди: ціна суттєво нижча за ринок + гроші наперед.
# Слова про бронювання самі по собі нічого не означають — «вільна,
# готова до бронювання та перегляду» це звичайне запрошення дивитись.
# Тому потрібен грошовий контекст поруч, інакше найкраща знахідка дня
# отримує лячне попередження ні за що.
PREPAY = ("завдаток", "передоплат", "предоплат", "аванс наперед",
          "закріпити за собою", "перерахувати на карт", "перекажіть",
          "оплата за бронь", "за бронювання", "внести бронь")
REMOTE = ("не можу показати", "перебуваю за кордоном", "виїхав за кордон",
          "покажу по відео", "огляд по відеозв", "ключі надішлю",
          "ключі передам через", "нова пошта ключі")
URGENCY = ("тільки сьогодні", "терміново здам", "перший хто", "залишилось кілька годин")


def _age_days(created):
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(created)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).days


def flags(ad, median_price_per_m2=None):
    """Повертає список коротких міток ризику."""
    out = []
    text = f"{ad.get('title') or ''} {ad.get('description') or ''}".lower()

    age = _age_days(ad.get("user_created"))
    if age is not None:
        if age < VERY_YOUNG_ACCOUNT_DAYS:
            out.append(f"акаунту {age} дн.")
        elif age < YOUNG_ACCOUNT_DAYS:
            out.append(f"акаунту {age // 30 or 1} міс.")

    if any(w in text for w in PREPAY):
        out.append("просить передоплату")
    if any(w in text for w in REMOTE):
        out.append("не показує особисто")
    if any(w in text for w in URGENCY):
        out.append("тисне терміновістю")

    # Занадто хороша ціна — головна принада в орендному скамі
    if median_price_per_m2 and ad.get("price") and ad.get("area"):
        own = ad["price"] / ad["area"]
        if own < median_price_per_m2 * 0.6:
            out.append("ціна нижча за ринок на 40%+")

    if not ad.get("photos"):
        out.append("без фото")

    # Телефон у тексті замість кнопки OLX — обхід модерації
    if re.search(r"\+?38?0\d{2}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", text):
        out.append("телефон у тексті")

    return out


def corpus_flags(conn):
    """Ознаки, які видно лише на всій вибірці, а не в окремому оголошенні.

    Головна з них: свіжий акаунт із кількома оголошеннями. Власник, який
    щойно зареєструвався, виставляє одну квартиру. Кілька за день з
    акаунта віком у добу — це або новий посередник, або схема, і в обох
    випадках варто дивитись уважніше.
    """
    rows = conn.execute(
        """SELECT user_id, count(*) n, min(user_created) created
           FROM ads WHERE user_id IS NOT NULL AND rejected IS NULL
           GROUP BY user_id HAVING n > 1"""
    ).fetchall()

    marked = 0
    for row in rows:
        age = _age_days(row["created"])
        if age is None or age >= YOUNG_ACCOUNT_DAYS:
            continue
        mark = f"акаунту {age} дн. і вже {row['n']} оголошення"
        for ad in conn.execute(
            "SELECT id, risk_flags FROM ads WHERE user_id = ? AND rejected IS NULL",
            (row["user_id"],),
        ).fetchall():
            if mark in (ad["risk_flags"] or ""):
                continue
            flags = [f for f in (ad["risk_flags"] or "").split("; ") if f and "акаунту" not in f]
            flags.append(mark)
            conn.execute(
                "UPDATE ads SET risk_flags = ?, risk_level = ?, score = max(0, score - 12) WHERE id = ?",
                ("; ".join(flags), "середній" if age > 3 else "високий", ad["id"]),
            )
            marked += 1
    conn.commit()
    return marked


def level(flag_list):
    """Загальний рівень: чим більше збігів, тим підозріліше."""
    if not flag_list:
        return None
    heavy = {"просить передоплату", "не показує особисто", "ціна нижча за ринок на 40%+"}
    if len(set(flag_list) & heavy) >= 2 or len(flag_list) >= 3:
        return "високий"
    if set(flag_list) & heavy:
        return "середній"
    return "низький"
