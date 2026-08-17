#!/usr/bin/env python3
"""flat-hunt — пошук квартири у Львові біля церкви св. Анни.

  python3 run.py collect          один прохід: зібрати, оцінити, надіслати нове
  python3 run.py collect --quiet  те саме, але без надсилання (для першого прогону)
  python3 run.py report           таблиця найкращого з бази
  python3 run.py report --all     разом із відсіяними
  python3 run.py stats            що в базі загалом
"""
import argparse
import os
import re
from datetime import datetime
from pathlib import Path

import yaml

from . import commands, dimria, geo, llm, lun, notify, olx, photo, score, store, sync

ROOT = Path(__file__).resolve().parent.parent


def _find(name, env_var):
    """Шукає файл: змінна оточення → поточна тека → корінь проєкту."""
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    here = Path.cwd() / name
    return here if here.exists() else ROOT / name


CONFIG = _find("config.yaml", "FLAT_HUNT_CONFIG")
ENV_FILE = _find(".env", "FLAT_HUNT_ENV")


def load_env():
    """Читає .env у os.environ. Файл у .gitignore і в чат не потрапляє."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config():
    load_env()
    with open(CONFIG, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    # Секрети завжди беруться з оточення й перекривають yaml
    cfg["notify"]["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", "")
    # Запасний одержувач із .env, якщо в config.yaml список порожній
    fallback = os.environ.get("TELEGRAM_CHAT_ID", "")
    cfg["notify"]["telegram_chat_id"] = fallback
    if not cfg["notify"].get("recipients") and fallback:
        cfg["notify"]["recipients"] = [{"name": "основний", "chat_id": fallback}]
    cfg["llm"] = {
        "gemini_key": os.environ.get("GEMINI_API_KEY", ""),
        # gemini-3.7-flash — найновіша Flash (13.08.2026); у tier 1 це
        # 1K RPM / 2M TPM / 10K RPD, тобто з великим запасом під наші ~10 на добу
        "model": os.environ.get("GEMINI_MODEL", "gemini-3.7-flash"),
        "model_heavy": os.environ.get("GEMINI_MODEL_HEAVY", "gemini-3.1-pro"),
    }
    return cfg


def cmd_collect(args):
    cfg = load_config()
    conn = store.connect()

    mongo = sync.client()
    if mongo:
        got_ads, got_geo = sync.pull(conn, mongo)
        if got_ads or got_geo:
            print(f"mongo pull: {got_ads} sent-marks, {got_geo} geocache")
    elif sync.configured():
        # Мовчазна деградація тут найгірша: спільний стан відвалюється,
        # а дізнаєшся про це з подвійних сповіщень
        print("WARNING: MONGODB_URI is set but mongo is unreachable — "
              "running without shared state")

    if not args.force:
        hour = datetime.now().hour
        lo, hi = cfg["collector"]["active_hours"]
        if not (lo <= hour < hi):
            print(f"outside active hours {lo}:00-{hi}:00, skipping")
            return

    total_fetched = total_new = total_sent = 0

    batches = []
    for search in cfg["searches"]:
        try:
            batches.append((search["key"], olx.collect(search, cfg)))
        except Exception as exc:
            print(f"[{search['key']}] olx fetch failed: {exc}")
            store.log_run(conn, "olx", 0, 0, 0, f"{search['key']}: {exc}")

    try:
        found = lun.collect(cfg["searches"], cfg)
        if found:
            batches.append(("lun", found))
            print(f"[lun] fetched {len(found)}")
    except Exception as exc:
        print(f"[lun] fetch failed: {exc}")
        store.log_run(conn, "lun", 0, 0, 0, str(exc))

    try:
        ria = dimria.collect(cfg["searches"], cfg)
        if ria:
            batches.append(("dimria", ria))
            print(f"[dimria] fetched {len(ria)}")
    except Exception as exc:
        print(f"[dimria] fetch failed: {exc}")
        store.log_run(conn, "dimria", 0, 0, 0, str(exc))

    for label, ads in batches:

        ppm = [a["price"] / a["area"] for a in ads if a.get("price") and a.get("area")]
        ppm.sort()
        median_ppm = ppm[len(ppm) // 2] if ppm else None

        new_here = 0
        known = {r["id"]: r for r in conn.execute(
            "SELECT id, street, street_conf, lat, lon, dist_km, walk_min, walk_km FROM ads"
        ).fetchall()}

        for ad in ads:
            # Збирач знає лише те, що віддав OLX. Адресу, знайдену моделлю,
            # і пораховану відстань він не знає — а locate без них скочується
            # на центр району й записує гіршу точність поверх кращої.
            # Тому спершу підкладаємо вже відоме.
            prev = known.get(ad["id"])
            if prev is not None and prev["street"]:
                for field in ("street", "street_conf", "lat", "lon", "dist_km", "walk_min", "walk_km"):
                    if prev[field] is not None:
                        ad.setdefault(field, prev[field])
                        ad[field] = prev[field]

            geo.locate(conn, ad, cfg)
            score.evaluate(ad, cfg, median_ppm)
            prev = store.record_price(conn, ad["id"], ad.get("price"))
            if prev:
                ad["price_prev"] = prev
                ad["price_drop"] = prev - (ad.get("price") or prev)
            if store.upsert(conn, ad):
                new_here += 1
        conn.commit()

        # Оголошення, яких більше немає у видачі, після двох пропусків
        # вважаємо знятими. Робимо це лише для конфігурацій, які зібрались
        # успішно: інакше збій мережі позначив би зниклим геть усе.
        if label not in ("dimria", "lun") and ads:
            gone = store.mark_missing(conn, label, [a["id"] for a in ads])
            if gone:
                print(f"[{label}] gone from olx: {gone}")
            conn.commit()

        total_fetched += len(ads)
        total_new += new_here
        if label != "dimria":
            print(f"[{label}] fetched {len(ads)}, new {new_here}")

    # Розбір описів моделлю: вулиця там, де регекс не впорався, плюс
    # комісія, комуналка й обмеження — усе це живе тільки у вільному тексті
    if llm.enabled(cfg):
        done, streets = llm.enrich_pending(conn, cfg, limit=args.llm_limit)
        if done:
            print(f"llm parsed {done}, streets found {streets}")
            for row in conn.execute(
                "SELECT * FROM ads WHERE llm_done = 1 AND rejected IS NULL"
            ).fetchall():
                ad = dict(row)
                score.evaluate(ad, cfg)
                conn.execute("UPDATE ads SET score=?, rejected=?, real_cost=? WHERE id=?",
                             (ad["score"], ad["rejected"], ad["real_cost"], ad["id"]))
            conn.commit()

    # Адреси, які знайшла модель, ще не геокодовані: гео працює під час
    # збору, а модель розбирає описи після нього. Без цього кроку
    # оголошення з адресою від моделі назавжди лишалось без хвилин ходьби.
    # Не лише ті, у кого координат немає зовсім: запасна прив'язка до
    # центру району теж записує lat, і без цієї умови точна адреса,
    # знайдена моделлю, ніколи б не геокодувалась — район назавжди
    # заблокував би вулицю.
    pending_geo = conn.execute(
        """SELECT * FROM ads WHERE street IS NOT NULL
           AND (lat IS NULL OR ifnull(street_conf,'') = 'район')
           AND rejected IS NULL LIMIT 40"""
    ).fetchall()
    located = 0
    for row in pending_geo:
        ad = dict(row)
        ad["lat"] = ad["lon"] = ad["walk_min"] = None   # інакше стара точка лишиться
        geo.locate(conn, ad, cfg)
        if ad.get("lat"):
            score.evaluate(ad, cfg)
            conn.execute(
                """UPDATE ads SET lat=?, lon=?, dist_km=?, walk_min=?, walk_km=?,
                       score=?, rejected=?, tier=? WHERE id=?""",
                (ad["lat"], ad["lon"], ad.get("dist_km"), ad.get("walk_min"),
                 ad.get("walk_km"), ad["score"], ad["rejected"], ad["tier"], row["id"]),
            )
            located += 1
    if located:
        conn.commit()
        print(f"geocoded llm addresses: {located}")

    photo.fill_missing(conn, limit=60)
    store.refresh_market_signals(conn)

    # Переоцінка після всіх збагачень. Порядок тут принциповий: вік
    # оголошення, дані від моделі й хеші фото зʼявляються вже після
    # першої оцінки, тож без цього проходу відсів «висить понад рік»
    # не спрацював би на новому оголошенні жодного разу.
    rescored = 0
    for row in conn.execute("SELECT * FROM ads").fetchall():
        ad = dict(row)
        before = (ad["score"], ad["rejected"])
        score.evaluate(ad, cfg)
        if (ad["score"], ad["rejected"]) != before:
            conn.execute(
                "UPDATE ads SET score=?, rejected=?, tier=?, real_cost=? WHERE id=?",
                (ad["score"], ad["rejected"], ad["tier"], ad["real_cost"], ad["id"]),
            )
            rescored += 1
    conn.commit()
    if rescored:
        print(f"rescored after enrichment: {rescored}")

    from . import risk
    suspicious = risk.corpus_flags(conn)
    if suspicious:
        print(f"flagged fresh account with multiple ads: {suspicious}")

    by_url = store.sweep_by_url(conn)
    dups = store.sweep_duplicates(conn)
    conn.commit()
    photo_dups, stolen = photo.group_by_photo(conn)
    if dups or photo_dups or by_url:
        print(f"duplicates: {dups} by address, {by_url} by url, {photo_dups} by photo")
    if stolen:
        print(f"suspected stolen photos: {stolen}")

    if args.seed:
        chats = [r["chat_id"] for r in recipients(cfg)]
        n = store.seed_notifications(conn, chats)
        conn.commit()
        print(f"backlog marked as seen: {n} marks for {len(chats)} recipient(s). "
              f"Only ads appearing from now on will be sent.")
        return

    if not args.quiet:
        total_sent = deliver(cfg, conn)

    store.log_run(conn, "olx", total_fetched, total_new, total_sent)
    conn.commit()

    if mongo:
        try:
            n_ads, n_geo = sync.push(conn, mongo)
            print(f"mongo push: {n_ads} ads, {n_geo} geocache")
        except Exception as exc:
            print(f"mongo unreachable, skipping sync: {exc}")

    print(f"total: fetched {total_fetched}, new {total_new}, sent {total_sent} "
          f"[{datetime.now():%H:%M:%S}]")


def recipients(cfg):
    """Одержувачі з config.yaml. Порожні chat_id ігноруються."""
    return [r for r in (cfg["notify"].get("recipients") or []) if r.get("chat_id")]


def deliver(cfg, conn):
    """Розсилає кожному його добірку. У кожного свої фільтри."""
    default_min = cfg["notify"]["min_score"]
    sent_total = 0
    for who in recipients(cfg):
        pending = store.pending_for(conn, who, default_min)
        if not pending:
            continue
        sent = 0
        for row in pending:
            ok, err = notify.send_ad(cfg, row, who["chat_id"])
            if not ok:
                print(f"[{who['name']}] send failed: {err}")
                break
            store.mark_sent(conn, row["id"], who["chat_id"])
            sent += 1
        conn.commit()
        sent_total += sent
        if sent:
            print(f"[{who['name']}] sent {sent}")
    return sent_total


def cmd_report(args):
    cfg = load_config()
    conn = store.connect()
    where = "" if args.all else "WHERE rejected IS NULL AND dup_of IS NULL"
    rows = conn.execute(
        f"SELECT * FROM ads {where} ORDER BY score DESC, walk_min IS NULL, walk_min LIMIT ?",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("empty — run first: flat-hunt collect --quiet --force")
        return

    header = (f"{'score':>5} {'min':>4} {'price':>7} {'real':>8} {'r':>1} {'m2':>4} "
              f"{'who':<7} {'src':<6} street")
    print(header)
    print("-" * len(header))
    for r in rows:
        who = "owner" if not r["is_business"] else "agency"
        walk = f"{r['walk_min']}" if r["walk_min"] else "?"
        real = r["real_cost"] or round((r["price"] or 0) / max(1, r["people"] or 1))
        where_txt = r["street"] or ((r["district"] or "") + " " + (r["title"] or ""))[:34]
        marks = []
        if r["risk_level"]:
            marks.append("risk:" + r["risk_level"])
        if r["commission"] == "немає":
            marks.append("no-fee")
        if r["price_drop"]:
            marks.append("price-drop")
        if r["rejected"]:
            marks.append("✗ " + r["rejected"])
        print(f"{r['score']:>4} {walk:>4} {r['price'] or 0:>7} {real:>8} {r['rooms'] or 0:>1} "
              f"{r['area'] or 0:>4.0f} {who:<7} {(r['source'] or '')[:6]:<6} "
              f"{where_txt[:32]:<32} {' '.join(marks)}")
    print(f"\ntotal rows: {len(rows)}")


def cmd_chatid(args):
    """Знаходить chat_id і дописує його в .env. Спершу напиши боту будь-що."""
    import json
    import urllib.request

    cfg = load_config()
    token = cfg["notify"]["telegram_token"]
    if not token:
        print("TELEGRAM_TOKEN missing in .env")
        return

    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/getUpdates", timeout=20
    ) as resp:
        data = json.load(resp)

    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id"):
            chats[chat["id"]] = chat.get("username") or chat.get("first_name") or chat.get("title")

    if not chats:
        print("no updates. Open the bot in Telegram, press Start\n"
              "or send it any message, then run this command again.")
        return

    chat_id, who = next(iter(chats.items()))
    text = ENV_FILE.read_text(encoding="utf-8")
    if "TELEGRAM_CHAT_ID=" in text:
        text = re.sub(r"TELEGRAM_CHAT_ID=.*", f"TELEGRAM_CHAT_ID={chat_id}", text)
    else:
        text += f"\nTELEGRAM_CHAT_ID={chat_id}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    print(f"found chat_id {chat_id} ({who}) — written to .env")


def cmd_test(args):
    """Надсилає найкращий варіант із бази як пробну картку."""
    cfg = load_config()
    conn = store.connect()
    row = conn.execute(
        """SELECT * FROM ads WHERE rejected IS NULL AND dup_of IS NULL
           ORDER BY score DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        print("database is empty")
        return
    for who in recipients(cfg):
        ok, err = notify.send_ad(cfg, row, who["chat_id"])
        print(f"[{who['name']}] " + ("sent" if ok else f"failed: {err}"))


def cmd_watch(args):
    """Нескінченний цикл для контейнера: збирає з інтервалом і шле нове."""
    import time

    import threading

    window = "цілодобово" if args.force else "робоче вікно з config.yaml"
    print(f"watch: interval {args.interval}s, {window}", flush=True)

    cfg = load_config()
    if recipients(cfg):
        threading.Thread(target=commands.listen_forever, args=(cfg, recipients(cfg)),
                         daemon=True).start()
        print("buttons: listening in a separate thread", flush=True)
    while True:
        started = time.time()
        # Без мітки часу неможливо зрозуміти, чому оголошення дійшло
        # за двадцять хвилин замість десяти: чи то прохід затягнувся,
        # чи то OLX показав його в видачі не одразу
        print(f"\n--- pass {datetime.now():%H:%M:%S} ---", flush=True)
        try:
            cmd_collect(argparse.Namespace(
                quiet=args.quiet, force=args.force, seed=False, llm_limit=args.llm_limit))
        except Exception as exc:  # цикл не має падати через одну помилку мережі
            print(f"pass failed: {exc}", flush=True)
        sleep_for = max(60, args.interval - (time.time() - started))
        time.sleep(sleep_for)


def cmd_sync(args):
    """Ручна синхронізація з Mongo в обидва боки."""
    load_config()
    conn = store.connect()
    cli = sync.client()
    if cli is None:
        print("mongo unreachable or MONGODB_URI not set")
        return
    got = sync.pull(conn, cli)
    put = sync.push(conn, cli)
    print(f"pulled: {got[0]} marks, {got[1]} geocache")
    print(f"pushed: {put[0]} ads, {put[1]} geocache")


def cmd_doctor(args):
    """Перевірка оточення: що працює, що ні і чому."""
    cfg = load_config()
    print("=== Mongo ===")
    for line in sync.diagnose():
        print("  " + line)
    print("\n=== Telegram ===")
    token = cfg["notify"].get("telegram_token")
    print(f"  token set: {bool(token)}")
    print(f"  recipients: {len(recipients(cfg))}")
    print("\n=== Gemini ===")
    print(f"  key set: {bool(cfg['llm'].get('gemini_key'))} | model: {cfg['llm'].get('model')}")
    print("\n=== DIM.RIA ===")
    print(f"  key set: {bool(os.environ.get('RIA_API_KEY'))}")
    print("\n=== Database ===")
    conn = store.connect()
    print(f"  file: {store.DB_PATH}")
    print(f"  ads: {conn.execute('SELECT count(*) FROM ads').fetchone()[0]}")


def cmd_stats(args):
    conn = store.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print("ads in database:    ", q("SELECT count(*) FROM ads"))
    print("live candidates:    ", q("SELECT count(*) FROM ads WHERE rejected IS NULL AND dup_of IS NULL"))
    print("rejected:           ", q("SELECT count(*) FROM ads WHERE rejected IS NOT NULL"))
    print("duplicates:         ", q("SELECT count(*) FROM ads WHERE dup_of IS NOT NULL"))
    print("from owners:        ", q("SELECT count(*) FROM ads WHERE is_business = 0"))
    print("with street:        ", q("SELECT count(*) FROM ads WHERE street IS NOT NULL"))
    print("with coordinates:   ", q("SELECT count(*) FROM ads WHERE dist_km IS NOT NULL"))
    print("llm parsed:         ", q("SELECT count(*) FROM ads WHERE llm_done = 1"))
    print("listed 30+ days:    ", q("SELECT count(*) FROM ads WHERE days_listed >= 30"))
    conn2 = conn.execute("SELECT chat_id, count(*) n FROM notifications GROUP BY chat_id").fetchall()
    if conn2:
        print("sent:")
        for row in conn2:
            print(f"   {row['n']:>4}  chat {row['chat_id']}")
    else:
        print("sent:                nothing")
    print("\nrejection reasons:")
    for row in conn.execute(
        "SELECT rejected, count(*) n FROM ads WHERE rejected IS NOT NULL GROUP BY rejected ORDER BY n DESC LIMIT 12"
    ):
        print(f"  {row['n']:>3}  {row['rejected']}")


def main():
    ap = argparse.ArgumentParser(description="flat-hunt")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("collect")
    c.add_argument("--quiet", action="store_true", help="не надсилати в Telegram")
    c.add_argument("--force", action="store_true", help="ігнорувати робоче вікно")
    c.add_argument("--seed", action="store_true", help="позначити наявне як переглянуте, нічого не слати")
    c.add_argument("--llm-limit", type=int, default=40, help="скільки описів розбирати моделлю за прохід")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("report")
    r.add_argument("--all", action="store_true")
    r.add_argument("--limit", type=int, default=40)
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("doctor", help="перевірити оточення й підключення")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("stats")
    s.set_defaults(func=cmd_stats)

    ci = sub.add_parser("chatid", help="знайти chat_id і записати в .env")
    ci.set_defaults(func=cmd_chatid)

    t = sub.add_parser("test", help="надіслати пробну картку в Telegram")
    t.set_defaults(func=cmd_test)

    sy = sub.add_parser("sync", help="синхронізувати стан з MongoDB")
    sy.set_defaults(func=cmd_sync)

    w = sub.add_parser("watch", help="нескінченний цикл (для контейнера)")
    w.add_argument("--interval", type=int, default=600, help="секунд між проходами")
    w.add_argument("--force", action="store_true", help="ігнорувати робоче вікно")
    w.add_argument("--quiet", action="store_true", help="збирати, але не слати")
    w.add_argument("--llm-limit", type=int, default=40)
    w.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
