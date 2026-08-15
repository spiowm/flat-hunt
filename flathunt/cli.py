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

from . import geo, notify, olx, score, store, sync

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
    cfg["notify"]["telegram_token"] = os.environ.get("TELEGRAM_TOKEN", cfg["notify"]["telegram_token"])
    cfg["notify"]["telegram_chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", cfg["notify"]["telegram_chat_id"])
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

    for search in cfg["searches"]:
        try:
            ads = olx.collect(search, cfg)
        except Exception as exc:
            print(f"[{search['key']}] помилка збору: {exc}")
            store.log_run(conn, "olx", 0, 0, 0, f"{search['key']}: {exc}")
            continue

        ppm = [a["price"] / a["area"] for a in ads if a.get("price") and a.get("area")]
        ppm.sort()
        median_ppm = ppm[len(ppm) // 2] if ppm else None

        new_here = 0
        for ad in ads:
            geo.locate(conn, ad, cfg)
            score.evaluate(ad, cfg, median_ppm)
            if store.upsert(conn, ad):
                new_here += 1
        conn.commit()

        total_fetched += len(ads)
        total_new += new_here
        print(f"[{search['key']}] зібрано {len(ads)}, нових {new_here}")

    dups = store.sweep_duplicates(conn)
    conn.commit()
    if dups:
        print(f"позначено повторів: {dups}")

    if args.seed:
        n = conn.execute("UPDATE ads SET notified = 1 WHERE notified = 0").rowcount
        conn.commit()
        print(f"бекленг позначено як переглянутий: {n} шт. "
              f"Далі в Telegram піде лише те, що зʼявиться після цього моменту.")
        return

    if not args.quiet:
        min_score = cfg["notify"]["min_score"]
        for row in store.pending_notifications(conn, min_score):
            ok, err = notify.send_ad(cfg, row)
            if ok:
                store.mark_notified(conn, row["id"])
                total_sent += 1
            else:
                print(f"не надіслано ({err})")
                break
        conn.commit()

    store.log_run(conn, "olx", total_fetched, total_new, total_sent)
    conn.commit()

    if mongo:
        try:
            n_ads, n_geo = sync.push(conn, mongo)
            print(f"у Mongo вивантажено: оголошень {n_ads}, геокешу {n_geo}")
        except Exception as exc:
            print(f"Mongo недоступна, пропускаю синхронізацію: {exc}")

    print(f"разом: зібрано {total_fetched}, нових {total_new}, надіслано {total_sent}")


def cmd_report(args):
    cfg = load_config()
    conn = store.connect()
    where = "" if args.all else "WHERE rejected IS NULL AND dup_of IS NULL"
    rows = conn.execute(
        f"SELECT * FROM ads {where} ORDER BY score DESC, dist_km IS NULL, dist_km LIMIT ?",
        (args.limit,),
    ).fetchall()

    if not rows:
        print("порожньо — спершу запусти: python3 run.py collect --quiet --force")
        return

    print(f"{'бал':>4} {'км':>5} {'ціна':>7} {'/особу':>7} {'к':>1} {'м²':>5}  {'хто':<8} вулиця / заголовок")
    print("-" * 108)
    for r in rows:
        per = round((r["price"] or 0) / max(1, r["people"] or 1))
        who = "власник" if not r["is_business"] else "агенція"
        dist = f"{r['dist_km']:.2f}" if r["dist_km"] is not None else "  ?"
        where_txt = r["street"] or (r["district"] or "") + " " + (r["title"] or "")[:36]
        flag = f"  ✗ {r['rejected']}" if r["rejected"] else ""
        print(f"{r['score']:>4} {dist:>5} {r['price'] or 0:>7} {per:>7} {r['rooms'] or 0:>1} "
              f"{r['area'] or 0:>5.0f}  {who:<8} {where_txt[:46]}{flag}")
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
    ok, err = notify.send_ad(cfg, row)
    print("надіслано" if ok else f"не вдалось: {err}")


def cmd_watch(args):
    """Нескінченний цикл для контейнера: збирає з інтервалом і шле нове."""
    import time

    print(f"watch: інтервал {args.interval} с, робоче вікно з config.yaml", flush=True)
    while True:
        started = time.time()
        try:
            cmd_collect(argparse.Namespace(quiet=False, force=False, seed=False))
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
    print("надіслано:          ", q("SELECT count(*) FROM ads WHERE notified = 1"))
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
    w.set_defaults(func=cmd_watch)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
