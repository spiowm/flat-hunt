"""Tests for the bug classes that actually happened.

Every case here is a regression that reached production and cost either
a missed flat or a wrong recommendation. They are written against the
failure, not against the happy path.
"""

import pytest

from flathunt import geo, llm, lun, score, store


@pytest.fixture()
def conn(tmp_path):
    c = store.connect(tmp_path / "test.db")
    yield c
    c.close()


@pytest.fixture()
def cfg():
    return {
        "anchor": {"lat": 49.8431, "lon": 24.0149},
        "walk_tiers": {"ideal": 20, "good": 35, "ok": 55, "ok_street_only": 90},
        "tiers": {"ideal": 1.5, "good": 2.5, "ok": 4.0},
        "searches": [
            {"key": "r1", "rooms": "odnokomnatnye", "rooms_count": 1, "people": 1,
             "price_min": 7000, "price_max": 12000},
            {"key": "r2", "rooms": "dvuhkomnatnye", "rooms_count": 2, "people": 2,
             "price_min": 13000, "price_max": 20000},
        ],
        "exclude_areas": ["Сихівський", "Левандівка"],
        "exclude_text": ["подобово"],
        "exclude_llm": ["гуртожит"],
        "penalise_llm": {"пічне опалення": 15},
        "require_separate_layout_from_rooms": 2,
        "max_days_listed": 365,
        "account_age": {"reject_below_days": 3},
        "utilities_estimate": {1: 1800, 2: 2400},
    }


def base_ad(**over):
    ad = {"id": "olx:1", "source": "olx", "ext_id": "1", "search_key": "r1",
          "people": 1, "price": 10000, "rooms": 1, "area": 30.0}
    ad.update(over)
    return ad


# --------------------------------------------------------------------------
# The collector used to wipe enrichment it knows nothing about
# --------------------------------------------------------------------------

def test_collector_pass_does_not_erase_enrichment(conn):
    store.upsert(conn, base_ad(street="Менцинського, 3", street_conf="модель+буд",
                               walk_min=9, phone="380670000000"))
    conn.commit()
    # a fresh collector object knows none of that
    store.upsert(conn, base_ad(street=None, street_conf=None, walk_min=None, phone=None))
    conn.commit()

    row = conn.execute("SELECT * FROM ads WHERE id = 'olx:1'").fetchone()
    assert row["street"] == "Менцинського, 3"
    assert row["walk_min"] == 9
    assert row["phone"] == "380670000000"


def test_clearable_fields_still_reset(conn):
    store.upsert(conn, base_ad(rejected="too far", risk_level="high", dup_of="olx:9"))
    conn.commit()
    store.upsert(conn, base_ad(rejected=None, risk_level=None))
    conn.commit()

    row = conn.execute("SELECT * FROM ads WHERE id = 'olx:1'").fetchone()
    assert row["rejected"] is None, "a lifted rejection must be written"
    assert row["risk_level"] is None
    assert row["dup_of"] == "olx:9", "dup_of is decided elsewhere and must survive"


# --------------------------------------------------------------------------
# Disappearance detection wiped a whole new source
# --------------------------------------------------------------------------

def test_missing_counter_is_scoped_to_source(conn):
    store.upsert(conn, base_ad(id="olx:1", source="olx"))
    store.upsert(conn, base_ad(id="lun:1", source="rieltor.ua"))
    conn.commit()

    # an OLX pass that saw only its own ad must not blame the LUN one
    for _ in range(3):
        store.mark_missing(conn, "r1", [base_ad(id="olx:1", source="olx")])
    conn.commit()

    lun_ad = conn.execute("SELECT misses FROM ads WHERE id = 'lun:1'").fetchone()
    assert (lun_ad["misses"] or 0) == 0


def test_ad_absent_twice_is_marked_gone(conn):
    store.upsert(conn, base_ad(id="olx:1", source="olx"))
    store.upsert(conn, base_ad(id="olx:2", source="olx"))
    conn.commit()
    for _ in range(2):
        store.mark_missing(conn, "r1", [base_ad(id="olx:1", source="olx")])
    conn.commit()

    rows = {r["id"]: r["misses"] or 0 for r in conn.execute("SELECT id, misses FROM ads")}
    assert rows["olx:1"] == 0
    assert rows["olx:2"] >= store.MISS_LIMIT


def test_returning_ad_clears_the_miss(conn):
    store.upsert(conn, base_ad(id="olx:2", source="olx"))
    conn.commit()
    store.mark_missing(conn, "r1", [base_ad(id="olx:1", source="olx")])
    store.mark_missing(conn, "r1", [base_ad(id="olx:2", source="olx")])
    conn.commit()
    row = conn.execute("SELECT misses FROM ads WHERE id = 'olx:2'").fetchone()
    assert (row["misses"] or 0) == 0


# --------------------------------------------------------------------------
# Deduplication used to throw away what the copy knew
# --------------------------------------------------------------------------

def test_canonical_inherits_missing_fields_from_duplicate(conn):
    store.upsert(conn, base_ad(id="olx:1", source="olx", url="https://x/1",
                               street="Зелена", street_conf="вул", lat=49.8, lon=24.0))
    store.upsert(conn, base_ad(id="lun:1", source="olx.ua", url="https://x/1",
                               phone="380670000000", photo_rooms="кухня, санвузол"))
    conn.commit()
    store.sweep_by_url(conn)
    conn.commit()

    can = conn.execute("SELECT * FROM ads WHERE id = 'olx:1'").fetchone()
    assert can["phone"] == "380670000000"
    assert can["photo_rooms"] == "кухня, санвузол"


def test_exact_coordinates_replace_a_street_level_guess(conn):
    store.upsert(conn, base_ad(id="olx:1", source="olx", url="https://x/1",
                               street="Зелена", street_conf="вул",
                               lat=49.80, lon=24.00, dist_km=5.0, walk_min=90))
    store.upsert(conn, base_ad(id="lun:1", source="olx.ua", url="https://x/1",
                               street="Зелена, 5", street_conf="вул+буд",
                               lat=49.845, lon=24.016))
    conn.commit()
    store.sweep_by_url(conn)
    conn.commit()

    can = conn.execute("SELECT * FROM ads WHERE id = 'olx:1'").fetchone()
    assert can["street_conf"] == "вул+буд"
    assert can["lat"] == pytest.approx(49.845)
    assert can["dist_km"] is None, "distance must be recomputed from the better point"


def test_owner_listing_wins_over_agency_copy(conn):
    store.upsert(conn, base_ad(id="olx:1", source="olx", is_business=1,
                               street="Зелена", area=30.0, floor=2, first_seen="2026-01-01"))
    store.upsert(conn, base_ad(id="olx:2", source="olx", is_business=0,
                               street="Зелена", area=30.0, floor=2, first_seen="2026-02-01"))
    conn.commit()
    store.sweep_duplicates(conn)
    conn.commit()

    rows = {r["id"]: r["dup_of"] for r in conn.execute("SELECT id, dup_of FROM ads")}
    assert rows["olx:2"] is None, "the owner listing must stay canonical"
    assert rows["olx:1"] == "olx:2"


# --------------------------------------------------------------------------
# The database was never validated against the current config
# --------------------------------------------------------------------------

def test_price_outside_the_current_band_is_rejected(cfg):
    ad = base_ad(price=14000, search_key="r1")
    assert "outside band" in (score.reject_reason(ad, cfg) or "")


def test_removed_search_config_is_rejected(cfg):
    ad = base_ad(search_key="r3", price=25000, rooms=3)
    assert score.reject_reason(ad, cfg) == "search config disabled"


def test_fresh_seller_account_is_rejected(cfg):
    from datetime import datetime, timezone
    ad = base_ad(user_created=datetime.now(timezone.utc).isoformat())
    assert "too fresh" in (score.reject_reason(ad, cfg) or "")


def test_district_point_never_triggers_a_distance_rejection(cfg):
    ad = base_ad(walk_min=120, street_conf="район")
    assert score.reject_reason(ad, cfg) is None
    ad_precise = base_ad(walk_min=120, street_conf="вул+буд")
    assert "too far" in (score.reject_reason(ad_precise, cfg) or "")


# --------------------------------------------------------------------------
# Street parsing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("declined,nominative", [
    ("Садовій", "Садова"), ("Замарстинівській", "Замарстинівська"),
    ("Личаківській", "Личаківська"), ("Пороховій", "Порохова"),
])
def test_locative_street_is_converted_back(declined, nominative):
    assert geo.undecline(declined) == nominative


def test_nominative_street_is_left_alone():
    assert geo.undecline("Наливайка") is None


def test_street_type_is_preserved():
    _, _, _, kind = geo.extract_street("Оренда 1 кім на пр. Шевченка. Центр", "")
    assert kind == "проспект", "avenue and street are different places"


def test_stored_address_is_used_when_the_regex_fails():
    assert geo._split_stored("Менцинського, 3") == ("Менцинського", "3")


# --------------------------------------------------------------------------
# LUN parsing
# --------------------------------------------------------------------------

def test_lun_address_accepts_only_its_own_format():
    assert lun._street({"header": "Варшавська вул., 201-А"}) == ("Варшавська, 201-А", "вул+буд")
    # a title from another site must never land in the street column
    assert lun._street({"header": "Вільна 2 кімнатна квартира у центрі"}) == (None, None)


def test_lun_price_is_converted_to_hryvnia(monkeypatch):
    monkeypatch.setattr(lun, "_rates", {"uah": 1.0, "usd": 40.0})
    assert lun._price_uah({"price": 500, "currency": "usd"}) == 20000
    assert lun._price_uah({"price": 15000, "currency": "uah"}) == 15000


def test_lun_commission_is_read_from_the_rate():
    assert lun._commission({"withoutCommission": True}) == "немає"
    assert lun._commission({"commissionRate": 100, "commissionType": "%"}) == "повна"
    assert lun._commission({"commissionRate": 50, "commissionType": "%"}) == "половина"


# --------------------------------------------------------------------------
# Model claims must be grounded in the text
# --------------------------------------------------------------------------

def test_unsupported_claims_are_dropped():
    data = {"commission": "немає", "utilities": "входить", "backup_power": "є"}
    llm._grounded(data, "Затишна квартира в центрі, поруч парк.")
    assert data["commission"] == "невідомо"
    assert data["utilities"] == "невідомо"
    assert data["backup_power"] == "невідомо"


def test_supported_claims_survive():
    data = {"commission": "немає", "utilities": "окремо", "backup_power": "є"}
    llm._grounded(data, "Без комісії. Комунальні окремо за лічильником. Є генератор.")
    assert data == {"commission": "немає", "utilities": "окремо", "backup_power": "є"}


def test_owner_listing_justifies_no_commission():
    data = {"commission": "немає"}
    llm._grounded(data, "Здається квартира в центрі.", is_business=0)
    assert data["commission"] == "немає", "an owner charges no agency fee by definition"


# --------------------------------------------------------------------------
# Liveness check
# --------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text


def test_removed_listing_is_detected(monkeypatch):
    from flathunt import alive
    monkeypatch.setattr(alive.http, "get", lambda *a, **k: FakeResponse(410))
    assert alive.check("https://x/1") is False


def test_live_page_is_not_killed_by_ui_translations(monkeypatch):
    """OLX ships its whole translation dictionary into every page, so the
    phrase about a removed listing appears on live pages too."""
    from flathunt import alive
    body = 'listing.removed":"Оголошення більше не доступне"'
    monkeypatch.setattr(alive.http, "get", lambda *a, **k: FakeResponse(200, body))
    assert alive.check("https://x/1") is True


def test_network_failure_keeps_the_listing(monkeypatch):
    from flathunt import alive

    def boom(*a, **k):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(alive.http, "get", boom)
    assert alive.check("https://x/1") is None, "unknown must not hide a good listing"


def test_booking_alone_is_not_a_prepayment_demand():
    from flathunt import risk
    flags = risk.flags({"title": "", "description": "Вільна, готова до бронювання та перегляду."})
    assert "просить передоплату" not in flags


# --------------------------------------------------------------------------
# DIM.RIA: every field below was mapped wrong before the API was ever called
# --------------------------------------------------------------------------

# Скорочений реальний відповідь info/34701240, знята з живого API
RIA_ITEM = {
    "realty_id": 34701240,
    "beautiful_url": "realty-dolgosrochnaya-arenda-kvartira-lvov-galitskiy-sheptitskih-ulitsa-34701240.html",
    "street_name_uk": "вул. Шептицьких",
    "building_number_str": "",
    "building_number_for_map": 40,
    "is_show_building_no": 1,
    "latitude": 49.8370255, "longitude": 24.00674488, "radius_location": 0,
    "price": 18000, "price_total": 18000, "currency_type_id": 3,
    "priceArr": {"1": "401", "2": "343", "3": "18 000"},
    "rooms_count": 2, "total_square_meters": 52, "floor": 1, "floors_count": 3,
    "description_uk": "Оренда 2 кім.квартири по вул.Шептицьких, дві ізольовані кімнати.",
    "publishing_date": "2026-08-20 13:09:44", "photos_count": 8,
    "main_photo": "dom/photo/34120/3412076/341207652/341207652.jpg",
    "city_name_uk": "Львів", "district_name_uk": "Галицький", "user_id": 17369909,
}

RIA_SEARCH = {"key": "r2", "rooms_count": 2, "people": 2,
              "price_min": 13000, "price_max": 20000}


def test_ria_house_number_comes_from_the_map_field():
    """building_number_str порожній навіть там, де номер відомий."""
    from flathunt import dimria
    assert dimria._street(RIA_ITEM) == ("Шептицьких, 40", "вул+буд")


def test_ria_street_type_prefix_is_stripped():
    """«вул. Шептицьких» у полі адреси зламало б дедуплікацію з ЛУНом."""
    from flathunt import dimria
    street, _ = dimria._street({"street_name_uk": "просп. Шевченка"})
    assert street == "Шевченка"


def test_ria_price_is_read_in_hryvnia():
    """priceUAH, на яке спирався попередній код, в API не існує."""
    from flathunt import dimria
    assert dimria._price_uah(RIA_ITEM) == 18000
    # у доларовому оголошенні price — це долари, а гривня лежить у priceArr
    assert dimria._price_uah({"price": 450, "currency_type_id": 1,
                              "priceArr": {"1": "450", "3": "19 000"}}) == 19000


def test_ria_blurred_coordinates_are_refused():
    """Ненульовий радіус — це не адреса будинку, а пляма."""
    from flathunt import dimria
    assert dimria._coords(RIA_ITEM) == (49.8370255, 24.00674488)
    assert dimria._coords({**RIA_ITEM, "radius_location": 500}) == (None, None)


def test_ria_photo_url_is_built_from_the_slug():
    """Шлях із main_photo веде на 415: CDN хоче слаґ оголошення."""
    from flathunt import dimria
    url = dimria._photo_url(RIA_ITEM)
    assert url == ("https://cdn.riastatic.com/photosnew/dom/photo/"
                   "dolgosrochnaya-arenda-kvartira-lvov-galitskiy-sheptitskih-ulitsa"
                   "__341207652fl.jpg")


def test_ria_listing_normalizes_into_the_common_schema():
    from flathunt import dimria
    ad = dimria.normalize(RIA_ITEM, RIA_SEARCH)
    assert ad["id"] == "dimria:34701240"
    assert ad["url"].startswith("https://dom.ria.com/uk/realty-")
    assert ad["price"] == 18000 and ad["rooms"] == 2
    assert ad["street"] == "Шептицьких, 40"
    assert ad["is_business"] == 0, "оголошення без agency_id — від власника"
    assert dimria._matches(ad, RIA_SEARCH)


def test_ria_publishing_date_is_parseable():
    """Дата приходить без T, і risk._age_days мусить її прийняти."""
    from flathunt import dimria, risk
    ad = dimria.normalize(RIA_ITEM, RIA_SEARCH)
    assert risk._age_days(ad["created_time"]) is not None


# --------------------------------------------------------------------------
# The quota ledger: 1000 requests a month is easy to burn in a day
# --------------------------------------------------------------------------

def test_quota_is_shared_across_both_limits(conn):
    from flathunt import budget
    budget.charge(conn, "dimria", 10)
    assert budget.allowance(conn, "dimria", monthly=900, hourly=25) == 15
    budget.charge(conn, "dimria", 15)
    assert budget.allowance(conn, "dimria", monthly=900, hourly=25) == 0


def test_monthly_cap_wins_when_it_is_the_tighter_one(conn):
    """Витрачене раніше цього місяця вже не тисне на годинний ліміт,
    але місячний з'їдає — саме так квота й закінчується."""
    import uuid
    from datetime import timedelta

    from flathunt import budget
    earlier = (budget._now() - timedelta(days=3)).isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO api_spend (uid, service, ts) VALUES (?, 'dimria', ?)",
        [(uuid.uuid4().hex, earlier) for _ in range(898)])
    conn.commit()
    assert budget.allowance(conn, "dimria", monthly=900, hourly=25) == 2


def test_spending_of_another_machine_counts(conn):
    """Ключ один на VPS і на ноутбук — витрати мусять складатись."""
    from flathunt import budget
    budget.charge(conn, "dimria", 5)
    budget.charge(conn, "dimria", 5)
    assert budget.spent(conn, "dimria", budget._month_start()) == 10


def test_collector_waits_out_its_interval(conn):
    from flathunt import budget
    assert budget.due(conn, "dimria", every_hours=6), "перший запуск дозволено"
    budget.charge(conn, "dimria")
    assert not budget.due(conn, "dimria", every_hours=6)


def test_collector_stops_at_the_wallet_limit(conn, cfg, monkeypatch):
    """Вичерпана квота зупиняє прохід, а не блокує ключ."""
    from flathunt import dimria
    cfg["dimria"] = {"monthly_cap": 900, "hourly_cap": 3, "delay": 0,
                     "per_pass_details": 20}
    monkeypatch.setenv("RIA_API_KEY", "x")
    calls = []
    monkeypatch.setattr(dimria, "_session", lambda: None)
    monkeypatch.setattr(dimria, "search_ids",
                        lambda s, k, w, search: (w.take(), calls.append("search"),
                                                 ["1", "2", "3", "4"])[2])
    monkeypatch.setattr(dimria, "fetch_one",
                        lambda s, k, w, rid: (w.take(), calls.append(rid), RIA_ITEM)[2])

    ads, live = dimria.collect(cfg["searches"], cfg, conn)
    assert len(calls) == 3, f"витрачено рівно ліміт, а не більше: {calls}"
    assert live, "живі ідентифікатори з успішного пошуку лишаються"


def test_known_listing_is_never_fetched_twice(conn, cfg, monkeypatch):
    """Квартира не переїжджає — платити за неї вдруге немає за що."""
    from flathunt import dimria
    store.upsert(conn, base_ad(id="dimria:34701240", source="dimria",
                               ext_id="34701240", search_key="r2"))
    conn.commit()
    cfg["dimria"] = {"monthly_cap": 900, "hourly_cap": 25, "delay": 0,
                     "per_pass_details": 20}
    monkeypatch.setenv("RIA_API_KEY", "x")
    fetched = []
    monkeypatch.setattr(dimria, "_session", lambda: None)
    monkeypatch.setattr(dimria, "search_ids",
                        lambda s, k, w, search: (w.take(), ["34701240"])[1])
    monkeypatch.setattr(dimria, "fetch_one",
                        lambda s, k, w, rid: (w.take(), fetched.append(rid), RIA_ITEM)[2])

    dimria.collect(cfg["searches"], cfg, conn)
    assert fetched == [], "вже відоме оголошення не коштує жодного запиту"


def test_disappearance_needs_no_detail_requests(conn, cfg, monkeypatch):
    """Пошук і так вертає всі живі id — за зникнення платити не треба."""
    from flathunt import dimria
    cfg["dimria"] = {"monthly_cap": 900, "hourly_cap": 25, "delay": 0,
                     "per_pass_details": 0}
    monkeypatch.setenv("RIA_API_KEY", "x")
    monkeypatch.setattr(dimria, "_session", lambda: None)
    monkeypatch.setattr(dimria, "search_ids",
                        lambda s, k, w, search: (w.take(), ["777"])[1])

    _, live = dimria.collect(cfg["searches"], cfg, conn)
    stub = live["r1"][0]
    assert stub["id"] == "dimria:777" and stub["source"] == "dimria"


# --------------------------------------------------------------------------
# A source that knows the exact building must not be overruled by a regex
# --------------------------------------------------------------------------

class _Cfg(dict):
    pass


@pytest.fixture()
def geocfg(cfg):
    cfg["geo"] = {"walk_speed_kmh": 4.5}
    return cfg


def test_source_coordinates_survive_text_extraction(conn, geocfg, monkeypatch):
    """ЛУН дає точку будинку; регекс із опису тягнув її за кілометри."""
    from flathunt import geo
    monkeypatch.setattr(geo, "walk_route", lambda *a, **k: (12, 0.9))
    called = []
    monkeypatch.setattr(geo, "geocode", lambda *a, **k: called.append(a) or (49.0, 24.9))

    ad = base_ad(street="Кульпарківська", street_conf="вул",
                 lat=49.81252, lon=23.98302, coords_from_source=True,
                 title="Оренда на Стуса", description="поруч вул. Патона")
    geo.locate(conn, ad, geocfg)

    assert (ad["lat"], ad["lon"]) == (49.81252, 23.98302)
    assert ad["street"] == "Кульпарківська"
    assert called == [], "геокодер не потрібен, коли точка вже відома"
    assert ad["walk_min"] == 12


def test_source_point_without_address_keeps_the_point(conn, geocfg, monkeypatch):
    """Картка ЛУНа з чужого сайту має координати, але замість адреси
    заголовок. Назву з тексту беремо як підпис, точку не рухаємо."""
    from flathunt import geo
    monkeypatch.setattr(geo, "walk_route", lambda *a, **k: (30, 2.2))
    ad = base_ad(street=None, street_conf=None, lat=49.81929, lon=24.04196,
                 coords_from_source=True, title="Квартира на вул. Стуса, 5",
                 description="")
    geo.locate(conn, ad, geocfg)

    assert (ad["lat"], ad["lon"]) == (49.81929, 24.04196)
    assert ad["street"] == "Стуса, 5"
    assert ad["street_conf"] == "вул"


def test_olx_still_geocodes_from_text(conn, geocfg, monkeypatch):
    """Без координат джерела все лишається як було."""
    from flathunt import geo
    monkeypatch.setattr(geo, "walk_route", lambda *a, **k: (20, 1.5))
    monkeypatch.setattr(geo, "geocode", lambda *a, **k: (49.8431, 24.0149))
    ad = base_ad(title="Оренда на вул. Зеленій, 12", description="")
    geo.locate(conn, ad, geocfg)
    assert ad["lat"] == 49.8431 and ad["street_conf"] == "вул+буд"


def test_both_coordinate_sources_declare_themselves():
    """Прапорець ставлять саме ті збирачі, що дають точку будинку."""
    from flathunt import dimria, lun
    ria = dimria.normalize(RIA_ITEM, RIA_SEARCH)
    assert ria["coords_from_source"] is True
    card = {"id": 1, "location": [24.0, 49.8], "header": "Варшавська вул., 201-А",
            "price": 10000, "currency": "uah", "roomCount": 1}
    assert lun.normalize(card, RIA_SEARCH)["coords_from_source"] is True


# --------------------------------------------------------------------------
# A shallow LUN pass sees three pages out of a hundred listings
# --------------------------------------------------------------------------

def test_partial_pass_must_not_declare_the_rest_gone(conn):
    """Неглибокий обхід — не підстава ховати решту джерела.

    Глибокий обхід ЛУНа робиться раз на годину, решта проходів беруть
    три сторінки. Двох таких проходів вистачало, щоб усе, що не влізло
    в перші сторінки, перейшло поріг і зникло з розсилки на пʼятдесят
    хвилин щогодини.
    """
    for i in range(10):
        store.upsert(conn, base_ad(id=f"lun:{i}", source="rieltor.ua", search_key="r1"))
    conn.commit()

    shallow = [base_ad(id="lun:0", source="rieltor.ua", search_key="r1")]
    for _ in range(3):
        store.mark_missing(conn, "r1", shallow)
    conn.commit()

    hidden = conn.execute(
        f"SELECT count(*) n FROM ads a WHERE NOT ({store.gone_clause('a')})").fetchone()["n"]
    assert hidden == 9, "саме цю поведінку й треба обходити на неглибокому проході"


def test_each_search_band_is_counted_separately(conn):
    """У пакеті ЛУНа перемішані обидві конфігурації, а бралась перша."""
    store.upsert(conn, base_ad(id="lun:a", source="rieltor.ua", search_key="r1"))
    store.upsert(conn, base_ad(id="lun:b", source="rieltor.ua", search_key="r2"))
    conn.commit()

    batch = [base_ad(id="lun:a", source="rieltor.ua", search_key="r1"),
             base_ad(id="lun:b", source="rieltor.ua", search_key="r2")]
    by_key = {}
    for ad in batch:
        by_key.setdefault(ad["search_key"], []).append(ad)
    for key, part in by_key.items():
        store.mark_missing(conn, key, part)
    conn.commit()

    rows = {r["id"]: (r["misses"] or 0) for r in conn.execute("SELECT id, misses FROM ads")}
    assert rows == {"lun:a": 0, "lun:b": 0}, "обидві конфігурації бачили свої оголошення"


# --------------------------------------------------------------------------
# Isolated rooms: the whole point of a three-room flat shared with brothers
# --------------------------------------------------------------------------

def test_olx_planning_field_is_actually_read():
    """Ключ називається layout. Код шукав flat_planning і planning,
    яких в OLX немає, тож колонка стояла порожня у всій базі."""
    from flathunt import olx
    raw = {"id": 1, "params": [{"key": "layout", "value": "Роздільна"},
                               {"key": "total_area", "value": "95 м²"}]}
    assert olx._params_map(raw)["layout"] == "Роздільна"
    ad = olx.normalize(raw, {"key": "r3", "rooms": "trehkomnatnye", "people": 3})
    assert ad["layout"] == "Роздільна", "нормалізація мусить донести поле до бази"


def test_planning_field_decides_before_anything_else(cfg):
    from flathunt import layout
    assert layout.status(base_ad(rooms=3, area=90, layout="Суміжна, прохідна"), cfg) == "ні"
    assert layout.status(base_ad(rooms=3, area=50, layout="Роздільна"), cfg) == "так"


def test_open_plan_is_not_separate_rooms(cfg):
    """«Вільне планування» — порожня коробка без стін, а не готові кімнати."""
    from flathunt import layout
    assert layout.status(base_ad(rooms=3, area=90, layout="Вільне планування"), cfg) == "ні"


def test_model_verdict_is_used_when_the_field_is_empty(cfg):
    from flathunt import layout
    ad = base_ad(rooms=3, area=90, layout=None, separate_rooms="ні")
    assert layout.status(ad, cfg) == "ні"


def test_small_three_room_is_guessed_walkthrough(cfg):
    """До 65 м² прохідних 57-60%, від 65 м² роздільних 79-88%."""
    from flathunt import layout
    assert layout.status(base_ad(rooms=3, area=54, layout=None), cfg) == "мабуть ні"
    assert layout.status(base_ad(rooms=3, area=80, layout=None), cfg) is None


def test_area_guess_is_not_applied_to_two_room(cfg):
    """Для двокімнатних залежності площі й планування немає."""
    from flathunt import layout
    assert layout.status(base_ad(rooms=2, area=40, layout=None), cfg) is None


def test_guess_and_fact_are_scored_differently(cfg):
    from flathunt import score
    fact = base_ad(rooms=3, area=90, price=25000, search_key="r3",
                   people=3, layout="Суміжна, прохідна")
    guess = base_ad(rooms=3, area=54, price=25000, search_key="r3",
                    people=3, layout=None)
    clean = base_ad(rooms=3, area=90, price=25000, search_key="r3",
                    people=3, layout="Роздільна")
    cfg["searches"].append({"key": "r3", "rooms": "trehkomnatnye", "rooms_count": 3,
                            "people": 3, "price_min": 20000, "price_max": 30000})
    for ad in (fact, guess, clean):
        score.evaluate(ad, cfg)
    assert clean["score"] > guess["score"] > fact["score"]


def test_walkthrough_is_flagged_but_never_rejected(cfg):
    """Відсіювати прохідні жорстко не можна — так уже губився
    найближчий до Анни варіант."""
    from flathunt import score
    cfg["searches"].append({"key": "r3", "rooms": "trehkomnatnye", "rooms_count": 3,
                            "people": 3, "price_min": 20000, "price_max": 30000})
    ad = base_ad(rooms=3, area=90, price=25000, search_key="r3", people=3,
                 layout="Суміжна, прохідна", walk_min=15, street_conf="вул+буд")
    score.evaluate(ad, cfg)
    assert ad["rejected"] is None
    assert "прохідні" in (ad["restrictions"] or "")


def test_card_puts_the_layout_next_to_the_room_count():
    from flathunt import notify
    text = notify.card(base_ad(rooms=3, area=90, price=25000, street="Зелена, 5",
                               layout_status="ні", people=3))
    first = text.splitlines()[0]
    assert "3-кімнатна" in first and "прохідні" in first


def test_abbreviated_title_in_street_name(tmp_path):
    """«вул.Кн.Ольги» давало назву «Кн»: регекс обривався на крапці,
    і така картка вже пішла в розсилку."""
    from flathunt import geo
    assert geo.extract_street("Оренда по вул.Кн.Ольги-Л. Кобилиці", "")[0] == "Княгині Ольги"
    assert geo.extract_street("вул. Св.Юра, 5", "")[:2] == ("Святого Юра", "5")


def test_hyphenated_street_survives_the_initial_trim():
    """Обрізати треба лише ініціал, а не другу частину назви."""
    from flathunt import geo
    assert geo.extract_street("вул. Шолом-Алейхема, 12", "")[0] == "Шолом-Алейхема"


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _geo_cfg():
    return {"anchor": {"lat": 49.8431, "lon": 24.0149},
            "geo": {"min_interval": 0, "nominatim_url": "http://x",
                    "contact_email": "a@b.c"}}


def test_geocoder_hit_in_a_suburban_village_is_refused(conn, monkeypatch):
    """«вулиця Святого Теодора» знайшлась у Малечковичах за 10.7 км.
    Рамка міста це пропускала: прямокутник 19 на 19 км, села влазять."""
    from flathunt import geo
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _Resp(
        [{"lat": "49.7561871", "lon": "23.9522207", "display_name": "Малечковичі"}]))
    assert geo.geocode(conn, "Святого Теодора", None, _geo_cfg()) is None


def test_a_central_address_still_geocodes(conn, monkeypatch):
    from flathunt import geo
    monkeypatch.setattr(geo.requests, "get", lambda *a, **k: _Resp(
        [{"lat": "49.8472788", "lon": "24.0265686",
          "display_name": "площа Святого Теодора, Львів"}]))
    assert geo.geocode(conn, "Теодора", None, _geo_cfg()) == (49.8472788, 24.0265686)


# --------------------------------------------------------------------------
# The reply keyboard on the phone is persistent and outlives a rename
# --------------------------------------------------------------------------

def test_every_search_config_has_a_button():
    """Конфігурацію додали, а кнопку — ні: трикімнатні не було як
    подивитись, доки не прийде картка."""
    from flathunt import cli, commands, notify
    cfg = cli.load_config()
    labels = {b["text"].lower() for row in notify.KEYBOARD["keyboard"] for b in row}
    for search in cfg["searches"]:
        assert any(commands.BUTTONS.get(x) and commands.BUTTONS[x][0] == search["key"]
                   for x in labels), f"немає кнопки для {search['key']}"


def test_old_button_labels_keep_working():
    """Клавіатура в Telegram persistent: на телефоні лишається стара,
    доки не прийде повідомлення з новою розміткою."""
    from flathunt import commands
    assert commands.BUTTONS["🔎 однокімнатні"][0] == "r1"
    assert commands.BUTTONS["🔎 двокімнатні"][0] == "r2"
