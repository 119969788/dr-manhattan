import os
import time
import math
from datetime import datetime, timezone

import dr_manhattan

# ====== CONFIG ======
COIN = "BTC"
PREFERRED_TOPIC_ID = "5791"   # 优先跑这场；到期后自动切到下一场 BTC Hourly
OUTCOME = "UP"
SPEND_USDC = 6.0

BUY_TH = 1.0                 # 只在 ask <= BUY_TH 才买；不想限制就 1.0
CHECK_SEC = 1.0

PRICE_BUFFER = 0.002
MAX_PRICE = 0.999

FILL_TIMEOUT_SEC = 15.0
FILL_POLL_SEC = 1.0
CANCEL_IF_NOT_FILLED = True
# ====================


def now_utc():
    return datetime.now(timezone.utc)


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


def extract_order_id(resp):
    if resp is None:
        return None
    if isinstance(resp, dict):
        return (
            resp.get("id")
            or resp.get("order_id")
            or resp.get("orderId")
            or (resp.get("result") or {}).get("id")
        )
    return getattr(resp, "id", None) or getattr(resp, "order_id", None) or getattr(resp, "orderId", None)


def _get(o, key, default=None):
    return o.get(key, default) if isinstance(o, dict) else getattr(o, key, default)


def confirm_fill(op, order_id: str, timeout_sec=FILL_TIMEOUT_SEC, poll_sec=FILL_POLL_SEC):
    end = time.time() + float(timeout_sec)
    last = None

    while time.time() < end:
        try:
            o = op.fetch_order(order_id)
            last = o

            status = _get(o, "status", None)
            filled = _get(o, "filled_size", None) or _get(o, "filled", 0)
            remaining = _get(o, "remaining_size", None) or _get(o, "remaining", 0)

            s = (str(status) if status is not None else "UNKNOWN").upper()

            if "FILLED" in s or s in {"DONE", "CLOSED"}:
                return ("FILLED", float(filled or 0), float(remaining or 0), o)
            if "PART" in s:
                return ("PARTIAL", float(filled or 0), float(remaining or 0), o)
            if "CANCEL" in s:
                return ("CANCELED", float(filled or 0), float(remaining or 0), o)
            if "REJECT" in s:
                return ("REJECTED", float(filled or 0), float(remaining or 0), o)

        except Exception:
            pass

        time.sleep(float(poll_sec))

    if last is not None:
        status = _get(last, "status", "OPEN")
        filled = _get(last, "filled_size", None) or _get(last, "filled", 0)
        remaining = _get(last, "remaining_size", None) or _get(last, "remaining", 0)
        return (str(status).upper(), float(filled or 0), float(remaining or 0), last)

    return ("UNKNOWN", 0.0, 0.0, None)


def pick_market(op, preferred_id: str):
    """
    优先拿 preferred_id（未到期），否则自动找 BTC Hourly 当前/下一场。
    """
    # 1) preferred
    try:
        m = op.fetch_market_by_id(preferred_id)
        if m and m.close_time and now_utc() < m.close_time and not (m.metadata or {}).get("closed", False):
            return m
    except Exception:
        pass

    # 2) fallback: BTC hourly (may be None)
    try:
        return op.find_crypto_hourly_market(COIN)
    except Exception:
        return None


def main():
    op = dr_manhattan.Opinion({
        "api_key": os.environ["OPINION_API_KEY"],
        "private_key": os.environ["OPINION_PRIVATE_KEY"],
        "multi_sig_addr": os.environ["OPINION_MULTI_SIG_ADDR"],
        "timeout": 30,
    })

    # required methods in your build
    for need in ["get_orderbook", "fetch_order", "create_order", "cancel_order", "fetch_market_by_id", "find_crypto_hourly_market"]:
        if not hasattr(op, need):
            raise SystemExit(f"Opinion object missing method: {need}")

    current_market_id = None
    bought_once = set()  # (market_id, outcome)
    using_preferred = True

    while True:
        m = pick_market(op, PREFERRED_TOPIC_ID)

        if m is None:
            print("no market found (preferred expired and hourly None). retry...")
            time.sleep(CHECK_SEC)
            continue

        # once preferred expires, we switch to hourly permanently
        if m.id != PREFERRED_TOPIC_ID and using_preferred:
            using_preferred = False
            print(f"\nPREFERRED {PREFERRED_TOPIC_ID} expired/invalid -> switch to hourly markets\n")

        if current_market_id != m.id:
            current_market_id = m.id
            print("\n============================")
            print("ROLLOVER -> market:", m.id)
            print("question:", m.question)
            print("close_time:", m.close_time)
            print("============================\n")

        # 到期保护：preferred 到期会切；hourly 到期下一轮会切
        if m.close_time and now_utc() >= m.close_time:
            time.sleep(CHECK_SEC)
            continue

        token_id = m.metadata["tokens"][OUTCOME]

        # 读盘口
        try:
            ob = op.get_orderbook(token_id)
            bid, ask = best_bid_ask(ob)
        except Exception as e:
            print("orderbook error:", e)
            time.sleep(CHECK_SEC)
            continue

        print(f"[{OUTCOME}] market={m.id} bid={bid} ask={ask} close={m.close_time}")

        key = (m.id, OUTCOME)
        if key not in bought_once and ask is not None and ask <= BUY_TH:
            bought_once.add(key)

            price = min(MAX_PRICE, ask + PRICE_BUFFER)
            shares = SPEND_USDC / price
            shares = math.floor(shares * 10000) / 10000.0
            est_cost = shares * price

            print("=== BUY PREVIEW (REAL) ===")
            print("market:", m.id)
            print("outcome:", OUTCOME)
            print("token_id:", token_id)
            print("best_ask:", ask, "-> limit_price:", price)
            print("shares:", shares, "est_cost_usdc:", est_cost)
            print("==========================")

            resp = op.create_order(
                market_id=m.id,
                outcome=OUTCOME,
                side=dr_manhattan.OrderSide.BUY,
                price=price,
                size=shares,
                params={"token_id": token_id},
            )
            print("ORDER SENT:", resp)

            order_id = extract_order_id(resp)
            print("ORDER_ID:", order_id)

            if order_id:
                st, filled, remaining, _ = confirm_fill(op, order_id)
                print(f"FILL CHECK -> status={st} filled={filled} remaining={remaining}")

                if CANCEL_IF_NOT_FILLED and st in {"OPEN", "UNKNOWN"} and filled == 0:
                    try:
                        op.cancel_order(order_id)
                        print("CANCEL -> sent:", order_id)
                    except Exception as e:
                        print("CANCEL -> failed:", e)
            else:
                print("⚠️ Cannot extract order_id; skip fill check.")

        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
