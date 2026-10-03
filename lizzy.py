import os
import json
import time
import uuid
import boto3
from decimal import Decimal, ROUND_HALF_UP
from delta_rest_client import DeltaRestClient

# ============================================================
# SELL LADDER BOT - SEPARATE FROM GRID3
# ============================================================

SECRET_NAME = "Lizzie"
AWS_REGION = "ap-south-1"

BASE_URL = "https://api.india.delta.exchange"
SYMBOL = "ETHUSD"
PRODUCT_ID = 3136

STATE_FILE = "/home/ec2-user/delta-bot/lizzy/lizzy_state.json"
BOT_PREFIX = "LIZ-"

POLL_SECONDS = 2


def log(msg):
    print(time.strftime("%Y-%m-%dT%H:%M:%S"), "|", msg, flush=True)


# ============================================================
# AWS SECRET
# ============================================================

def load_secret():
    session = boto3.session.Session(region_name=AWS_REGION)
    sm = session.client("secretsmanager")

    response = sm.get_secret_value(SecretId=SECRET_NAME)
    data = json.loads(response["SecretString"])

    required = [
        "API_Key",
        "API_Secret_Key",
        "Order_Size",
        "Max_Position",
        "Step",
        "Counter_Step",
    ]

    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(
            f"AWS secret {SECRET_NAME} is missing required fields: {missing}"
        )

    api_key = str(data["API_Key"]).strip()
    api_secret = str(data["API_Secret_Key"]).strip()

    if not api_key or not api_secret:
        raise RuntimeError(
            f"AWS secret {SECRET_NAME} contains empty API credentials"
        )

    def parse_number(value, field):
        text = str(value).strip().lower()
        text = text.replace("$", "")
        text = text.replace("lots", "")
        text = text.replace("lot", "")
        text = text.strip()

        try:
            return float(text)
        except ValueError:
            raise RuntimeError(
                f"Invalid {field} in AWS secret {SECRET_NAME}: {value!r}"
            )

    max_position = parse_number(data["Max_Position"], "Max_Position")
    order_size = parse_number(data["Order_Size"], "Order_Size")
    step = parse_number(data["Step"], "Step")
    counter_step = parse_number(data['Counter_Step'], 'Counter_Step')

    if max_position <= 0 or order_size <= 0 or step <= 0 or counter_step <= 0:
        raise RuntimeError(
            f"AWS secret {SECRET_NAME} contains non-positive strategy values"
        )

    if max_position % 1 != 0 or order_size % 1 != 0:
        raise RuntimeError(
            "Max_Position and Order_Size must be whole lots"
        )

    return api_key, api_secret, int(max_position), int(order_size), step, counter_step


def default_state():
    return {
        "version": 1,
        "bot_position": 0,
        "next_sequence": 1,
        "initialized": False,
        "orders": {}
    }



def load_state():
    if not os.path.exists(STATE_FILE):
        return default_state()

    try:
        with open(STATE_FILE, "r") as f:
            state = json.load(f)

        if not isinstance(state, dict):
            return default_state()

        state.setdefault("version", 1)
        state.setdefault("bot_position", 0)
        state.setdefault("next_sequence", 1)
        state.setdefault("initialized", False)
        state.setdefault("orders", {})

        return state

    except Exception as e:
        log(f"STATE LOAD ERROR: {e}")
        return default_state()


def save_state():
    temp = STATE_FILE + ".tmp"

    with open(temp, "w") as f:
        json.dump(STATE, f, indent=2)

    os.replace(temp, STATE_FILE)


# ============================================================
# PRICE
# ============================================================

def round_cmp(price):
    d = Decimal(str(price))

    rounded = d.quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP
    )

    return float(rounded)


def price_string(price):
    if float(price).is_integer():
        return str(int(price))

    return f"{price:.2f}"


# ============================================================
# DELTA CLIENT
# ============================================================

API_KEY, API_SECRET, MAX_POSITION, ORDER_SIZE, STEP, COUNTER_STEP = load_secret()

client = DeltaRestClient(
    base_url=BASE_URL,
    api_key=API_KEY,
    api_secret=API_SECRET,
    raise_for_status=True
)

STATE = load_state()


# ============================================================
# ORDER ID
# ============================================================

def new_client_order_id(role):
    seq = STATE["next_sequence"]
    STATE["next_sequence"] = seq + 1

    oid = f"{BOT_PREFIX}{role}-{seq}-{uuid.uuid4().hex[:6]}"

    return oid


# ============================================================
# TICKER
# ============================================================

def get_cmp():
    ticker = client.get_ticker(SYMBOL)

    if isinstance(ticker, dict):
        for key in (
            "close",
            "last_price",
            "mark_price",
            "spot_price"
        ):
            if ticker.get(key) is not None:
                return float(ticker[key])

    raise RuntimeError(
        f"Unable to read CMP from ticker: {ticker}"
    )


# ============================================================
# OPEN ORDERS
# ============================================================

def get_live_orders():
    orders = client.get_live_orders()

    if orders is None:
        return []

    if isinstance(orders, dict):
        orders = orders.get(
            "result",
            orders.get("orders", [])
        )

    if not isinstance(orders, list):
        return []

    return [
        o for o in orders
        if int(o.get("product_id", -1)) == PRODUCT_ID
    ]


def get_bot_open_orders():
    result = {}

    for order in get_live_orders():
        client_oid = order.get(
            "client_order_id", ""
        )

        if client_oid.startswith(BOT_PREFIX):
            result[str(order["id"])] = order

    return result


# ============================================================
# PLACE LIMIT ORDER
# ============================================================

def reconcile_startup():
    log("Checking Delta position and LIZ- orders against local state...")

    position = client.get_position(PRODUCT_ID)
    exchange_position = abs(int(position.get("size", 0)))

    live_bot_orders = get_bot_open_orders()

    exchange_order_ids = {
        str(o.get("id")) for o in live_bot_orders
    }

    local_orders = STATE.get("orders", {})

    local_order_ids = {
        str(order_id)
        for order_id, order in local_orders.items()
        if not order.get("handled", False)
    }

    if exchange_position == 0 and not exchange_order_ids:
        if (
            STATE.get("bot_position", 0) != 0
            or local_order_ids
            or STATE.get("initialized", False)
        ):
            log("Exchange is clean but local state is stale.")
            log("Resetting local Lizzy state.")

            STATE.clear()
            STATE.update(default_state())
            save_state()

        log("STARTUP RECONCILIATION OK: exchange is clean.")
        return

    if exchange_position != int(STATE.get("bot_position", 0)):
        raise RuntimeError(
            "STARTUP RECONCILIATION FAILED: "
            f"Delta position={exchange_position}, "
            f"local bot_position={STATE.get('bot_position', 0)}"
        )

    if exchange_order_ids != local_order_ids:
        raise RuntimeError(
            "STARTUP RECONCILIATION FAILED: "
            f"Delta LIZ orders={sorted(exchange_order_ids)}, "
            f"local LIZ orders={sorted(local_order_ids)}"
        )

    log(
        "STARTUP RECONCILIATION OK: "
        f"position={exchange_position}, "
        f"LIZ orders={len(exchange_order_ids)}"
    )


def place_limit(side, price, role):
    client_oid = new_client_order_id(role)

    # BUY orders close the short position.
    # SELL orders create/increase the short position.
    reduce_only = side.lower() == "buy"

    payload = {
        "product_id": PRODUCT_ID,
        "size": int(ORDER_SIZE),
        "side": side,
        "order_type": "limit_order",
        "limit_price": price_string(price),
        "time_in_force": "gtc",
        "post_only": False,
        "reduce_only": reduce_only,
        "client_order_id": client_oid
    }

    log(
        f"PLACING {side.upper()} "
        f"{ORDER_SIZE} LOT @ ${price_string(price)}"
    )

    result = client.create_order(payload)

    if not isinstance(result, dict):
        raise RuntimeError(
            f"Unexpected order response: {result}"
        )

    order_id = result.get("id")

    if order_id is None:
        raise RuntimeError(
            f"No order ID returned: {result}"
        )

    STATE["orders"][str(order_id)] = {
        "role": role,
        "side": side,
        "price": float(price),
        "size": int(ORDER_SIZE),
        "client_order_id": client_oid,
        "handled": False
    }

    save_state()

    log(
        f"ORDER CREATED | ID={order_id} "
        f"| {side.upper()} ${price_string(price)} "
        f"| {client_oid}"
    )

    return result


# ============================================================
# INITIAL SELL LADDER
# ============================================================

def initialize_grid():
    if STATE["initialized"]:
        return

    cmp = get_cmp()
    anchor = round_cmp(cmp)

    log("==========================================")
    log("STARTING CLEAN ETHUSD SELL LADDER")
    log("==========================================")
    log(f"STEP: {STEP}")
    log(f"ORDER SIZE: {ORDER_SIZE}")
    log(f"MAX POSITION: {MAX_POSITION}")
    log(f"CMP: {cmp}")
    log(f"ROUNDED SELL PRICE: {anchor}")

    levels = MAX_POSITION // ORDER_SIZE

    if levels <= 0:
        raise RuntimeError(
            "MAX_POSITION must be at least ORDER_SIZE"
        )

    for level in range(levels):
        price = anchor + (level * STEP)

        place_limit(
            side="sell",
            price=price,
            role="S"
        )

        time.sleep(0.15)

    STATE["initialized"] = True
    save_state()

    log(
        f"INITIAL SELL LADDER READY | "
        f"{levels} SELL ORDERS"
    )


# ============================================================
# ORDER STATUS
# ============================================================

def get_order_status(order_id):
    try:
        return client.get_order_by_id(int(order_id))

    except Exception as e:
        log(
            f"ORDER STATUS ERROR {order_id}: {e}"
        )

        return None


# ============================================================
# HANDLE FILLED SELL
# ============================================================

def handle_sell_fill(order_id, info, order):
    sell_price = float(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"]
            )
        )
    )

    STATE["bot_position"] += info["size"]

    log(
        f"SELL FILLED | ID={order_id} "
        f"| PRICE=${price_string(sell_price)} "
        f"| BOT SHORT POSITION={STATE['bot_position']}"
    )

    # Corresponding buy exactly STEP below.
    buy_price = sell_price - COUNTER_STEP

    place_limit(
        side="buy",
        price=buy_price,
        role="B"
    )

    log(
        f"CORRESPONDING BUY CREATED @ "
        f"${price_string(buy_price)}"
    )

    info["handled"] = True
    info["fill_price"] = sell_price

    save_state()


# ============================================================
# HANDLE FILLED BUY
# ============================================================

def handle_buy_fill(order_id, info, order):
    buy_price = float(
        order.get(
            "average_fill_price",
            order.get(
                "limit_price",
                info["price"]
            )
        )
    )

    STATE["bot_position"] -= info["size"]

    if STATE["bot_position"] < 0:
        STATE["bot_position"] = 0

    log(
        f"BUY FILLED | ID={order_id} "
        f"| PRICE=${price_string(buy_price)} "
        f"| BOT SHORT POSITION={STATE['bot_position']}"
    )

    # Corresponding sell exactly STEP above.
    sell_price = buy_price + COUNTER_STEP

    place_limit(
        side="sell",
        price=sell_price,
        role="S"
    )

    log(
        f"CORRESPONDING SELL CREATED @ "
        f"${price_string(sell_price)}"
    )

    info["handled"] = True
    info["fill_price"] = buy_price

    save_state()


# ============================================================
# PROCESS ORDERS
# ============================================================

def process_tracked_orders():
    for order_id, info in list(
        STATE["orders"].items()
    ):

        if info.get("handled"):
            continue

        order = get_order_status(order_id)

        if not order:
            continue

        state = str(
            order.get("state", "")
        ).lower()

        if state in ("open", "pending"):
            continue

        if state == "closed":
            side = info.get("side")

            if side == "sell":
                handle_sell_fill(
                    order_id,
                    info,
                    order
                )

            elif side == "buy":
                handle_buy_fill(
                    order_id,
                    info,
                    order
                )

        elif state in (
            "cancelled",
            "canceled"
        ):
            log(
                f"BOT ORDER CANCELLED | "
                f"ID={order_id}"
            )

            info["handled"] = True

            save_state()


# ============================================================
# RECONCILE
# ============================================================

def reconcile():
    live_bot_orders = get_bot_open_orders()

    tracked_open = 0

    for order_id, info in STATE["orders"].items():

        if info.get("handled"):
            continue

        if str(order_id) in live_bot_orders:
            tracked_open += 1

    log(
        f"RECONCILE | "
        f"BOT SHORT POSITION={STATE['bot_position']} "
        f"| OPEN BOT ORDERS={tracked_open}"
    )


# ============================================================
# MAIN
# ============================================================

def main():

    log("==========================================")
    log("STARTING ETHUSD SELL LADDER")
    log("==========================================")

    log(
        "Credentials loaded from AWS secret: Lizzie"
    )

    log(f"Symbol: {SYMBOL}")
    log(f"Product ID: {PRODUCT_ID}")

    log(
        f"CONFIG: STEP={STEP} "
        f"ORDER_SIZE={ORDER_SIZE} "
        f"MAX_POSITION={MAX_POSITION}"
    )

    log("Testing Delta API...")

    cmp = get_cmp()

    log(f"Delta API OK. CMP: {cmp}")

    reconcile_startup()

    initialize_grid()

    log("SELL LADDER RUNNING")

    while True:

        try:
            process_tracked_orders()

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            log("SELL LADDER STOPPED")
            break

        except Exception as e:
            log(
                f"MAIN LOOP ERROR: "
                f"{type(e).__name__}: {e}"
            )

            time.sleep(5)


if __name__ == "__main__":
    main()
