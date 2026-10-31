import asyncio
import hmac
import html
import logging
import os
import secrets
import smtplib
import ssl
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.message import EmailMessage
from email.utils import parseaddr
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, abort, jsonify, render_template, request, session, url_for
from requests.exceptions import RequestException
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.middleware.proxy_fix import ProxyFix
from yookassa import Configuration, Payment
from yookassa.domain.exceptions import ApiError

from admin import admin_bp
from db import (
    Event,
    Game,
    Order,
    Product,
    PRODUCT_SUBCATEGORIES,
    db,
    event_games,
    current_event_datetime,
    get_events,
    get_games,
    get_products,
    init_db,
    migrate_event_dates,
    migrate_game_photos,
    migrate_order_booking_fields,
    migrate_product_details,
    release_reserved_seats,
    utcnow,
)

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
secret_key = os.environ.get('FLASK_SECRET_KEY')
if not secret_key:
    raise RuntimeError('FLASK_SECRET_KEY must be configured before starting the app.')
debug_enabled = os.environ.get('FLASK_DEBUG', 'false').lower() in {'1', 'true', 'yes'}
app.config['SECRET_KEY'] = secret_key
app.config['SESSION_COOKIE_SECURE'] = not debug_enabled
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=12)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.register_blueprint(admin_bp, url_prefix='/admin')


def generate_csrf_token():
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {'csrf_token': generate_csrf_token}


@app.before_request
def csrf_protect_admin_forms():
    if request.method != 'POST' or request.blueprint != 'admin':
        return None
    token = session.get('_csrf_token')
    sent_token = request.form.get('csrf_token', '')
    if not token or not sent_token or not hmac.compare_digest(token, sent_token):
        abort(400, 'Отсутствует или недействителен CSRF-токен. Обновите страницу.')
    return None


if debug_enabled:
    app.config['TEMPLATES_AUTO_RELOAD'] = True
    app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

    @app.after_request
    def disable_cache_in_debug(response):
        response.headers['Cache-Control'] = 'no-store'
        return response

PICKUP_LOCATION = os.environ.get(
    'PICKUP_LOCATION', 'Якутск, точный адрес после оформления заказа'
)
PERSONAL_DATA_CONSENT_VERSION = '2026-10-31'
init_db(app)
with app.app_context():
    db.create_all()
    migrate_product_details()
    migrate_game_photos()
    migrate_order_booking_fields()
    migrate_event_dates()
Configuration.account_id = os.environ.get('YOOKASSA_SHOP_ID', '')
Configuration.secret_key = os.environ.get('YOOKASSA_SECRET_KEY', '')


def render_page(template, title, **context):
    return render_template(template, page_title=title, **context)


class OrderValidationError(ValueError):
    pass


class PaymentConfigurationError(RuntimeError):
    pass


def positive_price(value):
    try:
        price = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return price if price.is_finite() and price > 0 else None


class OrderConflictError(RuntimeError):
    def __init__(self, message, code='order_conflict'):
        super().__init__(message)
        self.code = code


def parse_order_request(data, idempotency_key):
    if not isinstance(data, dict):
        raise OrderValidationError('Тело запроса должно быть JSON-объектом.')
    if (
        not isinstance(idempotency_key, str)
        or len(idempotency_key) < 16
        or len(idempotency_key) > 100
        or not idempotency_key.replace('-', '').replace('_', '').isalnum()
    ):
        raise OrderValidationError(
            'Передайте корректный ключ запроса в заголовке Idempotency-Key.'
        )

    required = {
        'user_name', 'user_phone', 'user_email', 'item_type',
        'item_id', 'quantity', 'total_price',
    }
    missing = required - data.keys()
    if missing:
        raise OrderValidationError(
            f'Не заполнены обязательные поля: {", ".join(sorted(missing))}.'
        )
    if data.get('personal_data_consent') is not True:
        raise OrderValidationError(
            'Необходимо согласиться на обработку персональных данных.'
        )
    if data.get('offer_accepted') is not True:
        raise OrderValidationError(
            'Необходимо принять условия публичной оферты.'
        )

    user_name = data['user_name'].strip() if isinstance(data['user_name'], str) else ''
    user_phone = data['user_phone'].strip() if isinstance(data['user_phone'], str) else ''
    user_email = data['user_email'].strip() if isinstance(data['user_email'], str) else ''
    if not user_name or len(user_name) > 200:
        raise OrderValidationError('Укажите имя длиной не более 200 символов.')
    if not user_phone or len(user_phone) > 20:
        raise OrderValidationError('Укажите телефон длиной не более 20 символов.')
    _, parsed_email = parseaddr(user_email)
    if (
        not user_email
        or len(user_email) > 120
        or parsed_email != user_email
        or '@' not in user_email
    ):
        raise OrderValidationError('Укажите корректный email.')
    if not isinstance(data['item_type'], str) or data['item_type'] not in {'game', 'product'}:
        raise OrderValidationError('item_type должен быть game или product.')
    if isinstance(data['quantity'], bool):
        raise OrderValidationError('quantity должно быть положительным целым числом.')
    try:
        quantity = int(data['quantity'])
    except (TypeError, ValueError):
        raise OrderValidationError('quantity должно быть положительным целым числом.') from None
    if quantity < 1 or str(quantity) != str(data['quantity']).strip():
        raise OrderValidationError('quantity должно быть положительным целым числом.')

    try:
        client_total = Decimal(str(data['total_price'])).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise OrderValidationError('total_price должно быть положительной суммой.') from None
    if not client_total.is_finite() or client_total <= 0:
        raise OrderValidationError('total_price должно быть положительной суммой.')

    item_id = str(data['item_id']).strip()
    if not item_id or len(item_id) > 80:
        raise OrderValidationError('Укажите корректный item_id.')

    event_id = None
    if data['item_type'] == 'game':
        fulfillment_method = 'event'
        delivery_address = None
        if isinstance(data.get('event_id'), bool):
            raise OrderValidationError('Для игры укажите event_id.')
        try:
            event_id = int(data['event_id'])
        except (KeyError, TypeError, ValueError):
            raise OrderValidationError('Для игры укажите корректный event_id.') from None
        if event_id < 1:
            raise OrderValidationError('Для игры укажите корректный event_id.')
    else:
        fulfillment_method = data.get('fulfillment_method', 'delivery')
        if (
            not isinstance(fulfillment_method, str)
            or fulfillment_method not in {'pickup', 'delivery'}
        ):
            raise OrderValidationError(
                'Выберите самовывоз или доставку.'
            )
        delivery_address = (
            data.get('delivery_address', '').strip()
            if isinstance(data.get('delivery_address', ''), str)
            else ''
        )
        if fulfillment_method == 'delivery':
            if not delivery_address or len(delivery_address) > 500:
                raise OrderValidationError(
                    'Укажите адрес доставки длиной не более 500 символов.'
                )
        else:
            delivery_address = None

    return {
        'user_name': user_name,
        'user_phone': user_phone,
        'user_email': user_email,
        'item_type': data['item_type'],
        'item_id': item_id,
        'event_id': event_id,
        'quantity': quantity,
        'fulfillment_method': fulfillment_method,
        'delivery_address': delivery_address,
        'personal_data_consent_at': utcnow(),
        'personal_data_consent_version': PERSONAL_DATA_CONSENT_VERSION,
        'offer_accepted_at': utcnow(),
        'client_total': client_total,
        'idempotency_key': idempotency_key,
    }


def order_item_and_price(order_data):
    if order_data['item_type'] == 'game':
        game = db.session.get(Game, order_data['item_id'])
        event = db.session.get(Event, order_data['event_id'])
        if game is None or event is None or game not in event.games:
            raise OrderValidationError('Игра или выбранное для неё событие не найдены.')
        if event.starts_at is None:
            raise OrderValidationError(
                'У выбранного события не настроена корректная дата.'
            )
        if event.starts_at <= current_event_datetime():
            raise OrderValidationError('Выбранное событие уже прошло.')
        unit_price = positive_price(game.price)
        if unit_price is None:
            raise OrderValidationError('Цена игры ещё не настроена администратором.')
        if game.capacity is None or game.capacity <= 0:
            raise OrderValidationError('Вместимость игры ещё не настроена администратором.')
        return game.title, unit_price, game

    try:
        product_id = int(order_data['item_id'])
    except ValueError:
        raise OrderValidationError('Для товара item_id должен быть целым числом.') from None
    product = db.session.get(Product, product_id)
    # Re-enable this category here when the trees section is restored.
    if product is None or product.category == 'trees':
        raise OrderValidationError('Товар не найден.')
    unit_price = positive_price(product.price)
    if unit_price is None:
        raise OrderValidationError('Цена товара должна быть больше нуля.')
    return product.name, unit_price, None


def create_yookassa_payment(order_id, return_url_base):
    with app.app_context():
        order = db.session.get(Order, order_id)
        if order is None:
            raise RuntimeError(f'Заказ {order_id} не найден для создания платежа.')
        if not Configuration.account_id or not Configuration.secret_key:
            raise PaymentConfigurationError(
                'Платёжный сервис не настроен: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.'
            )

        amount = Decimal(str(order.amount)).quantize(Decimal('0.01'))
        if not order.payment_return_url:
            order.payment_return_url = build_order_return_url(return_url_base, order)
        if not order.payment_expires_at:
            order.payment_expires_at = (
                datetime.now(timezone.utc) + timedelta(minutes=30)
            ).replace(microsecond=0).isoformat()
        db.session.commit()

        try:
            payment = Payment.create(
                {
                    'amount': {'value': f'{amount:.2f}', 'currency': 'RUB'},
                    'confirmation': {
                        'type': 'redirect',
                        'return_url': order.payment_return_url,
                    },
                    'capture': True,
                    'expires_at': order.payment_expires_at,
                    'description': f'Заказ №{order.id}: {order.product_name}',
                    'metadata': {'order_id': order.id},
                },
                idempotency_key=f'order-{order.id}',
            )
        except ApiError:
            mark_order_failed(order.id)
            raise

        confirmation_url = getattr(
            getattr(payment, 'confirmation', None),
            'confirmation_url',
            None,
        )
        if not payment.id or not confirmation_url:
            if payment.id:
                cancel_payment_best_effort(payment.id, order.id)
            raise RuntimeError('ЮKassa вернула платеж без ID или ссылки подтверждения.')

        order.yookassa_id = payment.id
        order.confirmation_url = confirmation_url
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            logging.exception(
                'Could not save YooKassa payment %s for order %s; its seat reservation remains held.',
                payment.id,
                order.id,
            )
            cancel_payment_best_effort(payment.id, order.id)
            raise
        return payment, confirmation_url


def build_order_return_url(return_url_base, order):
    return_parts = urlsplit(return_url_base)
    return_query = dict(parse_qsl(return_parts.query, keep_blank_values=True))
    return_query['order_id'] = str(order.id)
    return_query['token'] = order.return_token
    return urlunsplit(return_parts._replace(query=urlencode(return_query)))


def create_order_and_payment(order_data):
    with app.app_context():
        existing_order = Order.query.filter_by(
            idempotency_key=order_data['idempotency_key']
        ).one_or_none()
        if existing_order is not None:
            return resolve_existing_order(existing_order, order_data)

        item_name, unit_price, game = order_item_and_price(order_data)
        item_amount = (
            unit_price * order_data['quantity']
        ).quantize(Decimal('0.01'))
        amount = item_amount
        if amount != order_data['client_total']:
            raise OrderValidationError(
                'total_price не совпадает с ценой, рассчитанной сервером.'
            )
        requires_payment = order_data['fulfillment_method'] != 'pickup'
        if requires_payment and (
            not Configuration.account_id or not Configuration.secret_key
        ):
            raise PaymentConfigurationError(
                'Платёжный сервис не настроен: задайте YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY.'
            )

        order = Order(
            user_name=order_data['user_name'],
            user_email=order_data['user_email'],
            user_phone=order_data['user_phone'],
            product_name=item_name,
            amount=float(amount),
            status='pending',
            item_type=order_data['item_type'],
            item_id=order_data['item_id'],
            event_id=order_data['event_id'],
            quantity=order_data['quantity'],
            fulfillment_method=order_data['fulfillment_method'],
            delivery_address=order_data['delivery_address'],
            delivery_fee=0,
            personal_data_consent_at=order_data['personal_data_consent_at'],
            personal_data_consent_version=order_data['personal_data_consent_version'],
            offer_accepted_at=order_data['offer_accepted_at'],
            return_token=secrets.token_urlsafe(32),
            idempotency_key=order_data['idempotency_key'],
        )
        db.session.add(order)
        try:
            db.session.flush()
            order.payment_return_url = build_order_return_url(
                order_data['return_url_base'],
                order,
            )
            if not requires_payment:
                order.confirmation_url = order.payment_return_url
            if game is not None:
                reservation = db.session.execute(
                    update(event_games)
                    .where(
                        event_games.c.event_id == order_data['event_id'],
                        event_games.c.game_number == order_data['item_id'],
                        select(Game.capacity)
                        .where(Game.id == event_games.c.game_number)
                        .scalar_subquery()
                        - event_games.c.reserved_places
                        - event_games.c.booked_places
                        >= order_data['quantity'],
                    )
                    .values(
                        reserved_places=(
                            event_games.c.reserved_places + order_data['quantity']
                        )
                    )
                    .execution_options(synchronize_session=False)
                )
                if reservation.rowcount != 1:
                    db.session.rollback()
                    raise OrderValidationError(
                        'На выбранное событие не осталось свободных мест.'
                    )
                order.seat_reserved = True
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            existing_order = Order.query.filter_by(
                idempotency_key=order_data['idempotency_key']
            ).one_or_none()
            if existing_order is not None:
                return resolve_existing_order(existing_order, order_data)
            raise
        except (SQLAlchemyError, OrderValidationError):
            db.session.rollback()
            raise

        order_id = order.id
        if not requires_payment:
            return {
                'order_id': order_id,
                'confirmation_url': order.confirmation_url,
                'payment_required': False,
                '_replayed': False,
            }
        payment, confirmation_url = create_yookassa_payment(
            order_id,
            order_data['return_url_base'],
        )

        return {
            'order_id': order_id,
            'confirmation_url': confirmation_url,
            'payment_id': payment.id,
            'payment_required': True,
            '_replayed': False,
        }


def order_matches_request(order, order_data):
    return (
        order.user_name == order_data['user_name']
        and order.user_email == order_data['user_email']
        and order.user_phone == order_data['user_phone']
        and order.item_type == order_data['item_type']
        and order.item_id == order_data['item_id']
        and order.event_id == order_data['event_id']
        and order.quantity == order_data['quantity']
        and order.fulfillment_method == order_data['fulfillment_method']
        and order.delivery_address == order_data['delivery_address']
        and Decimal(str(order.amount)).quantize(Decimal('0.01'))
        == order_data['client_total']
    )


def existing_order_result(order, order_data):
    if not order_matches_request(order, order_data):
        raise OrderConflictError(
            'Этот ключ уже использован с другими данными заказа. '
            'Обновите форму, чтобы создать новый заказ.',
            code='idempotency_key_reused',
        )
    if order.status != 'pending':
        code = {
            'failed': 'order_failed',
            'canceled': 'order_canceled',
            'paid': 'order_paid',
        }.get(order.status, 'order_closed')
        raise OrderConflictError(
            'Этот ключ уже использован для завершённого или отменённого заказа. '
            'Обновите форму, чтобы создать новый заказ.',
            code=code,
        )
    if order.fulfillment_method == 'pickup':
        if not order.confirmation_url:
            raise OrderConflictError(
                'Заказ уже создаётся. Подождите несколько секунд и повторите запрос.'
            )
        return {
            'order_id': order.id,
            'confirmation_url': order.confirmation_url,
            'payment_required': False,
            '_replayed': True,
        }
    if not order.yookassa_id or not order.confirmation_url:
        raise OrderConflictError(
            'Заказ уже создаётся. Подождите несколько секунд и повторите запрос.'
        )
    return {
        'order_id': order.id,
        'confirmation_url': order.confirmation_url,
        'payment_id': order.yookassa_id,
        'payment_required': True,
        '_replayed': True,
    }


def resolve_existing_order(order, order_data):
    if (
        order.status == 'pending'
        and order.fulfillment_method != 'pickup'
        and not order.confirmation_url
    ):
        create_yookassa_payment(order.id, order_data['return_url_base'])
        db.session.expire_all()
        order = db.session.get(Order, order.id)
    return existing_order_result(order, order_data)


def mark_order_failed(order_id):
    with app.app_context():
        order = db.session.get(Order, order_id)
        if order is None:
            raise RuntimeError(f'Заказ {order_id} не найден при освобождении мест.')
        if order.seat_reserved:
            if not release_reserved_seats(order.event_id, order.item_id, order.quantity):
                db.session.rollback()
                raise RuntimeError(
                    f'Не удалось освободить зарезервированные места заказа {order_id}.'
                )
            order.seat_reserved = False
        order.status = 'failed'
        db.session.commit()


def cancel_payment_best_effort(payment_id, order_id):
    try:
        cancellation = Payment.cancel(
            payment_id,
            idempotency_key=f'cancel-order-{order_id}',
        )
    except ApiError:
        logging.exception(
            'Could not cancel YooKassa payment %s for order %s; reservation remains held.',
            payment_id,
            order_id,
        )
        return
    if read_payment_field(cancellation, 'status') == 'canceled':
        mark_order_failed(order_id)


@app.route('/api/orders/create', methods=['POST'])
async def api_create_order():
    data = request.get_json(silent=True)
    try:
        order_data = parse_order_request(data, request.headers.get('Idempotency-Key'))
        order_data['return_url_base'] = (
            os.environ.get('PAYMENT_RETURN_URL')
            or url_for('payment_success', _external=True)
        )
        result = await asyncio.to_thread(create_order_and_payment, order_data)
    except OrderConflictError as error:
        return jsonify({'error': str(error), 'code': error.code}), 409
    except OrderValidationError as error:
        return jsonify({'error': str(error)}), 400
    except PaymentConfigurationError as error:
        return jsonify({'error': str(error)}), 503
    except ApiError:
        logging.exception('YooKassa could not create the payment.')
        return jsonify({'error': 'Не удалось создать платеж. Попробуйте позже.'}), 502
    except RequestException:
        logging.exception('Could not reach YooKassa while creating a payment.')
        return jsonify({
            'error': 'Не удалось подтвердить создание платежа. Повторите запрос — заказ не будет продублирован.'
        }), 502
    except SQLAlchemyError:
        logging.exception('Could not persist the order or its payment details.')
        return jsonify({
            'error': 'Не удалось сохранить заказ. Повторите запрос с тем же ключом.'
        }), 503
    if not result['payment_required']:
        order_data_for_notification = await asyncio.to_thread(
            get_notification_data,
            result['order_id'],
        )
        notification_errors = await asyncio.gather(
            send_order_notification(
                result['order_id'],
                'email',
                send_customer_email,
                order_data_for_notification,
            ),
            send_admin_notification_safe(order_data_for_notification),
        )
        errors = [error for error in notification_errors if error]
        if errors:
            logging.error(
                'Pickup order %s was created but notifications failed: %s',
                result['order_id'],
                errors,
            )
    replayed = result.pop('_replayed', False)
    return jsonify(result), 200 if replayed else 201


def read_payment_field(payment, field):
    if isinstance(payment, dict):
        return payment.get(field)
    return getattr(payment, field, None)


def apply_payment_webhook(event_name, payment_data):
    payment_id = payment_data.get('id')
    if not isinstance(payment_id, str) or not payment_id:
        raise OrderValidationError('В webhook отсутствует ID платежа.')

    payment = Payment.find_one(payment_id)
    actual_status = read_payment_field(payment, 'status')
    expected_status = 'succeeded' if event_name == 'payment.succeeded' else 'canceled'
    if actual_status != expected_status:
        raise RuntimeError('Статус платежа в ЮKassa пока не подтверждён.')

    metadata = read_payment_field(payment, 'metadata') or {}
    order_id = metadata.get('order_id') if isinstance(metadata, dict) else None
    if order_id is None:
        raise OrderValidationError('В metadata платежа отсутствует ID заказа.')

    payment_amount = read_payment_field(payment, 'amount')
    amount_value = read_payment_field(payment_amount, 'value')
    currency = read_payment_field(payment_amount, 'currency')
    if currency != 'RUB':
        raise OrderValidationError('Валюта платежа должна быть RUB.')
    try:
        verified_amount = Decimal(str(amount_value)).quantize(Decimal('0.01'))
    except (InvalidOperation, ValueError):
        raise OrderValidationError('В платеже ЮKassa указана некорректная сумма.') from None
    if not verified_amount.is_finite() or verified_amount <= 0:
        raise OrderValidationError('В платеже ЮKassa указана некорректная сумма.')
    try:
        order_pk = int(order_id)
    except (TypeError, ValueError):
        raise OrderValidationError('В metadata указан некорректный ID заказа.') from None

    with app.app_context():
        order = db.session.get(Order, order_pk)
        if order is None or order.yookassa_id != payment_id:
            raise OrderValidationError('Заказ для этого платежа не найден.')
        expected_amount = Decimal(str(order.amount)).quantize(Decimal('0.01'))
        if verified_amount != expected_amount:
            raise OrderValidationError('Сумма платежа не совпадает с суммой заказа.')

        target_status = 'paid' if expected_status == 'succeeded' else 'canceled'
        if order.status in {'pending', 'создан'}:
            if order.event_id is not None and order.seat_reserved:
                if target_status == 'paid':
                    result = db.session.execute(
                        update(event_games)
                        .where(
                            event_games.c.event_id == order.event_id,
                            event_games.c.game_number == order.item_id,
                            event_games.c.reserved_places >= order.quantity,
                        )
                        .values(
                            reserved_places=(
                                event_games.c.reserved_places - order.quantity
                            ),
                            booked_places=(
                                event_games.c.booked_places + order.quantity
                            ),
                        )
                        .execution_options(synchronize_session=False)
                    )
                    seats_updated = result.rowcount == 1
                else:
                    seats_updated = release_reserved_seats(
                        order.event_id, order.item_id, order.quantity
                    )
                if not seats_updated:
                    db.session.rollback()
                    raise RuntimeError('Для заказа не найдена активная резервация мест.')
                order.seat_reserved = False
            elif order.event_id is not None and target_status == 'paid':
                raise RuntimeError('Успешному платежу не соответствует резервация мест.')
            order.status = target_status
            db.session.commit()
        elif order.status == 'failed' and target_status == 'canceled':
            order.status = 'canceled'
            db.session.commit()
        elif order.status != target_status:
            raise OrderValidationError('Заказ уже завершён с другим статусом.')

        return order.id


def get_notification_data(order_id):
    with app.app_context():
        order = db.session.get(Order, order_id)
        if order is None:
            raise RuntimeError('Заказ для отправки уведомления не найден.')
        event = db.session.get(Event, order.event_id) if order.event_id else None
        return {
            'order_id': order.id,
            'user_name': order.user_name or 'Клиент',
            'user_email': order.user_email,
            'user_phone': order.user_phone or 'не указан',
            'product_name': order.product_name,
            'amount': Decimal(str(order.amount)).quantize(Decimal('0.01')),
            'quantity': order.quantity,
            'event_date': event.date if event else None,
            'status': order.status,
            'fulfillment_method': order.fulfillment_method,
            'delivery_address': order.delivery_address,
            'delivery_fee': order.delivery_fee,
        }


NOTIFICATION_COLUMNS = {
    'email': (Order.email_notification_status, Order.email_notification_claimed_at),
}


def claim_notification(order_id, channel):
    status_column, claimed_column = NOTIFICATION_COLUMNS[channel]
    now = utcnow()
    with app.app_context():
        result = db.session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                or_(
                    status_column == 'pending',
                    and_(
                        status_column == 'sending',
                        claimed_column < now - timedelta(minutes=5),
                    ),
                ),
            )
            .values(**{
                status_column.key: 'sending',
                claimed_column.key: now,
            })
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
        return result.rowcount == 1


def finish_notification(order_id, channel, sent):
    status_column, claimed_column = NOTIFICATION_COLUMNS[channel]
    with app.app_context():
        db.session.execute(
            update(Order)
            .where(Order.id == order_id, status_column == 'sending')
            .values(**{
                status_column.key: 'sent' if sent else 'pending',
                claimed_column.key: None,
            })
            .execution_options(synchronize_session=False)
        )
        db.session.commit()


def smtp_is_configured():
    return all((
        os.environ.get('SMTP_HOST', ''),
        os.environ.get('SMTP_USERNAME', ''),
        os.environ.get('SMTP_APP_PASSWORD', ''),
    ))


def send_smtp_message(message):
    smtp_host = os.environ.get('SMTP_HOST', '')
    smtp_username = os.environ.get('SMTP_USERNAME', '')
    smtp_password = os.environ.get('SMTP_APP_PASSWORD', '')
    try:
        smtp_port = int(os.environ.get('SMTP_PORT', '587'))
    except ValueError:
        raise RuntimeError('SMTP_PORT должен быть числом.') from None
    if not smtp_host or not smtp_username or not smtp_password:
        raise RuntimeError(
            'SMTP_HOST, SMTP_USERNAME и SMTP_APP_PASSWORD должны быть настроены.'
        )
    context = ssl.create_default_context()
    use_ssl = os.environ.get('SMTP_USE_SSL', '').lower() in {'1', 'true', 'yes'}
    if use_ssl:
        smtp_connection = smtplib.SMTP_SSL(
            smtp_host, smtp_port, timeout=20, context=context
        )
    else:
        smtp_connection = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
    with smtp_connection as smtp:
        if not use_ssl:
            smtp.starttls(context=context)
        smtp.login(smtp_username, smtp_password)
        smtp.send_message(message)


def send_customer_email(order_data):
    message = EmailMessage()
    message['Subject'] = (
        f'Заказ №{order_data["order_id"]} — {order_data["product_name"]}'
    )
    message['From'] = os.environ.get('SMTP_FROM', smtp_username)
    message['To'] = order_data['user_email']
    paid = order_data['status'] == 'paid'
    payment_summary = (
        'Оплата подтверждена.'
        if paid
        else 'Заказ принят. Оплата будет при получении.'
    )
    if order_data['fulfillment_method'] == 'delivery':
        if order_data['delivery_fee'] > 0:
            fulfillment_summary = (
                f'Доставка по России включена в заказ: '
                f'{order_data["delivery_fee"]} ₽. '
            )
        else:
            fulfillment_summary = (
                'Стоимость доставки не входит в сумму заказа и оплачивается '
                'покупателем отдельно перевозчику по его тарифам. '
            )
        fulfillment_summary += f'Адрес доставки: {order_data["delivery_address"]}.'
    else:
        fulfillment_summary = {
            'pickup': f'Самовывоз: {PICKUP_LOCATION}.',
            'event': 'Запись на игру.',
        }.get(order_data['fulfillment_method'], '')
    message.set_content(
        f'Здравствуйте, {order_data["user_name"]}! {payment_summary} '
        f'Заказ: «{order_data["product_name"]}», количество: '
        f'{order_data["quantity"]}. Сумма: {order_data["amount"]:.2f} ₽. '
        f'{fulfillment_summary}'
    )
    event_detail = (
        f'<p>Дата события: {html.escape(order_data["event_date"])}</p>'
        if order_data['event_date']
        else ''
    )
    message.add_alternative(
        '<html><body>'
        f'<h2>Спасибо за заказ, {html.escape(order_data["user_name"])}!</h2>'
        f'<p>{html.escape(payment_summary)}</p>'
        f'<p>Заказ: «{html.escape(order_data["product_name"])}»</p>'
        f'<p>Количество: {order_data["quantity"]}</p>'
        f'<p>Сумма: {order_data["amount"]:.2f} ₽</p>'
        f'<p>{html.escape(fulfillment_summary)}</p>'
        f'{event_detail}'
        '<p>Если у вас возникнут вопросы, ответьте на это письмо.</p>'
        '</body></html>',
        subtype='html',
    )
    send_smtp_message(message)


def send_admin_email_notification(order_data):
    admin_email = os.environ.get('ADMIN_EMAIL', '')
    if not admin_email:
        raise RuntimeError('ADMIN_EMAIL не настроен.')
    event_detail = (
        f'\nДата события: {order_data["event_date"]}'
        if order_data['event_date']
        else ''
    )
    order_title = (
        'Оплачен новый заказ'
        if order_data['status'] == 'paid'
        else 'Новый заказ — оплата при получении'
    )
    if order_data['fulfillment_method'] == 'delivery':
        delivery_detail = (
            f'\nДоставка включена в сумму: {order_data["delivery_fee"]} ₽.'
            if order_data['delivery_fee'] > 0
            else '\nДоставка: за счёт покупателя, оплата перевозчику отдельно.'
        )
        delivery_detail += f'\nАдрес доставки: {order_data["delivery_address"]}.'
    elif order_data['fulfillment_method'] == 'pickup':
        delivery_detail = f'\nСамовывоз: {PICKUP_LOCATION}.'
    else:
        delivery_detail = ''
    message = EmailMessage()
    message['Subject'] = f'{order_title} — №{order_data["order_id"]}'
    message['From'] = os.environ.get(
        'SMTP_FROM', os.environ.get('SMTP_USERNAME', '')
    )
    message['To'] = admin_email
    message.set_content(
        f'{order_title}\n'
        f'Заказ №{order_data["order_id"]}\n'
        f'Игра/товар: {order_data["product_name"]}\n'
        f'Количество: {order_data["quantity"]}\n'
        f'Сумма: {order_data["amount"]:.2f} ₽{event_detail}{delivery_detail}\n'
        f'Покупатель: {order_data["user_name"]}\n'
        f'Телефон: {order_data["user_phone"]}\n'
        f'Email: {order_data["user_email"]}\n'
        f'\nПодробности: /admin/orders/{order_data["order_id"]}'
    )
    send_smtp_message(message)


async def send_admin_notification_safe(order_data):
    if not smtp_is_configured() or not os.environ.get('ADMIN_EMAIL', ''):
        logging.info(
            'SMTP или ADMIN_EMAIL не настроены; уведомление администратора '
            'о заказе %s пропущено.',
            order_data['order_id'],
        )
        return None
    try:
        await asyncio.to_thread(send_admin_email_notification, order_data)
    except (OSError, RuntimeError, smtplib.SMTPException) as error:
        logging.error(
            'Failed sending admin email notification for order %s: %s',
            order_data['order_id'],
            error,
        )
        return f'admin_email: {error}'
    return None


def mark_notification_skipped(order_id, channel):
    status_column, _ = NOTIFICATION_COLUMNS[channel]
    with app.app_context():
        db.session.execute(
            update(Order)
            .where(Order.id == order_id, status_column == 'pending')
            .values(**{status_column.key: 'skipped'})
            .execution_options(synchronize_session=False)
        )
        db.session.commit()


async def send_order_notification(order_id, channel, sender, order_data):
    if channel == 'email' and not smtp_is_configured():
        logging.info(
            'SMTP не настроен; письмо покупателю по заказу %s пропущено.',
            order_id,
        )
        await asyncio.to_thread(mark_notification_skipped, order_id, channel)
        return None
    claimed = await asyncio.to_thread(claim_notification, order_id, channel)
    if not claimed:
        return None
    try:
        await asyncio.to_thread(sender, order_data)
    except (OSError, RuntimeError, smtplib.SMTPException) as error:
        await asyncio.to_thread(finish_notification, order_id, channel, False)
        logging.error(
            'Failed sending %s notification for order %s: %s',
            channel,
            order_id,
            error,
        )
        return f'{channel}: {error}'
    await asyncio.to_thread(finish_notification, order_id, channel, True)
    return None


async def handle_yookassa_webhook():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({'error': 'Ожидался JSON webhook.'}), 400

    event_name = body.get('event')
    if event_name not in {'payment.succeeded', 'payment.canceled'}:
        return jsonify({'received': True}), 200
    payment_data = body.get('object')
    if not isinstance(payment_data, dict):
        return jsonify({'error': 'В webhook отсутствует объект платежа.'}), 400

    try:
        order_id = await asyncio.to_thread(
            apply_payment_webhook, event_name, payment_data
        )
    except OrderValidationError as error:
        return jsonify({'error': str(error)}), 400
    except ApiError:
        logging.exception('Could not verify YooKassa webhook payment.')
        return jsonify({'error': 'Не удалось проверить платеж в ЮKassa.'}), 502
    except RuntimeError as error:
        logging.error('YooKassa webhook processing failed: %s', error)
        return jsonify({'error': str(error)}), 503

    if event_name == 'payment.canceled':
        return jsonify({'received': True, 'order_id': order_id}), 200

    order_data = await asyncio.to_thread(get_notification_data, order_id)
    notification_errors = await asyncio.gather(
        send_order_notification(
            order_id, 'email', send_customer_email, order_data
        ),
        send_admin_notification_safe(order_data),
    )
    errors = [error for error in notification_errors if error]
    if errors:
        return jsonify({
            'error': 'Оплата подтверждена, но часть уведомлений не отправлена.',
            'order_id': order_id,
            'notification_errors': errors,
        }), 503
    return jsonify({'received': True, 'order_id': order_id}), 200


# Обработчик скрытого сигнала (Webhook) от ЮKassa, который меняет статус на "оплачен"
@app.route('/api/payments/webhook', methods=['POST'])
@app.route('/yookassa-webhook', methods=['POST'])
async def api_payment_webhook():
    return await handle_yookassa_webhook()


def order_by_return_token(order_id, token):
    order = db.session.get(Order, order_id) if order_id else None
    if (
        order is None
        or not order.return_token
        or not token
        or not hmac.compare_digest(order.return_token, token)
    ):
        abort(404)
    return order


def normalize_order_status(status):
    if status in {'paid', 'оплачен'}:
        return 'paid'
    if status in {'canceled', 'failed', 'отклонен'}:
        return 'canceled'
    return 'pending'


@app.route('/payment-success')
def payment_success():
    order = order_by_return_token(
        request.args.get('order_id', type=int),
        request.args.get('token', ''),
    )
    token = order.return_token

    if order.fulfillment_method == 'pickup' and order.status == 'paid':
        payment_state = 'pickup_paid'
    elif order.fulfillment_method == 'pickup' and order.status == 'canceled':
        payment_state = 'pickup_canceled'
    elif order.fulfillment_method == 'pickup' and order.status == 'pending':
        payment_state = 'pickup'
    else:
        payment_state = normalize_order_status(order.status)
    response = app.make_response(render_page(
        'success.html',
        'Статус оплаты',
        order=order,
        payment_state=payment_state,
        return_token=token,
        masked_email=mask_email(order.user_email),
        pickup_location=PICKUP_LOCATION,
    ))
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'
    return response


def mask_email(email):
    local_part, separator, domain = email.partition('@')
    if not separator:
        return '***'
    return f'{local_part[:1]}***@{domain}'


@app.route('/api/orders/<int:order_id>/status')
def api_order_status(order_id):
    order = order_by_return_token(order_id, request.args.get('token', ''))
    response = jsonify({'status': normalize_order_status(order.status)})
    response.headers['Cache-Control'] = 'no-store'
    return response


# ========================================================
# ВАШИ СУЩЕСТВУЮЩИЕ РОУТЫ СТРАНИЦ
# ========================================================
@app.route('/')
def home():
    return render_page('index.html', 'Главная', events=get_events())

@app.route('/health')
def health():
    db.session.execute(select(1))
    return jsonify({'status': 'ok'})

@app.route('/games')
def games():
    return render_page('games.html', 'Игры', games=get_games(), events=get_events())

@app.route('/games/<string:game_id>')
def game_detail(game_id):
    game = db.session.get(Game, game_id)
    if game is None:
        abort(404)

    game_photos = [photo for photo in game.photos if photo] if isinstance(game.photos, list) else []

    if request.headers.get('X-SPA-Request') == 'true':
        return render_template(
            '_game_detail_content.html',
            game=game,
            game_photos=game_photos,
        )

    return render_page(
        'game_detail.html',
        game.title,
        game=game,
        game_photos=game_photos,
        page_description=game.desc,
        page_image=game_photos[0] if game_photos else 'derevo.jpg',
    )

@app.route('/consultations')
def consultations():
    return render_page('consultations.html', 'Консультации')

@app.route('/obuchenie')
def obuchenie():
    return render_page('obuchenie.html', 'Обучение', events=get_events())

@app.route('/contact')
def contact():
    checkout_item = None
    event_options = []
    item_type = request.args.get('item_type')
    item_id = request.args.get('item_id', '').strip()
    selected_event_id = request.args.get('event_id', type=int)

    if item_type == 'game':
        game = db.session.get(Game, item_id) if item_id else None
        if game is None:
            abort(404)
        unit_price = positive_price(game.price)
        checkout_item = {
            'item_type': 'game',
            'item_id': game.id,
            'name': game.title,
            'unit_price': unit_price,
            'can_checkout': unit_price is not None,
        }
        scheduled_events = (
            db.session.query(
                Event,
                event_games.c.reserved_places,
                event_games.c.booked_places,
            )
            .join(event_games, event_games.c.event_id == Event.id)
            .filter(event_games.c.game_number == game.id)
            .filter(Event.starts_at > current_event_datetime())
            .order_by(Event.id)
            .all()
        )
        for event, reserved, booked in scheduled_events:
            available = max((game.capacity or 0) - reserved - booked, 0)
            event_options.append({
                'id': event.id,
                'date': event.date,
                'available': available,
                'selected': event.id == selected_event_id,
            })
    elif item_type == 'product':
        try:
            product_id = int(item_id)
        except ValueError:
            abort(404)
        product = db.session.get(Product, product_id)
        # Re-enable this category here when the trees section is restored.
        if product is None or product.category == 'trees':
            abort(404)
        unit_price = positive_price(product.price)
        checkout_item = {
            'item_type': 'product',
            'item_id': str(product.id),
            'name': product.name,
            'podzagolovok': product.podzagolovok,
            'unit_price': unit_price,
            'can_checkout': unit_price is not None,
        }
    elif item_type:
        abort(404)

    return render_page(
        'contact.html',
        'Оформление заказа' if checkout_item else 'Написать нам',
        product_name=request.args.get('product', ''),
        game_name=request.args.get('game', ''),
        checkout_item=checkout_item,
        event_options=event_options,
        pickup_location=PICKUP_LOCATION,
    )

@app.route('/privacy')
def privacy_policy():
    return render_page('legal/privacy.html', 'Политика конфиденциальности и условия заказа')

@app.route('/market')
def market():
    categories = [
        # To restore the trees category, uncomment this entry and the route below.
        # {
        #     'title': 'Древа жизни',
        #     'description': 'Образы укоренения и связи поколений — материальное напоминание о своей опоре.',
        #     'action': 'Найти корни',
        #     'endpoint': 'market_trees',
        #     'photo': 'derevo.jpg',
        #     'tone': 'trees',
        # },
        {
            'title': 'Мандалы',
            'description': 'Геометрия личной силы — образы для собранности, намерения и собственного ритма.',
            'action': 'Собрать себя',
            'endpoint': 'market_mandalas',
            'photo': 'mandala11.jpg',
            'tone': 'mandalas',
        },
        {
            'title': 'Куклы-мотанки',
            'description': 'Авторские текстильные образы, созданные как символ тепла, заботы и внимания.',
            'action': 'Обрести защиту',
            'endpoint': 'market_dolls',
            'photo': 'dolss.jpg',
            'tone': 'dolls',
        },
    ]
    return render_page('market.html', 'Сакральный маркет', categories=categories)

@app.route('/market/mandalas')
def market_mandalas():
    selected_subcategory = request.args.get('subcategory', '').strip()
    if selected_subcategory and selected_subcategory not in PRODUCT_SUBCATEGORIES:
        abort(400)

    return render_page(
        'mandalas.html',
        'Мандалы',
        products=get_products('mandalas', selected_subcategory or None),
        subcategories=PRODUCT_SUBCATEGORIES,
        selected_subcategory=selected_subcategory,
    )

# To restore this page, uncomment the route and the trees entry in market().
# @app.route('/market/trees')
# def market_trees():
#     return render_page('trees.html', 'Древа жизни', products=get_products('trees'))
@app.route('/market/dolls')
def market_dolls():
    return render_page('dolls.html', 'Куклы-мотанки', products=get_products('dolls'))

@app.route('/market/product/<int:product_id>')
def product_detail(product_id):
    product = db.session.get(Product, product_id)
    # Re-enable this category here when the trees section is restored.
    if product is None or product.category == 'trees':
        abort(404)

    product_photos = [photo for photo in [product.photo] if photo]
    if isinstance(product.photos, list):
        product_photos.extend(photo for photo in product.photos if photo)

    if request.headers.get('X-SPA-Request') == 'true':
        return render_template(
            '_product_detail_content.html',
            product=product,
            product_photos=product_photos,
        )

    return render_page(
        'product_detail.html',
        product.name,
        product=product,
        product_photos=product_photos,
        page_description=product.description,
        page_image=product_photos[0] if product_photos else 'derevo.jpg',
    )

@app.route('/about')
def about():
    return render_page('about.html', 'О нас')

# ========================================================
# ЗАПУСК ПРИЛОЖЕНИЯ С АВТО-СОЗДАНИЕМ ТАБЛИЦ БАЗЫ ДАННЫХ
# ========================================================
if __name__ == '__main__':
    with app.app_context():
        db.create_all()  # Создает файлы базы данных строго при запуске
    app.run(
            host='0.0.0.0',
        port=int(os.environ.get('PORT', '5000')),
        debug=debug_enabled,
    )
