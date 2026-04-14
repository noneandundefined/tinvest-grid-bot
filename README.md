## Trading Bot

Данный проект представляет собой торгового бота для работы с API Т-Инвестиций.

Бот реализует сеточную стратегию (grid trading):

- Выставляет лимитные ордера выше и ниже текущей цены.
- Отслеживает исполнение ордеров.
- Автоматически перестраивает сетку.
- Поддерживает стоп-лосс.
- Может работать в режиме симуляции (dry_run).

### Запуск бота

```bash
python grid_bot_sdk.py
```

### Требования

- python 3.9+
- pip
- Git

### Установка

1. Клонирование репозитория

```bash
git clone https://github.com/noneandundefined/tinvest-grid-bot.git
cd tinvest-grid-bot
```

### Установка зависимостей

```bash
pip install -r requirements.txt
```

### Настройка venv и установка зависимостей

```bash
pip install -r requirements.txt
```

### Настройка

Создай файл .env в корне проекта.

```.env
INVEST_TOKEN=your_token_here
ACCOUNT_ID=your_account_id
```

### Конфигурация бота

```python
couples = {
    "VKCO": {
        "enable": "ON", # ON / OFF
        "symbol": "VKCO", # тикер MOEX
        "class_code": "TQBR", # класс по умолчанию для акций на МосБирже
        "size": 1, # лотов в одном ордере сетки
        "orders_side": 23, # число лимиток ниже и выше цены (каждая сторона)
        "range_pct": 7, # общий диапазон сетки: +20% / -20% от якорной цены
        "sl": 10, # стоп-лосс, % ниже самого нижнего уровня покупок (для длинной позиции) # False: при ответе API 90001 «Need confirmation» бот сам повторит заявку с подтверждением. # True: сразу слать с confirm_margin_trade (удобно для маржинального счёта).
        "confirm_margin_trade": True,
        "dry_run": False, # True — только логи, без выставления заявок
    }
}
```
