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

# Какие занятия нужны. Проверяется название направления целиком (без учёта регистра и «ё»),
# слова могут стоять в любом месте названия, начало не важно.
#  1) Стретчинг — берём любые занятия.
STRETCHING = r"стретчинг"
#  2) Герли хип-хоп, хип-хоп и джаз-фанк — только если в названии есть и направление, и признак уровня «с нуля».
DIRECTIONS = [
    r"герли|girly",
    r"хип[\s-]*хоп|hip[\s-]*hop",
    r"джаз[\s-]*фанк|jazz[\s-]*funk",
]
LEVEL = r"начин|с\s*0|с\s*нуля|нулев|basic|бейсик|бэйсик|beginner"
EXCLUDE = []  # например [r"закрыт"], чтобы скрыть закрытые группы
INCLUDE_TEACHERS = ["Нефедов"]  # все занятия этих преподавателей (по фамилии), любой уровень
EXCLUDE_TEACHERS = ["Генин"]  # занятия этих преподавателей (по фамилии) скрываются

# Этаж/как найти зал — API это не отдаёт, впишите сами, например:
# "КЛОУЗ": "2 этаж, налево от лифта"
HALL_NOTES = {}


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
    if any(re.search(p, n) for p in EXCLUDE):
        return False
    if re.search(STRETCHING, n):
        return True
    return any(re.search(p, n) for p in DIRECTIONS) and bool(re.search(LEVEL, n))


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
    events_teacher = {}
    for s in schedule + extra:
        for d in dates:
            if not occurs_on(s, d) or (s["id"], d) in cancelled:
                continue
            teacher = substitutes.get((s["id"], d)) or teacher_name((s.get("group") or {}).get("teacher1"))
            has = lambda names: any(x.lower() in teacher.lower() for x in names)
            if has(EXCLUDE_TEACHERS) or not (has(INCLUDE_TEACHERS) or wanted(style_name(s))):
                continue
            events[(s["id"], d)] = (s, d)
            events_teacher[(s["id"], d)] = teacher

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
        teacher = events_teacher[(sid, d)]
        surname = teacher.split()[-1] if teacher else ""
        title = style_name(s).strip()
        if (sid, d) in substitutes:
            title += " (замена)"
        hall = (s.get("hall") or {}).get("name")
        area = (s.get("hall") or {}).get("area")
        desc = "\\n".join(x for x in (
            f"Преподаватель: {teacher}" if teacher else "",
            f"Зал: {hall}" + (f", {area} м²" if area else "") if hall else "",
            HALL_NOTES.get(hall, ""),
        ) if x)
        fmt = lambda x: x.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        lines += [
            "BEGIN:VEVENT",
            f"UID:{sid}-{d.isoformat()}@nrg-dance",
            f"DTSTAMP:{now}",
            f"DTSTART:{fmt(start)}",
            f"DTEND:{fmt(end)}",
            # "\\n" в iCalendar — перенос строки: фамилия окажется второй строкой названия
            f"SUMMARY:{title}\\n{surname}" if surname else f"SUMMARY:{title}",
            f"LOCATION:{hall or ''}",
            f"DESCRIPTION:{desc}",
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
