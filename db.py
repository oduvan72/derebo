import sqlite3
import logging
import os
import re
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import event, inspect, text, update
from flask_sqlalchemy import SQLAlchemy


db = SQLAlchemy()

PRODUCT_SUBCATEGORIES = ('Денежные', 'Здоровье', 'Мужское/женское начало', 'Маленькие','vip','Родовые','Самореализация')
YAKUTSK_TIMEZONE = timezone(timedelta(hours=9))
RUSSIAN_MONTHS = {
    'января': 1, 'янв': 1,
    'февраля': 2, 'фев': 2,
    'марта': 3, 'мар': 3,
    'апреля': 4, 'апр': 4,
    'мая': 5, 'май': 5,
    'июня': 6, 'июн': 6,
    'июля': 7, 'июл': 7,
    'августа': 8, 'авг': 8,
    'сентября': 9, 'сен': 9,
    'октября': 10, 'окт': 10,
    'ноября': 11, 'ноя': 11,
    'декабря': 12, 'дек': 12,
}


def current_event_datetime():
    return datetime.now(YAKUTSK_TIMEZONE).replace(tzinfo=None)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_event_datetime(value, now=None):
    value = str(value).strip()
    if not value:
        return None

    try:
        parsed_date = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        parsed_date = None
    if parsed_date is not None:
        if parsed_date.tzinfo is not None:
            parsed_date = parsed_date.astimezone(YAKUTSK_TIMEZONE).replace(tzinfo=None)
        if 'T' not in value and ' ' not in value:
            return datetime.combine(parsed_date.date(), time.max)
        return parsed_date

    for date_format in ('%d.%m.%Y %H:%M', '%d.%m.%Y'):
        try:
            parsed_date = datetime.strptime(value, date_format)
        except ValueError:
            continue
        if date_format == '%d.%m.%Y':
            return datetime.combine(parsed_date.date(), time.max)
        return parsed_date

    match = re.fullmatch(
        r'(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?(?:\s*,?\s+(\d{1,2}:\d{2}))?',
        value.casefold(),
    )
    if match is None:
        return None
    day, month_name, year_text, time_text = match.groups()
    month = RUSSIAN_MONTHS.get(month_name.rstrip('.'))
    if month is None:
        return None
    now = now or current_event_datetime()
    year = int(year_text) if year_text else now.year
    event_time = time.max
    if time_text:
        try:
            event_time = datetime.strptime(time_text, '%H:%M').time()
        except ValueError:
            return None
    try:
        parsed_date = datetime(year, month, int(day), event_time.hour, event_time.minute, event_time.second)
    except ValueError:
        return None
    if not year_text and parsed_date.date() < now.date():
        try:
            parsed_date = parsed_date.replace(year=year + 1)
        except ValueError:
            return None
    return parsed_date


def format_event_datetime(value):
    if value.time() == time.max:
        return value.strftime('%d.%m.%Y')
    return value.strftime('%d.%m.%Y в %H:%M')


def init_db(app):
    app.config.setdefault(
        'SQLALCHEMY_DATABASE_URI',
        os.getenv('DATABASE_URL', 'sqlite:///drevo_zhizni.db'),
    )
    app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)
    db.init_app(app)

    with app.app_context():
        if db.engine.url.get_backend_name() != 'sqlite':
            return

        @event.listens_for(db.engine, 'connect')
        def _set_sqlite_pragmas(connection, _):
            cursor = connection.cursor()
            cursor.execute('PRAGMA journal_mode=WAL')
            cursor.execute('PRAGMA busy_timeout=5000')
            cursor.execute('PRAGMA foreign_keys=ON')
            cursor.close()


def get_db_connection(database_path=None):
    """Open a sqlite3 connection to the site's database or to an explicit path."""
    if database_path is None:
        database_path = Path(__file__).resolve().parent / 'instance' / 'drevo_zhizni.db'
    else:
        database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def release_reserved_seats(event_id, game_id, quantity):
    result = db.session.execute(
        update(event_games)
        .where(
            event_games.c.event_id == event_id,
            event_games.c.game_number == game_id,
            event_games.c.reserved_places >= quantity,
        )
        .values(reserved_places=event_games.c.reserved_places - quantity)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_name = db.Column(db.String(200), nullable=True)
    user_email = db.Column(db.String(120), nullable=False)
    user_phone = db.Column(db.String(20), nullable=True)
    product_name = db.Column(db.String(200), nullable=False)   # Название игры или товара
    amount = db.Column(db.Float, nullable=False)               # Цена без лишних символов
    status = db.Column(db.String(20), default='pending')
    yookassa_id = db.Column(db.String(100), nullable=True)     # ID платежа внутри ЮKassa
    item_type = db.Column(db.String(20), nullable=True)
    item_id = db.Column(db.String(80), nullable=True)
    event_id = db.Column(db.Integer, nullable=True)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    seat_reserved = db.Column(db.Boolean, nullable=False, default=False)
    fulfillment_method = db.Column(
        db.String(20), nullable=False, default='online'
    )
    delivery_address = db.Column(db.Text, nullable=True)
    delivery_fee = db.Column(db.Integer, nullable=False, default=0)
    personal_data_consent_at = db.Column(db.DateTime, nullable=True)
    personal_data_consent_version = db.Column(db.String(20), nullable=True)
    offer_accepted_at = db.Column(db.DateTime, nullable=True)
    return_token = db.Column(db.String(64), nullable=True, unique=True)
    idempotency_key = db.Column(db.String(100), nullable=True, unique=True)
    confirmation_url = db.Column(db.Text, nullable=True)
    payment_expires_at = db.Column(db.String(40), nullable=True)
    payment_return_url = db.Column(db.Text, nullable=True)
    email_notification_status = db.Column(db.String(20), nullable=False, default='pending')
    email_notification_claimed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, nullable=True, default=utcnow, index=True)

event_games = db.Table(
    'event_games',
    db.Column('event_id', db.Integer, db.ForeignKey('event.id'), primary_key=True),
    db.Column('game_number', db.String(2), db.ForeignKey('game.id'), primary_key=True),
    db.Column('reserved_places', db.Integer, nullable=False, server_default='0'),
    db.Column('booked_places', db.Integer, nullable=False, server_default='0'),
)


class Game(db.Model):
    id = db.Column(db.String(2), primary_key=True)
    title = db.Column(db.String(200), nullable=False, unique=True)
    target = db.Column(db.String(200), nullable=False)
    desc = db.Column(db.Text, nullable=False)
    photos = db.Column(db.JSON, nullable=True)
    price = db.Column(db.Integer, nullable=True)
    capacity = db.Column(db.Integer, nullable=True)


class Event(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(80), nullable=False)
    starts_at = db.Column(db.DateTime, nullable=True, index=True)
    games = db.relationship('Game', secondary=event_games, order_by=Game.id)


class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    author = db.Column(db.String(100), nullable=False)
    body = db.Column(db.Text, nullable=False)
    is_published = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    category = db.Column(db.String(30), nullable=False, index=True)
    subcategory = db.Column(db.String(80), nullable=True)
    name = db.Column(db.String(200), nullable=False)
    podzagolovok = db.Column(db.String(255), nullable=True)
    description = db.Column(db.Text, nullable=False)
    price = db.Column(db.Integer, nullable=False)
    photo = db.Column(db.String(255), nullable=False)
    photos = db.Column(db.JSON, nullable=True)
    full_description = db.Column(db.Text, nullable=True)
    materials = db.Column(db.String(255), nullable=True)
    dimensions = db.Column(db.String(120), nullable=True)


def migrate_product_details():
    columns = {
        column['name']
        for column in inspect(db.engine).get_columns('product')
    }
    new_columns = {
        'subcategory': 'VARCHAR(80)',
        'photos': 'JSON',
        'full_description': 'TEXT',
        'materials': 'VARCHAR(255)',
        'dimensions': 'VARCHAR(120)',
        'podzagolovok': 'VARCHAR(255)',
    }
    detail_columns = (
        'full_description',
        'materials',
        'dimensions',
    )

    with db.engine.begin() as connection:
        for name, column_type in new_columns.items():
            if name not in columns:
                connection.execute(text(
                    f'ALTER TABLE product ADD COLUMN {name} {column_type}'
                ))

        if db.engine.dialect.name == 'sqlite':
            connection.execute(text(
                '''
                UPDATE product
                SET photos = NULL
                WHERE photos IS NOT NULL AND NOT json_valid(photos)
                '''
            ))

        if 'product_detail' in inspect(db.engine).get_table_names():
            for name in detail_columns:
                connection.execute(text(
                    f'''
                    UPDATE product
                    SET {name} = (
                        SELECT product_detail.{name}
                        FROM product_detail
                        WHERE product_detail.product_id = product.id
                    )
                    WHERE {name} IS NULL
                      AND id IN (SELECT product_id FROM product_detail)
                    '''
                ))
            connection.execute(text('DROP TABLE product_detail'))


def migrate_game_photos():
    columns = {
        column['name']
        for column in inspect(db.engine).get_columns('game')
    }
    with db.engine.begin() as connection:
        if 'photos' not in columns:
            connection.execute(text('ALTER TABLE game ADD COLUMN photos JSON'))
        if db.engine.dialect.name == 'sqlite':
            connection.execute(text(
                '''
                UPDATE game
                SET photos = NULL
                WHERE photos IS NOT NULL AND NOT json_valid(photos)
                '''
            ))


def migrate_order_booking_fields():
    migrations = {
        'game': {
            'price': 'INTEGER',
            'capacity': 'INTEGER',
        },
        'order': {
            'user_name': 'VARCHAR(200)',
            'item_type': 'VARCHAR(20)',
            'item_id': 'VARCHAR(80)',
            'event_id': 'INTEGER',
            'quantity': 'INTEGER NOT NULL DEFAULT 1',
            'seat_reserved': 'BOOLEAN NOT NULL DEFAULT 0',
            'fulfillment_method': "VARCHAR(20) NOT NULL DEFAULT 'online'",
            'delivery_address': 'TEXT',
            'delivery_fee': 'INTEGER NOT NULL DEFAULT 0',
            'personal_data_consent_at': 'DATETIME',
            'personal_data_consent_version': 'VARCHAR(20)',
            'offer_accepted_at': 'DATETIME',
            'return_token': 'VARCHAR(64)',
            'idempotency_key': 'VARCHAR(100)',
            'confirmation_url': 'TEXT',
            'payment_expires_at': 'VARCHAR(40)',
            'payment_return_url': 'TEXT',
            'email_notification_status': "VARCHAR(20) NOT NULL DEFAULT 'pending'",
            'email_notification_claimed_at': 'DATETIME',
            'created_at': 'DATETIME',
        },
    }

    with db.engine.begin() as connection:
        for table, new_columns in migrations.items():
            existing_columns = {
                column['name']
                for column in inspect(connection).get_columns(table)
            }
            for name, column_type in new_columns.items():
                if name not in existing_columns:
                    connection.execute(text(
                        f'ALTER TABLE "{table}" ADD COLUMN "{name}" {column_type}'
                    ))
        connection.execute(text(
            'CREATE UNIQUE INDEX IF NOT EXISTS "ix_order_idempotency_key" '
            'ON "order" ("idempotency_key")'
        ))
        link_columns = {
            column['name']
            for column in inspect(connection).get_columns('event_games')
        }
        for name in ('reserved_places', 'booked_places'):
            if name not in link_columns:
                connection.execute(text(
                    f'ALTER TABLE "event_games" ADD COLUMN "{name}" '
                    'INTEGER NOT NULL DEFAULT 0'
                ))

        event_columns = {
            column['name']
            for column in inspect(connection).get_columns('event')
        }
        game_columns = {
            column['name']
            for column in inspect(connection).get_columns('game')
        }
        if 'capacity' in event_columns and 'capacity' in game_columns:
            connection.execute(text(
                '''
                UPDATE game
                SET capacity = (
                    SELECT MAX(event.capacity)
                    FROM event_games
                    JOIN event ON event.id = event_games.event_id
                    WHERE event_games.game_number = game.id
                )
                WHERE capacity IS NULL
                  AND EXISTS (
                    SELECT 1 FROM event_games
                    WHERE event_games.game_number = game.id
                  )
                '''
            ))


def migrate_event_dates():
    columns = {
        column['name']
        for column in inspect(db.engine).get_columns('event')
    }
    if 'starts_at' not in columns:
        with db.engine.begin() as connection:
            connection.execute(text(
                'ALTER TABLE "event" ADD COLUMN "starts_at" DATETIME'
            ))
    with db.engine.begin() as connection:
        connection.execute(text(
            'CREATE INDEX IF NOT EXISTS "ix_event_starts_at" ON "event" ("starts_at")'
        ))

    with db.engine.begin() as connection:
        rows = connection.execute(text(
            'SELECT id, date FROM "event" WHERE starts_at IS NULL'
        )).mappings().all()
        for row in rows:
            starts_at = parse_event_datetime(row['date'])
            if starts_at is None:
                logging.warning(
                    'Could not parse legacy event date for event %s: %r',
                    row['id'],
                    row['date'],
                )
                continue
            connection.execute(
                text(
                    'UPDATE "event" SET starts_at = :starts_at, date = :date '
                    'WHERE id = :event_id'
                ),
                {
                    'starts_at': starts_at,
                    'date': format_event_datetime(starts_at),
                    'event_id': row['id'],
                },
            )


def get_games():
    return [
        {
            'number': game.id,
            'title': game.title,
            'target': game.target,
            'desc': game.desc,
            'price': game.price,
            'capacity': game.capacity,
        }
        for game in Game.query.order_by(Game.id).all()
    ]


def get_events():
    rows = db.session.execute(
        db.select(
            Event.id,
            Event.date,
            Game.id,
            Game.title,
            Game.target,
            Game.desc,
            Game.capacity,
            event_games.c.reserved_places,
            event_games.c.booked_places,
        )
        .select_from(event_games)
        .join(Event, Event.id == event_games.c.event_id)
        .join(Game, Game.id == event_games.c.game_number)
        .where(Event.starts_at > current_event_datetime())
        .order_by(Event.starts_at.asc(), Event.id, Game.id)
    ).all()

    return [
        {
            'date': date,
            'event_id': event_id,
            'title': title,
            'target': target,
            'desc': desc,
            'capacity': capacity,
            'available_places': max((capacity or 0) - reserved - booked, 0),
            'link': (
                f'/contact?item_type=game&item_id={quote(game_id)}'
                f'&event_id={event_id}'
            ),
        }
        for event_id, date, game_id, title, target, desc, capacity, reserved, booked
        in rows
    ]


def get_products(category, subcategory=None):
    query = Product.query.filter_by(category=category)
    if subcategory is not None:
        query = query.filter_by(subcategory=subcategory)

    return [
        {
            'id': product.id,
            'category': product.category,
            'subcategory': product.subcategory,
            'name': product.name,
            'podzagolovok': product.podzagolovok,
            'description': product.description,
            'price': product.price,
            'photo': product.photo,
        }
        for product in query.order_by(Product.id).all()
    ]
