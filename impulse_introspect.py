#!/usr/bin/env python3
"""
impulse_introspect.py — снимает реальную схему полей с вашего аккаунта
impulseCRM: по одной записи каждой из 25 сущностей, чтобы увидеть
фактические имена и типы полей. Только чтение, ничего не изменяет.

Зачем: база знаний impulseCRM не публикует состав полей сущностей и
синтаксис фильтра `columns` (см. Справочник по API impulseCRM,
разделы 3 и 10) — быстрее и надёжнее посмотреть один реальный ответ,
чем угадывать по документации.

Запуск (macOS/Linux, bash):
    pip install requests
    export IMPULSE_BASE="https://ВАШ-ДОМЕН.impulsecrm.ru"
    export IMPULSE_USER="логин"
    export IMPULSE_KEY="ключ-api"
    export IMPULSE_API_PATH="/api/public/{entity}/{action}"   # из «Примера запроса»
    python impulse_introspect.py --md schema.md --json schema.json

Запуск (Windows, PowerShell — export там не работает, нужен $env:):
    pip install requests
    $env:IMPULSE_BASE="https://ВАШ-ДОМЕН.impulsecrm.ru"
    $env:IMPULSE_USER="логин"
    $env:IMPULSE_KEY="ключ-api"
    $env:IMPULSE_API_PATH="/api/public/{entity}/{action}"
    python impulse_introspect.py --md schema.md --json schema.json

После запуска сверьте вывод с константами IMPULSE_FIELD_* в settings.py
и поправьте несовпадающие имена полей через .env — без правки кода.
"""

import argparse
import json
import os
import sys
from typing import Any, Dict, List

import requests

ENTITIES = [
    "branch", "client", "ancestor", "deposit",
    "group_account", "group_single", "individual_account", "individual_single",
    "self_account", "self_single", "rent_account", "rent_single",
    "product", "employee_balance", "charge", "hall", "teacher", "user",
    "style", "informer", "group", "pipeline", "status",
    "schedule", "reservation",
]


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def fetch_sample(session: requests.Session, base: str, api_path: str, entity: str) -> Dict[str, Any]:
    url = f"{base}{api_path.format(entity=entity, action='list')}"
    try:
        # page нумеруется с 1 (подтверждено примером запроса из личного
        # кабинета), не с 0.
        response = session.post(url, json={"limit": 1, "page": 1}, timeout=20)
    except requests.RequestException as e:
        return {"error": f"сеть: {e}"}

    if response.status_code >= 400:
        return {"error": f"HTTP {response.status_code}: {response.text[:300]}"}

    try:
        data = response.json()
    except ValueError:
        return {"error": f"не JSON: {response.text[:300]}"}

    # Пробуем несколько вероятных форм обёртки ответа — она тоже не
    # подтверждена вендором.
    items: List[Dict[str, Any]] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("items", "data", "list", "result", "results"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            if "id" in data:
                items = [data]

    if not items:
        return {"error": "пусто (нет записей или неизвестная форма ответа)", "raw_keys": list(data.keys()) if isinstance(data, dict) else None}

    return {"sample": items[0]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--md", default="schema.md", help="путь для markdown-отчёта")
    parser.add_argument("--json", dest="json_path", default="schema.json", help="путь для JSON-дампа")
    parser.add_argument(
        "--entities", nargs="*", default=None,
        help="ограничить список сущностей (по умолчанию — все 25)",
    )
    args = parser.parse_args()

    base = os.environ.get("IMPULSE_BASE", "").rstrip("/")
    user = os.environ.get("IMPULSE_USER", "")
    key = os.environ.get("IMPULSE_KEY", "")
    api_path = os.environ.get("IMPULSE_API_PATH", "/api/public/{entity}/{action}")

    if not base or not user or not key:
        print("Нужны переменные окружения IMPULSE_BASE, IMPULSE_USER, IMPULSE_KEY", file=sys.stderr)
        return 1

    entities = args.entities or ENTITIES
    session = requests.Session()
    # ВАЖНО: реальный пример запроса из личного кабинета показывает
    # "Authorization: Basic <ключ>" БЕЗ base64(login:key) — requests'
    # session.auth=(user, key) считает именно base64(user:key), что даёт
    # ДРУГОЙ, неверный заголовок. Поэтому здесь явный заголовок, а не auth=.
    session.headers["Authorization"] = f"Basic {key}"
    session.headers["Content-Type"] = "application/json"

    report: Dict[str, Any] = {}
    md_lines = ["# Схема полей impulseCRM (снята автоматически)\n"]

    for entity in entities:
        print(f"→ {entity} ...", file=sys.stderr)
        result = fetch_sample(session, base, api_path, entity)
        report[entity] = result

        md_lines.append(f"## {entity}\n")
        if "error" in result:
            md_lines.append(f"⚠️ {result['error']}\n")
            continue

        sample = result["sample"]
        md_lines.append("| поле | тип | пример значения |")
        md_lines.append("|---|---|---|")
        for field, value in sample.items():
            example = json.dumps(value, ensure_ascii=False)
            if len(example) > 80:
                example = example[:80] + "…"
            md_lines.append(f"| `{field}` | {_type_name(value)} | {example} |")
        md_lines.append("")

    with open(args.md, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    with open(args.json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\nГотово: {args.md}, {args.json_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
