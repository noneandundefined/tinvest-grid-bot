from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict, List, Optional, Set, Tuple

from grpc import StatusCode

from t_tech.invest import Client
from t_tech.invest.exceptions import RequestError
from t_tech.invest.schemas import (
    AccountStatus,
    AccountType,
    InstrumentIdType,
    OrderDirection,
    OrderExecutionReportStatus,
    OrderType,
    StopOrderDirection,
    StopOrderExpirationType,
    StopOrderType,
)
from t_tech.invest.utils import decimal_to_quotation, quotation_to_decimal

logger = logging.getLogger("grid_bot")


def _api_needs_trade_confirmation(err: RequestError) -> bool:
    """90001 + trailing metadata «Need confirmation» — нужен флаг confirm_margin_trade."""
    if err.code != StatusCode.FAILED_PRECONDITION:
        return False
    if (err.details or "").strip() == "90001":
        return True
    msg = ""
    if err.metadata is not None and getattr(err.metadata, "message", None):
        msg = str(err.metadata.message).lower()
    return "confirm" in msg


def _log_request_error(ctx: str, ex: RequestError) -> None:
    extra = ""
    if ex.metadata is not None and getattr(ex.metadata, "message", None):
        extra = f" — {ex.metadata.message!r}"
    logger.error("%s: %s %s%s", ctx, ex.code.name, ex.details, extra)


@dataclass
class CoupleParams:
    setup_name: str
    symbol: str
    class_code: str
    lots_per_order: int
    orders_side: int
    range_pct: Decimal
    sl_pct: Decimal
    confirm_margin_trade: bool
    dry_run: bool


def _parse_enable(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    s = str(v).strip().upper()
    return s in ("ON", "1", "TRUE", "YES", "ДА")


def parse_couple(setup_name: str, raw: Dict[str, Any]) -> CoupleParams:
    symbol = str(raw.get("symbol", setup_name)).strip().upper()
    class_code = str(raw.get("class_code", "TQBR")).strip().upper()
    size = int(raw["size"])
    orders_side = int(raw["orders_side"])
    range_pct = Decimal(str(raw["range_pct"]))
    sl = Decimal(str(raw.get("sl", 0)))
    if size < 1:
        raise ValueError(f"{setup_name}: size must be >= 1")
    if orders_side < 1:
        raise ValueError(f"{setup_name}: orders_side must be >= 1")
    if range_pct <= 0:
        raise ValueError(f"{setup_name}: range_pct must be > 0")
    if sl < 0:
        raise ValueError(f"{setup_name}: sl must be >= 0")
    return CoupleParams(
        setup_name=setup_name,
        symbol=symbol,
        class_code=class_code,
        lots_per_order=size,
        orders_side=orders_side,
        range_pct=range_pct,
        sl_pct=sl,
        confirm_margin_trade=bool(raw.get("confirm_margin_trade", False)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def round_to_increment(price: Decimal, inc: Decimal) -> Decimal:
    if inc <= 0:
        return price.quantize(Decimal("0.01"))
    q = (price / inc).to_integral_value(rounding=ROUND_HALF_UP)
    return (q * inc).quantize(inc)


def build_grid_prices(
    ref: Decimal, range_pct: Decimal, n: int, inc: Decimal, side: str
) -> List[Decimal]:
    """n уникальных уровней ниже (buy) или выше (sell) в диапазоне range_pct от ref."""
    out: List[Decimal] = []
    seen: Set[Decimal] = set()
    if n < 1:
        return out
    step_pct = range_pct / Decimal(n)
    k = 1
    max_k = n * 50
    while len(out) < n and k <= max_k:
        delta = step_pct / Decimal(100) * Decimal(k)
        if side == "buy":
            p = ref * (Decimal(1) - delta)
        else:
            p = ref * (Decimal(1) + delta)
        if p <= 0:
            k += 1
            continue
        rp = round_to_increment(p, inc)
        if side == "buy" and rp >= ref:
            k += 1
            continue
        if side == "sell" and rp <= ref:
            k += 1
            continue
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
        k += 1
    return out


def setup_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(logging.DEBUG)
    h.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)-5s | %(threadName)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.handlers.clear()
    root.addHandler(h)

def pick_account_id(services, explicit: str) -> str:
    if explicit:
        logger.info("Счёт из конфига/окружения: %s", explicit)
        return explicit
    acc = services.users.get_accounts()
    for a in acc.accounts:
        if a.status != AccountStatus.ACCOUNT_STATUS_OPEN:
            continue
        if a.type == AccountType.ACCOUNT_TYPE_TINKOFF:
            logger.info("Выбран брокерский счёт id=%s name=%s", a.id, a.name)
            return a.id
    for a in acc.accounts:
        if a.status == AccountStatus.ACCOUNT_STATUS_OPEN:
            logger.info("Выбран счёт id=%s name=%s", a.id, a.name)
            return a.id
    raise RuntimeError("Не найден открытый счёт")

def position_lots_securities(services, account_id: str, figi: str) -> int:
    pos = services.operations.get_positions(account_id=account_id)
    for s in pos.securities:
        if s.figi == figi:
            return int(s.balance)
    return 0

def last_price(services, figi: str) -> Decimal:
    lp = services.market_data.get_last_prices(figi=[figi])
    if not lp.last_prices:
        raise RuntimeError(f"Нет котировки для figi={figi}")
    return quotation_to_decimal(lp.last_prices[0].price)

def load_share(services, p: CoupleParams):
    r = services.instruments.share_by(
        id_type=InstrumentIdType.INSTRUMENT_ID_TYPE_TICKER,
        class_code=p.class_code,
        id=p.symbol,
    )
    sh = r.instrument
    inc = quotation_to_decimal(sh.min_price_increment)
    return sh.figi, sh.uid, int(sh.lot), inc, sh.ticker

def cancel_tracked_orders(
    services, account_id: str, figi: str, order_ids: Set[str]
) -> None:
    for oid in list(order_ids):
        try:
            st = services.orders.get_order_state(account_id=account_id, order_id=oid)
            if st.execution_report_status in (
                OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
                OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
            ):
                services.orders.cancel_order(account_id=account_id, order_id=oid)
                logger.info("Отмена лимитки order_id=%s figi=%s", oid, figi)
        except Exception as ex:  # noqa: BLE001
            logger.debug("cancel_order %s: %s", oid, ex)

def cancel_stop_if_any(
    services, account_id: str, figi: str, stop_id: Optional[str]
) -> None:
    if not stop_id:
        return
    try:
        services.stop_orders.cancel_stop_order(
            account_id=account_id, stop_order_id=stop_id
        )
        logger.info("Отменён стоп stop_order_id=%s figi=%s", stop_id, figi)
    except Exception as ex:  # noqa: BLE001
        logger.debug("cancel_stop_order: %s", ex)

def place_limit(
    services,
    account_id: str,
    figi: str,
    instrument_uid: str,
    instrument_alt_id: str,
    lots: int,
    price: Decimal,
    direction: OrderDirection,
    confirm_margin: bool,
    dry_run: bool,
) -> Optional[str]:
    """PostOrder: по контракту API нужны instrument_id (uid / ticker_class) и order_id (UUID), figi устарел."""
    q = decimal_to_quotation(price)
    if dry_run:
        d = "BUY" if direction == OrderDirection.ORDER_DIRECTION_BUY else "SELL"
        logger.info(
            "DRY_RUN лимит %s %s лот=%s по цене %s figi=%s",
            d,
            figi,
            lots,
            price,
            figi,
        )
        return None

    id_chain: List[str] = []
    for x in (instrument_uid, instrument_alt_id, figi):
        if x and x not in id_chain:
            id_chain.append(x)
    if not id_chain:
        raise RuntimeError("place_limit: пустой instrument_id")

    confirm_opts = list(dict.fromkeys([confirm_margin, True]))
    last_err: Optional[RequestError] = None
    for inst_id in id_chain:
        for conf in confirm_opts:
            try:
                resp = services.orders.post_order(
                    figi="",
                    instrument_id=inst_id,
                    order_id=str(uuid.uuid4()),
                    quantity=lots,
                    price=q,
                    direction=direction,
                    account_id=account_id,
                    order_type=OrderType.ORDER_TYPE_LIMIT,
                    confirm_margin_trade=conf,
                )
                if inst_id != instrument_uid or conf != confirm_margin:
                    logger.info(
                        "PostOrder успех (fallback): instrument_id=%s confirm_margin_trade=%s",
                        inst_id,
                        conf,
                    )
                logger.info(
                    "Выставлена лимитка order_id=%s %s %s лот=%s @ %s статус=%s",
                    resp.order_id,
                    figi,
                    "BUY" if direction == OrderDirection.ORDER_DIRECTION_BUY else "SELL",
                    lots,
                    price,
                    resp.execution_report_status.name,
                )
                return resp.order_id
            except RequestError as e:
                last_err = e
                det = (e.details or "").strip()
                if det != "90001" and not _api_needs_trade_confirmation(e):
                    raise
                meta_msg = ""
                if e.metadata is not None and getattr(e.metadata, "message", None):
                    meta_msg = repr(e.metadata.message)
                    print(e.metadata)
                logger.warning(
                    "PostOrder 90001: instrument_id=%s confirm_margin_trade=%s — пробуем далее. %s",
                    inst_id,
                    conf,
                    meta_msg,
                )
                continue

    if last_err is not None:
        if last_err.metadata is not None and getattr(last_err.metadata, "message", None):
            logger.error(
                "PostOrder: исчерпаны варианты. Последнее сообщение API: %r",
                last_err.metadata.message,
            )
        raise last_err
    raise RuntimeError("PostOrder: неожиданный сбой")

def sync_active_order_ids(token: str, account_id: str, figi: str) -> Set[str]:
    for i in range(5):
        try:
            with Client(token) as client:
                r = client.orders.get_orders(account_id=account_id)

                out: Set[str] = set()
                for o in r.orders:
                    if o.figi != figi:
                        continue
                    if o.execution_report_status in (
                        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_NEW,
                        OrderExecutionReportStatus.EXECUTION_REPORT_STATUS_PARTIALLYFILL,
                    ):
                        out.add(o.order_id)

                return out

        except RequestError as e:
            print("RETRY get_orders:", e)
            time.sleep(1 + i)

    print("TInvest API error connect!")
    return set()

def sync_active_stop_id(services, account_id: str, figi: str) -> Optional[str]:
    """Возвращает активный стоп-лосс SELL по figi, если такой есть."""
    try:
        r = services.stop_orders.get_stop_orders(account_id=account_id)
    except Exception as ex:  # noqa: BLE001
        logger.debug("get_stop_orders: %s", ex)
        return None

    for s in r.stop_orders:
        if s.figi != figi:
            continue
        if s.direction != StopOrderDirection.STOP_ORDER_DIRECTION_SELL:
            continue
        if s.stop_order_type != StopOrderType.STOP_ORDER_TYPE_STOP_LOSS:
            continue
        return s.stop_order_id
    return None

def explain_gone_order(
    services, account_id: str, oid: str
) -> Tuple[str, str]:
    try:
        st = services.orders.get_order_state(account_id=account_id, order_id=oid)
        return st.execution_report_status.name, ""
    except Exception as ex:  # noqa: BLE001
        return "UNKNOWN", str(ex)

def ensure_stop_loss_long(
    services,
    account_id: str,
    figi: str,
    uid: str,
    instrument_alt_id: str,
    lots: int,
    lowest_buy_level: Decimal,
    sl_pct: Decimal,
    inc: Decimal,
    prev_stop_id: Optional[str],
    dry_run: bool,
    confirm_margin_trade: bool,
) -> Optional[str]:
    if lots <= 0 or sl_pct <= 0:
        cancel_stop_if_any(services, account_id, figi, prev_stop_id)
        return None
    stop_px = round_to_increment(
        lowest_buy_level * (Decimal(1) - sl_pct / Decimal(100)),
        inc,
    )
    cancel_stop_if_any(services, account_id, figi, prev_stop_id)
    if dry_run:
        logger.info(
            "DRY_RUN стоп-лосс SELL лот=%s stop_price≈%s (от нижнего уровня покупок %s, sl=%s%%)",
            lots,
            stop_px,
            lowest_buy_level,
            sl_pct,
        )
        return None

    id_chain: List[str] = []
    for x in (uid, instrument_alt_id, figi):
        if x and x not in id_chain:
            id_chain.append(x)
    confirm_opts = [True]
    last_err: Optional[RequestError] = None
    for inst_id in id_chain:
        for conf in confirm_opts:
            try:
                r = services.stop_orders.post_stop_order(
                    figi="",
                    instrument_id=inst_id,
                    order_id=str(uuid.uuid4()),
                    quantity=lots,
                    stop_price=decimal_to_quotation(stop_px),
                    direction=StopOrderDirection.STOP_ORDER_DIRECTION_SELL,
                    account_id=account_id,
                    expiration_type=StopOrderExpirationType.STOP_ORDER_EXPIRATION_TYPE_GOOD_TILL_CANCEL,
                    stop_order_type=StopOrderType.STOP_ORDER_TYPE_STOP_LOSS,
                    confirm_margin_trade=conf,
                )
                if inst_id != uid or conf != confirm_margin_trade:
                    logger.info(
                        "PostStopOrder успех (fallback): instrument_id=%s confirm_margin_trade=%s",
                        inst_id,
                        conf,
                    )
                logger.info(
                    "Стоп-лосс выставлен stop_order_id=%s figi=%s лот=%s stop_price=%s",
                    r.stop_order_id,
                    figi,
                    lots,
                    stop_px,
                )
                return r.stop_order_id
            except RequestError as e:
                last_err = e
                det = (e.details or "").strip()
                if det != "90001" and not _api_needs_trade_confirmation(e):
                    raise
                meta_msg = ""
                if e.metadata is not None and getattr(e.metadata, "message", None):
                    meta_msg = repr(e.metadata.message)
                logger.warning(
                    "PostStopOrder 90001: instrument_id=%s confirm=%s — пробуем далее. %s",
                    inst_id,
                    conf,
                    meta_msg,
                )
                continue

    if last_err is not None:
        raise last_err
    raise RuntimeError("PostStopOrder: неожиданный сбой")

DRY_TRACK_SENTINEL = "__dry_run__"

def place_full_grid(
    services,
    account_id: str,
    figi: str,
    uid: str,
    p: CoupleParams,
    ref: Decimal,
    inc: Decimal,
) -> Tuple[Set[str], List[Decimal], List[Decimal], Optional[str]]:
    buys = build_grid_prices(ref, p.range_pct, p.orders_side, inc, "buy")
    sells = build_grid_prices(ref, p.range_pct, p.orders_side, inc, "sell")
    held = position_lots_securities(services, account_id, figi)
    # max_sell_orders = min(p.orders_side, held // p.lots_per_order if p.lots_per_order else 0)
    max_sell_orders = p.orders_side
    ticker_class_id = f"{p.symbol}_{p.class_code}"

    ids: Set[str] = set()
    logger.info(
        "Сетка %s: якорь=%s, покупки=%s, продажи (план)=%s, в портфеле лотов=%s, "
        "выставляем продаж до %s уровней",
        p.setup_name,
        ref,
        [str(x) for x in buys],
        [str(x) for x in sells],
        held,
        max_sell_orders,
    )

    for px in buys:
        oid = place_limit(
            services,
            account_id,
            figi,
            uid,
            ticker_class_id,
            p.lots_per_order,
            px,
            OrderDirection.ORDER_DIRECTION_BUY,
            p.confirm_margin_trade,
            p.dry_run,
        )
        if oid:
            ids.add(oid)

    for px in sells[:max_sell_orders]:
        oid = place_limit(
            services,
            account_id,
            figi,
            uid,
            ticker_class_id,
            p.lots_per_order,
            px,
            OrderDirection.ORDER_DIRECTION_SELL,
            p.confirm_margin_trade,
            p.dry_run,
        )
        if oid:
            ids.add(oid)

    lowest_buy = min(buys) if buys else ref
    stop_id = ensure_stop_loss_long(
        services,
        account_id,
        figi,
        uid,
        ticker_class_id,
        held,
        lowest_buy,
        p.sl_pct,
        inc,
        None,
        p.dry_run,
        p.confirm_margin_trade,
    )
    return ids, buys, sells, stop_id

def run_couple_loop(token: str, account_id: str, p: CoupleParams, poll_sec: float) -> None:
    logger.info(
        "Thread %s: start symbol=%s range=%s%% orders_side=%s size=%s dry_run=%s",
        p.setup_name,
        p.symbol,
        p.range_pct,
        p.orders_side,
        p.lots_per_order,
        p.dry_run,
    )

    tracked: Set[str] = set()
    stop_id: Optional[str] = None
    anchor_ref: Optional[Decimal] = None
    tick = 0

    aid: Optional[str] = None

    figi: str = ""
    uid: str = ""
    inc = Decimal(0)
    ticker: str = ""

    while True:
        tick += 1

        try:
            with Client(token) as services:
                if aid is None:
                    aid = pick_account_id(services, account_id)
                    figi, uid, _lot, inc, ticker = load_share(services, p)

                if not figi:
                    logger.error("FIGI не инициализирован")
                    time.sleep(2)
                    continue

                try:
                    ref = last_price(services, figi)
                except Exception as ex:  # noqa: BLE001
                    logger.error("Ошибка котировки: %s", ex)
                    time.sleep(poll_sec)
                    continue

                if anchor_ref is not None:
                    lo = anchor_ref * (Decimal(1) - p.range_pct / Decimal(100))
                    hi = anchor_ref * (Decimal(1) + p.range_pct / Decimal(100))
                    out_of_band = ref < lo or ref > hi
                else:
                    lo = hi = Decimal(0)
                    out_of_band = False

                if not tracked:
                    if out_of_band:
                        if tick % 10 == 0:
                            logger.warning(
                                "Цена %s вне диапазона [%s .. %s] от якоря %s. "
                                "Новые заявки не выставляются.",
                                ref,
                                lo,
                                hi,
                                anchor_ref,
                            )

                        time.sleep(poll_sec)
                        continue

                    tracked = sync_active_order_ids(token, aid, figi)

                    try:
                        stop_id = sync_active_stop_id(services, aid, figi)
                    except Exception as e:
                        logger.error("Ошибка стопа: %s", e)
                        stop_id = None

                    if tracked:
                        logger.info(
                            "Найдены активные заявки после запуска: %s шт. Продолжаем сопровождение.",
                            len(tracked),
                        )
                        anchor_ref = ref
                        if stop_id:
                            logger.info("Найден активный стоп-лосс stop_order_id=%s", stop_id)
                        time.sleep(poll_sec)
                        continue

                    logger.info("Первичная расстановка сетки по цене %s", ref)

                    try:
                        tracked, buys, sells, stop_id = place_full_grid(
                            services, aid, figi, uid, p, ref, inc
                        )
                        anchor_ref = ref
                    except RequestError as ex:
                        _log_request_error("Первичная сетка", ex)
                        time.sleep(max(poll_sec, 5.0))
                        continue

                    if p.dry_run:
                        tracked = {DRY_TRACK_SENTINEL}

                    time.sleep(poll_sec)
                    continue

                if DRY_TRACK_SENTINEL in tracked:
                    if tick % 20 == 0:
                        logger.info(
                            "DRY_RUN %s: котировка=%s (исполнения не отслеживаются)",
                            p.setup_name,
                            ref,
                        )

                    time.sleep(poll_sec)
                    continue

                if out_of_band:
                    logger.warning(
                        "Цена %s вне диапазона [%s .. %s] от якоря %s. "
                        "Отменяем активные заявки и ждём возврата в диапазон.",
                        ref,
                        lo,
                        hi,
                        anchor_ref,
                    )

                    cancel_tracked_orders(services, aid, figi, tracked)
                    tracked.clear()

                    cancel_stop_if_any(services, aid, figi, stop_id)
                    stop_id = None

                    time.sleep(poll_sec)
                    continue

                api_active = sync_active_order_ids(token, aid, figi)
                still = tracked & api_active
                gone = tracked - api_active

                if tick % 20 == 0:
                    logger.info(
                        "Пульс %s: last=%s отслеживаемых=%s активных_на_бирже=%s",
                        p.setup_name,
                        ref,
                        len(tracked),
                        len(still),
                    )

                if not gone:
                    time.sleep(poll_sec)
                    continue

                for oid in gone:
                    status, msg = explain_gone_order(services, aid, oid)
                    logger.info(
                        "Заявка исчезла с биржи order_id=%s статус=%s msg=%s → перенос сетки",
                        oid,
                        status,
                        msg,
                    )

                logger.info("Перенос: отмена оставшихся лимиток и стопа")

                cancel_tracked_orders(services, aid, figi, tracked)
                tracked.clear()

                cancel_stop_if_any(services, aid, figi, stop_id)
                tracked.clear()
                stop_id = None

                time.sleep(0.5)

                try:
                    ref2 = last_price(services, figi)
                except Exception:
                    ref2 = ref

                logger.info("Новая якорная цена после сделки: %s", ref2)
                try:
                    tracked, _, _, stop_id = place_full_grid(
                        services, aid, figi, uid, p, ref2, inc
                    )
                    anchor_ref = ref2
                except RequestError as ex:
                    _log_request_error("Перенос сетки", ex)
                    tracked = set()
                    stop_id = None
                    anchor_ref = None
                    time.sleep(max(poll_sec, 5.0))
                    continue

                if p.dry_run:
                    tracked = {DRY_TRACK_SENTINEL}

                time.sleep(poll_sec)
        except Exception as e:
            logger.error("RECONNECT: %s", e)
            time.sleep(2)

def main() -> None:
    setup_logging()
    try:
        import config as user_cfg  # noqa: WPS433
    except ImportError:
        logger.error("Файл config.py не найден в текущей директории.")
        sys.exit(1)

    token = (getattr(user_cfg, "TOKEN", None) or "").strip() or os.environ.get(
        "TINVEST_TOKEN", os.environ.get("INVEST_TOKEN", "")
    ).strip()
    if not token:
        logger.error(
            "Задайте TOKEN в config.py или переменную окружения TINVEST_TOKEN / INVEST_TOKEN."
        )
        sys.exit(1)

    account_id = getattr(user_cfg, "ACCOUNT_ID", "") or ""
    poll_sec = float(getattr(user_cfg, "POLL_INTERVAL_SEC", 3.0))
    raw_couples: Dict[str, Any] = getattr(user_cfg, "couples", {})
    threads: List[threading.Thread] = []

    for name, body in raw_couples.items():
        if not isinstance(body, dict):
            logger.warning("Пропуск %s: не словарь", name)
            continue
        if not _parse_enable(body.get("enable", "OFF")):
            logger.info("Сетап %s выключен (enable)", name)
            continue
        try:
            params = parse_couple(name, body)
        except Exception as ex:  # noqa: BLE001
            logger.error("Ошибка разбора %s: %s", name, ex)
            continue
        t = threading.Thread(
            target=run_couple_loop,
            args=(token, account_id, params, poll_sec),
            name=f"grid-{params.symbol}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    if not threads:
        logger.error("Нет включённых сетапов в couples.")
        sys.exit(1)

    try:
        while any(t.is_alive() for t in threads):
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("Останов по Ctrl+C (заявки на бирже не отменяются автоматически).")


if __name__ == "__main__":
    main()
