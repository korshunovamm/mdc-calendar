#!/usr/bin/env python3
"""Собирает .ics с занятиями NRG Пролетарская на 30 дней вперёд.

Источник — публичный API impulse CRM, которым пользуется виджет на nrg-style.ru.
Отменённые занятия не попадают в файл, перенесённые — только в новом времени,
замены преподавателей отражаются в названии.
"""
import json
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API = "https://crmmdcnrgru.impulsecrm.ru/api/public"
BRANCH_ID = 1  # Пролетарская
DAYS_AHEAD = 30
TZ = ZoneInfo("Europe/Moscow")
OUT = "docs/dance.ics"

# Какие занятия нужны: регулярки по названию направления (без учёта регистра).
INCLUDE = [
    r"стретчинг",
    r"герли.*(начин|с 0|с нуля)",
    r"хип-?хоп.*(начин|бейсик)",
    r"джаз-?фанк.*(начин|с нуля)",
]
EXCLUDE = []  # например [r"закрыт"], чтобы скрыть закрытые группы


def post(path, body):
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "dance-calendar"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def style_name(s):
    return ((s.get("group") or {}).get("style") or {}).get("name") or ""


def teacher_name(t):
    if not t:
        return ""
    return " ".join(p for p in (t.get("name"), t.get("lastName")) if p)


def wanted(name):
    n = name.lower().replace("ё", "е")
    return any(re.search(p, n) for p in INCLUDE) and not any(re.search(p, n) for p in EXCLUDE)


def day_ts(d):
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def ts_date(ts):
    return datetime.fromtimestamp(ts, timezone.utc).date()


def occurs_on(s, d):
    """Попадает ли шаблон занятия s на дату d (до учёта изменений)."""
    if s.get("regular"):
        if s["day"] != d.isoweekday():
            return False
        if s.get("dateBegin") and ts_date(s["dateBegin"]) > d:
            return False
        if s.get("dateEnd") and ts_date(s["dateEnd"]) < d:
            return False
        return True
    return s.get("date") is not None and ts_date(s["date"]) == d


def main():
    today = datetime.now(TZ).date()
    dates = [today + timedelta(days=i) for i in range(DAYS_AHEAD)]
    rng = {"from": day_ts(dates[0]), "to": day_ts(dates[-1])}

    schedule = post("widget_schedule/list", {
        "range": rng,
        "filters": {"branch_id": BRANCH_ID, "age": None, "hall_id": None, "style_id": None,
                    "teacher_id": None, "groups": True, "individuals": False,
                    "rents": False, "scale": "week"},
    })["items"]
    changes = [c for c in post("widget_schedule/changes", {"range": rng})["items"]
               if (c.get("branch") or {}).get("id") == BRANCH_ID]

    cancelled = set()   # (id занятия, дата)
    substitutes = {}    # (id занятия, дата) -> преподаватель
    extra = []          # (занятие, дата) — результат переносов
    for c in changes:
        d = ts_date(c["date"])
        key = (c["schedule"]["id"], d)
        if c["entity"] == "cancel":
            cancelled.add(key)
        elif c["entity"] == "transfer":
            cancelled.add(key)
            extra.append(c["newSchedule"])
        elif c["entity"] == "substitute":
            substitutes[key] = teacher_name(c.get("teacher"))

    events = {}
    for s in schedule + extra:
        if not wanted(style_name(s)):
            continue
        for d in dates:
            if not occurs_on(s, d) or (s["id"], d) in cancelled:
                continue
            events[(s["id"], d)] = (s, d)

    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//dance-calendar//NRG//RU",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "X-WR-CALNAME:Танцы", "X-WR-TIMEZONE:Europe/Moscow",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
    ]
    for (sid, d), (s, _) in sorted(events.items(), key=lambda kv: (kv[0][1], kv[1][0]["minutesBegin"])):
        start = datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(minutes=s["minutesBegin"])
        end = datetime(d.year, d.month, d.day, tzinfo=TZ) + timedelta(minutes=s["minutesEnd"])
        teacher = substitutes.get((sid, d)) or teacher_name((s.get("group") or {}).get("teacher1"))
        surname = teacher.split()[-1] if teacher else ""
        title = style_name(s).strip()
        if (sid, d) in substitutes:
            title += " (замена)"
        hall = (s.get("hall") or {}).get("name")
        fmt = lambda x: x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{sid}-{d.isoformat()}@nrg-dance",
            f"DTSTAMP:{now}",
            f"DTSTART:{fmt(start)}",
            f"DTEND:{fmt(end)}",
            f"SUMMARY:{title}",
            # вторая строка события в Календаре — это LOCATION
            f"LOCATION:{' · '.join(x for x in (surname, hall) if x)}",
            f"DESCRIPTION:Преподаватель: {teacher}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")

    # RFC 5545: CRLF и складывание строк длиннее 75 байт не обязательны для iOS,
    # но CRLF соблюдаем.
    import os
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")

    for (sid, d), (s, _) in sorted(events.items(), key=lambda kv: (kv[0][1], kv[1][0]["minutesBegin"])):
        m = s["minutesBegin"]
        tag = " [замена]" if (sid, d) in substitutes else ""
        print(f"{d:%a %d.%m} {m // 60:02d}:{m % 60:02d}  {style_name(s).strip()}  "
              f"{(s.get('hall') or {}).get('name', '')}{tag}")
    print(f"\nВсего занятий: {len(events)}  (отмен/переносов учтено: {len(cancelled)})", file=sys.stderr)


if __name__ == "__main__":
    main()
