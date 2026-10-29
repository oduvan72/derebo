# Site payments

## Configure

Install the exact, tested runtime dependency versions with
`pip install -r requirements.txt`. Configure the following environment variables
on the server; do not commit their values:

- Copy `.env.example` to `.env` for Docker Compose. Generate a unique Flask
  secret with `python -c "import secrets; print(secrets.token_hex(32))"` and set
  it as `FLASK_SECRET_KEY`; Compose refuses to start without it. For direct local
  runs, export the same variables in the shell.
- `PICKUP_LOCATION` customizes the pickup address; it defaults to the city-level
  location shown in `.env.example`.
- `FLASK_DEBUG` — enables Flask debug mode only when explicitly set to `1`, `true`,
  or `yes`; leave it disabled on production servers.
- `YOOKASSA_SHOP_ID` and `YOOKASSA_SECRET_KEY` — YooKassa credentials.
- `PAYMENT_RETURN_URL` — optional URL to return to after payment. If omitted,
  the site uses its `/payment-success` page.
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_APP_PASSWORD` — SMTP settings.
  Port defaults to `587` with STARTTLS; set `SMTP_USE_SSL=1` for implicit TLS.
  `SMTP_FROM` is optional and defaults to `SMTP_USERNAME`.
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ADMIN_IDS` — the existing Telegram bot and
  comma-separated numeric admin IDs used by the admin panel and order alerts.

The Telegram admin bot runs separately with long polling (`python bot.py` or
`docker compose up --build -d telegram-bot`). It does not need a public
Telegram webhook or ngrok. On startup it removes any previous Telegram webhook
without discarding pending updates. Keep `instance/` and `static/` mounted to
the same host directories used by the Flask website so both processes share
the SQLite database and admin-uploaded images. Create a local `.env` from
`.env.example` and set a newly issued bot token; do not put credentials in
source files or commit `.env`. Bot updates run in aiogram's polling loop; the
existing Flask-SQLAlchemy admin operations are explicitly offloaded to worker
threads with `asyncio.to_thread`, so synchronous ORM access does not block that
loop or introduce a second set of mappings for the shared SQLite schema.

Register `https://<site-host>/api/payments/webhook` in YooKassa for
`payment.succeeded` and `payment.canceled` events. Payment links expire after
30 minutes; canceled payments release their reserved event places. This
YooKassa webhook is separate from the removed Telegram webhook.

## SQLite backups

Create a consistent backup while the site is running with:

```powershell
.\.venv313\Scripts\python.exe backup_db.py
```

On Linux or in another Python environment, run `python backup_db.py`. The command
uses SQLite's online backup API, checks the result with `PRAGMA integrity_check`,
and stores the newest seven copies by default in
`~/igri-eltoi5-backups` (the current user's home directory, outside the project).
Use `--destination <directory>` to choose another protected location and
`--keep <count>` to change the number of retained copies. The destination must
remain outside the project folder and should not be publicly served. Backups
contain customer/order data, so restrict access and include them in the site's
secure backup plan.

Automated tests use a temporary SQLite database and mock payment and
notification services; they do not send real payments, emails, or Telegram
messages. Run them with:

```powershell
.\.venv313\Scripts\python.exe -m unittest discover -s tests -v
```

Existing games need a price and capacity set before game orders can be accepted.
In Telegram `/admin`, use **Настроить цены игр** and
**Настроить вместимость игр**. Newly created games request both values during
setup. A game's capacity applies separately to each event where it is
scheduled; reserved and paid seats are tracked for each game-event pair.
Events themselves do not have a capacity setting. When migrating an older
database, a prior event capacity is copied to its games if they do not already
have a capacity. Events with missing or unparseable dates stay out of public
lists and cannot be ordered until an administrator corrects their date in
Telegram.

## Create an order

Send `POST /api/orders/create` with JSON. Prices are read from the database and
`total_price` must match the server-calculated total; it is never trusted as the
payment amount. Product delivery costs are paid separately by the buyer to the
carrier according to its tariff and are not included in the website payment.
Product customers may instead select free pickup in Yakutsk and pay when
collecting the order. Delivery requires an address; games continue to use
online payment and do not offer shipping. Send a unique `Idempotency-Key` header for each intended order;
retries of the same request with the same key return the existing payment link
instead of creating another order. A reused key with different order data, or
a request whose payment is still being created, returns HTTP 409. For games,
`event_id` is mandatory and the selected event must include that game.

```json
{
  "user_name": "Имя клиента",
  "user_phone": "+70000000000",
  "user_email": "client@example.com",
  "offer_accepted": true,
  "personal_data_consent": true,
  "item_type": "game",
  "item_id": "01",
  "event_id": 3,
  "quantity": 2,
  "total_price": 6000
}
```

Both confirmations are mandatory. The server records the acceptance timestamps
and the personal-data consent version with the order. There is no advertising
consent because the site does not send marketing messages. Public legal pages
are available at `/privacy`, `/personal-data-consent`, `/offer`, and `/cookies`.

For a product, use `"item_type": "product"` and set `item_id` to its database
ID; omit `event_id`. Include `fulfillment_method` as `"delivery"` or `"pickup"`.
Delivery requests must include `delivery_address`; the buyer pays the carrier's
delivery charge separately. Pickup orders do not create a YooKassa payment and remain pending until they are
handled at collection. They return `order_id`, `confirmation_url`, and
`payment_required: false`; order confirmation email and Telegram admin alerts
are sent immediately. The admin alert has actions to mark a collected order as
paid or cancel it. Telegram alerts contain the order number, item, quantity,
amount, and event date only; customer name, phone, email, and delivery address
are not sent to Telegram. Paid orders return `payment_id`,
`payment_required: true`, and `confirmation_url`. A new order returns HTTP 201;
an idempotent replay returns HTTP 200. Invalid data, a mismatched price, or
unavailable places return HTTP 400.

The game and product pages open an order form on `/contact`. Its total is only
an estimate; the server recalculates and validates the final amount before
creating a payment or pickup order. Game places are reserved atomically with the pending order
before the payment is created, and deterministic payment-creation failures
release that reservation. After payment, YooKassa returns the customer to
`/payment-success?order_id=...&token=...`. The unguessable token is checked
before displaying the order status; the email address is masked on the page.
The page polls
`GET /api/orders/<order_id>/status?token=...` while the webhook is pending and
updates when payment is confirmed. Do not remove or rewrite these return
parameters when configuring `PAYMENT_RETURN_URL`.

## Payment webhook

The webhook retrieves the payment from YooKassa and verifies its status,
metadata, currency, amount and association with the order before changing the
database. Successful game payments convert reserved places into booked places.
The customer confirmation email and Telegram admin notification are sent after
the transaction commits; unsuccessful notifications are retried when YooKassa
re-delivers the webhook.
