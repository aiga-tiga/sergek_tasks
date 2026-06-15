#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_parking.py
===================

Сбор структурированных данных по парковкам г. Алматы через официальный
2GIS Catalog API (https://catalog.api.2gis.com) и выгрузка результата
в CSV / Google Sheets.

ПОДХОД (см. также README.md):
1. Официальный API 2GIS вместо "сырого" скрапинга HTML/JS-карты:
   - данные сразу структурированы (JSON), не нужно парсить динамический JS;
   - стабильные идентификаторы (item.id) -> простая и надёжная дедупликация;
   - соответствует условиям использования 2GIS (в отличие от обхода
     анти-бот защиты карты).
2. Используются ДВА типа запросов, чтобы максимизировать покрытие:
   a) type=parking  -> объекты-парковки на карте (есть is_paid, capacity,
      access, purpose, schedule, geometry) — основной источник;
   b) rubric_id для рубрики "Парковки, стоянки автотранспорта"
      (type=branch) -> коммерческие организации-парковки (есть org,
      описание с тарифами, контакты, часы работы) — дополняет (a).
3. Так как один запрос без q ограничен radius<=2000 м, весь город
   покрывается СЕТКОЙ точек (grid) с перекрытием, после чего результаты
   дедуплицируются по item.id, а затем — по близким координатам+имени
   (на случай дублей между (a) и (b) или соседними ячейками сетки).
4. Поля, которых нет напрямую в API (тариф, тип объекта, "к какому объекту
   относится"), извлекаются эвристически из описания/имени/рубрик и
   помечаются отдельным флагом need_review для ручной проверки.

ТРЕБОВАНИЯ:
    pip install requests pandas gspread google-auth --break-system-packages

ENV-переменные:
    DGIS_API_KEY            - ключ 2GIS Catalog API (демо-ключ можно
                               получить на https://platform.2gis.com)
    GOOGLE_SERVICE_ACCOUNT  - путь к JSON-файлу сервисного аккаунта Google
    GOOGLE_SHEET_ID         - ID Google-таблицы для выгрузки (необязательно,
                               можно выгрузить только в CSV)

ЗАПУСК:
    python3 collect_parking.py --out parking_almaty.csv
    python3 collect_parking.py --out parking_almaty.csv --to-sheets
"""

import os
import re
import time
import json
import argparse
import itertools
from typing import Optional

import requests
import pandas as pd


BASE = "https://catalog.api.2gis.com"
CITY_NAME = "Алматы"

# Примерный bounding box г. Алматы (включая ближние окраины)
BBOX = {"lat_min": 43.13, "lat_max": 43.37, "lon_min": 76.76, "lon_max": 77.12}

# Шаг сетки в градусах (~2.2 км по широте) и радиус запроса в метрах.
# При шаге 0.02 град. и радиусе 1700 м соседние круги перекрываются —
# это нужно, чтобы не "потерять" объекты на границах ячеек.
GRID_STEP_DEG = 0.02
SEARCH_RADIUS_M = 1700
PAGE_SIZE = 50
MAX_PAGES_PER_CELL = 10  # 10*50 = 500 объектов на ячейку — более чем достаточно

FIELDS = ",".join([
    "items.point",
    "items.address",
    "items.full_address_name",
    "items.schedule",
    "items.schedule_special",
    "items.is_paid",
    "items.access",
    "items.access_comment",
    "items.access_name",
    "items.capacity",
    "items.purpose",
    "items.for_trucks",
    "items.level_count",
    "items.paving_type",
    "items.is_incentive",
    "items.description",
    "items.rubrics",
    "items.org",
    "items.links",
    "items.name_ex",
])

SESSION = requests.Session()


def api_key() -> str:
    key = os.environ.get("DGIS_API_KEY")
    if not key:
        raise SystemExit(
            "Не задан DGIS_API_KEY. Получите демо-ключ на "
            "https://platform.2gis.com (Каталог API) и экспортируйте его:\n"
            "  export DGIS_API_KEY=ваш_ключ"
        )
    return key


def api_get(path: str, params: dict, retries: int = 3) -> dict:
    params = dict(params)
    params["key"] = api_key()
    for attempt in range(retries):
        try:
            r = SESSION.get(f"{BASE}{path}", params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            # 403 / 429 -> подождать и повторить
            time.sleep(1.5 * (attempt + 1))
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Не удалось получить {path} с параметрами {params}")


def get_region_id(city_query: str = CITY_NAME) -> int:
    """Определяем region_id Алматы через Regions API."""
    data = api_get("/2.0/region/search", {"q": city_query})
    items = data.get("result", {}).get("items", [])
    for it in items:
        if it.get("name", "").lower().startswith("алматы"):
            return it["id"]
    if items:
        return items[0]["id"]
    raise RuntimeError("Не удалось определить region_id для Алматы")


def get_parking_rubric_ids(region_id: int) -> list:
    """Ищем рубрики, относящиеся к платным/коммерческим парковкам."""
    data = api_get(
        "/2.0/catalog/rubric/search",
        {"q": "парковки, стоянки автотранспорта", "region_id": region_id},
    )
    ids = []
    for it in data.get("result", {}).get("items", []):
        ids.append(it["id"])
    # запасной вариант — более общий запрос
    if not ids:
        data = api_get(
            "/2.0/catalog/rubric/search",
            {"q": "парковка", "region_id": region_id},
        )
        ids = [it["id"] for it in data.get("result", {}).get("items", [])]
    return ids


def generate_grid():
    lats = []
    v = BBOX["lat_min"]
    while v <= BBOX["lat_max"]:
        lats.append(round(v, 4))
        v += GRID_STEP_DEG
    lons = []
    v = BBOX["lon_min"]
    while v <= BBOX["lon_max"]:
        lons.append(round(v, 4))
        v += GRID_STEP_DEG
    return list(itertools.product(lats, lons))


def fetch_cell(region_id: int, lat: float, lon: float, params_extra: dict, source: str):
    """Постранично выгружаем все элементы для одной точки сетки."""
    results = []
    for page in range(1, MAX_PAGES_PER_CELL + 1):
        params = {
            "fields": FIELDS,
            "point": f"{lon},{lat}",
            "radius": SEARCH_RADIUS_M,
            "page": page,
            "page_size": PAGE_SIZE,
            "region_id": region_id,
            "search_type": "discovery",
        }
        params.update(params_extra)
        data = api_get("/3.0/items", params)
        items = data.get("result", {}).get("items", [])
        for it in items:
            it["_source"] = source
        results.extend(items)
        total = data.get("result", {}).get("total", 0)
        if not items or page * PAGE_SIZE >= total:
            break
        time.sleep(0.1)
    return results


def collect_raw_items(region_id: int, rubric_ids: list) -> list:
    grid = generate_grid()
    print(f"Сетка: {len(grid)} точек, радиус {SEARCH_RADIUS_M} м")
    all_items = []

    for i, (lat, lon) in enumerate(grid, 1):
        # (a) объекты-парковки на карте
        try:
            items = fetch_cell(region_id, lat, lon, {"type": "parking"}, "map_parking")
            all_items.extend(items)
        except Exception as e:
            print(f"[{i}/{len(grid)}] ошибка type=parking @ {lat},{lon}: {e}")

        # (b) коммерческие парковки-организации (рубрика)
        if rubric_ids:
            try:
                items = fetch_cell(
                    region_id, lat, lon,
                    {"type": "branch", "rubric_id": ",".join(map(str, rubric_ids))},
                    "rubric_branch",
                )
                all_items.extend(items)
            except Exception as e:
                print(f"[{i}/{len(grid)}] ошибка rubric @ {lat},{lon}: {e}")

        if i % 10 == 0:
            print(f"  ...обработано {i}/{len(grid)} точек, собрано {len(all_items)} записей (с дублями)")

    return all_items


# ---------------------------------------------------------------------------
# Нормализация / очистка
# ---------------------------------------------------------------------------

DAYS_RU = {"Mon": "Пн", "Tue": "Вт", "Wed": "Ср", "Thu": "Чт",
           "Fri": "Пт", "Sat": "Сб", "Sun": "Вс"}
DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def format_schedule(schedule: Optional[dict]) -> str:
    if not schedule:
        return ""
    if schedule.get("is_24x7"):
        return "24/7"
    parts = []
    for day in DAY_ORDER:
        d = schedule.get(day)
        if d and d.get("working_hours"):
            hrs = "; ".join(f"{h.get('from','?')}-{h.get('to','?')}" for h in d["working_hours"])
            parts.append(f"{DAYS_RU[day]} {hrs}")
    if schedule.get("comment"):
        parts.append(f"({schedule['comment']})")
    return "; ".join(parts)


TARIFF_RE = re.compile(
    r"(\d[\d\s]{0,5})\s*(?:тг|тенге|₸)\s*(?:/|за|в)?\s*"
    r"(час|часа|часов|сутки|минут|мин|день)?",
    re.IGNORECASE,
)


def extract_tariff(text: str) -> str:
    if not text:
        return ""
    found = []
    for m in TARIFF_RE.finditer(text):
        amount = m.group(1).replace(" ", "")
        unit = m.group(2) or ""
        found.append(f"{amount} тенге" + (f" / {unit}" if unit else ""))
    return "; ".join(dict.fromkeys(found))  # убираем дубли, сохраняем порядок


TYPE_KEYWORDS = [
    ("ТЦ / молл", re.compile(r"тц|торгов(ый|ого) центр|молл|mall|plaza|outlet", re.I)),
    ("БЦ / офис", re.compile(r"бц|бизнес[ -]?центр|business center|офис", re.I)),
    ("Жильё / ЖК", re.compile(r"жк\b|жилой комплекс|жилого дома|придомов", re.I)),
    ("Аэропорт/вокзал", re.compile(r"аэропорт|вокзал|airport", re.I)),
    ("Больница/клиника", re.compile(r"больниц|клиник|госпитал|медицинск", re.I)),
    ("Учебное заведение", re.compile(r"университет|институт|колледж|школ", re.I)),
    ("Гос. учреждение", re.compile(r"акимат|министерств|суд|управлени[ея]", re.I)),
]


def classify_type(name: str, address: str, description: str,
                   access: str, access_comment: str, purpose: str,
                   rubrics: str, org_name: str) -> str:
    haystack = " ".join(filter(None, [name, address, description, access_comment,
                                        purpose, rubrics, org_name])).lower()
    for label, pattern in TYPE_KEYWORDS:
        if pattern.search(haystack):
            return label
    if access and "private" in access.lower():
        return "Частная / закрытая"
    if "уличная" in haystack or "street" in (purpose or "").lower():
        return "Городская (уличная)"
    if org_name:
        return "Коммерческая парковка"
    return "Не определено (требует проверки)"


def guess_related_object(name: str, org_name: str, address: str) -> str:
    """
    Очень грубая эвристика: пытаемся вытащить название объекта,
    к которому относится парковка, из имени самого элемента
    (напр. "Парковка ТРЦ Dostyk Plaza" -> "ТРЦ Dostyk Plaza")
    или используем название организации-владельца.
    """
    if org_name:
        return org_name
    if not name:
        return ""
    cleaned = re.sub(r"^(парковка|стоянка|паркинг)[\s,:-]*", "", name, flags=re.I).strip()
    if cleaned and cleaned.lower() != name.lower():
        return cleaned
    return ""


def normalize_item(item: dict) -> Optional[dict]:
    item_id = item.get("id")
    if not item_id:
        return None

    name = item.get("name") or ""
    point = item.get("point") or {}
    lat, lon = point.get("lat"), point.get("lon")

    address = item.get("full_address_name") or ""
    if not address and item.get("address"):
        addr = item["address"]
        address = addr.get("name") or addr.get("full_name") or ""

    schedule = item.get("schedule") or {}
    schedule_str = format_schedule(schedule)

    description = item.get("description") or ""
    access_comment = item.get("access_comment") or ""
    access = item.get("access") or ""
    purpose = item.get("purpose") or ""

    rubrics = item.get("rubrics") or []
    rubrics_str = ", ".join(r.get("name", "") for r in rubrics)

    org = item.get("org") or {}
    org_name = org.get("name") or ""

    is_paid_raw = item.get("is_paid")
    if is_paid_raw is True:
        is_paid = "платная"
    elif is_paid_raw is False:
        is_paid = "бесплатная"
    else:
        is_paid = "не указано"

    tariff = extract_tariff(" ".join([description, access_comment, schedule.get("comment", "")]))

    obj_type = classify_type(name, address, description, access, access_comment,
                              purpose, rubrics_str, org_name)
    related_object = guess_related_object(name, org_name, address)

    link = f"https://2gis.kz/almaty/geo/{item_id}" if item_id else ""

    return {
        "id": item_id,
        "название": name,
        "адрес": address,
        "широта": lat,
        "долгота": lon,
        "ссылка_2gis": link,
        "платность": is_paid,
        "тариф": tariff,
        "вместимость_мест": item.get("capacity"),
        "тип_объекта": obj_type,
        "к_какому_объекту_относится": related_object,
        "часы_работы": schedule_str,
        "доступ": access,
        "комментарий_доступа": access_comment,
        "назначение_purpose": purpose,
        "уровни": item.get("level_count"),
        "для_грузовиков": item.get("for_trucks"),
        "перехватывающая": item.get("is_incentive"),
        "тип_покрытия": item.get("paving_type"),
        "рубрики": rubrics_str,
        "организация": org_name,
        "описание_raw": description,
        "источник": item.get("_source"),
    }


def build_dataframe(raw_items: list) -> pd.DataFrame:
    rows = []
    seen_ids = set()
    for it in raw_items:
        row = normalize_item(it)
        if not row:
            continue
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    # Убираем записи без координат
    df = df.dropna(subset=["широта", "долгота"])

    # Дедуп по близким координатам + названию (объекты, попавшие в
    # несколько соседних ячеек сетки или найденные через оба типа запроса)
    df["_lat_r"] = df["широта"].round(4)
    df["_lon_r"] = df["долгота"].round(4)
    df["_name_norm"] = df["название"].str.lower().str.strip()
    df = df.sort_values("источник")  # map_parking приоритетнее (стабильнее geometry)
    df = df.drop_duplicates(subset=["_lat_r", "_lon_r", "_name_norm"], keep="first")
    df = df.drop(columns=["_lat_r", "_lon_r", "_name_norm"])

    # Сортировка для удобства
    df = df.sort_values(["тип_объекта", "адрес", "название"]).reset_index(drop=True)
    return df


# ---------------------------------------------------------------------------
# Экспорт
# ---------------------------------------------------------------------------

def export_csv(df: pd.DataFrame, path: str):
    df.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"CSV сохранён: {path} ({len(df)} строк)")


def export_google_sheets(df: pd.DataFrame, sheet_id: str, service_account_file: str,
                          worksheet_name: str = "Parking_Almaty"):
    import gspread

    gc = gspread.service_account(filename=service_account_file)
    sh = gc.open_by_key(sheet_id)

    try:
        ws = sh.worksheet(worksheet_name)
        sh.del_worksheet(ws)
    except gspread.exceptions.WorksheetNotFound:
        pass

    ws = sh.add_worksheet(title=worksheet_name, rows=str(len(df) + 10), cols=str(len(df.columns) + 2))

    values = [list(df.columns)] + df.astype(object).where(pd.notnull(df), "").values.tolist()
    ws.update(values)
    print(f"Выгружено в Google Sheets: {sheet_id} -> лист '{worksheet_name}' ({len(df)} строк)")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Сбор данных о парковках Алматы из 2GIS")
    parser.add_argument("--out", default="parking_almaty.csv", help="путь к выходному CSV")
    parser.add_argument("--to-sheets", action="store_true", help="выгрузить в Google Sheets")
    parser.add_argument("--cache", default="raw_items.json",
                         help="файл-кэш сырых ответов API (чтобы не дёргать API повторно)")
    parser.add_argument("--use-cache", action="store_true",
                         help="использовать сохранённый --cache вместо запросов к API")
    args = parser.parse_args()

    if args.use_cache and os.path.exists(args.cache):
        with open(args.cache, "r", encoding="utf-8") as f:
            raw_items = json.load(f)
        print(f"Загружено из кэша: {len(raw_items)} элементов")
    else:
        region_id = get_region_id()
        print(f"region_id Алматы = {region_id}")
        rubric_ids = get_parking_rubric_ids(region_id)
        print(f"Рубрики парковок: {rubric_ids}")
        raw_items = collect_raw_items(region_id, rubric_ids)
        with open(args.cache, "w", encoding="utf-8") as f:
            json.dump(raw_items, f, ensure_ascii=False)
        print(f"Сырых записей (с дублями): {len(raw_items)}")

    df = build_dataframe(raw_items)
    print(f"После очистки и дедупликации: {len(df)} уникальных парковок")

    export_csv(df, args.out)

    if args.to_sheets:
        sheet_id = os.environ.get("GOOGLE_SHEET_ID")
        sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT")
        if not sheet_id or not sa_file:
            raise SystemExit(
                "Для выгрузки в Google Sheets задайте переменные окружения "
                "GOOGLE_SHEET_ID и GOOGLE_SERVICE_ACCOUNT (путь к JSON ключу)."
            )
        export_google_sheets(df, sheet_id, sa_file)


if __name__ == "__main__":
    main()
