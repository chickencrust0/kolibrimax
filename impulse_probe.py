#!/usr/bin/env python3
"""
impulse_probe.py — ПРОВЕРКА внутреннего API отметки посещений
(/api/check_visits/...) вашего аккаунта impulseCRM.

ТОЛЬКО ЧТЕНИЕ. Скрипт не отправляет ни одного запроса, изменяющего
данные: никаких check / burn_one, только GET visits и пробные обращения,
по коду ответа которых видно, принимается ли API-ключ.

Зачем: эндпоинты check_visits найдены разбором фронтенда impulseCRM и
вендором не документированы. Главный риск — авторизация: браузер ходит
с cookie-сессией, а бот с "Authorization: Basic <apiToken>". Этот скрипт
отвечает на вопрос «принимает ли внутренний API наш ключ» до того, как
бот попробует что-то записать.

Запуск (Windows, PowerShell):
    $env:IMPULSE_BASE="https://akademiakolibriyandexru.impulsecrm.ru"
    $env:IMPULSE_KEY="ваш-ключ"
    python impulse_probe.py

Что означает результат:
    200 + JSON  -> ключ подходит, можно включать IMPULSE_CHECK_VISITS_ENABLED=true
    401/403     -> внутренний API требует сессию браузера, ключ не годится
    HTML вместо JSON -> редирект на страницу входа, то же самое
    404         -> путь другой; уточните его в DevTools (см. ниже)
"""

import json
import os
import sys
from datetime import datetime, timezone

import requests

# Внутренние эндпоинты impulseCRM — POST-только: на GET Symfony отвечает
# MethodNotAllowed (Allow: POST). Раньше проба ходила GET-ом и получала
# HTML страницы ошибки, что легко перепутать со страницей входа.
#
# ВНИМАНИЕ: сюда включены только БЕЗОПАСНЫЕ вызовы. check и burn_one
# НАМЕРЕННО не вызываются даже пустым телом — они меняют данные.
PATHS = [
    # Контрольный вызов: обычный публичный метод, который у бота точно
    # работает. Нужен для сравнения — если он отдаёт JSON, а check_visits
    # отдаёт 401, значит дело не в формате ключа, а в правах именно на
    # эти методы. Если 401 у обоих — проблема в самом ключе/заголовке.
    ("POST", "client/list", {"limit": 1, "page": 1}),
    ("POST", "check_visits/visits", {}),
    ("POST", "client/last_accounts", {}),
    # GET по существующему POST-роуту: ждём 405 Allow: POST. Это отличный
    # индикатор — он доказывает, что роутинг отработал, то есть запрос
    # дошёл до приложения, а не был завёрнут на страницу входа.
    ("GET", "check_visits/visits", None),
]

SYMFONY_MARKERS = (
    "MethodNotAllowedHttpException",
    "NotFoundHttpException",
    "AccessDeniedHttpException",
    "UnauthorizedHttpException",
    "Symfony\\Component",
    "Код ошибки",
)

LOGIN_MARKERS = ("<form", "password", "login", "Вход", "авторизац")

# Оболочка SPA (index.html Vue-приложения). Сервер отдаёт её со статусом
# 200 на любой путь, который не прошёл аутентификацию — вместо честного
# 401. Поэтому "200 + SPA" означает НЕ успех, а отказ в доступе.
SPA_MARKERS = (
    "notranslate translate=no",
    "<div id=app",
    '<div id="app"',
    "X-UA-Compatible",
)


def classify(response: requests.Response) -> tuple:
    """
    Возвращает (вердикт, пояснение).

    Ключевое различие, которое легко спутать: HTML-страница ошибки
    Symfony (роутинг отработал, приложение ответило) и HTML-страница
    входа (запрос завёрнут авторизацией). Первое означает, что доступ
    ЕСТЬ, второе — что ключ не принят.
    """
    ctype = response.headers.get("Content-Type", "")
    body = response.text
    head = body[:2000]

    if "json" in ctype.lower():
        try:
            return "JSON", json.dumps(response.json(), ensure_ascii=False)[:400]
        except ValueError:
            pass

    if any(m in head for m in SYMFONY_MARKERS):
        line = next(
            (ln.strip() for ln in head.splitlines()
             if any(m in ln for m in SYMFONY_MARKERS)),
            head[:200],
        )
        return "SYMFONY", f"ответило приложение (роутинг отработал): {line[:250]}"

    if "<html" in head.lower() and any(m in head.lower() for m in LOGIN_MARKERS):
        return "LOGIN", "HTML со страницей входа — запрос завёрнут авторизацией"

    if sum(1 for m in SPA_MARKERS if m in head) >= 2:
        return "SPA", (
            "оболочка веб-приложения вместо данных — запрос не прошёл "
            "аутентификацию (сервер отдаёт index.html вместо 401)"
        )

    if "<html" in head.lower():
        return "HTML", f"HTML без явных признаков входа: {head[:200]}"

    return "OTHER", f"{ctype or 'без типа'}: {head[:200]}"


def main() -> int:
    base = os.environ.get("IMPULSE_BASE", "").rstrip("/")
    key = os.environ.get("IMPULSE_KEY", "")
    internal = os.environ.get("IMPULSE_INTERNAL_PATH", "/api/public/{path}")

    if not base or not key:
        print("Нужны переменные окружения IMPULSE_BASE и IMPULSE_KEY", file=sys.stderr)
        return 1

    today_ts = int(datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp())

    cookie = os.environ.get("IMPULSE_SESSION_COOKIE", "").strip()

    session = requests.Session()
    session.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
    })
    # Ключ шлём всегда, cookie добавляем сверху — так протухшая cookie не
    # "съедает" рабочий ключ.
    session.headers["Authorization"] = f"Basic {key}"
    if cookie:
        session.headers["Cookie"] = cookie
        print("Авторизация: API-ключ + cookie сессии\n")
    else:
        print("Авторизация: API-ключ (Authorization: Basic ...)\n")

    print(f"База: {base}")
    print(f"Шаблон внутреннего пути: {internal}")
    print(f"Пробная дата (Unix): {today_ts}\n")

    reached_app = False
    got_json = False
    hit_login = False
    control_ok = False
    internal_denied = False

    for method, path, body in PATHS:
        url = f"{base}{internal.format(path=path)}"
        print(f"--- {method} {url}")
        try:
            if method == "POST":
                payload = dict(body or {})
                payload.setdefault("date", today_ts)
                response = session.post(
                    url, json=payload, timeout=20, allow_redirects=False
                )
            else:
                response = session.get(
                    url, params={"date": today_ts}, timeout=20, allow_redirects=False
                )
        except requests.RequestException as e:
            print(f"    сеть: {e}\n")
            continue

        print(f"    HTTP {response.status_code}")
        allow = response.headers.get("Allow")
        if allow:
            print(f"    Allow: {allow}")

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location", "")
            print(f"    редирект -> {location}")
            if any(m in location.lower() for m in ("login", "signin", "auth")):
                hit_login = True
                print("    (редирект на вход = запрос завёрнут авторизацией)")
        else:
            verdict, detail = classify(response)
            print(f"    [{verdict}] {detail}")
            is_control = path == "client/list"
            if verdict == "JSON":
                reached_app = True
                if response.status_code == 401:
                    if not is_control:
                        internal_denied = True
                else:
                    got_json = True
                    if is_control:
                        control_ok = True
            elif verdict == "SYMFONY":
                # Приложение ответило своим исключением — значит запрос
                # дошёл до роутера, а не был перехвачен страницей входа.
                reached_app = True
            elif verdict in ("LOGIN", "SPA"):
                hit_login = True

        if response.status_code in (401, 403):
            hit_login = True
        print()

    # Если внутренние методы заработали С cookie — проверяем, нужна ли она
    # на самом деле: повторяем ключевой вызов БЕЗ cookie. От этого зависит,
    # сможет ли бот работать постоянно или до протухания сессии.
    if cookie and got_json and not internal_denied:
        print("=" * 60)
        print("КОНТРОЛЬ: нужен ли cookie? Повторяю check_visits/visits")
        print("только с API-ключом, без cookie...\n")
        bare = requests.Session()
        bare.headers.update({
            "Authorization": f"Basic {key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })
        url = f"{base}{internal.format(path='check_visits/visits')}"
        try:
            r = bare.post(url, json={"date": today_ts}, timeout=20, allow_redirects=False)
            verdict, detail = classify(r)
            print(f"    HTTP {r.status_code} [{verdict}] {detail[:160]}")
            print()
            if verdict == "JSON" and r.status_code == 200:
                print("РЕЗУЛЬТАТ: cookie НЕ НУЖНА — хватает API-ключа.")
                print("Уберите IMPULSE_SESSION_COOKIE из .env и поставьте")
                print("IMPULSE_CHECK_VISITS_ENABLED=true — бот будет работать")
                print("постоянно, без ручного обновления сессии.")
            else:
                print("РЕЗУЛЬТАТ: cookie НУЖНА — по одному ключу доступа нет.")
                print("Бот заработает с IMPULSE_SESSION_COOKIE в .env, но её")
                print("придётся обновлять при протухании. Постоянное решение —")
                print("запросить доступ к check_visits/* по API-ключу у")
                print("поддержки impulseCRM (@impulsCRM_manager).")
        except requests.RequestException as e:
            print(f"    сеть: {e}")
        return 0

    print("=" * 60)
    if control_ok and internal_denied:
        print("ВЫВОД: ключ рабочий, но прав на внутренние методы нет.")
        print("Контрольный client/list отдал данные тем же ключом, а")
        print("check_visits/* отвечает 401 «Требуется аутентификация».")
        print("Значит формат авторизации верный, а доступ к отметке")
        print("посещений по API-ключу просто не выдан.")
        print("")
        print("ЧТО ДЕЛАТЬ: это готовый вопрос поддержке impulseCRM")
        print("(@impulsCRM_manager): «методы check_visits/* и")
        print("client/last_accounts возвращают 401 по API-ключу, тогда как")
        print("client/list работает — как получить к ним доступ?»")
        print("Временный обход — cookie сессии в IMPULSE_SESSION_COOKIE.")
        return 0

    if got_json:
        print("ВЫВОД: внутренний API вернул JSON — ключ принят.")
        print("Можно ставить IMPULSE_CHECK_VISITS_ENABLED=true в .env.")
    elif reached_app and not hit_login:
        print("ВЫВОД: запросы доходят до приложения (оно отвечает своими")
        print("исключениями Symfony), на страницу входа не заворачивает.")
        print("Скорее всего доступ есть, но не угадано тело запроса или метод.")
        print("Сверьте реальный запрос в DevTools -> Payload и пришлите его.")
    elif hit_login:
        print("ВЫВОД: запрос НЕ прошёл аутентификацию.")
        if cookie:
            print("Cookie задана, но не принята — скорее всего протухла.")
            print("Скопируйте свежую из DevTools -> Application -> Cookies.")
        else:
            print("API-ключ для внутреннего API не годится: сервер отдаёт")
            print("оболочку веб-приложения вместо данных.")
            print("")
            print("РЕШАЮЩАЯ ПРОВЕРКА — повторите пробу с cookie сессии:")
            print('    $env:IMPULSE_SESSION_COOKIE="PHPSESSID=..."')
            print("    python impulse_probe.py")
            print("Если с cookie появится JSON — значит нужна именно сессия,")
            print("и вопрос к поддержке impulseCRM: как получить постоянный")
            print("доступ к этим методам по API-ключу.")
    else:
        print("ВЫВОД: неоднозначно. Пришлите вывод выше целиком.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
