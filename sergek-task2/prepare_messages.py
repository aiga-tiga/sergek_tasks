#!/usr/bin/env python3
"""
Подготовка таблицы для рассыльщика (whatsapp_sender.py) на основе
выгрузки парковок из задачи 1 (almaty_parkings.csv).

Создаёт CSV с двумя столбцами: phone_number, message.

Для dry-run всем строкам присваивается один и тот же тестовый номер
(--test-number) — он не используется для реальной отправки, нужен только
чтобы продемонстрировать обработку полной таблицы.

Пример:
    python prepare_messages.py --input almaty_parkings.csv \
        --output messages.csv --test-number +77001234567
"""

import argparse
import pandas as pd


def build_message(row) -> str:
    parts = [f"Парковка: {row.get('name') or 'без названия'}"]

    address = row.get("address")
    if isinstance(address, str) and address.strip():
        parts.append(f"Адрес: {address}")

    rating = row.get("rating")
    if pd.notna(rating):
        parts.append(f"Рейтинг: {rating}")

    is_24x7 = row.get("is_24x7")
    if pd.notna(is_24x7):
        parts.append("Режим: круглосуточно" if bool(is_24x7) else "Режим: не круглосуточно")

    link = row.get("2gis_link")
    if isinstance(link, str) and link.strip():
        parts.append(f"2GIS: {link}")

    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="almaty_parkings.csv")
    parser.add_argument("--output", default="messages.csv")
    parser.add_argument("--test-number", default="+77000000000",
                         help="номер, используемый для всех строк в dry-run демонстрации")
    parser.add_argument("--limit", type=int, default=None,
                         help="ограничить число строк (для быстрых тестов)")
    args = parser.parse_args()

    df = pd.read_csv(args.input)
    if args.limit:
        df = df.head(args.limit)

    out = pd.DataFrame({
        "phone_number": args.test_number,
        "message": df.apply(build_message, axis=1),
    })
    out.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"Сохранено {len(out)} строк в {args.output}")


if __name__ == "__main__":
    main()
