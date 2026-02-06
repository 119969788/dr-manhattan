import os, time, math, json
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

import dr_manhattan

# ========= ENV CONFIG =========
SIMULATION = os.getenv("SIMULATION", "true").lower() == "true"

LOG_DIR = os.getenv("LOG_DIR", "/root/dr-manhattan/logs")
CHECK_SEC = float(os.getenv("CHECK_SEC", "1.0"))

COIN = os.getenv("COIN", "BTC")
PREFERRED_TOPIC_ID = (os.getenv("PREFERRED_TOPIC_ID") or "").strip() or None

BUY_PRICE = float(os.getenv("BUY_PRICE", "0.60"))
SELL_PRICE = float(os.getenv("SELL_PRICE", "0.90"))
PRICE_BUFFER = float(os.getenv("PRICE_BUFFER", "0.002"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0.999"))

UP_SPEND_USDC = float(os.getenv("UP_SPEND_USDC", os.getenv("SPEND_USDC", "6")))
DOWN_SPEND_USDC = float(os.getenv("DOWN_SPEND_USDC", os.getenv("SPEND_USDC", "6")))

FILL_TIMEOUT_SEC = float(os.getenv("FILL_TIMEOUT_SEC", "15"))
FILL_POLL_SEC = float(os.getenv("FILL_POLL_SEC", "1"))
CANCEL_IF_NOT_FILLED = os.getenv("CANCEL_IF_NOT_FILLED", "true").lower() == "true"
# ===============================

OUTCOMES = ("UP", "DOWN")

def now_utc():
    return datetime.now(timezone.utc)

def ts():
    return now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")

def ensure_dirs():
    os.makedirs(LOG_DIR, exist_ok=True)

def setup_logger():
    ensure_dirs()
    log_path = os.path.join(LOG_DIR, "bot.log")
    logger = logging.getLogger("dual_arb")
    logger.setLevel(logging.INFO)

    # avoid duplicate handlers if rerun in same interpreter
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger, log_path, os.path.join(LOG_DIR, "trades.jsonl")

def best_bid_ask(ob: dict):
    bids = ob.get("bids") or ob.get("buy") or []
    asks = ob.get("asks") or ob.get("sell") or []

    def px(level):
        if isinstance(level, dict):
            return float(level.get("price") or level.get("px"))
        return float(level[0])

    best_bid = max([px(x) for x in bids], default=None)
    best_ask = min([px(x) for x in asks], default=None)
    return best_bid, best_ask

def mid_price(bid, ask):
    if bid is None and ask is None:
        return None
    if bid is None:
        return float(ask)
    if ask is None:
        return float(bid)
    return (float(bid) + float(ask)) / 2.0

def extract_order_id(resp):
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp.get("id") or resp.get("order_id") or resp.get("orderId") or (resp.get("result") or {}).get("id")
    return getattr(resp, "id", None) or getattr(resp, "order_id", None) or getattr(resp, "orderId", None)

def _get(o, key, default=None):
    return o.get(key, default) if isinstance(o, dict) else getattr(o, key, default)

def normalize_status(s):
    if s is None:
        return "UNKNOWN"
    st = str(s).lower()
    if "open" in st:
        return "OPEN"
    if "filled" in st or "done" in st or "closed" in st:
        return "FILLED"
    if "part" in st:
        return "PARTIAL"
    if "cancel" in st:
        return "CANCELED"
    if "reject" in st:
        return "REJECTED"
    return st.upper()

def confirm_fill(op, order_id: str, timeout_sec: float, poll_sec: float):
    end = time.time() + float(timeout_sec)
    last = None
    while time.time() < end:
        try:
            o = op.fetch_order(order_id)
            last = o
            status = normalize_status(_get(o, "status", None))
            filled = float(_get(o, "filled", 0) or 0)
            size = float(_get(o, "size", 0) or 0)
            remaining = _get(o, "remaining_size", None)
            if remaining is None:
                remaining = max(0.0, size - filled)
            else:
                remaining = float(remaining or 0)

            if size > 0 and filled >= size - 1e-12:
                return ("FILLED", filled, 0.0, o)
            if status in {"FILLED", "PARTIAL", "CANCELED", "REJECTED"}:
                return (status, filled, remaining, o)
        except Exception:
            pass
        time.sleep(float(poll_sec))

    if last is not None:
        status = normalize_status(_get(last, "status", None))
        filled = float(_get(last, "filled", 0) or 0)
        size = float(_get(last, "size", 0) or 0)
        remaining = max(0.0, size - filled)
        if status == "UNKNOWN":
            status = "OPEN"
        return (status, filled, remaining, last)

    return ("UNKNOWN", 0.0, 0.0, None)

def cancel_with_retry(op, order_id: str, tries=5, sleep_s=1.0):
    last = None
    for _ in range(int(tries)):
        try:
            op.cancel_order(order_id)
            return True, None
        except Exception as e:
            last = e
            time.sleep(float(sleep_s))
    return False, last

@dataclass
class Position:
    shares: float = 0.0
    avg_cost: float = 0.0  # USDC per share

@dataclass
class PnL:
    realized: float = 0.0
    cash_flow: float = 0.0  # sells +, buys -

def buy_position(pos: Position, shares: float, price: float):
    if shares <= 0:
        return
    if pos.shares <= 0:
        pos.shares = shares
        pos.avg_cost = price
        return
    new_sh = pos.shares + shares
    pos.avg_cost = (pos.avg_cost * pos.shares + price * shares) / new_sh
    pos.shares = new_sh

def sell_position(pos: Position, shares: float, price: float):
    if shares <= 0 or pos.shares <= 0:
        return 0.0, 0.0
    sh = min(shares, pos.shares)
    proceeds = sh * price
    cost_basis = sh * pos.avg_cost
    pos.shares -= sh
    if pos.shares <= 1e-12:
        pos.shares = 0.0
        pos.avg_cost = 0.0
    return proceeds - cost_basis, sh  # realized pnl, sold shares

def market_is_tradeable(m) -> bool:
    if m is None:
        return False
    if getattr(m, "close_time", None) and now_utc() >= m.close_time:
        return False
    md = getattr(m, "metadata", {}) or {}
    if md.get("closed") is True:
        return False
    # status (Activated) if present
    st = md.get("status")
    if st is not None:
        s = str(st).lower()
        # allow activated; be permissive if unknown
        if ("activated" not in s) and ("active" not in s) and ("2" not in s):
            # some builds show enum values; we don't hard fail, just warn by returning True if unsure
            pass
    return True

def search_hourly_fallback(op, logger):
    """
    Fallback search: find BTC Up or Down - Hourly upcoming market from search_markets or fetch_markets.
    """
    keyword = f"{COIN} Up or Down - Hourly"
    candidates = []

    # 1) try search_markets
    if hasattr(op, "search_markets"):
        try:
            ms = op.search_markets(keyword)
            candidates.extend(ms or [])
        except Exception as e:
            logger.info(f"fallback search_markets error: {e}")

    # 2) try fetch_markets (often returns a page of recent markets)
    if not candidates and hasattr(op, "fetch_markets"):
        try:
            ms = op.fetch_markets()
            candidates.extend(ms or [])
        except Exception as e:
            logger.info(f"fallback fetch_markets error: {e}")

    if not candidates:
        return None

    # filter by title/question contains keyword and close_time in future
    upcoming = []
    for m in candidates:
        q = (getattr(m, "question", "") or "")
        if keyword.lower() not in q.lower():
            continue
        if not getattr(m, "close_time", None):
            continue
        if now_utc() >= m.close_time:
            continue
        if not market_is_tradeable(m):
            continue
        upcoming.append(m)

    if not upcoming:
        return None

    # choose nearest close_time
    upcoming.sort(key=lambda x: x.close_time)
    return upcoming[0]

def pick_market(op, logger):
    """
    Priority:
      1) preferred topic id (if set & tradeable)
      2) find_crypto_hourly_market(COIN)
      3) fallback keyword search to locate upcoming hourly
    """
    # 1) preferred
    if PREFERRED_TOPIC_ID:
        try:
            m = op.fetch_market_by_id(PREFERRED_TOPIC_ID)
            if market_is_tradeable(m):
                return m
            else:
                logger.info(f"preferred {PREFERRED_TOPIC_ID} not tradeable (expired/closed).")
        except Exception as e:
            logger.info(f"preferred fetch_market_by_id error: {e}")

    # 2) hourly helper
    try:
        m = op.find_crypto_hourly_market(COIN)
        if market_is_tradeable(m):
            return m
        if m is None:
            logger.info("find_crypto_hourly_market returned None.")
        else:
            logger.info(f"find_crypto_hourly_market returned not-tradeable market id={getattr(m,'id',None)} close={getattr(m,'close_time',None)}")
    except Exception as e:
        logger.info(f"find_crypto_hourly_market error: {e}")

    # 3) fallback search
    m = search_hourly_fallback(op, logger)
    if market_is_tradeable(m):
        logger.info(f"fallback picked market id={m.id} close={m.close_time}")
        return m

    return None

def write_trade(trades_path, rec: dict):
    rec = dict(rec)
    rec["ts"] = ts()
    with open(trades_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def main():
    logger, log_path, trades_path = setup_logger()
    logger.info(f"START | SIMULATION={SIMULATION} | COIN={COIN} | preferred={PREFERRED_TOPIC_ID} | BUY={BUY_PRICE} SELL={SELL_PRICE} | UP_USDC={UP_SPEND_USDC} DOWN_USDC={DOWN_SPEND_USDC}")
    logger.info(f"logs: {log_path} | trades: {trades_path}")

    op = dr_manhattan.Opinion({
        "api_key": os.environ["OPINION_API_KEY"],
        "private_key": os.environ.get("OPINION_PRIVATE_KEY", ""),
        "multi_sig_addr": os.environ.get("OPINION_MULTI_SIG_ADDR", ""),
        "timeout": 30,
    })

    positions = {}  # (market_id, outcome) -> Position
    pnls = {}       # market_id -> PnL
    buy_once = set()  # (market_id, outcome)

    current_market_id = None
    last_pnl_print = 0.0

    while True:
        m = pick_market(op, logger)
        if m is None:
            logger.info("no market found, retry...")
            time.sleep(CHECK_SEC)
            continue

        if current_market_id != m.id:
            current_market_id = m.id
            logger.info("========================================")
            logger.info(f"ROLLOVER -> market={m.id} close={m.close_time}")
            logger.info(f"question: {m.question}")
            logger.info("========================================")

        if m.close_time and now_utc() >= m.close_time:
            time.sleep(CHECK_SEC)
            continue

        md = m.metadata or {}
        tokens = md.get("tokens") or {}
        if not tokens:
            logger.info("no tokens in metadata, retry...")
            time.sleep(CHECK_SEC)
            continue

        for outcome in OUTCOMES:
            token_id = tokens.get(outcome)
            if not token_id:
                continue

            try:
                ob = op.get_orderbook(token_id)
                bid, ask = best_bid_ask(ob)
            except Exception as e:
                logger.info(f"[{outcome}] orderbook error: {e}")
                continue

            key = (m.id, outcome)
            pos = positions.setdefault(key, Position())
            pnl = pnls.setdefault(m.id, PnL())

            logger.info(f"[{outcome}] market={m.id} bid={bid} ask={ask} pos={pos.shares:.4f}@{pos.avg_cost:.4f}")

            # SELL: bid >= SELL_PRICE
            if pos.shares > 0 and bid is not None and float(bid) >= SELL_PRICE:
                limit_price = max(0.0, float(bid) - PRICE_BUFFER)
                sell_shares = pos.shares

                if SIMULATION:
                    exec_price = min(float(bid), limit_price) if limit_price > 0 else float(bid)
                    rpnl, sold = sell_position(pos, sell_shares, exec_price)
                    pnl.realized += rpnl
                    pnl.cash_flow += sold * exec_price
                    write_trade(trades_path, {"mode":"SIM","action":"SELL","market_id":m.id,"outcome":outcome,"price":exec_price,"shares":sold,"realized_pnl":rpnl})
                    logger.info(f"[{outcome}] SIM SELL exec_price={exec_price} shares={sold:.4f} realized_pnl={rpnl:.6f}")
                else:
                    resp = op.create_order(market_id=m.id, outcome=outcome, side=dr_manhattan.OrderSide.SELL, price=limit_price, size=sell_shares, params={"token_id": token_id})
                    oid = extract_order_id(resp)
                    logger.info(f"[{outcome}] SELL SENT oid={oid} price={limit_price} size={sell_shares:.4f}")
                    write_trade(trades_path, {"mode":"LIVE","action":"SELL_SENT","market_id":m.id,"outcome":outcome,"price":limit_price,"shares":sell_shares,"order_id":oid})
                    if oid:
                        st, filled, remaining, _ = confirm_fill(op, oid, FILL_TIMEOUT_SEC, FILL_POLL_SEC)
                        if filled > 0:
                            rpnl, sold = sell_position(pos, filled, limit_price)
                            pnl.realized += rpnl
                            pnl.cash_flow += sold * limit_price
                        write_trade(trades_path, {"mode":"LIVE","action":"SELL_FILL","market_id":m.id,"outcome":outcome,"status":st,"filled":filled,"remaining":remaining,"order_id":oid})
                        logger.info(f"[{outcome}] SELL FILL status={st} filled={filled} remaining={remaining}")
                        if CANCEL_IF_NOT_FILLED and st in {"OPEN","UNKNOWN"} and filled == 0:
                            ok, err = cancel_with_retry(op, oid, tries=5, sleep_s=1.0)
                            logger.info(f"[{outcome}] SELL CANCEL ok={ok} err={err}")
                            write_trade(trades_path, {"mode":"LIVE","action":"SELL_CANCEL","market_id":m.id,"outcome":outcome,"ok":ok,"err":str(err) if err else None,"order_id":oid})
                continue

            # BUY: ask <= BUY_PRICE
            spend = UP_SPEND_USDC if outcome == "UP" else DOWN_SPEND_USDC
            if ask is not None and float(ask) <= BUY_PRICE and spend > 0:
                if (m.id, outcome) in buy_once:
                    continue
                limit_price = min(MAX_PRICE, float(ask) + PRICE_BUFFER)
                shares = spend / limit_price
                shares = math.floor(shares * 10000) / 10000.0
                buy_once.add((m.id, outcome))

                if SIMULATION:
                    exec_price = max(float(ask), min(limit_price, float(ask) + PRICE_BUFFER))
                    buy_position(pos, shares, exec_price)
                    pnl.cash_flow -= shares * exec_price
                    write_trade(trades_path, {"mode":"SIM","action":"BUY","market_id":m.id,"outcome":outcome,"price":exec_price,"shares":shares,"cost":shares*exec_price})
                    logger.info(f"[{outcome}] SIM BUY exec_price={exec_price} shares={shares:.4f} cost≈{shares*exec_price:.6f}")
                else:
                    resp = op.create_order(market_id=m.id, outcome=outcome, side=dr_manhattan.OrderSide.BUY, price=limit_price, size=shares, params={"token_id": token_id})
                    oid = extract_order_id(resp)
                    logger.info(f"[{outcome}] BUY SENT oid={oid} price={limit_price} size={shares:.4f}")
                    write_trade(trades_path, {"mode":"LIVE","action":"BUY_SENT","market_id":m.id,"outcome":outcome,"price":limit_price,"shares":shares,"order_id":oid})
                    if oid:
                        st, filled, remaining, _ = confirm_fill(op, oid, FILL_TIMEOUT_SEC, FILL_POLL_SEC)
                        if filled > 0:
                            buy_position(pos, filled, limit_price)
                            pnl.cash_flow -= filled * limit_price
                        write_trade(trades_path, {"mode":"LIVE","action":"BUY_FILL","market_id":m.id,"outcome":outcome,"status":st,"filled":filled,"remaining":remaining,"order_id":oid})
                        logger.info(f"[{outcome}] BUY FILL status={st} filled={filled} remaining={remaining}")
                        if CANCEL_IF_NOT_FILLED and st in {"OPEN","UNKNOWN"} and filled == 0:
                            ok, err = cancel_with_retry(op, oid, tries=5, sleep_s=1.0)
                            logger.info(f"[{outcome}] BUY CANCEL ok={ok} err={err}")
                            write_trade(trades_path, {"mode":"LIVE","action":"BUY_CANCEL","market_id":m.id,"outcome":outcome,"ok":ok,"err":str(err) if err else None,"order_id":oid})

        # PnL summary every 10s
        if time.time() - last_pnl_print >= 10:
            last_pnl_print = time.time()
            pnl = pnls.get(current_market_id)
            if pnl:
                mtm = 0.0
                for outcome in OUTCOMES:
                    key = (current_market_id, outcome)
                    pos = positions.get(key)
                    if not pos or pos.shares <= 0:
                        continue
                    try:
                        token_id = (m.metadata.get("tokens") or {}).get(outcome)
                        if not token_id:
                            continue
                        ob = op.get_orderbook(token_id)
                        bid, ask = best_bid_ask(ob)
                        mid = mid_price(bid, ask)
                        if mid is not None:
                            mtm += pos.shares * float(mid)
                    except Exception:
                        pass
                equity = pnl.cash_flow + mtm
                logger.info(f"PNL market={current_market_id} | realized={pnl.realized:.6f} | cash_flow={pnl.cash_flow:.6f} | mtm≈{mtm:.6f} | equity≈{equity:.6f}")

        time.sleep(CHECK_SEC)

if __name__ == "__main__":
    main()
