import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event as sqlalchemy_event, text


_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix='igri-eltoi5-tests-')
_TEST_DATABASE = Path(_TEST_DIRECTORY.name) / 'payments.sqlite3'
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TEST_DATABASE.as_posix()}')
os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key-that-is-not-used-in-production')
os.environ.setdefault('SMTP_HOST', 'smtp.example.test')
os.environ.setdefault('SMTP_USERNAME', 'orders@example.test')
os.environ.setdefault('SMTP_APP_PASSWORD', 'test-smtp-password')
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'test-admin-password')
os.environ.setdefault('ADMIN_EMAIL', 'admin@example.test')

import app as site
from db import (
    Event,
    Game,
    Order,
    Product,
    db,
    event_games,
    migrate_product_details,
)


def tearDownModule():
    with site.app.app_context():
        db.session.remove()
        db.engine.dispose()
    _TEST_DIRECTORY.cleanup()


class PaymentFlowTests(unittest.TestCase):
    def setUp(self):
        with site.app.app_context():
            db.drop_all()
            db.create_all()
            db.session.add_all([
                Product(
                    category='dolls',
                    name='Тестовый товар',
                    description='Товар для автоматического теста.',
                    price=1000,
                    photo='test-product.jpg',
                ),
                Product(
                    category='trees',
                    name='Отключённый товар',
                    description='Товар для проверки недоступной категории.',
                    price=1000,
                    photo='test-tree.jpg',
                ),
            ])
            db.session.commit()

        site.Configuration.account_id = 'test-account'
        site.Configuration.secret_key = 'test-secret'
        self.client = site.app.test_client()
        payment = SimpleNamespace(
            id='test-payment-id',
            confirmation=SimpleNamespace(
                confirmation_url='https://payments.example.test/confirm'
            ),
        )
        self.payment_create = patch.object(
            site.Payment,
            'create',
            return_value=payment,
        )
        self.payment_find = patch.object(
            site.Payment,
            'find_one',
            return_value=None,
        )
        self.email_sender = patch.object(site, 'send_customer_email')
        self.admin_sender = patch.object(site, 'send_admin_email_notification')
        self.payment_create_mock = self.payment_create.start()
        self.payment_find_mock = self.payment_find.start()
        self.email_sender_mock = self.email_sender.start()
        self.admin_sender_mock = self.admin_sender.start()
        self.addCleanup(self.payment_create.stop)
        self.addCleanup(self.payment_find.stop)
        self.addCleanup(self.email_sender.stop)
        self.addCleanup(self.admin_sender.stop)

    def tearDown(self):
        with site.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def test_health_endpoint_checks_database_and_returns_ok(self):
        response = self.client.get('/health')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {'status': 'ok'})

    def test_market_page_handles_empty_product_photos_value(self):
        with site.app.app_context():
            db.session.execute(
                text("UPDATE product SET photos = '' WHERE category = 'dolls'")
            )
            db.session.commit()

            migrate_product_details()
            db.session.expire_all()
            product = Product.query.filter_by(category='dolls').one()
            self.assertIsNone(product.photos)

        response = self.client.get('/market/dolls')
        self.assertEqual(response.status_code, 200)

    def test_base_template_renders_metadata_and_current_mobile_navigation(self):
        response = self.client.get('/market/dolls')

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            b'<link rel="canonical" href="http://localhost/market/dolls">',
            response.data,
        )
        self.assertIn(b'property="og:title"', response.data)
        self.assertIn(
            b'name="twitter:card" content="summary_large_image"',
            response.data,
        )
        self.assertIn(b'property="og:image"', response.data)
        self.assertIn(b'href="/static/tailwind.css"', response.data)
        self.assertIn(
            'aria-current="page">Сакральный маркет</a>'.encode(),
            response.data,
        )
        self.assertNotIn(b'cdn.tailwindcss.com', response.data)
        self.assertNotIn(b'fonts.googleapis.com', response.data)
        font_response = self.client.get(
            '/static/files/manrope-cyrillic-wght-normal.woff2'
        )
        self.assertEqual(font_response.status_code, 200)
        font_response.close()

    def test_public_urls_respect_https_forwarded_by_reverse_proxy(self):
        response = self.client.get(
            '/market/dolls',
            headers={
                'X-Forwarded-Host': 'drevoeltoi.ru',
                'X-Forwarded-Proto': 'https',
            },
        )

        self.assertIn(
            b'<link rel="canonical" href="https://drevoeltoi.ru/market/dolls">',
            response.data,
        )
        self.assertIn(
            b'property="og:image" content="https://drevoeltoi.ru/static/',
            response.data,
        )

    def test_product_detail_uses_product_description_for_metadata(self):
        response = self.client.get('/market/product/1')

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            'content="Товар для автоматического теста."'.encode(),
            response.data,
        )

    def test_trees_category_and_existing_products_are_unavailable(self):
        self.assertNotIn('Древа жизни'.encode(), self.client.get('/market').data)
        self.assertNotIn(b'branch-link--trees', self.client.get('/').data)
        self.assertEqual(self.client.get('/market/trees').status_code, 404)
        self.assertEqual(self.client.get('/market/product/2').status_code, 404)
        self.assertEqual(
            self.client.get('/contact?item_type=product&item_id=2').status_code,
            404,
        )

        response = self.create_online_order(
            self.product_payload(item_id='2'),
            key='disabled-tree-order-0001',
        )
        self.assertEqual(response.status_code, 400)
        self.payment_create_mock.assert_not_called()

    @staticmethod
    def product_payload(**overrides):
        payload = {
            'user_name': 'Тестовый Покупатель',
            'user_phone': '+79990000000',
            'user_email': 'buyer@example.test',
            'offer_accepted': True,
            'personal_data_consent': True,
            'item_type': 'product',
            'item_id': '1',
            'quantity': 1,
            'fulfillment_method': 'delivery',
            'delivery_address': 'Якутск, тестовая улица, дом 1',
            'total_price': '1000.00',
        }
        payload.update(overrides)
        return payload

    def create_online_order(self, payload=None, key='test-order-key-0001'):
        return self.client.post(
            '/api/orders/create',
            json=payload or self.product_payload(),
            headers={'Idempotency-Key': key},
        )

    def test_site_pages_render_with_shared_base_template(self):
        with site.app.app_context():
            db.session.add(
                Game(
                    id='01',
                    title='Тестовая игра',
                    target='Для теста',
                    desc='Описание',
                    price=500,
                    capacity=10,
                )
            )
            db.session.commit()

        paths = (
            '/',
            '/games',
            '/games/01',
            '/consultations',
            '/obuchenie',
            '/contact',
            '/privacy',
            '/contact?item_type=product&item_id=1',
            '/market',
            '/market/mandalas',
            '/market/dolls',
            '/market/product/1',
            '/about',
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.data.count(b'<header class="site-header'),
                    1,
                )
                self.assertEqual(
                    response.data.count(b'<footer class="site-footer'),
                    1,
                )

    def test_removed_legal_pages_are_not_routed(self):
        for path in ('/personal-data-consent', '/offer', '/cookies'):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_product_checkout_explains_delivery_is_paid_separately(self):
        response = self.client.get('/contact?item_type=product&item_id=1')

        self.assertEqual(response.status_code, 200)
        self.assertIn('оплата перевозчику отдельно'.encode(), response.data)
        self.assertIn('Доставка не входит в сумму заказа на сайте'.encode(), response.data)
        self.assertNotIn('DELIVERY_FEE'.encode(), response.data)

    def test_product_checkout_with_non_numeric_price_shows_contact_fallback(self):
        with site.app.app_context():
            product = db.session.get(Product, 1)
            product.price = 'не указана'
            db.session.commit()

        response = self.client.get('/contact?item_type=product&item_id=1')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Стоимость пока уточняется'.encode(), response.data)
        self.assertNotIn(b'id="checkout-form"', response.data)

    def test_order_with_non_numeric_product_price_returns_validation_error(self):
        with site.app.app_context():
            product = db.session.get(Product, 1)
            product.price = 'не указана'
            db.session.commit()

        response = self.create_online_order()

        self.assertEqual(response.status_code, 400)
        self.assertIn('Цена товара'.encode(), response.get_json()['error'].encode())
        self.payment_create_mock.assert_not_called()

    def test_get_events_loads_all_rows_with_one_query(self):
        with site.app.app_context():
            games = [
                Game(
                    id='01',
                    title='Тестовая игра 1',
                    target='Для теста',
                    desc='Описание',
                    capacity=10,
                ),
                Game(
                    id='02',
                    title='Тестовая игра 2',
                    target='Для теста',
                    desc='Описание',
                    capacity=10,
                ),
            ]
            events = [
                Event(
                    date='Будущее событие',
                    starts_at=datetime.now() + timedelta(days=30 + index),
                    games=games,
                )
                for index in range(2)
            ]
            db.session.add_all(events)
            db.session.commit()

            statements = []

            def record_query(*args):
                statements.append(args[2])

            sqlalchemy_event.listen(
                db.engine,
                'before_cursor_execute',
                record_query,
            )
            try:
                results = site.get_events()
            finally:
                sqlalchemy_event.remove(
                    db.engine,
                    'before_cursor_execute',
                    record_query,
                )

        self.assertEqual(len(statements), 1)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(event['available_places'] == 10 for event in results))

    def test_rejects_client_total_that_differs_from_server_price(self):
        response = self.create_online_order(
            self.product_payload(total_price='1.00')
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('total_price', response.get_json()['error'])
        self.payment_create_mock.assert_not_called()
        with site.app.app_context():
            self.assertEqual(Order.query.count(), 0)

    def test_delivery_cost_is_not_added_to_the_online_order_total(self):
        response = self.create_online_order(
            self.product_payload(total_price='1666.00')
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('total_price', response.get_json()['error'])
        self.payment_create_mock.assert_not_called()

    def test_rejects_delivery_order_without_address(self):
        response = self.create_online_order(
            self.product_payload(delivery_address='')
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('адрес доставки', response.get_json()['error'])
        self.payment_create_mock.assert_not_called()

    def test_requires_offer_acceptance_and_personal_data_consent(self):
        for field in ('offer_accepted', 'personal_data_consent'):
            with self.subTest(field=field):
                payload = self.product_payload(**{field: False})
                response = self.create_online_order(payload)

                self.assertEqual(response.status_code, 400)
                self.payment_create_mock.assert_not_called()
                with site.app.app_context():
                    self.assertEqual(Order.query.count(), 0)

    def test_idempotent_retry_reuses_payment_instead_of_creating_duplicate(self):
        first = self.create_online_order()
        second = self.create_online_order()

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(
            first.get_json()['confirmation_url'],
            second.get_json()['confirmation_url'],
        )
        self.payment_create_mock.assert_called_once()
        with site.app.app_context():
            self.assertEqual(Order.query.count(), 1)
            order = Order.query.first()
            self.assertEqual(order.amount, 1000.0)
            self.assertEqual(order.delivery_fee, 0)
            self.assertIsNotNone(order.personal_data_consent_at)
            self.assertEqual(
                order.personal_data_consent_version,
                site.PERSONAL_DATA_CONSENT_VERSION,
            )
            self.assertIsNotNone(order.offer_accepted_at)

    def test_admin_email_notice_contains_order_and_customer_contacts(self):
        self.admin_sender.stop()
        order_data = {
            'order_id': 12,
            'product_name': 'Тестовый товар',
            'quantity': 1,
            'amount': 1666.0,
            'status': 'paid',
            'event_date': None,
            'fulfillment_method': 'delivery',
            'user_name': 'Персональные Имя',
            'user_phone': '+79990000000',
            'user_email': 'private@example.test',
            'delivery_address': 'Персональный адрес',
            'delivery_fee': 0,
        }

        with patch.object(site, 'send_smtp_message') as send_message:
            site.send_admin_email_notification(order_data)

        message = send_message.call_args.args[0]
        self.assertEqual(message['To'], 'admin@example.test')
        self.assertIn('№12', str(message['Subject']))
        body = message.get_content()
        self.assertIn('Тестовый товар', body)
        self.assertIn('Доставка: за счёт покупателя', body)
        for contact_value in (
            'Персональные Имя',
            '+79990000000',
            'private@example.test',
            'Персональный адрес',
        ):
            self.assertIn(contact_value, body)

    def test_rejects_reusing_idempotency_key_with_different_order(self):
        self.create_online_order()
        response = self.create_online_order(
            self.product_payload(
                quantity=2,
                total_price='2000.00',
            )
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.get_json()['code'],
            'idempotency_key_reused',
        )
        self.payment_create_mock.assert_called_once()

    def test_successful_payment_webhook_marks_order_and_sends_notifications(self):
        created = self.create_online_order()
        order_id = created.get_json()['order_id']
        self.payment_find_mock.return_value = SimpleNamespace(
            status='succeeded',
            metadata={'order_id': order_id},
            amount=SimpleNamespace(value='1000.00', currency='RUB'),
        )

        response = self.client.post(
            '/api/payments/webhook',
            json={'event': 'payment.succeeded', 'object': {'id': 'test-payment-id'}},
        )

        self.assertEqual(response.status_code, 200)
        with site.app.app_context():
            order = db.session.get(Order, order_id)
            self.assertEqual(order.status, 'paid')
            self.assertEqual(order.email_notification_status, 'sent')
        self.email_sender_mock.assert_called_once()
        self.admin_sender_mock.assert_called_once()

    def test_paid_game_order_moves_reserved_places_to_booked_places(self):
        with site.app.app_context():
            game = Game(
                id='01',
                title='Игра для подтверждения оплаты',
                target='Взрослые',
                desc='Тест.',
                price=500,
                capacity=2,
            )
            event = Event(
                date='Тестовое будущее событие',
                starts_at=datetime.now() + timedelta(days=30),
                games=[game],
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id
        self.payment_create_mock.return_value = SimpleNamespace(
            id='game-payment-id',
            confirmation=SimpleNamespace(
                confirmation_url='https://payments.example.test/game'
            ),
        )
        created = self.client.post(
            '/api/orders/create',
            json={
                'user_name': 'Тестовый участник',
                'user_phone': '+79990000000',
                'user_email': 'player@example.test',
                'offer_accepted': True,
                'personal_data_consent': True,
                'item_type': 'game',
                'item_id': '01',
                'event_id': event_id,
                'quantity': 1,
                'total_price': '500.00',
            },
            headers={'Idempotency-Key': 'test-paid-game-order-0001'},
        )
        self.assertEqual(created.status_code, 201)
        order_id = created.get_json()['order_id']
        with site.app.app_context():
            places = db.session.execute(
                db.select(
                    event_games.c.reserved_places,
                    event_games.c.booked_places,
                ).where(
                    event_games.c.event_id == event_id,
                    event_games.c.game_number == '01',
                )
            ).one()
            self.assertEqual(tuple(places), (1, 0))

        self.payment_find_mock.return_value = SimpleNamespace(
            status='succeeded',
            metadata={'order_id': order_id},
            amount=SimpleNamespace(value='500.00', currency='RUB'),
        )
        response = self.client.post(
            '/api/payments/webhook',
            json={'event': 'payment.succeeded', 'object': {'id': 'game-payment-id'}},
        )

        self.assertEqual(response.status_code, 200)
        with site.app.app_context():
            places = db.session.execute(
                db.select(
                    event_games.c.reserved_places,
                    event_games.c.booked_places,
                ).where(
                    event_games.c.event_id == event_id,
                    event_games.c.game_number == '01',
                )
            ).one()
            self.assertEqual(tuple(places), (0, 1))
            self.assertEqual(db.session.get(Order, order_id).status, 'paid')

    def test_wrong_amount_in_payment_webhook_does_not_mark_order_paid(self):
        created = self.create_online_order()
        order_id = created.get_json()['order_id']
        self.payment_find_mock.return_value = SimpleNamespace(
            status='succeeded',
            metadata={'order_id': order_id},
            amount=SimpleNamespace(value='10.00', currency='RUB'),
        )

        response = self.client.post(
            '/api/payments/webhook',
            json={'event': 'payment.succeeded', 'object': {'id': 'test-payment-id'}},
        )

        self.assertEqual(response.status_code, 400)
        with site.app.app_context():
            self.assertEqual(db.session.get(Order, order_id).status, 'pending')
        self.email_sender_mock.assert_not_called()
        self.admin_sender_mock.assert_not_called()

    def test_canceled_payment_does_not_send_paid_notifications(self):
        created = self.create_online_order()
        order_id = created.get_json()['order_id']
        self.payment_find_mock.return_value = SimpleNamespace(
            status='canceled',
            metadata={'order_id': order_id},
            amount=SimpleNamespace(value='1000.00', currency='RUB'),
        )

        response = self.client.post(
            '/api/payments/webhook',
            json={'event': 'payment.canceled', 'object': {'id': 'test-payment-id'}},
        )

        self.assertEqual(response.status_code, 200)
        with site.app.app_context():
            self.assertEqual(db.session.get(Order, order_id).status, 'canceled')
        self.email_sender_mock.assert_not_called()
        self.admin_sender_mock.assert_not_called()

    def test_rejects_game_order_exceeding_event_capacity(self):
        with site.app.app_context():
            game = Game(
                id='01',
                title='Тестовая игра',
                target='Взрослые',
                desc='Описание тестовой игры.',
                price=500,
                capacity=1,
            )
            event = Event(
                date='Тестовое будущее событие',
                starts_at=datetime.now() + timedelta(days=30),
                games=[game],
            )
            db.session.add(event)
            db.session.commit()
            event_id = event.id

        response = self.client.post(
            '/api/orders/create',
            json={
                'user_name': 'Тестовый участник',
                'user_phone': '+79990000000',
                'user_email': 'player@example.test',
                'offer_accepted': True,
                'personal_data_consent': True,
                'item_type': 'game',
                'item_id': '01',
                'event_id': event_id,
                'quantity': 2,
                'total_price': '1000.00',
            },
            headers={'Idempotency-Key': 'test-game-order-0001'},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn('свободных мест', response.get_json()['error'])
        self.payment_create_mock.assert_not_called()
        with site.app.app_context():
            self.assertEqual(Order.query.count(), 0)
            reservation = db.session.execute(
                db.select(event_games.c.reserved_places).where(
                    event_games.c.event_id == event_id,
                    event_games.c.game_number == '01',
                )
            ).scalar_one()
            self.assertEqual(reservation, 0)


if __name__ == '__main__':
    unittest.main()
