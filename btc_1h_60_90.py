import os
import time
import dr_manhattan

TOPIC_ID = "5788"        # 你给的这场（也可以后面改成自动找下一场）
OUTCOME = "UP"           # 先做 UP；要做 DOWN 改这里
BUY_TH = 0.01
SELL_TH = 0.90
CHECK_SEC = 1.0
PRICE_BUFFER = 0.002     # 提高成交概率：买=ask+buffer，卖=bid-buffer
FILL_TIMEOUT_SEC = 6.0   # 你之后可加“查成交确认”，先留接口

def pick_orderbook_fn(op):
    for name in ["fetch_order_book","fetch_orderbook","get_order_book","get_orderbook"]:
        if hasattr(op, name):
            return getattr(op, name), name
    return None, None

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

def main():
    op = dr_manhattan.Opinion({
        "api_key": os.environ["OPINION_API_KEY"],
        "private_key": os.environ["OPINION_PRIVATE_KEY"],
        "multi_sig_addr": os.environ["OPINION_MULTI_SIG_ADDR"],
        "timeout": 30,
    })

    ob_fn, ob_name = pick_orderbook_fn(op)
    if not ob_fn:
        raise SystemExit("No orderbook method found on Opinion exchange object.")

    bought_once = set()  # (topic_id, outcome)

    while True:
        m = op.fetch_market_by_id(TOPIC_ID)
        if getattr(m, "metadata", None) is None or "tokens" not in m.metadata:
            print("no tokens in metadata; skip")
            time.sleep(CHECK_SEC)
            continue

        token_id = m.metadata["tokens"][OUTCOME]
        close_time = m.close_time
        status = m.metadata.get("status")

        # 读盘口
        try:
            ob = ob_fn(token_id)
            bid, ask = best_bid_ask(ob)
        except Exception as e:
            print("orderbook error:", e)
            time.sleep(CHECK_SEC)
            continue

        print(f"[{OUTCOME}] bid={bid} ask={ask} close={close_time} status={status}")

        key = (TOPIC_ID, OUTCOME)

        # 卖出条件：bid >= 0.90（你可加持仓检查；先按“触发就卖”写骨架）
        if bid is not None and bid >= SELL_TH:
            price = max(0.0, bid - PRICE_BUFFER)
            print("SELL trigger:", "price=", price)
            # size 这里要按你仓位来卖：建议后面接 fetch_positions_for_market -> 找 shares
            # 先留空：你先确认 create_order 能跑通再补“卖多少”
            # op.create_order(market_id=TOPIC_ID, outcome=OUTCOME, side=dr_manhattan.OrderSide.SELL,
            #                price=price, size=SELL_SIZE, params={"token_id": token_id})
            # print("SELL sent")
            time.sleep(CHECK_SEC)
            continue

        # 买入条件：ask <= 0.60，且每场每方向只买一次（失败也锁）
        if ask is not None and ask <= BUY_TH and key not in bought_once:
            bought_once.add(key)
            price = min(1.0, ask + PRICE_BUFFER)
            print("BUY trigger:", "price=", price)
            # 注意：size 单位依赖 dr-manhattan 的实现（通常是 shares）
            # 先用一个小值测试（例如 5 shares），确认下单参数没问题后再换成“按 USDC 预算换算”
            BUY_SIZE = 5.0
            op.create_order(
                market_id=TOPIC_ID,
                outcome=OUTCOME,
                side=dr_manhattan.OrderSide.BUY,
                price=price,
                size=BUY_SIZE,
                params={"token_id": token_id},
            )
            print("BUY sent")

        time.sleep(CHECK_SEC)

if __name__ == "__main__":
    main()
