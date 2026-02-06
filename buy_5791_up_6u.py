import os, time, math
import dr_manhattan

TOPIC_ID = "5791"
OUTCOME = "UP"
SPEND_USDC = 6.0

PRICE_BUFFER = 0.002
MAX_PRICE = 0.999

FILL_TIMEOUT_SEC = 15.0
FILL_POLL_SEC = 1.0

CANCEL_IF_NOT_FILLED = True
CANCEL_RETRY = 5
CANCEL_RETRY_SLEEP = 1.0

def best_bid_ask(ob: dict):
    bids = ob.get("bids") or ob.get("buy") or []
    asks = ob.get("asks") or ob.get("sell") or []
    def px(level):
        if isinstance(level, dict):
            return float(level.get("price") or level.get("px"))
        return float(level[0])
    return max([px(x) for x in bids], default=None), min([px(x) for x in asks], default=None)

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
    # enum / str / whatever -> normalized uppercase token
    st = str(s).lower()
    # handle enum repr like "OrderStatus.OPEN" or "<OrderStatus.OPEN: 'open'>"
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

def confirm_fill(op, order_id: str, timeout_sec=FILL_TIMEOUT_SEC, poll_sec=FILL_POLL_SEC):
    """
    Returns: (status, filled, remaining, raw_order)
    """
    end = time.time() + float(timeout_sec)
    last = None

    while time.time() < end:
        try:
            o = op.fetch_order(order_id)
            last = o

            status = normalize_status(_get(o, "status", None))
            filled = float(_get(o, "filled", 0) or 0)
            size = float(_get(o, "size", 0) or 0)

            # remaining 如果接口没给，就自己算
            remaining = _get(o, "remaining_size", None)
            if remaining is None:
                remaining = max(0.0, size - filled)
            else:
                remaining = float(remaining or 0)

            # 如果 filled >= size 也可以直接判定 FILLED
            if size > 0 and filled >= size - 1e-12:
                return ("FILLED", filled, 0.0, o)

            if status in {"FILLED", "PARTIAL", "CANCELED", "REJECTED"}:
                return (status, filled, remaining, o)

            # OPEN/UNKNOWN -> 继续等
        except Exception:
            pass

        time.sleep(float(poll_sec))

    # timeout -> return last snapshot
    if last is not None:
        status = normalize_status(_get(last, "status", None))
        filled = float(_get(last, "filled", 0) or 0)
        size = float(_get(last, "size", 0) or 0)
        remaining = _get(last, "remaining_size", None)
        if remaining is None:
            remaining = max(0.0, size - filled)
        else:
            remaining = float(remaining or 0)
        # timeout 时 status 多半是 OPEN
        if status == "UNKNOWN":
            status = "OPEN"
        return (status, filled, remaining, last)

    return ("UNKNOWN", 0.0, 0.0, None)

def cancel_with_retry(op, order_id: str):
    last_err = None
    for i in range(CANCEL_RETRY):
        try:
            op.cancel_order(order_id)
            return True, None
        except Exception as e:
            last_err = e
            time.sleep(CANCEL_RETRY_SLEEP)
    return False, last_err

def main():
    op = dr_manhattan.Opinion({
        "api_key": os.environ["OPINION_API_KEY"],
        "private_key": os.environ["OPINION_PRIVATE_KEY"],
        "multi_sig_addr": os.environ["OPINION_MULTI_SIG_ADDR"],
        "timeout": 30,
    })

    m = op.fetch_market_by_id(TOPIC_ID)
    token_id = m.metadata["tokens"][OUTCOME]

    ob = op.get_orderbook(token_id)
    bid, ask = best_bid_ask(ob)
    if ask is None:
        raise SystemExit("No asks in orderbook; cannot buy.")

    price = min(MAX_PRICE, ask + PRICE_BUFFER)
    shares = SPEND_USDC / price
    shares = math.floor(shares * 10000) / 10000.0
    est_cost = shares * price

    print("=== BUY PREVIEW (REAL) ===")
    print("topic:", TOPIC_ID)
    print("question:", m.question)
    print("outcome:", OUTCOME)
    print("token_id:", token_id)
    print("best_bid:", bid, "best_ask:", ask)
    print("limit_price:", price)
    print("shares:", shares, "est_cost_usdc:", est_cost)
    print("==========================")

    resp = op.create_order(
        market_id=TOPIC_ID,
        outcome=OUTCOME,
        side=dr_manhattan.OrderSide.BUY,
        price=price,
        size=shares,
        params={"token_id": token_id},
    )
    print("ORDER SENT:", resp)

    order_id = extract_order_id(resp)
    print("ORDER_ID:", order_id)

    if not order_id:
        return

    st, filled, remaining, _ = confirm_fill(op, order_id)
    print(f"FILL CHECK -> status={st} filled={filled} remaining={remaining}")

    # 超时仍 OPEN 且没成交 -> 尝试撤单（撤单失败不崩溃，会复核订单状态）
    if CANCEL_IF_NOT_FILLED and st in {"OPEN", "UNKNOWN"} and filled == 0:
        ok, err = cancel_with_retry(op, order_id)
        if ok:
            print("CANCEL -> ok:", order_id)
        else:
            print("CANCEL -> failed after retries:", err)
        # 复核一下当前状态（很多时候接口 500 但实际已撤/已成交）
        try:
            o2 = op.fetch_order(order_id)
            st2 = normalize_status(_get(o2, "status", None))
            filled2 = float(_get(o2, "filled", 0) or 0)
            size2 = float(_get(o2, "size", 0) or 0)
            rem2 = max(0.0, size2 - filled2)
            if size2 > 0 and filled2 >= size2 - 1e-12:
                st2 = "FILLED"
                rem2 = 0.0
            print(f"POST-CANCEL CHECK -> status={st2} filled={filled2} remaining={rem2}")
        except Exception as e:
            print("POST-CANCEL CHECK -> fetch_order failed:", e)

if __name__ == "__main__":
    main()
