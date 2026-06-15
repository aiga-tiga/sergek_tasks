#!/usr/bin/env python3
"""
WhatsApp Sender — учебная демонстрация принципа массовой/тестовой рассылки.

РЕЖИМЫ
------
dry-run (по умолчанию):
    Ничего не отправляется. Программа читает таблицу (CSV), проверяет
    номера и тексты сообщений, и записывает результат в:
      - лог-файл (sender.log)  — построчный журнал событий;
      - отчёт (report.csv)     — статус по каждой строке (что было бы отправлено).
    Можно безопасно прогонять на ПОЛНОЙ базе (например, выгрузке из задачи 1).

live:
    Реальная отправка через официальный WhatsApp Cloud API (Meta).
    По условиям задания используется ТОЛЬКО для 1–2 сообщений на
    собственный/тестовый номер. Требует токен и phone_number_id,
    которые выдаются в Meta for Developers (тестовый номер бесплатен).

ОБРАБОТКА ОШИБОК
----------------
- невалидный номер телефона -> строка помечается ERROR, остальные строки
  продолжают обрабатываться;
- пустое сообщение -> ERROR, пропуск;
- обрыв соединения / таймаут -> перехватывается, пишется в лог, программа
  не падает, переходит к следующей строке;
- ограничение скорости (HTTP 429 / rate limit) -> распознаётся отдельно,
  делается пауза перед следующей попыткой;
- любые прочие ошибки API -> текст ошибки выводится в лог без падения скрипта.

ПРИМЕРЫ ЗАПУСКА
---------------
  # dry-run на полной базе из задачи 1
  python whatsapp_sender.py --input messages.csv --mode dry-run

  # live: отправка 1-2 сообщений на свой тестовый номер
  python whatsapp_sender.py --input messages.csv --mode live \
      --max-messages 2 --token "$WA_TOKEN" --phone-id "$WA_PHONE_ID"
"""

import argparse
import csv
import logging
import re
import sys
import time

PHONE_RE = re.compile(r"^\+?[1-9]\d{7,14}$")


def validate_phone(raw):
    """Возвращает номер в формате +<digits> или None, если номер некорректен."""
    if raw is None:
        return None
    cleaned = re.sub(r"[\s\-\(\)]", "", str(raw).strip())
    if PHONE_RE.match(cleaned):
        if not cleaned.startswith("+"):
            cleaned = "+" + cleaned
        return cleaned
    return None


def load_rows(path, phone_col, message_col):
    rows = []
    try:
        with open(path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            if phone_col not in reader.fieldnames or message_col not in reader.fieldnames:
                raise ValueError(
                    f"В файле {path} должны быть столбцы "
                    f"'{phone_col}' и '{message_col}'. "
                    f"Найдены: {reader.fieldnames}"
                )
            for i, row in enumerate(reader, start=1):
                rows.append((i, row.get(phone_col), row.get(message_col)))
    except FileNotFoundError:
        print(f"ОШИБКА: входной файл не найден: {path}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(1)
    return rows


def setup_logger(log_path):
    logger = logging.getLogger("whatsapp_sender")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# DRY-RUN
# ---------------------------------------------------------------------------
def dry_run(rows, logger, report_path):
    ok, errors = 0, 0
    with open(report_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["row", "phone", "message_preview", "status", "reason"])

        for idx, phone, message in rows:
            phone_norm = validate_phone(phone)
            preview = (message or "")[:60].replace("\n", " ")

            if phone_norm is None:
                logger.warning(f"Строка {idx}: невалидный номер '{phone}' -> пропуск")
                writer.writerow([idx, phone, preview, "ERROR", "invalid_phone"])
                errors += 1
                continue

            if not message or not str(message).strip():
                logger.warning(f"Строка {idx}: пустое сообщение для {phone_norm} -> пропуск")
                writer.writerow([idx, phone_norm, preview, "ERROR", "empty_message"])
                errors += 1
                continue

            logger.info(f"Строка {idx}: [DRY-RUN] было бы отправлено на {phone_norm}: {preview}")
            writer.writerow([idx, phone_norm, preview, "DRY_RUN_OK", ""])
            ok += 1

    logger.info(f"DRY-RUN завершён. would_send={ok}, errors={errors}, total={len(rows)}")
    return ok, errors


# ---------------------------------------------------------------------------
# LIVE (WhatsApp Cloud API)
# ---------------------------------------------------------------------------
def send_via_cloud_api(phone, message, token, phone_id, logger, timeout=15, retries=1):
    """
    Отправляет одно сообщение через WhatsApp Cloud API.
    Возвращает (success: bool, status: str).
    Никогда не бросает исключение наружу — все сетевые/HTTP ошибки перехватываются.
    """
    import requests

    url = f"https://graph.facebook.com/v18.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone.lstrip("+"),
        "type": "text",
        "text": {"body": message},
    }

    for attempt in range(1, retries + 2):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Обрыв соединения при отправке на {phone}: {e}")
            return False, "connection_error"
        except requests.exceptions.Timeout:
            logger.error(f"Таймаут запроса при отправке на {phone}")
            return False, "timeout"
        except requests.exceptions.RequestException as e:
            logger.error(f"Сетевая ошибка при отправке на {phone}: {e}")
            return False, "request_error"

        if resp.status_code == 200:
            return True, "ok"

        if resp.status_code == 429:
            logger.warning(f"Превышен лимит запросов (429) для {phone}, попытка {attempt}")
            if attempt <= retries:
                time.sleep(5)
                continue
            return False, "rate_limited"

        try:
            err_json = resp.json()
            err_msg = err_json.get("error", {}).get("message", resp.text)
        except ValueError:
            err_msg = resp.text

        logger.error(f"Ошибка API ({resp.status_code}) при отправке на {phone}: {err_msg}")
        return False, f"api_error_{resp.status_code}"

    return False, "unknown_error"


def live_run(rows, logger, token, phone_id, max_messages=2, delay_between=3):
    if not token or not phone_id:
        logger.error(
            "Для режима live требуются --token и --phone-id "
            "(получаются в Meta for Developers -> WhatsApp -> тестовый номер)."
        )
        return 0, 0

    sent, errors = 0, 0
    to_process = [r for r in rows if r[1] and r[2]][:max_messages]

    if not to_process:
        logger.warning("Нет подходящих строк для отправки (нужны номер и текст).")

    for idx, phone, message in to_process:
        phone_norm = validate_phone(phone)
        if phone_norm is None:
            logger.warning(f"Строка {idx}: невалидный номер '{phone}' -> пропуск")
            errors += 1
            continue

        if not message or not str(message).strip():
            logger.warning(f"Строка {idx}: пустое сообщение -> пропуск")
            errors += 1
            continue

        logger.info(f"Строка {idx}: отправка LIVE-сообщения на {phone_norm}...")
        success, status = send_via_cloud_api(phone_norm, message, token, phone_id, logger)

        if success:
            logger.info(f"Строка {idx}: успешно отправлено на {phone_norm} (status=ok)")
            sent += 1
        else:
            logger.error(f"Строка {idx}: НЕ отправлено на {phone_norm} (причина: {status})")
            errors += 1

        time.sleep(delay_between)

    logger.info(f"LIVE завершён. sent={sent}, errors={errors}, processed={len(to_process)}")
    return sent, errors


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="WhatsApp Sender (демонстрация принципа)")
    parser.add_argument("--input", required=True, help="CSV с колонками номера и текста")
    parser.add_argument("--phone-col", default="phone_number", help="имя столбца с номером")
    parser.add_argument("--message-col", default="message", help="имя столбца с текстом")
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run",
                         help="dry-run (по умолчанию) — только лог/отчёт; "
                              "live — реальная отправка 1-2 сообщений")
    parser.add_argument("--log-file", default="sender.log")
    parser.add_argument("--report-file", default="report.csv")
    parser.add_argument("--max-messages", type=int, default=2,
                         help="ограничение на число реальных отправок в live-режиме")
    parser.add_argument("--token", default=None, help="токен WhatsApp Cloud API (live)")
    parser.add_argument("--phone-id", default=None, help="phone_number_id WhatsApp Cloud API (live)")
    args = parser.parse_args()

    logger = setup_logger(args.log_file)
    rows = load_rows(args.input, args.phone_col, args.message_col)
    logger.info(f"Загружено строк: {len(rows)} из {args.input} | режим: {args.mode}")

    try:
        if args.mode == "dry-run":
            dry_run(rows, logger, args.report_file)
        else:
            if args.max_messages > 2:
                logger.warning(
                    "По условиям задания live-режим ограничен 1-2 сообщениями "
                    "на свой/тестовый номер. Принудительно ограничиваю до 2."
                )
                args.max_messages = 2
            live_run(rows, logger, args.token, args.phone_id, args.max_messages)
    except KeyboardInterrupt:
        logger.warning("Прервано пользователем (Ctrl+C). Завершение без падения.")
    except Exception as e:
        # Последний рубеж: программа не должна "падать" неконтролируемо.
        logger.error(f"Непредвиденная ошибка: {e}")


if __name__ == "__main__":
    main()
