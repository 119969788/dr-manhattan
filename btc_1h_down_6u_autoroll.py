import os
import time
import math
from datetime import datetime, timezone

import dr_manhattan

# ====== CONFIG ======
COIN = "BTC"
OUTCOME = "DOWN"

SPEND_USDC = 6.0          # 真实下单预算：6 USDC
BUY_TH = 1.0              # 只在 ask <= BUY_TH 才买；不想限制就 1.0
CHECK_SEC = 1.0           # 主循环频率

PRICE_BUFFER = 0.002      # 买价 = best_ask + buffer（更容易成交）
MAX_PRICE = 0.999

FILL_TIMEOUT_SEC = 15.0   # 下单后等待成交时间
FILL_POLL_SEC = 1.0

CANCEL_IF_NOT_FILLED = True   # 超时仍未成交 -> 撤单（更安全）
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
    if isinstance(o, dict):
        return o.get(key, default)
    return getattr(o, key, default)


def confirm_fill(op, order_id: str, timeout_sec=FILL_TIMEOUT_SEC, poll_sec=FILL_POLL_SEC):
    """
    Returns: (status, filled_size, remaining_size, raw_order)
    status in: FILLED / PARTIAL / OPEN / CANCELED / REJECTED / UNKNOWN
    """
    end = time.time() + float(timeout_sec)
    last = None

    while time.time() < end:
        try:
            o = op.fetch_order(order_id)
            last = o

            status = _get(o, "status", None)
            filled = _get(o, "filled_size", None)
            remaining = _get(o, "remaining_size", None)

            # fallback field names
            if filled is None:
                filled = _get(o, "filled", 0)
            if remaining is None:
                remaining = _get(o, "remaining", None)

            s = (str(status) if status is not None else "UNKNOWN").upper()

            if "FILLED" in s or s in {"DONE", "CLOSED"}:
                return ("FILLED", float(filled or 0), float(remaining or 0), o)
            if "PART" in s:
                return ("PARTIAL", float(filled or 0), float(remaining or 0), o)
            if "CANCEL" in s:
                return ("CANCELED", float(filled or 0), float(remaining or 0), o)
            if "REJECT" in s:
                return ("REJECTED", float(filled or 0), float(remaining or 0), o)

            # OPEN/NEW/etc -> keep polling
        except Exception:
            pass

        time.sleep(float(poll_sec))

    # timeout
    if last is not None:
        status = _get(last, "status", None)
        filled = _get(last, "filled_size", None)
        remaining = _get(last, "remaining_size", None)
        if filled is None:
            filled = _get(last, "filled", 0)
        if remaining is None:
            remaining = _get(last, "remaining", 0)
        s = (str(status) if status else "OPEN").upper()
        return (s, float(filled or 0), float(remaining or 0), last)

    return ("UNKNOWN", 0.0, 0.0, None)


def main():
    # 初始化 Opinion
    op = dr_manhattan.Opinion({
        "api_key": os.environ["OPINION_API_KEY"],
        "private_key": os.environ["OPINION_PRIVATE_KEY"],
        "multi_sig_addr": os.environ["OPINION_MULTI_SIG_ADDR"],
        "timeout": 30,
    })

    # 你这版明确有 get_orderbook / fetch_order
    if not hasattr(op, "get_orderbook"):
        raise SystemExit("Opinion object has no get_orderbook().")
    if not hasattr(op, "fetch_order"):
        raise SystemExit("Opinion object has no fetch_order().")

    current_market_id = None
    bought_once = set()  # (market_id, outcome)

    while True:
        # 自动找 BTC 1H 当前/下一场
        m = op.find_crypto_hourly_market(COIN)

        if current_market_id != m.id:
            current_market_id = m.id
            print("\n============================")
            print("ROLLOVER -> market:", m.id)
            print("question:", m.question)
            print("close_time:", m.close_time)
            print("============================\n")

        # 到期保护（下一轮会自然切到下一场）
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

            # 用 best_ask + buffer 做吃单限价
            price = min(MAX_PRICE, ask + PRICE_BUFFER)

            # 预算 -> shares（保守截断避免超预算）
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

            # REAL ORDER
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

            # 下单后查成交
            if order_id:
                st, filled, remaining, raw = confirm_fill(op, order_id)
                print(f"FILL CHECK -> status={st} filled={filled} remaining={remaining}")

                # 超时未成交则撤单（更安全）
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
