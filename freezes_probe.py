#!/usr/bin/env python3
"""
freezes_probe.py — почему бот показывает 0 беспричинных заморозок.

ТОЛЬКО ЧТЕНИЕ. Скрипт ничего не меняет в CRM: делает те же запросы, что
и бот при показе остатка, и печатает СЫРОЙ ответ.

Смысл: «0 заморозок» может означать три совершенно разные вещи, и по
самому боту их не различить —
    1) клиент не найден (load вернул не ту запись или список неполон);
    2) в записи клиента нет поля address вовсе;
    3) поле есть, но число лежит в форме, которую разбор не узнаёт
       (например, вложено в объект с неожиданным ключом).
Скрипт печатает, какой из трёх случаев ваш, и показывает реальную
структуру поля — по ней разбор чинится точно, а не наугад.

Запуск:
    python freezes_probe.py                  # первые 5 клиентов
    python freezes_probe.py --phone +79001234567
    python freezes_probe.py --id 754
    python freezes_probe.py --all            # все клиенты, у кого есть address

Переменные берутся из .env рядом со скриптом (как у самого бота).
"""

import argparse
import asyncio
import json
import logging
import sys

import settings
from impulse_client import ImpulseCRMClient

logging.basicConfig(
    level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr
)


def _name(client) -> str:
    parts = [
        client.get(settings.IMPULSE_FIELD_CLIENT_LAST_NAME),
        client.get(settings.IMPULSE_FIELD_CLIENT_FIRST_NAME),
        client.get(settings.IMPULSE_FIELD_CLIENT_MIDDLE_NAME),
    ]
    return " ".join(str(p) for p in parts if p) or "—"


def report(client) -> None:
    field = ImpulseCRMClient.freezes_field(client)
    raw = client.get(field, "<ПОЛЯ НЕТ В ЗАПИСИ>")
    parsed = ImpulseCRMClient.parse_free_freezes(client)

    print(f"\n{'=' * 62}")
    print(f"Клиент id={client.get('id')}  {_name(client)}")
    print(f"  поле счётчика : {field}")
    print(f"  сырое значение: {raw!r}")
    print(f"  тип значения  : {type(raw).__name__}")
    print(f"  бот прочитал  : {parsed}")

    if raw == "<ПОЛЯ НЕТ В ЗАПИСИ>":
        print("  ⚠️  Поля нет в ответе CRM. Либо оно называется иначе, либо")
        print("      list не отдаёт его без явного параметра fields.")
        print("      Все поля этой записи:")
        print("      " + ", ".join(sorted(client.keys())))
    elif parsed == 0 and raw not in (None, "", []):
        print("  ⚠️  Поле есть, но числа в нём не нашлось. Структура целиком:")
        print("      " + json.dumps(raw, ensure_ascii=False, indent=6)[:1500])


async def run(args) -> int:
    impulse = ImpulseCRMClient()
    try:
        print(f"CRM     : {settings.IMPULSE_DOMAIN}")
        print(f"Поле    : {settings.IMPULSE_FIELD_CLIENT_FREEZES or '(автоопределение)'}")

        clients = await impulse.load_all_clients()
        print(f"Клиентов в списке: {len(clients)}")
        if not clients:
            print("❌ Список клиентов пуст — проблема не в разборе поля, а в доступе.")
            return 1

        # Все ключи первой записи: сразу видно, есть ли вообще поле адреса
        # и как оно называется в вашей установке.
        print("\nПоля, которые CRM отдаёт в записи клиента:")
        print("  " + ", ".join(sorted(clients[0].keys())))

        if args.id:
            found = [c for c in clients if str(c.get("id")) == str(args.id)]
            if not found:
                print(f"\n❌ Клиент id={args.id} не найден в списке из {len(clients)}.")
                print("   Это и есть причина нуля: бот ищет запись так же.")
                return 1
            for c in found:
                report(c)

            # Отдельно проверяем путь, которым ходит сам бот.
            loaded = await impulse.load("client", args.id)
            print(f"\nload('client', {args.id}) вернул id="
                  f"{(loaded or {}).get('id')!r} — должно совпасть с {args.id}")
            if loaded and str(loaded.get("id")) != str(args.id):
                print("   ⚠️  ВЕРНУЛСЯ ДРУГОЙ КЛИЕНТ. Остаток заморозок берётся")
                print("       из чужой карточки — вот источник нуля.")

        elif args.phone:
            client = await impulse.find_customer_by_phone(args.phone)
            if not client:
                print(f"\n❌ Клиент с телефоном {args.phone} не найден.")
                return 1
            report(client)

        elif args.all:
            shown = 0
            for c in clients:
                if c.get(ImpulseCRMClient.freezes_field(c)):
                    report(c)
                    shown += 1
            print(f"\nЗаписей с заполненным полем адреса: {shown} из {len(clients)}")

        else:
            for c in clients[:5]:
                report(c)
            print(f"\n(показаны первые 5 из {len(clients)}; "
                  f"для конкретного клиента: --id 754 или --phone +7…)")

        return 0
    finally:
        await impulse.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", help="id клиента в CRM")
    parser.add_argument("--phone", help="телефон клиента")
    parser.add_argument("--all", action="store_true",
                        help="все клиенты с заполненным полем адреса")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
