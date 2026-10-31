import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


_TEST_DIRECTORY = tempfile.TemporaryDirectory(prefix='igri-eltoi5-admin-tests-')
_TEST_DATABASE = Path(_TEST_DIRECTORY.name) / 'admin.sqlite3'
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_TEST_DATABASE.as_posix()}')
os.environ.setdefault('FLASK_SECRET_KEY', 'test-secret-key-that-is-not-used-in-production')
os.environ.setdefault('SMTP_HOST', 'smtp.example.test')
os.environ.setdefault('SMTP_USERNAME', 'orders@example.test')
os.environ.setdefault('SMTP_APP_PASSWORD', 'test-smtp-password')
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'test-admin-password')
os.environ.setdefault('ADMIN_EMAIL', 'admin@example.test')

import app as site
from db import Event, Game, Order, Product, Review, db


CSRF_TOKEN = 'test-csrf-token'


class AdminPanelTests(unittest.TestCase):
    def setUp(self):
        with site.app.app_context():
            db.drop_all()
            db.create_all()
            db.session.commit()
        self.client = site.app.test_client()

    def tearDown(self):
        with site.app.app_context():
            db.session.remove()
            db.engine.dispose()

    def authenticate(self):
        with self.client.session_transaction() as flask_session:
            flask_session['admin_authenticated'] = True
            flask_session['_csrf_token'] = CSRF_TOKEN

    def csrf_data(self, **fields):
        fields['csrf_token'] = CSRF_TOKEN
        return fields

    def test_admin_pages_redirect_anonymous_visitors_to_login(self):
        for path in (
            '/admin/',
            '/admin/orders',
            '/admin/games',
            '/admin/products',
            '/admin/events',
            '/admin/reviews',
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 302)
                self.assertIn('/admin/login', response.headers['Location'])

    def test_login_rejects_wrong_password_and_accepts_correct_one(self):
        login_page = self.client.get('/admin/login')
        self.assertEqual(login_page.status_code, 200)
        token = re.search(
            rb'name="csrf_token" value="([^"]+)"', login_page.data
        ).group(1).decode()

        bad = self.client.post('/admin/login', data={
            'csrf_token': token,
            'username': 'admin',
            'password': 'wrong-password',
        })
        self.assertEqual(bad.status_code, 200)
        self.assertIn('Неверный логин или пароль'.encode(), bad.data)

        good = self.client.post('/admin/login', data={
            'csrf_token': token,
            'username': 'admin',
            'password': 'test-admin-password',
        })
        self.assertEqual(good.status_code, 302)
        self.assertIn('/admin/', good.headers['Location'])
        self.assertEqual(self.client.get('/admin/').status_code, 200)

    def test_admin_post_without_csrf_token_is_rejected(self):
        self.authenticate()
        response = self.client.post('/admin/games/add', data={
            'title': 'Игра без токена',
            'target': 'Все',
            'desc': 'Описание',
        })
        self.assertEqual(response.status_code, 400)
        with site.app.app_context():
            self.assertEqual(Game.query.count(), 0)

    def test_dashboard_shows_orders_and_revenue(self):
        with site.app.app_context():
            db.session.add_all([
                Order(
                    user_email='paid@example.test',
                    product_name='Оплаченный товар',
                    amount=1500.0,
                    status='paid',
                ),
                Order(
                    user_email='pending@example.test',
                    product_name='Ожидающий товар',
                    amount=700.0,
                    status='pending',
                ),
            ])
            db.session.commit()

        self.authenticate()
        response = self.client.get('/admin/')

        self.assertEqual(response.status_code, 200)
        self.assertIn('1500.00'.encode(), response.data)
        self.assertIn('Оплаченный товар'.encode(), response.data)

    def test_game_crud(self):
        self.authenticate()

        created = self.client.post('/admin/games/add', data=self.csrf_data(
            title='Тестовая игра',
            target='Взрослые',
            desc='Описание игры',
            price='500',
            capacity='10',
        ))
        self.assertEqual(created.status_code, 302)
        with site.app.app_context():
            game = Game.query.filter_by(title='Тестовая игра').one()
            self.assertEqual(game.price, 500)
            game_id = game.id

        updated = self.client.post(
            f'/admin/games/{game_id}/edit',
            data=self.csrf_data(
                title='Переименованная игра',
                target='Взрослые',
                desc='Новое описание',
                price='600',
                capacity='12',
            ),
        )
        self.assertEqual(updated.status_code, 302)
        with site.app.app_context():
            game = db.session.get(Game, game_id)
            self.assertEqual(game.title, 'Переименованная игра')
            self.assertEqual(game.price, 600)

        deleted = self.client.post(
            f'/admin/games/{game_id}/delete',
            data=self.csrf_data(),
        )
        self.assertEqual(deleted.status_code, 302)
        with site.app.app_context():
            self.assertIsNone(db.session.get(Game, game_id))

    def test_product_crud(self):
        self.authenticate()

        created = self.client.post('/admin/products/add', data=self.csrf_data(
            category='mandalas',
            subcategory='Денежные',
            name='Тестовая мандала',
            podzagolovok='Подзаголовок',
            description='Описание товара',
            price='1000',
            photo='mandala11.jpg',
        ))
        self.assertEqual(created.status_code, 302)
        with site.app.app_context():
            product = Product.query.filter_by(name='Тестовая мандала').one()
            self.assertEqual(product.price, 1000)
            product_id = product.id

        updated = self.client.post(
            f'/admin/products/{product_id}/edit',
            data=self.csrf_data(
                category='dolls',
                subcategory='',
                name='Тестовая кукла',
                podzagolovok='',
                description='Новое описание',
                full_description='',
                materials='',
                dimensions='',
                price='1200',
                photo='dolss.jpg',
            ),
        )
        self.assertEqual(updated.status_code, 302)
        with site.app.app_context():
            product = db.session.get(Product, product_id)
            self.assertEqual(product.category, 'dolls')
            self.assertEqual(product.price, 1200)

        deleted = self.client.post(
            f'/admin/products/{product_id}/delete',
            data=self.csrf_data(),
        )
        self.assertEqual(deleted.status_code, 302)
        with site.app.app_context():
            self.assertIsNone(db.session.get(Product, product_id))

    def test_event_crud(self):
        with site.app.app_context():
            db.session.add(Game(
                id='01',
                title='Игра для события',
                target='Взрослые',
                desc='Описание',
                price=500,
                capacity=10,
            ))
            db.session.commit()

        self.authenticate()
        starts_at = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%dT%H:%M')
        created = self.client.post('/admin/events/add', data=self.csrf_data(
            starts_at=starts_at,
            game_ids=['01'],
        ))
        self.assertEqual(created.status_code, 302)
        with site.app.app_context():
            event = Event.query.one()
            self.assertIsNotNone(event.starts_at)
            self.assertEqual([game.id for game in event.games], ['01'])
            event_id = event.id

        past_event = self.client.post('/admin/events/add', data=self.csrf_data(
            starts_at='2020-01-01T10:00',
            game_ids=['01'],
        ))
        self.assertEqual(past_event.status_code, 200)
        with site.app.app_context():
            self.assertEqual(Event.query.count(), 1)

        deleted = self.client.post(
            f'/admin/events/{event_id}/delete',
            data=self.csrf_data(),
        )
        self.assertEqual(deleted.status_code, 302)
        with site.app.app_context():
            self.assertEqual(Event.query.count(), 0)

    def test_review_crud_and_publish_toggle(self):
        self.authenticate()

        created = self.client.post('/admin/reviews/add', data=self.csrf_data(
            author='Автор',
            body='Текст отзыва',
        ))
        self.assertEqual(created.status_code, 302)
        with site.app.app_context():
            review = Review.query.one()
            self.assertFalse(review.is_published)
            review_id = review.id

        toggled = self.client.post(
            f'/admin/reviews/{review_id}/publish',
            data=self.csrf_data(),
        )
        self.assertEqual(toggled.status_code, 302)
        with site.app.app_context():
            self.assertTrue(db.session.get(Review, review_id).is_published)

        deleted = self.client.post(
            f'/admin/reviews/{review_id}/delete',
            data=self.csrf_data(),
        )
        self.assertEqual(deleted.status_code, 302)
        with site.app.app_context():
            self.assertEqual(Review.query.count(), 0)

    def test_reviews_list_paginates(self):
        with site.app.app_context():
            db.session.add_all([
                Review(author=f'Автор {index}', body='Текст')
                for index in range(25)
            ])
            db.session.commit()

        self.authenticate()
        first_page = self.client.get('/admin/reviews')
        second_page = self.client.get('/admin/reviews?page=2')

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(second_page.status_code, 200)
        self.assertIn('Автор 24'.encode(), first_page.data)
        self.assertIn('Автор 0'.encode(), second_page.data)

    def test_pickup_order_can_be_marked_paid_and_canceled(self):
        with site.app.app_context():
            db.session.add_all([
                Order(
                    user_email='one@example.test',
                    product_name='Заказ на самовывоз',
                    amount=900.0,
                    status='pending',
                    fulfillment_method='pickup',
                ),
                Order(
                    user_email='two@example.test',
                    product_name='Второй заказ',
                    amount=300.0,
                    status='pending',
                    fulfillment_method='pickup',
                ),
            ])
            db.session.commit()
            first_id, second_id = [order.id for order in Order.query.all()]

        self.authenticate()
        paid = self.client.post(
            f'/admin/orders/{first_id}/status',
            data=self.csrf_data(action='mark_paid'),
        )
        self.assertEqual(paid.status_code, 302)
        canceled = self.client.post(
            f'/admin/orders/{second_id}/status',
            data=self.csrf_data(action='cancel'),
        )
        self.assertEqual(canceled.status_code, 302)
        with site.app.app_context():
            self.assertEqual(db.session.get(Order, first_id).status, 'paid')
            self.assertEqual(db.session.get(Order, second_id).status, 'canceled')

        # Уже обработанный заказ нельзя изменить повторно.
        repeated = self.client.post(
            f'/admin/orders/{first_id}/status',
            data=self.csrf_data(action='cancel'),
        )
        self.assertEqual(repeated.status_code, 302)
        with site.app.app_context():
            self.assertEqual(db.session.get(Order, first_id).status, 'paid')

    def test_orders_list_filters_by_status(self):
        with site.app.app_context():
            db.session.add_all([
                Order(
                    user_email='paid@example.test',
                    product_name='Оплаченный заказ',
                    amount=100.0,
                    status='paid',
                ),
                Order(
                    user_email='pending@example.test',
                    product_name='Ожидающий заказ',
                    amount=100.0,
                    status='pending',
                ),
            ])
            db.session.commit()

        self.authenticate()
        response = self.client.get('/admin/orders?status=paid')

        self.assertEqual(response.status_code, 200)
        self.assertIn('Оплаченный заказ'.encode(), response.data)
        self.assertNotIn('Ожидающий заказ'.encode(), response.data)


if __name__ == '__main__':
    unittest.main()
