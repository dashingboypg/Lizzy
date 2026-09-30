# Lizzy — ETHUSD Sell Ladder

Disaster-recovery backup for the Delta Exchange India ETHUSD sell-ladder bot.

## Production location

`/home/ec2-user/delta-bot/sell-ladder/`

## Bot

- File: `sell_ladder_bot.py`
- PM2 application: `sell-ladder`
- Symbol: ETHUSD
- Product ID: 3136

## Configuration

The bot reads its API credentials and trading configuration from AWS
Secrets Manager at runtime.

**Credentials and secret values are intentionally NOT stored in this repository.**

## Recovery

1. Restore `sell_ladder_bot.py` to the production directory.
2. Restore the state file only after reconciling the live exchange orders and position.
3. Ensure the required Python environment and Delta REST client are installed.
4. Ensure the AWS secret is available to the EC2 instance.
5. Verify exchange state before starting PM2.
6. Start `sell-ladder` only after reconciliation.

Never blindly reset local state while live exchange orders or positions exist.
