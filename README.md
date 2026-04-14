## Trading Bot

This project is a trading bot for working with the T-Invest API.

The bot implements a grid trading strategy:

- Places limit orders above and below the current price.
- Tracks order execution.
- Automatically rebuilds the grid.
- Supports stop-loss.
- Can run in simulation mode (dry_run).

### Run the Bot

```bash
python grid_bot_sdk.py
```

### Requirements

- python 3.9+
- pip
- Git

### Setup

#### 1. Installation

Clone the repository:

```bash
git clone https://github.com/noneandundefined/tinvest-grid-bot.git
cd tinvest-grid-bot
```

#### 2. Install dependencies

```bash
pip install -r requirements.txt
```

#### 3. Configuration

Create a `.env` file in the project root.

```.env
INVEST_TOKEN=your_token_here
ACCOUNT_ID=your_account_id
```

#### 4. Bot configuration

```python
couples = {
    "VKCO": {
        "enable": "ON", # ON / OFF
        "symbol": "VKCO", # MOEX ticker
        "class_code": "TQBR", # default class for MOEX stocks
        "size": 1, # number of lots per grid order
        "orders_side": 23, # number of limit orders on each side (buy/sell)
        "range_pct": 7, # total grid range: +20% / -20% from the anchor price
        "sl": 10, # stop-loss: % below the lowest buy level (for long position)

        # False: on API error 90001 "Need confirmation", the bot retries with confirmation.
        # True: sends orders immediately with confirm_margin_trade flag (for margin accounts).
        "confirm_margin_trade": True,
        "dry_run": False, # True — logs only, no real orders.
    }
}
```
