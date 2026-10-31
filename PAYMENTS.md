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
  `SMTP_FROM` is optional and defaults to `SMTP_USERNAME`. While these are not
  set, the site works normally but skips order emails and logs that fact.
- `ADMIN_USERNAME` and `ADMIN_PASSWORD` — credentials for the web admin panel
  at `/admin`. Without `ADMIN_PASSWORD` the admin panel stays disabled.
- `ADMIN_EMAIL` — address that receives an email notification about every new
  or paid order.

The admin panel is served by the Flask app itself at `/admin` (dashboard with
orders and revenue, plus management of games, products, events, reviews, and
orders). All admin forms are protected by a session CSRF token. There is no
separate bot process anymore. Keep `instance/` and `static/` mounted to host
directories so the SQLite database and admin-uploaded images survive container
rebuilds. Create a local `.env` from `.env.example`; do not put credentials in
source files or commit `.env`.

Register `https://<site-host>/api/payments/webhook` in YooKassa for
`payment.succeeded` and `payment.canceled` events. Payment links expire after
30 minutes; canceled payments release their reserved event places.

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
notification services; they do not send real payments or emails. Run them with:

```powershell
.\.venv313\Scripts\python.exe -m unittest discover -s tests -v
```

Existing games need a price and capacity set before game orders can be accepted.
Set them in the web admin panel at `/admin/games` when adding or editing a
game. A game's capacity applies separately to each event where it is
scheduled; reserved and paid seats are tracked for each game-event pair.
Events themselves do not have a capacity setting. When migrating an older
database, a prior event capacity is copied to its games if they do not already
have a capacity. Events with missing or unparseable dates stay out of public
lists and cannot be ordered until an administrator corrects their date in the
admin panel.

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
`payment_required: false`; the order confirmation email to the customer and the
admin notification email are sent immediately. The administrator marks a
collected order as paid or cancels it on its page in `/admin/orders`. Paid
orders return `payment_id`,
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
The customer confirmation email and the admin notification email are sent after
the transaction commits; unsuccessful notifications are retried when YooKassa
re-delivers the webhook.
