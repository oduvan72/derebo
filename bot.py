import asyncio
import json
import logging
import os
import sys
import uuid
from io import BytesIO
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import or_, update

from db import (
    Event,
    Game,
    Order,
    Product,
    Review,
    TelegramAdminState,
    current_event_datetime,
    db,
    event_games,
    format_event_datetime,
    parse_event_datetime,
)


# ========================================================
# TELEGRAM ADMIN
# ========================================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
_admin_id_values = [
    value.strip()
    for value in os.getenv('TELEGRAM_ADMIN_IDS', '').split(',')
    if value.strip()
]
if any(not value.isdecimal() for value in _admin_id_values):
    raise ValueError('TELEGRAM_ADMIN_IDS must be a comma-separated list of numeric IDs.')
TELEGRAM_ADMIN_IDS = {int(value) for value in _admin_id_values}
# To restore this category in the bot, add 'trees' to this set.
PRODUCT_CATEGORIES = {'mandalas', 'dolls'}
MAX_PHOTO_SIZE = 10 * 1024 * 1024

router = Router(name='telegram-admin')
dispatcher = Dispatcher()
dispatcher.include_router(router)
_polling_bot = None
_polling_loop = None


def _as_markup(keyboard):
    if keyboard is None or isinstance(keyboard, InlineKeyboardMarkup):
        return keyboard
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def _invoke_bot_api(bot, method, data):
    if method == 'sendMessage':
        result = await bot.send_message(
            chat_id=data['chat_id'],
            text=data['text'],
            reply_markup=_as_markup(data.get('reply_markup')),
        )
        return result.model_dump()
    if method == 'editMessageText':
        result = await bot.edit_message_text(
            chat_id=data['chat_id'],
            message_id=data['message_id'],
            text=data['text'],
            reply_markup=_as_markup(data.get('reply_markup')),
        )
        return result.model_dump() if isinstance(result, Message) else result
    if method == 'getFile':
        result = await bot.get_file(data['file_id'])
        return result.model_dump()
    raise ValueError(f'Unsupported Telegram API operation: {method}')


async def _run_with_temporary_bot(method, data):
    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        return await _invoke_bot_api(bot, method, data)


def telegram_api(method, data):
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError('TELEGRAM_BOT_TOKEN is not configured.')
    try:
        if _polling_bot is not None and _polling_loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                _invoke_bot_api(_polling_bot, method, data),
                _polling_loop,
            )
            return future.result(timeout=30)
        return asyncio.run(_run_with_temporary_bot(method, data))
    except TelegramAPIError as error:
        logging.error(
            'Telegram Bot API request %s failed (%s).',
            method,
            type(error).__name__,
        )
        raise RuntimeError(f'Telegram API request failed: {method}') from error


def download_telegram_photo(file_id, file_size):
    if file_size is not None and file_size > MAX_PHOTO_SIZE:
        raise ValueError('Размер фото превышает 10 МБ.')

    async def download(bot):
        telegram_file = await bot.get_file(file_id)
        if not telegram_file.file_path:
            raise RuntimeError('Telegram не вернул путь к загруженному фото.')
        photo_data = BytesIO()
        await bot.download_file(
            telegram_file.file_path,
            destination=photo_data,
            timeout=30,
        )
        return photo_data.getvalue()

    if _polling_bot is not None and _polling_loop is not None:
        future = asyncio.run_coroutine_threadsafe(download(_polling_bot), _polling_loop)
        photo_data = future.result(timeout=35)
    else:
        async def download_with_temporary_bot():
            async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                return await download(bot)
        photo_data = asyncio.run(download_with_temporary_bot())

    photo_data = photo_data[:MAX_PHOTO_SIZE + 1]

    if len(photo_data) > MAX_PHOTO_SIZE:
        raise ValueError('Размер фото превышает 10 МБ.')
    if not photo_data.startswith(b'\xff\xd8\xff'):
        raise ValueError('Отправьте изображение как фото, а не как файл.')
    return photo_data


def save_entity_photo(entity_type, entity_id, photo_data):
    if entity_type not in {'games', 'products'}:
        raise ValueError('Неизвестный тип сущности для сохранения фото.')
    filename = f'{uuid.uuid4().hex}.jpg'
    relative_path = Path(entity_type) / str(entity_id) / filename
    photo_path = Path(__file__).resolve().parent / 'static' / relative_path
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    photo_path.write_bytes(photo_data)
    return relative_path.as_posix()


def photo_upload_keyboard(skip_label=None):
    buttons = [inline_button('Готово', 'game_photos_done')]
    if skip_label:
        buttons.append(inline_button(skip_label, 'game_photos_done'))
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def finish_game_photo_upload(chat_id, user_id):
    row = db.session.get(TelegramAdminState, user_id)
    if row is None or row.state not in {
        'game_add_photos',
        'game_edit_photos',
        'product_edit_photos',
    }:
        send_admin_message(chat_id, 'Загрузка фото не активна.')
        return

    payload = json.loads(row.payload)
    if row.state == 'product_edit_photos':
        product = db.session.get(Product, payload['product_id'])
        if product is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Товар больше не существует.')
            return
        product.photos = payload.get('photos', []) or None
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(
            chat_id,
            f'Фото товара «{product.name}» обновлены. Всего фото: {1 + len(product.photos or [])}.',
        )
        return

    photos = payload.get('photos', [])
    if row.state == 'game_add_photos':
        if db.session.get(Game, payload['game_id']) is not None:
            send_admin_message(chat_id, 'Этот ID уже занят. Отмените действие и добавьте игру заново.')
            return
        game = Game(
            id=payload['game_id'],
            title=payload['title'],
            target=payload['target'],
            desc=payload['desc'],
            photos=photos or None,
            price=payload.get('price'),
            capacity=payload.get('capacity'),
        )
        db.session.add(game)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Игра «{game.title}» добавлена с ID {game.id}. Фото: {len(photos)}.')
        return

    game = db.session.get(Game, payload['game_id'])
    if game is None:
        clear_admin_state(user_id)
        send_admin_message(chat_id, 'Игра больше не существует.')
        return

    game.photos = photos or None
    db.session.commit()
    clear_admin_state(user_id)
    send_admin_message(chat_id, f'Фото игры «{game.title}» обновлены. Всего фото: {len(photos)}.')


def discard_pending_photo_upload(user_id):
    row = db.session.get(TelegramAdminState, user_id)
    if row is None:
        return

    payload = json.loads(row.payload)
    static_root = Path(__file__).resolve().parent / 'static'
    for relative_path in payload.get('uploaded_photos', []):
        photo_path = (static_root / relative_path).resolve()
        expected_root = static_root / (
            'products' if row.state == 'product_edit_photos' else 'games'
        )
        if photo_path.parent.parent == expected_root and photo_path.name.endswith('.jpg') and photo_path.exists():
            photo_path.unlink()
            try:
                photo_path.parent.rmdir()
            except OSError:
                pass
    clear_admin_state(user_id)


def handle_admin_photo(chat_id, user_id, photo_sizes):
    row = db.session.get(TelegramAdminState, user_id)
    if row is None or row.state not in {
        'game_add_photos',
        'game_edit_photos',
        'product_edit_photos',
    }:
        send_admin_message(chat_id, 'Чтобы загрузить фото, сначала выберите действие в /admin.')
        return
    if not photo_sizes:
        send_admin_message(chat_id, 'Telegram не передал фото. Попробуйте отправить его ещё раз.')
        return

    payload = json.loads(row.payload)
    photos = payload.get('photos', [])
    is_product = row.state == 'product_edit_photos'
    photo = max(photo_sizes, key=lambda item: item.get('file_size', 0))
    try:
        photo_data = download_telegram_photo(photo['file_id'], photo.get('file_size'))
        entity_type = 'products' if is_product else 'games'
        entity_id = payload['product_id'] if is_product else payload['game_id']
        relative_path = save_entity_photo(entity_type, entity_id, photo_data)
    except (KeyError, RuntimeError, ValueError, OSError):
        logging.exception('Could not process a game photo uploaded by admin %s.', user_id)
        send_admin_message(
            chat_id,
            'Не удалось принять это фото. Попробуйте отправить его как обычное изображение; лимит — 10 МБ.',
            photo_upload_keyboard(),
        )
        return

    photos.append(relative_path)
    payload['photos'] = photos
    payload.setdefault('uploaded_photos', []).append(relative_path)
    save_admin_state(user_id, row.state, payload)
    send_admin_message(
        chat_id,
        f'Фото принято. В списке: {len(photos)} фото. '
        'Отправьте ещё или нажмите «Готово».',
        photo_upload_keyboard(),
    )


def send_admin_message(chat_id, text, keyboard=None):
    telegram_api('sendMessage', {
        'chat_id': chat_id,
        'text': text,
        'reply_markup': _as_markup(keyboard),
    })


def inline_button(text, callback_data):
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def admin_menu(chat_id, message_id=None):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [inline_button('📝 Редактировать описание игры', 'admin:game:edit')],
        [inline_button('💳 Настроить цены игр', 'admin:game:price')],
        [inline_button('🎟 Настроить вместимость игр', 'admin:game:capacity')],
        [inline_button('📷 Добавить фото игры', 'admin:game:photos')],
        [inline_button('➕ Добавить новую игру', 'admin:game:add'),
         inline_button('❌ Удалить игру', 'admin:game:delete')],
        [inline_button('📝 Редактировать продукты', 'admin:product:edit')],
        [inline_button('📷 Добавить фото товара', 'admin:product:photos')],
        [inline_button('➕ Добавить новый продукт', 'admin:product:add'),
         inline_button('❌ Удалить продукт', 'admin:product:delete')],
        [inline_button('📝 Редактировать ивенты, даты', 'admin:event:edit')],
        [inline_button('➕ Добавить новый ивент, дату', 'admin:event:add'),
         inline_button('❌ Удалить ивент, дату', 'admin:event:delete')],
        [inline_button('⭐ Отзывы', 'admin:review:list'),
         inline_button('➕ Добавить отзыв', 'admin:review:add')],
    ])
    if message_id is None:
        send_admin_message(chat_id, 'Админ-панель. Выберите действие:', keyboard)
    else:
        telegram_api('editMessageText', {
            'chat_id': chat_id,
            'message_id': message_id,
            'text': 'Админ-панель. Выберите действие:',
            'reply_markup': keyboard,
        })


def save_admin_state(user_id, state, payload=None):
    row = db.session.get(TelegramAdminState, user_id)
    if row is None:
        row = TelegramAdminState(user_id=user_id, state=state)
        db.session.add(row)
    row.state = state
    row.payload = json.dumps(payload or {}, ensure_ascii=False)
    db.session.commit()


def clear_admin_state(user_id):
    row = db.session.get(TelegramAdminState, user_id)
    if row is not None:
        db.session.delete(row)
        db.session.commit()


def entity_keyboard(items, callback_prefix, label):
    keyboard = [
        [inline_button(
            f'{item.id} — {getattr(item, label)}',
            f'{callback_prefix}:{item.id}',
        )]
        for item in items
    ]
    keyboard.append([inline_button('↩️ В меню', 'admin:menu')])
    return keyboard


def start_admin_action(chat_id, user_id, section, action):
    allowed_actions = {
        'game': {'edit', 'add', 'delete', 'photos', 'price', 'capacity'},
        'product': {'edit', 'add', 'delete', 'photos'},
        'event': {'edit', 'add', 'delete'},
        'review': {'list', 'add'},
    }
    if section not in allowed_actions or action not in allowed_actions[section]:
        send_admin_message(chat_id, 'Неизвестное действие.')
        return

    if section == 'game':
        items = Game.query.order_by(Game.id).all()
        if action == 'add':
            save_admin_state(user_id, 'game_add_title')
            send_admin_message(chat_id, 'Введите название новой игры:')
            return
        if action == 'photos':
            if not items:
                send_admin_message(chat_id, 'В списке пока нет игр.')
                return
            send_admin_message(
                chat_id,
                'Выберите игру, к которой нужно добавить фото:',
                entity_keyboard(items, 'game_photos', 'title'),
            )
            return
        if action == 'price':
            if not items:
                send_admin_message(chat_id, 'В списке пока нет игр.')
                return
            send_admin_message(
                chat_id,
                'Выберите игру, цену которой нужно изменить:',
                entity_keyboard(items, 'game_price', 'title'),
            )
            return
        if action == 'capacity':
            if not items:
                send_admin_message(chat_id, 'В списке пока нет игр.')
                return
            send_admin_message(
                chat_id,
                'Выберите игру, вместимость которой нужно изменить:',
                entity_keyboard(items, 'game_capacity', 'title'),
            )
            return
        if not items:
            send_admin_message(chat_id, 'В списке пока нет игр.')
            return
        labels = 'описание игры' if action == 'edit' else 'игру для удаления'
        send_admin_message(
            chat_id,
            f'Выберите {labels}:',
            entity_keyboard(items, f'game_{action}', 'title'),
        )
        return

    if section == 'product':
        items = Product.query.order_by(Product.id).all()
        if action == 'add':
            save_admin_state(user_id, 'product_add_category')
            send_admin_message(
                chat_id, 'Введите категорию товара: mandalas или dolls.'
            )
            return
        if not items:
            send_admin_message(chat_id, 'В списке пока нет продуктов.')
            return
        if action == 'photos':
            send_admin_message(
                chat_id,
                'Выберите товар, к которому нужно добавить фото:',
                entity_keyboard(items, 'product_photos', 'name'),
            )
            return
        labels = 'продукт для редактирования' if action == 'edit' else 'продукт для удаления'
        send_admin_message(
            chat_id,
            f'Выберите {labels}:',
            entity_keyboard(items, f'product_{action}', 'name'),
        )
        return

    if section == 'review':
        if action == 'add':
            save_admin_state(user_id, 'review_add_author')
            send_admin_message(chat_id, 'Введите имя автора отзыва:')
            return
        reviews = Review.query.order_by(Review.id.desc()).all()
        if not reviews:
            send_admin_message(chat_id, 'Отзывов пока нет. Добавьте первый отзыв.')
            return
        send_admin_message(
            chat_id,
            'Выберите отзыв для редактирования:',
            entity_keyboard(reviews, 'review_select', 'author'),
        )
        return

    items = Event.query.order_by(Event.id).all()
    if action == 'add':
        save_admin_state(user_id, 'event_add_date')
        send_admin_message(chat_id, 'Введите дату нового ивента:')
        return
    if not items:
        send_admin_message(chat_id, 'В списке пока нет ивентов.')
        return
    send_admin_message(
        chat_id,
        'Выберите ивент:' if action == 'edit' else 'Выберите ивент для удаления:',
        entity_keyboard(items, f'event_{action}', 'date'),
    )


def confirm_delete_keyboard(kind, entity_id):
    return [[
        inline_button('Да, удалить', f'delete:{kind}:{entity_id}'),
        inline_button('Отмена', 'admin:menu'),
    ]]


def _game_has_bookings(game_id):
    return db.session.query(event_games.c.event_id).filter(
        event_games.c.game_number == game_id,
        or_(
            event_games.c.reserved_places > 0,
            event_games.c.booked_places > 0,
        ),
    ).first() is not None


def _event_has_bookings(event_id):
    return db.session.query(event_games.c.event_id).filter(
        event_games.c.event_id == event_id,
        or_(
            event_games.c.reserved_places > 0,
            event_games.c.booked_places > 0,
        ),
    ).first() is not None


def _get_game_or_notify(chat_id, game_id):
    game = db.session.get(Game, game_id)
    if game is None:
        send_admin_message(chat_id, 'Игра не найдена.')
    return game


def _get_product_or_notify(chat_id, product_id):
    try:
        product = db.session.get(Product, int(product_id))
    except (TypeError, ValueError):
        product = None
    if product is None:
        send_admin_message(chat_id, 'Продукт не найден.')
    return product


def _get_event_or_notify(chat_id, event_id):
    try:
        event = db.session.get(Event, int(event_id))
    except (TypeError, ValueError):
        event = None
    if event is None:
        send_admin_message(chat_id, 'Ивент не найден.')
    return event


def _get_review_or_notify(chat_id, review_id):
    try:
        review = db.session.get(Review, int(review_id))
    except (TypeError, ValueError):
        review = None
    if review is None:
        send_admin_message(chat_id, 'Отзыв не найден.')
    return review


def _parse_positive_int(chat_id, text, field_label):
    try:
        value = int(text)
    except ValueError:
        send_admin_message(chat_id, f'{field_label} должна быть целым числом.')
        return None
    if value <= 0:
        send_admin_message(chat_id, f'{field_label} должна быть больше нуля.')
        return None
    return value


def _parse_future_event_date(chat_id, text):
    starts_at = parse_event_datetime(text)
    if starts_at is None:
        send_admin_message(
            chat_id,
            'Введите дату в формате ГГГГ-ММ-ДД ЧЧ:ММ '
            '(например, 2026-10-20 18:00) или ГГГГ-ММ-ДД.',
        )
        return None
    if starts_at <= current_event_datetime():
        send_admin_message(chat_id, 'Укажите дату и время в будущем.')
        return None
    return starts_at


def handle_admin_callback(chat_id, user_id, message_id, data):
    parts = data.split(':')
    if parts == ['admin', 'menu']:
        discard_pending_photo_upload(user_id)
        admin_menu(chat_id, message_id)
        return

    if len(parts) == 2 and parts[0] in {'pickup_paid', 'pickup_cancel'}:
        try:
            order_id = int(parts[1])
        except ValueError:
            send_admin_message(chat_id, 'Некорректный номер заказа.')
            return
        order = db.session.get(Order, order_id)
        if (
            order is None
            or order.fulfillment_method != 'pickup'
            or order.status != 'pending'
        ):
            send_admin_message(
                chat_id,
                'Заказ не найден или уже обработан.',
            )
            return
        status = 'paid' if parts[0] == 'pickup_paid' else 'canceled'
        updated = db.session.execute(
            update(Order)
            .where(
                Order.id == order_id,
                Order.fulfillment_method == 'pickup',
                Order.status == 'pending',
            )
            .values(status=status)
            .execution_options(synchronize_session=False)
        )
        db.session.commit()
        if updated.rowcount != 1:
            send_admin_message(chat_id, 'Заказ уже обработан.')
            return
        notice = (
            'Самовывоз отмечен как оплаченный.'
            if status == 'paid'
            else 'Заказ самовывоза отменён.'
        )
        send_admin_message(chat_id, f'{notice} Заказ №{order_id}.')
        return

    if len(parts) == 3 and parts[0] == 'admin':
        start_admin_action(chat_id, user_id, parts[1], parts[2])
        return

    if len(parts) == 2 and parts[0] in {'game_edit', 'game_delete'}:
        game = _get_game_or_notify(chat_id, parts[1])
        if game is None:
            return
        if parts[0] == 'game_edit':
            save_admin_state(user_id, 'game_edit_desc', {'game_id': game.id})
            send_admin_message(chat_id, f'Введите новое описание для «{game.title}»:')
        else:
            if _game_has_bookings(game.id):
                send_admin_message(
                    chat_id,
                    'Нельзя удалить игру с зарезервированными или оплаченными местами.',
                )
                return
            send_admin_message(
                chat_id,
                f'Удалить игру «{game.title}»?',
                confirm_delete_keyboard('game', game.id),
            )
        return

    if len(parts) == 2 and parts[0] == 'game_capacity':
        game = _get_game_or_notify(chat_id, parts[1])
        if game is None:
            return
        save_admin_state(user_id, 'game_edit_capacity', {'game_id': game.id})
        current_capacity = 'не задана' if game.capacity is None else f'{game.capacity} мест'
        send_admin_message(
            chat_id,
            f'Текущая вместимость игры «{game.title}»: {current_capacity}. '
            'Введите новую вместимость целым числом:',
        )
        return

    if len(parts) == 2 and parts[0] == 'game_price':
        game = _get_game_or_notify(chat_id, parts[1])
        if game is None:
            return
        save_admin_state(user_id, 'game_edit_price', {'game_id': game.id})
        current_price = 'не задана' if game.price is None else f'{game.price} ₽'
        send_admin_message(
            chat_id,
            f'Текущая цена игры «{game.title}»: {current_price}. Введите новую цену целым числом:',
        )
        return

    if parts == ['game_photos_done']:
        finish_game_photo_upload(chat_id, user_id)
        return

    if len(parts) == 2 and parts[0] == 'game_photos':
        game = _get_game_or_notify(chat_id, parts[1])
        if game is None:
            return
        photos = game.photos if isinstance(game.photos, list) else []
        save_admin_state(
            user_id,
            'game_edit_photos',
            {'game_id': game.id, 'photos': photos, 'uploaded_photos': []},
        )
        send_admin_message(
            chat_id,
            f'Отправьте фото игры «{game.title}» как изображение. '
            'Когда закончите, нажмите «Готово».',
            photo_upload_keyboard(),
        )
        return

    if len(parts) == 2 and parts[0] == 'product_photos':
        product = _get_product_or_notify(chat_id, parts[1])
        if product is None:
            return
        photos = product.photos if isinstance(product.photos, list) else []
        save_admin_state(
            user_id,
            'product_edit_photos',
            {
                'product_id': product.id,
                'photos': photos,
                'uploaded_photos': [],
            },
        )
        send_admin_message(
            chat_id,
            f'Отправьте дополнительные фото товара «{product.name}» '
            'как изображения, затем нажмите «Готово».',
            photo_upload_keyboard(),
        )
        return

    if len(parts) == 2 and parts[0] in {'product_edit', 'product_delete'}:
        product = _get_product_or_notify(chat_id, parts[1])
        if product is None:
            return
        if parts[0] == 'product_delete':
            send_admin_message(
                chat_id,
                f'Удалить продукт «{product.name}»?',
                confirm_delete_keyboard('product', product.id),
            )
        else:
            keyboard = [
                [inline_button('Название', f'product_field:{product.id}:name'),
                 inline_button('Описание', f'product_field:{product.id}:description')],
                [inline_button('Подзаголовок', f'product_field:{product.id}:podzagolovok')],
                [inline_button('Цена', f'product_field:{product.id}:price'),
                 inline_button('Категория', f'product_field:{product.id}:category')],
                [inline_button('Фото', f'product_field:{product.id}:photo')],
                [inline_button('↩️ В меню', 'admin:menu')],
            ]
            send_admin_message(chat_id, f'Что изменить у «{product.name}»?', keyboard)
        return

    if len(parts) == 2 and parts[0] in {'event_edit', 'event_delete'}:
        event = _get_event_or_notify(chat_id, parts[1])
        if event is None:
            return
        if parts[0] == 'event_delete':
            if _event_has_bookings(event.id):
                send_admin_message(
                    chat_id,
                    'Нельзя удалить ивент с зарезервированными или оплаченными местами.',
                )
                return
            send_admin_message(
                chat_id,
                f'Удалить ивент «{event.date}»?',
                confirm_delete_keyboard('event', event.id),
            )
        else:
            save_admin_state(user_id, 'event_edit_date', {'event_id': event.id})
            send_admin_message(chat_id, f'Введите новую дату для ивента «{event.date}»:')
        return

    if len(parts) == 2 and parts[0] == 'review_select':
        review = _get_review_or_notify(chat_id, parts[1])
        if review is None:
            return
        keyboard = [
            [inline_button('Изменить автора', f'review_field:{review.id}:author'),
             inline_button('Изменить текст', f'review_field:{review.id}:body')],
            [inline_button(
                'Снять с публикации' if review.is_published else 'Опубликовать',
                f'review_publish:{review.id}',
            )],
            [inline_button('Удалить', f'delete:review:{review.id}'),
             inline_button('↩️ К отзывам', 'admin:review:list')],
        ]
        visibility = 'опубликован' if review.is_published else 'скрыт'
        send_admin_message(
            chat_id,
            f'Отзыв #{review.id} ({visibility}) от «{review.author}»:\n{review.body}',
            keyboard,
        )
        return

    if len(parts) == 2 and parts[0] == 'review_publish':
        review = _get_review_or_notify(chat_id, parts[1])
        if review is None:
            return
        review.is_published = not review.is_published
        db.session.commit()
        send_admin_message(
            chat_id,
            'Отзыв опубликован.' if review.is_published else 'Отзыв снят с публикации.',
        )
        return

    if len(parts) == 3 and parts[0] == 'review_field':
        review = _get_review_or_notify(chat_id, parts[1])
        if review is None:
            return
        field = parts[2]
        if field not in {'author', 'body'}:
            send_admin_message(chat_id, 'Отзыв или поле не найдены.')
            return
        save_admin_state(
            user_id,
            'review_edit_value',
            {'review_id': review.id, 'field': field},
        )
        send_admin_message(
            chat_id,
            'Введите новое имя автора:' if field == 'author' else 'Введите новый текст отзыва:',
        )
        return

    if len(parts) == 3 and parts[0] == 'product_field':
        product = _get_product_or_notify(chat_id, parts[1])
        if product is None:
            return
        field = parts[2]
        if field not in {'name', 'podzagolovok', 'description', 'price', 'category', 'photo'}:
            send_admin_message(chat_id, 'Продукт или поле не найдены.')
            return
        save_admin_state(
            user_id, 'product_edit_value',
            {'product_id': product.id, 'field': field},
        )
        prompts = {
            'name': 'Введите новое название:',
            'podzagolovok': 'Введите новый подзаголовок (или отправьте —, если он не нужен):',
            'description': 'Введите новое описание:',
            'price': 'Введите новую цену целым числом:',
            'category': 'Введите категорию: mandalas или dolls.',
            'photo': 'Введите имя файла фотографии:',
        }
        send_admin_message(chat_id, prompts[field])
        return

    if len(parts) == 2 and parts[0] == 'event_add_game':
        state_row = db.session.get(TelegramAdminState, user_id)
        game = db.session.get(Game, parts[1])
        if state_row is None or state_row.state != 'event_add_games' or game is None:
            send_admin_message(chat_id, 'Не удалось выбрать игру для ивента.')
            return
        payload = json.loads(state_row.payload)
        game_ids = payload.setdefault('game_ids', [])
        if game.id not in game_ids:
            game_ids.append(game.id)
            state_row.payload = json.dumps(payload, ensure_ascii=False)
            db.session.commit()
        send_admin_message(
            chat_id,
            f'Выбрано игр: {len(game_ids)}. Можно выбрать ещё или завершить.',
            event_add_games_keyboard(game_ids),
        )
        return

    if parts == ['event_add_done']:
        state_row = db.session.get(TelegramAdminState, user_id)
        if state_row is None or state_row.state != 'event_add_games':
            send_admin_message(chat_id, 'Сначала укажите дату нового ивента.')
            return
        payload = json.loads(state_row.payload)
        game_ids = payload.get('game_ids', [])
        games_by_id = {
            game.id: game
            for game in Game.query.filter(Game.id.in_(game_ids)).all()
        } if game_ids else {}
        games = [
            games_by_id[game_id]
            for game_id in game_ids
            if game_id in games_by_id
        ]
        if not games:
            send_admin_message(chat_id, 'Выберите хотя бы одну игру для ивента.')
            return
        event = Event(
            date=payload['date'],
            starts_at=parse_event_datetime(payload['starts_at']),
            games=games,
        )
        db.session.add(event)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Ивент «{event.date}» добавлен.')
        return

    if len(parts) == 3 and parts[0] == 'delete':
        kind, entity_id = parts[1], parts[2]
        if kind == 'game':
            entity = db.session.get(Game, entity_id)
            if entity is not None:
                if _game_has_bookings(entity.id):
                    send_admin_message(
                        chat_id,
                        'Нельзя удалить игру с зарезервированными или оплаченными местами.',
                    )
                    return
                db.session.execute(
                    event_games.delete().where(event_games.c.game_number == entity.id)
                )
                db.session.delete(entity)
        elif kind == 'product':
            entity = db.session.get(Product, int(entity_id))
            if entity is not None:
                db.session.delete(entity)
        elif kind == 'event':
            entity = db.session.get(Event, int(entity_id))
            if entity is not None:
                if _event_has_bookings(entity.id):
                    send_admin_message(
                        chat_id,
                        'Нельзя удалить ивент с зарезервированными или оплаченными местами.',
                    )
                    return
                db.session.delete(entity)
        elif kind == 'review':
            try:
                entity = db.session.get(Review, int(entity_id))
            except ValueError:
                entity = None
        else:
            send_admin_message(chat_id, 'Неизвестное действие.')
            return
        if entity is None:
            send_admin_message(chat_id, 'Запись уже отсутствует.')
        else:
            db.session.commit()
            send_admin_message(chat_id, 'Запись удалена.')
        return

    send_admin_message(chat_id, 'Действие не распознано. Используйте /admin.')


def event_add_games_keyboard(selected_game_ids):
    keyboard = []
    for game in Game.query.order_by(Game.id).all():
        mark = '✓ ' if game.id in selected_game_ids else ''
        keyboard.append([inline_button(
            f'{mark}{game.id} — {game.title}',
            f'event_add_game:{game.id}',
        )])
    keyboard.append([inline_button('Готово', 'event_add_done')])
    return keyboard


def handle_admin_text(chat_id, user_id, text):
    row = db.session.get(TelegramAdminState, user_id)
    if row is None:
        send_admin_message(chat_id, 'Для управления введите /admin.')
        return

    payload = json.loads(row.payload)
    state = row.state

    if state == 'review_add_author':
        author = text.strip()
        if not author or len(author) > 100:
            send_admin_message(chat_id, 'Имя автора должно содержать от 1 до 100 символов.')
            return
        payload['author'] = author
        save_admin_state(user_id, 'review_add_body', payload)
        send_admin_message(chat_id, 'Введите текст отзыва (до 4000 символов):')
    elif state == 'review_add_body':
        body = text.strip()
        if not body or len(body) > 4000:
            send_admin_message(chat_id, 'Текст отзыва должен содержать от 1 до 4000 символов.')
            return
        review = Review(author=payload['author'], body=body, is_published=False)
        db.session.add(review)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(
            chat_id,
            f'Отзыв #{review.id} сохранён и скрыт. Опубликуйте его через раздел «Отзывы».',
        )
    elif state == 'review_edit_value':
        review = db.session.get(Review, payload['review_id'])
        if review is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Отзыв больше не существует.')
            return
        field = payload['field']
        value = text.strip()
        max_length = 100 if field == 'author' else 4000
        if not value or len(value) > max_length:
            send_admin_message(
                chat_id,
                f'Значение должно содержать от 1 до {max_length} символов.',
            )
            return
        setattr(review, field, value)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Отзыв #{review.id} обновлён.')
    elif state == 'game_add_title':
        if len(text) > 200:
            send_admin_message(chat_id, 'Название не должно превышать 200 символов.')
            return
        if Game.query.filter_by(title=text).first():
            send_admin_message(chat_id, 'Игра с таким названием уже существует.')
            return
        payload['title'] = text
        save_admin_state(user_id, 'game_add_target', payload)
        send_admin_message(chat_id, 'Введите целевую аудиторию игры:')
    elif state == 'game_add_target':
        if len(text) > 200:
            send_admin_message(chat_id, 'Текст не должен превышать 200 символов.')
            return
        payload['target'] = text
        save_admin_state(user_id, 'game_add_price', payload)
        send_admin_message(chat_id, 'Введите цену игры в рублях целым числом:')
    elif state == 'game_add_price':
        price = _parse_positive_int(chat_id, text, 'Цена')
        if price is None:
            return
        payload['price'] = price
        save_admin_state(user_id, 'game_add_capacity', payload)
        send_admin_message(chat_id, 'Введите вместимость игры (количество мест):')
    elif state == 'game_add_capacity':
        capacity = _parse_positive_int(chat_id, text, 'Вместимость')
        if capacity is None:
            return
        payload['capacity'] = capacity
        save_admin_state(user_id, 'game_add_desc', payload)
        send_admin_message(chat_id, 'Введите описание игры:')
    elif state == 'game_add_desc':
        existing_ids = {game_id for (game_id,) in db.session.query(Game.id).all()}
        game_id = next(
            (f'{number:02d}' for number in range(1, 100) if f'{number:02d}' not in existing_ids),
            None,
        )
        if game_id is None:
            send_admin_message(chat_id, 'Нет свободного двухзначного ID для новой игры.')
            return
        payload.update({'game_id': game_id, 'desc': text, 'photos': [], 'uploaded_photos': []})
        save_admin_state(user_id, 'game_add_photos', payload)
        send_admin_message(
            chat_id,
            f'Игра будет создана с ID {game_id}. Отправьте любое количество фото '
            'как изображения или нажмите «Пропустить фото».',
            photo_upload_keyboard('Создать без фото'),
        )
    elif state == 'game_edit_desc':
        game = db.session.get(Game, payload['game_id'])
        if game is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Игра больше не существует.')
            return
        game.desc = text
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Описание игры «{game.title}» обновлено.')
    elif state == 'game_edit_price':
        game = db.session.get(Game, payload['game_id'])
        if game is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Игра больше не существует.')
            return
        price = _parse_positive_int(chat_id, text, 'Цена')
        if price is None:
            return
        game.price = price
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Цена игры «{game.title}» обновлена: {price} ₽.')
    elif state == 'game_edit_capacity':
        game = db.session.get(Game, payload['game_id'])
        if game is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Игра больше не существует.')
            return
        capacity = _parse_positive_int(chat_id, text, 'Вместимость')
        if capacity is None:
            return
        bookings = db.session.query(
            event_games.c.reserved_places,
            event_games.c.booked_places,
        ).filter(event_games.c.game_number == game.id).all()
        max_occupied = max(
            (reserved + booked for reserved, booked in bookings),
            default=0,
        )
        if capacity < max_occupied:
            send_admin_message(
                chat_id,
                'Вместимость не может быть меньше числа занятых и зарезервированных '
                'мест в одном из событий.',
            )
            return
        game.capacity = capacity
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(
            chat_id,
            f'Вместимость игры «{game.title}» обновлена: {capacity} мест на каждое событие.',
        )
    elif state == 'product_add_category':
        category = text.casefold()
        if category not in PRODUCT_CATEGORIES:
            send_admin_message(chat_id, 'Допустимые категории: mandalas, dolls.')
            return
        payload['category'] = category
        save_admin_state(user_id, 'product_add_name', payload)
        send_admin_message(chat_id, 'Введите название продукта:')
    elif state == 'product_add_name':
        if len(text) > 200:
            send_admin_message(chat_id, 'Название не должно превышать 200 символов.')
            return
        payload['name'] = text
        save_admin_state(user_id, 'product_add_podzagolovok', payload)
        send_admin_message(
            chat_id,
            'Введите подзаголовок товара (или отправьте —, если он не нужен):',
        )
    elif state == 'product_add_podzagolovok':
        if len(text) > 255:
            send_admin_message(chat_id, 'Подзаголовок не должен превышать 255 символов.')
            return
        payload['podzagolovok'] = None if text.strip() == '—' else text
        save_admin_state(user_id, 'product_add_description', payload)
        send_admin_message(chat_id, 'Введите описание продукта:')
    elif state == 'product_add_description':
        payload['description'] = text
        save_admin_state(user_id, 'product_add_price', payload)
        send_admin_message(chat_id, 'Введите цену целым числом:')
    elif state == 'product_add_price':
        try:
            price = int(text)
        except ValueError:
            send_admin_message(chat_id, 'Цена должна быть целым числом.')
            return
        if price < 0:
            send_admin_message(chat_id, 'Цена не может быть отрицательной.')
            return
        payload['price'] = price
        save_admin_state(user_id, 'product_add_photo', payload)
        send_admin_message(chat_id, 'Введите имя файла фотографии (например, derevo.jpg):')
    elif state == 'product_add_photo':
        if len(text) > 255:
            send_admin_message(chat_id, 'Имя фотографии не должно превышать 255 символов.')
            return
        product = Product(**payload, photo=text)
        db.session.add(product)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Продукт «{product.name}» добавлен.')
    elif state == 'product_edit_value':
        product = db.session.get(Product, payload['product_id'])
        field = payload['field']
        if product is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Продукт больше не существует.')
            return
        if field == 'price':
            try:
                value = int(text)
            except ValueError:
                send_admin_message(chat_id, 'Цена должна быть целым числом.')
                return
            if value < 0:
                send_admin_message(chat_id, 'Цена не может быть отрицательной.')
                return
        elif field == 'category':
            value = text.casefold()
            if value not in PRODUCT_CATEGORIES:
                send_admin_message(chat_id, 'Допустимые категории: mandalas, dolls.')
                return
        elif field == 'podzagolovok':
            if len(text) > 255:
                send_admin_message(chat_id, 'Подзаголовок не должен превышать 255 символов.')
                return
            value = None if text.strip() == '—' else text
        else:
            max_length = {'name': 200, 'photo': 255}.get(field)
            if max_length is not None and len(text) > max_length:
                send_admin_message(chat_id, f'Значение не должно превышать {max_length} символов.')
                return
            value = text
        setattr(product, field, value)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Поле продукта «{product.name}» обновлено.')
    elif state == 'event_add_date':
        starts_at = _parse_future_event_date(chat_id, text)
        if starts_at is None:
            return
        save_admin_state(
            user_id,
            'event_add_games',
            {
                'date': format_event_datetime(starts_at),
                'starts_at': starts_at.isoformat(sep=' '),
                'game_ids': [],
            },
        )
        games = Game.query.order_by(Game.id).all()
        if not games:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Нельзя создать ивент: сначала добавьте игру.')
            return
        send_admin_message(
            chat_id, 'Выберите одну или несколько игр для ивента:',
            event_add_games_keyboard([]),
        )
    elif state == 'event_edit_date':
        starts_at = _parse_future_event_date(chat_id, text)
        if starts_at is None:
            return
        event = db.session.get(Event, payload['event_id'])
        if event is None:
            clear_admin_state(user_id)
            send_admin_message(chat_id, 'Ивент больше не существует.')
            return
        event.starts_at = starts_at
        event.date = format_event_datetime(starts_at)
        db.session.commit()
        clear_admin_state(user_id)
        send_admin_message(chat_id, f'Дата ивента изменена на «{event.date}».')
    elif state in {'game_add_photos', 'game_edit_photos', 'product_edit_photos'}:
        send_admin_message(
            chat_id,
            'Отправьте фото как изображение или нажмите «Готово» для сохранения.',
            photo_upload_keyboard(),
        )


def _run_admin_operation(operation, *args):
    from app import app

    with app.app_context():
        return operation(*args)


def _is_private_admin(update):
    user = update.from_user
    message = update if isinstance(update, Message) else update.message
    return (
        user is not None
        and user.id in TELEGRAM_ADMIN_IDS
        and message is not None
        and message.chat.type == 'private'
    )


@router.message(Command('admin'))
async def admin_command(message: Message):
    if not _is_private_admin(message):
        return
    user_id = message.from_user.id
    await asyncio.to_thread(_run_admin_operation, discard_pending_photo_upload, user_id)
    await asyncio.to_thread(_run_admin_operation, admin_menu, message.chat.id)


@router.message(Command('cancel'))
async def cancel_command(message: Message):
    if not _is_private_admin(message):
        return
    await asyncio.to_thread(
        _run_admin_operation,
        discard_pending_photo_upload,
        message.from_user.id,
    )
    await message.answer('Действие отменено.')


@router.message(F.photo)
async def admin_photo(message: Message):
    if not _is_private_admin(message):
        return
    photo_sizes = [
        {
            'file_id': photo.file_id,
            'file_size': photo.file_size,
        }
        for photo in message.photo
    ]
    await asyncio.to_thread(
        _run_admin_operation,
        handle_admin_photo,
        message.chat.id,
        message.from_user.id,
        photo_sizes,
    )


@router.message(F.text)
async def admin_text(message: Message):
    if not _is_private_admin(message):
        return
    await asyncio.to_thread(
        _run_admin_operation,
        handle_admin_text,
        message.chat.id,
        message.from_user.id,
        message.text.strip(),
    )


@router.callback_query()
async def admin_callback(callback: CallbackQuery):
    if not _is_private_admin(callback):
        await callback.answer()
        return
    await asyncio.to_thread(
        _run_admin_operation,
        handle_admin_callback,
        callback.message.chat.id,
        callback.from_user.id,
        callback.message.message_id,
        callback.data or '',
    )
    await callback.answer()


@router.errors()
async def handle_router_error(event: ErrorEvent):
    logging.exception(
        'Telegram admin update failed.',
        exc_info=(
            type(event.exception),
            event.exception,
            event.exception.__traceback__,
        ),
    )
    update = event.update
    message = update.message
    callback = update.callback_query
    if message is not None and _is_private_admin(message):
        await message.answer('Не удалось выполнить действие. Ошибка записана в журнал.')
    elif callback is not None and _is_private_admin(callback):
        await callback.answer('Не удалось выполнить действие.', show_alert=True)
    return True


async def main():
    global _polling_bot, _polling_loop
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError('Set TELEGRAM_BOT_TOKEN in the environment.')
    if not TELEGRAM_ADMIN_IDS:
        raise RuntimeError('Set TELEGRAM_ADMIN_IDS in the environment.')

    # Импорт app применяет create_all() и миграции БД на уровне модуля.
    from app import app  # noqa: F401

    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    _polling_bot = bot
    _polling_loop = asyncio.get_running_loop()
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await dispatcher.start_polling(
            bot,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        _polling_bot = None
        _polling_loop = None
        await bot.session.close()


if __name__ == '__main__':
    sys.modules['bot'] = sys.modules['__main__']
    logging.basicConfig(
        level=os.getenv('LOG_LEVEL', 'INFO').upper(),
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
    )
    asyncio.run(main())
