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

from . import dimria, geo, llm, notify, olx, photo, score, store, sync

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
            print(f"з Mongo підтягнуто: позначок {got_ads}, геокешу {got_geo}")

    if not args.force:
        hour = datetime.now().hour
        lo, hi = cfg["collector"]["active_hours"]
        if not (lo <= hour < hi):
            print(f"поза робочим вікном {lo}:00–{hi}:00, пропускаю")
            return

    total_fetched = total_new = total_sent = 0

    batches = []
    for search in cfg["searches"]:
        try:
            batches.append((search["key"], olx.collect(search, cfg)))
        except Exception as exc:
            print(f"[{search['key']}] OLX не зібрався: {exc}")
            store.log_run(conn, "olx", 0, 0, 0, f"{search['key']}: {exc}")

    try:
        ria = dimria.collect(cfg["searches"], cfg)
        if ria:
            batches.append(("dimria", ria))
            print(f"[dimria] зібрано {len(ria)}")
    except Exception as exc:
        print(f"[dimria] не зібралось: {exc}")
        store.log_run(conn, "dimria", 0, 0, 0, str(exc))

    for label, ads in batches:

        ppm = [a["price"] / a["area"] for a in ads if a.get("price") and a.get("area")]
        ppm.sort()
        median_ppm = ppm[len(ppm) // 2] if ppm else None

        new_here = 0
        for ad in ads:
            geo.locate(conn, ad, cfg)
            score.evaluate(ad, cfg, median_ppm)
            prev = store.record_price(conn, ad["id"], ad.get("price"))
            if prev:
                ad["price_prev"] = prev
                ad["price_drop"] = prev - (ad.get("price") or prev)
            if store.upsert(conn, ad):
                new_here += 1
        conn.commit()

        total_fetched += len(ads)
        total_new += new_here
        if label != "dimria":
            print(f"[{label}] зібрано {len(ads)}, нових {new_here}")

    # Розбір описів моделлю: вулиця там, де регекс не впорався, плюс
    # комісія, комуналка й обмеження — усе це живе тільки у вільному тексті
    if llm.enabled(cfg):
        done, streets = llm.enrich_pending(conn, cfg, limit=args.llm_limit)
        if done:
            print(f"розібрано моделлю {done}, з них знайдено вулиць {streets}")
            for row in conn.execute(
                "SELECT * FROM ads WHERE llm_done = 1 AND rejected IS NULL"
            ).fetchall():
                ad = dict(row)
                score.evaluate(ad, cfg)
                conn.execute("UPDATE ads SET score=?, rejected=?, real_cost=? WHERE id=?",
                             (ad["score"], ad["rejected"], ad["real_cost"], ad["id"]))
            conn.commit()

    photo.fill_missing(conn, limit=60)
    store.refresh_market_signals(conn)

    dups = store.sweep_duplicates(conn)
    conn.commit()
    photo_dups, stolen = photo.group_by_photo(conn)
    if dups or photo_dups:
        print(f"позначено повторів: {dups} за адресою, {photo_dups} за фото")
    if stolen:
        print(f"підозра на крадені фото: {stolen}")

    if args.seed:
        chats = [r["chat_id"] for r in recipients(cfg)]
        n = store.seed_notifications(conn, chats)
        conn.commit()
        print(f"бекленг позначено як переглянутий: {n} позначок на {len(chats)} одержувач(ів). "
              f"Далі піде лише те, що зʼявиться після цього моменту.")
        return

    if not args.quiet:
        total_sent = deliver(cfg, conn)

    store.log_run(conn, "olx", total_fetched, total_new, total_sent)
    conn.commit()

    if mongo:
        try:
            n_ads, n_geo = sync.push(conn, mongo)
            print(f"у Mongo вивантажено: оголошень {n_ads}, геокешу {n_geo}")
        except Exception as exc:
            print(f"Mongo недоступна, пропускаю синхронізацію: {exc}")

    print(f"разом: зібрано {total_fetched}, нових {total_new}, надіслано {total_sent}")


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
                print(f"[{who['name']}] не надіслано: {err}")
                break
            store.mark_sent(conn, row["id"], who["chat_id"])
            sent += 1
        conn.commit()
        sent_total += sent
        if sent:
            print(f"[{who['name']}] надіслано {sent}")
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
        print("порожньо — спершу запусти: python3 run.py collect --quiet --force")
        return

    header = (f"{'бал':>4} {'хв':>4} {'ціна':>7} {'реально':>8} {'к':>1} {'м²':>4} "
              f"{'хто':<7} {'дж':<6} вулиця")
    print(header)
    print("-" * len(header))
    for r in rows:
        who = "власник" if not r["is_business"] else "агенція"
        walk = f"{r['walk_min']}" if r["walk_min"] else "?"
        real = r["real_cost"] or round((r["price"] or 0) / max(1, r["people"] or 1))
        where_txt = r["street"] or ((r["district"] or "") + " " + (r["title"] or ""))[:34]
        marks = []
        if r["risk_level"]:
            marks.append("⚠" + r["risk_level"][:3])
        if r["commission"] == "немає":
            marks.append("безком")
        if r["price_drop"]:
            marks.append("↓ціна")
        if r["rejected"]:
            marks.append("✗ " + r["rejected"])
        print(f"{r['score']:>4} {walk:>4} {r['price'] or 0:>7} {real:>8} {r['rooms'] or 0:>1} "
              f"{r['area'] or 0:>4.0f} {who:<7} {(r['source'] or '')[:6]:<6} "
              f"{where_txt[:32]:<32} {' '.join(marks)}")
    print(f"\nвсього рядків: {len(rows)}")


def cmd_chatid(args):
    """Знаходить chat_id і дописує його в .env. Спершу напиши боту будь-що."""
    import json
    import urllib.request

    cfg = load_config()
    token = cfg["notify"]["telegram_token"]
    if not token:
        print("немає TELEGRAM_TOKEN у .env")
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
        print("оновлень немає. Відкрий бота в Telegram, натисни Start\n"
              "або напиши йому будь-що — і запусти цю команду ще раз.")
        return

    chat_id, who = next(iter(chats.items()))
    text = ENV_FILE.read_text(encoding="utf-8")
    if "TELEGRAM_CHAT_ID=" in text:
        text = re.sub(r"TELEGRAM_CHAT_ID=.*", f"TELEGRAM_CHAT_ID={chat_id}", text)
    else:
        text += f"\nTELEGRAM_CHAT_ID={chat_id}\n"
    ENV_FILE.write_text(text, encoding="utf-8")
    print(f"знайдено chat_id {chat_id} ({who}) — записано в .env")


def cmd_test(args):
    """Надсилає найкращий варіант із бази як пробну картку."""
    cfg = load_config()
    conn = store.connect()
    row = conn.execute(
        """SELECT * FROM ads WHERE rejected IS NULL AND dup_of IS NULL
           ORDER BY score DESC LIMIT 1"""
    ).fetchone()
    if row is None:
        print("база порожня")
        return
    for who in recipients(cfg):
        ok, err = notify.send_ad(cfg, row, who["chat_id"])
        print(f"[{who['name']}] " + ("надіслано" if ok else f"не вдалось: {err}"))


def cmd_watch(args):
    """Нескінченний цикл для контейнера: збирає з інтервалом і шле нове."""
    import time

    window = "цілодобово" if args.force else "робоче вікно з config.yaml"
    print(f"watch: інтервал {args.interval} с, {window}", flush=True)
    while True:
        started = time.time()
        try:
            cmd_collect(argparse.Namespace(
                quiet=args.quiet, force=args.force, seed=False, llm_limit=args.llm_limit))
        except Exception as exc:  # цикл не має падати через одну помилку мережі
            print(f"прохід впав: {exc}", flush=True)
        sleep_for = max(60, args.interval - (time.time() - started))
        time.sleep(sleep_for)


def cmd_sync(args):
    """Ручна синхронізація з Mongo в обидва боки."""
    load_config()
    conn = store.connect()
    cli = sync.client()
    if cli is None:
        print("Mongo недоступна або MONGODB_URI не заданий")
        return
    got = sync.pull(conn, cli)
    put = sync.push(conn, cli)
    print(f"підтягнуто: позначок {got[0]}, геокешу {got[1]}")
    print(f"вивантажено: оголошень {put[0]}, геокешу {put[1]}")


def cmd_stats(args):
    conn = store.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print("усього в базі:      ", q("SELECT count(*) FROM ads"))
    print("живих кандидатів:   ", q("SELECT count(*) FROM ads WHERE rejected IS NULL AND dup_of IS NULL"))
    print("відсіяно:           ", q("SELECT count(*) FROM ads WHERE rejected IS NOT NULL"))
    print("дублів:             ", q("SELECT count(*) FROM ads WHERE dup_of IS NOT NULL"))
    print("від власника:       ", q("SELECT count(*) FROM ads WHERE is_business = 0"))
    print("з розпізнаною вул.: ", q("SELECT count(*) FROM ads WHERE street IS NOT NULL"))
    print("з координатами:     ", q("SELECT count(*) FROM ads WHERE dist_km IS NOT NULL"))
    print("розібрано моделлю:  ", q("SELECT count(*) FROM ads WHERE llm_done = 1"))
    print("висить 30+ днів:    ", q("SELECT count(*) FROM ads WHERE days_listed >= 30"))
    conn2 = conn.execute("SELECT chat_id, count(*) n FROM notifications GROUP BY chat_id").fetchall()
    if conn2:
        print("надіслано:")
        for row in conn2:
            print(f"   {row['n']:>4}  chat {row['chat_id']}")
    else:
        print("надіслано:           нічого")
    print("\nпричини відсіву:")
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
