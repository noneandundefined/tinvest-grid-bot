import os

TOKEN = ""

couples = {
    "VKCO": {
        "enable": "ON",  # ON / OFF
        "symbol": "VKCO",  # тикер MOEX
        "class_code": "TQBR",  # класс по умолчанию для акций на МосБирже
        "size": 1,  # лотов в одном ордере сетки
        "orders_side": 23,  # число лимиток ниже и выше цены (каждая сторона)
        "range_pct": 7,  # общий диапазон сетки: +20% / -20% от якорной цены
        "sl": 10,  # стоп-лосс, % ниже самого нижнего уровня покупок (для длинной позиции)
        # False: при ответе API 90001 «Need confirmation» бот сам повторит заявку с подтверждением.
        # True: сразу слать с confirm_margin_trade (удобно для маржинального счёта).
        "confirm_margin_trade": True,
        "dry_run": False,  # True — только логи, без выставления заявок
    }
}

POLL_INTERVAL_SEC = 3.0

ACCOUNT_ID = os.environ.get("ACCOUNT_ID", "").strip()
