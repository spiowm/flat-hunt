"""Tests for the bug classes that actually happened.

Every case here is a regression that reached production and cost either
a missed flat or a wrong recommendation. They are written against the
failure, not against the happy path.
"""
import sqlite3

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
