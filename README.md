# Lizzy — ETHUSD Sell Ladder

Production Delta Exchange India ETHUSD sell-ladder trading bot.

## Production

- Strategy: `lizzy.py`
- Runtime state: `lizzy_state.json`
- PM2 process: `lizzy`
- Order prefix: `LIZ-`
- Production directory: `/home/ec2-user/delta-bot/lizzy/`

## Strategy configuration

All strategy parameters are loaded from AWS Secrets Manager.

AWS secret:

`Lizzie`

The current configuration separates:

- `Step` — initial ladder spacing
- `Counter_Step` — counter-order distance
- `Order_Size` — order size in lots
- `Max_Position` — maximum position in lots

No API credentials or AWS secret values are stored in this repository.

## Important

`lizzy.py` is the only production strategy file.

`lizzy_state.json` is runtime state and must not be manually edited during normal operation.

Before changing strategy code or state, reconcile the live Delta Exchange position and LIZ orders.
