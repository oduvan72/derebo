import functools
import hmac
import os
import uuid
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, or_, select
from werkzeug.utils import secure_filename

from db import (
    Event,
    Game,
    Order,
    Product,
    Review,
    PRODUCT_SUBCATEGORIES,
    current_event_datetime,
    db,
    event_games,
    format_event_datetime,
    parse_event_datetime,
    release_reserved_seats,
)

admin_bp = Blueprint('admin', __name__, template_folder='templates')

ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', 'admin')
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')
PRODUCT_CATEGORIES = ('mandalas', 'dolls')
PER_PAGE = 20
MAX_PHOTO_SIZE = 10 * 1024 * 1024
ALLOWED_PHOTO_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
STATIC_ROOT = Path(__file__).resolve().parent / 'static'

ORDER_STATUS_LABELS = {
    'pending': 'Ожидает оплаты',
    'paid': 'Оплачен',
    'canceled': 'Отменён',
    'failed': 'Ошибка оплаты',
}


@admin_bp.context_processor
def inject_admin_helpers():
    return {
        'order_status_labels': ORDER_STATUS_LABELS,
        'product_categories': PRODUCT_CATEGORIES,
        'product_subcategories': PRODUCT_SUBCATEGORIES,
    }


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('admin_authenticated'):
            return redirect(url_for('admin.login', next=request.path))
        return view(*args, **kwargs)
    return wrapped


def paginate(query):
    page = request.args.get('page', 1, type=int)
    return db.paginate(query, page=page, per_page=PER_PAGE, error_out=False)


def form_value(name, max_length):
    value = request.form.get(name, '').strip()
    if not value or len(value) > max_length:
        return None
    return value


def form_optional_value(name, max_length):
    value = request.form.get(name, '').strip()
    if len(value) > max_length:
        return None
    return value or None


def form_positive_int(name, label):
    raw = request.form.get(name, '').strip()
    if not raw:
        return None, None
    try:
        value = int(raw)
    except ValueError:
        return None, f'{label} должна быть целым числом.'
    if value <= 0:
        return None, f'{label} должна быть больше нуля.'
    return value, None


def _photo_signature_valid(data):
    if data.startswith(b'\xff\xd8\xff'):
        return True
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return True
    return data[:4] == b'RIFF' and data[8:12] == b'WEBP'


def save_entity_photo(entity_type, entity_id, file_storage):
    original_name = secure_filename(file_storage.filename or '')
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_PHOTO_EXTENSIONS:
        raise ValueError('Допустимы только изображения JPG, PNG или WebP.')
    data = file_storage.read(MAX_PHOTO_SIZE + 1)
    if not data:
        raise ValueError('Файл фотографии пустой.')
    if len(data) > MAX_PHOTO_SIZE:
        raise ValueError('Размер фото превышает 10 МБ.')
    if not _photo_signature_valid(data):
        raise ValueError('Файл не является изображением JPG, PNG или WebP.')
    filename = f'{uuid.uuid4().hex}{extension}'
    relative_path = Path(entity_type) / str(entity_id) / filename
    photo_path = STATIC_ROOT / relative_path
    photo_path.parent.mkdir(parents=True, exist_ok=True)
    photo_path.write_bytes(data)
    return relative_path.as_posix()


def delete_entity_photo(relative_path):
    photo_path = (STATIC_ROOT / relative_path).resolve()
    if (
        photo_path.is_relative_to(STATIC_ROOT)
        and photo_path.parent.parent in {STATIC_ROOT / 'games', STATIC_ROOT / 'products'}
        and photo_path.suffix.lower() in ALLOWED_PHOTO_EXTENSIONS
        and photo_path.exists()
    ):
        photo_path.unlink()
        try:
            photo_path.parent.rmdir()
        except OSError:
            pass


def apply_uploaded_photos(entity, entity_type, entity_id):
    photos = entity.photos if isinstance(entity.photos, list) else []
    for file_storage in request.files.getlist('photos'):
        if not file_storage or not file_storage.filename:
            continue
        photos.append(save_entity_photo(entity_type, entity_id, file_storage))
    entity.photos = photos or None


def remove_marked_photos(entity):
    photos = entity.photos if isinstance(entity.photos, list) else []
    marked = set(request.form.getlist('remove_photos'))
    kept = [photo for photo in photos if photo not in marked]
    for photo in photos:
        if photo in marked:
            delete_entity_photo(photo)
    entity.photos = kept or None


def game_has_bookings(game_id):
    return db.session.query(event_games.c.event_id).filter(
        event_games.c.game_number == game_id,
        or_(
            event_games.c.reserved_places > 0,
            event_games.c.booked_places > 0,
        ),
    ).first() is not None


def event_has_bookings(event_id):
    return db.session.query(event_games.c.event_id).filter(
        event_games.c.event_id == event_id,
        or_(
            event_games.c.reserved_places > 0,
            event_games.c.booked_places > 0,
        ),
    ).first() is not None


def parse_future_event_date(raw_value):
    starts_at = parse_event_datetime(raw_value)
    if starts_at is None:
        return None, 'Введите корректные дату и время.'
    if starts_at <= current_event_datetime():
        return None, 'Укажите дату и время в будущем.'
    return starts_at, None


# ========================================================
# ВХОД
# ========================================================
@admin_bp.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('admin_authenticated'):
        return redirect(url_for('admin.dashboard'))
    if not ADMIN_PASSWORD:
        return render_template('admin/login.html', admin_disabled=True), 503
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        credentials_valid = (
            hmac.compare_digest(username, ADMIN_USERNAME)
            and hmac.compare_digest(password, ADMIN_PASSWORD)
        )
        if credentials_valid:
            session.clear()
            session['admin_authenticated'] = True
            session.permanent = True
            next_url = request.args.get('next', '')
            if not next_url.startswith('/admin'):
                next_url = url_for('admin.dashboard')
            return redirect(next_url)
        flash('Неверный логин или пароль.', 'error')
    return render_template('admin/login.html', admin_disabled=False)


@admin_bp.route('/logout', methods=['POST'])
@admin_required
def logout():
    session.clear()
    return redirect(url_for('admin.login'))


# ========================================================
# ДАШБОРД
# ========================================================
@admin_bp.route('/')
@admin_required
def dashboard():
    status_counts = dict(
        db.session.execute(
            select(Order.status, func.count()).group_by(Order.status)
        ).all()
    )
    revenue_total = db.session.execute(
        select(func.coalesce(func.sum(Order.amount), 0)).where(Order.status == 'paid')
    ).scalar_one()
    recent_orders = Order.query.order_by(Order.id.desc()).limit(10).all()
    upcoming_events = (
        Event.query.filter(Event.starts_at > current_event_datetime())
        .order_by(Event.starts_at.asc())
        .limit(10)
        .all()
    )
    event_rows = []
    for event in upcoming_events:
        links = db.session.execute(
            select(
                Game.title,
                Game.capacity,
                event_games.c.reserved_places,
                event_games.c.booked_places,
            )
            .select_from(event_games)
            .join(Game, Game.id == event_games.c.game_number)
            .where(event_games.c.event_id == event.id)
            .order_by(Game.id)
        ).all()
        event_rows.append({'event': event, 'links': links})
    return render_template(
        'admin/dashboard.html',
        status_counts=status_counts,
        revenue_total=revenue_total,
        recent_orders=recent_orders,
        event_rows=event_rows,
    )


# ========================================================
# ЗАКАЗЫ
# ========================================================
@admin_bp.route('/orders')
@admin_required
def orders():
    status = request.args.get('status', '').strip()
    if status and status not in ORDER_STATUS_LABELS:
        abort(400)
    query = select(Order).order_by(Order.id.desc())
    if status:
        query = query.where(Order.status == status)
    pagination = paginate(query)
    return render_template(
        'admin/orders.html',
        pagination=pagination,
        selected_status=status,
    )


@admin_bp.route('/orders/<int:order_id>')
@admin_required
def order_detail(order_id):
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    event = db.session.get(Event, order.event_id) if order.event_id else None
    return render_template('admin/order_detail.html', order=order, event=event)


@admin_bp.route('/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def order_change_status(order_id):
    order = db.session.get(Order, order_id)
    if order is None:
        abort(404)
    action = request.form.get('action', '')
    if order.status != 'pending' or action not in {'mark_paid', 'cancel'}:
        flash('Этот заказ уже обработан.', 'error')
        return redirect(url_for('admin.order_detail', order_id=order.id))
    if action == 'mark_paid':
        if order.fulfillment_method != 'pickup':
            flash('Отметить оплаченным можно только заказ с самовывозом.', 'error')
            return redirect(url_for('admin.order_detail', order_id=order.id))
        order.status = 'paid'
        flash(f'Заказ №{order.id} отмечен как оплаченный.', 'success')
    else:
        if order.seat_reserved and order.event_id is not None:
            if not release_reserved_seats(order.event_id, order.item_id, order.quantity):
                db.session.rollback()
                flash('Не удалось освободить зарезервированные места.', 'error')
                return redirect(url_for('admin.order_detail', order_id=order.id))
            order.seat_reserved = False
        order.status = 'canceled'
        flash(f'Заказ №{order.id} отменён.', 'success')
    db.session.commit()
    return redirect(url_for('admin.order_detail', order_id=order.id))


# ========================================================
# ИГРЫ
# ========================================================
@admin_bp.route('/games')
@admin_required
def games():
    pagination = paginate(select(Game).order_by(Game.id))
    return render_template('admin/games.html', pagination=pagination)


def next_game_id():
    existing_ids = {game_id for (game_id,) in db.session.query(Game.id).all()}
    return next(
        (f'{number:02d}' for number in range(1, 100) if f'{number:02d}' not in existing_ids),
        None,
    )


def read_game_form():
    title = form_value('title', 200)
    target = form_value('target', 200)
    description = form_value('desc', 5000)
    if not title or not target or not description:
        return None, 'Заполните название, аудиторию и описание игры.'
    price, error = form_positive_int('price', 'Цена')
    if error:
        return None, error
    capacity, error = form_positive_int('capacity', 'Вместимость')
    if error:
        return None, error
    return {
        'title': title,
        'target': target,
        'desc': description,
        'price': price,
        'capacity': capacity,
    }, None


@admin_bp.route('/games/add', methods=['GET', 'POST'])
@admin_required
def game_add():
    game_id = next_game_id()
    if game_id is None:
        flash('Нет свободного двухзначного ID для новой игры.', 'error')
        return redirect(url_for('admin.games'))
    if request.method == 'POST':
        values, error = read_game_form()
        if error:
            flash(error, 'error')
        elif Game.query.filter_by(title=values['title']).first() is not None:
            flash('Игра с таким названием уже существует.', 'error')
        else:
            game = Game(id=game_id, **values)
            try:
                apply_uploaded_photos(game, 'games', game_id)
            except ValueError as upload_error:
                flash(str(upload_error), 'error')
                return render_template('admin/game_form.html', game=None, form=request.form)
            db.session.add(game)
            db.session.commit()
            flash(f'Игра «{game.title}» добавлена с ID {game.id}.', 'success')
            return redirect(url_for('admin.games'))
    return render_template('admin/game_form.html', game=None, form=request.form)


@admin_bp.route('/games/<game_id>/edit', methods=['GET', 'POST'])
@admin_required
def game_edit(game_id):
    game = db.session.get(Game, game_id)
    if game is None:
        abort(404)
    if request.method == 'POST':
        values, error = read_game_form()
        duplicate = Game.query.filter(
            Game.title == (values or {}).get('title', ''),
            Game.id != game.id,
        ).first()
        if error:
            flash(error, 'error')
        elif duplicate is not None:
            flash('Игра с таким названием уже существует.', 'error')
        else:
            if values['capacity'] is not None:
                bookings = db.session.query(
                    event_games.c.reserved_places,
                    event_games.c.booked_places,
                ).filter(event_games.c.game_number == game.id).all()
                max_occupied = max(
                    (reserved + booked for reserved, booked in bookings),
                    default=0,
                )
                if values['capacity'] < max_occupied:
                    flash(
                        'Вместимость не может быть меньше числа занятых '
                        'и зарезервированных мест в одном из событий.',
                        'error',
                    )
                    return render_template('admin/game_form.html', game=game, form=request.form)
            try:
                remove_marked_photos(game)
                apply_uploaded_photos(game, 'games', game.id)
            except ValueError as upload_error:
                flash(str(upload_error), 'error')
                return render_template('admin/game_form.html', game=game, form=request.form)
            for field, value in values.items():
                setattr(game, field, value)
            db.session.commit()
            flash(f'Игра «{game.title}» обновлена.', 'success')
            return redirect(url_for('admin.games'))
    return render_template('admin/game_form.html', game=game, form=request.form)


@admin_bp.route('/games/<game_id>/delete', methods=['POST'])
@admin_required
def game_delete(game_id):
    game = db.session.get(Game, game_id)
    if game is None:
        abort(404)
    if game_has_bookings(game.id):
        flash(
            'Нельзя удалить игру с зарезервированными или оплаченными местами.',
            'error',
        )
        return redirect(url_for('admin.games'))
    photos = game.photos if isinstance(game.photos, list) else []
    db.session.execute(
        event_games.delete().where(event_games.c.game_number == game.id)
    )
    db.session.delete(game)
    db.session.commit()
    for photo in photos:
        delete_entity_photo(photo)
    flash(f'Игра «{game.title}» удалена.', 'success')
    return redirect(url_for('admin.games'))


# ========================================================
# ТОВАРЫ
# ========================================================
@admin_bp.route('/products')
@admin_required
def products():
    category = request.args.get('category', '').strip()
    if category and category not in PRODUCT_CATEGORIES:
        abort(400)
    query = select(Product).order_by(Product.id)
    if category:
        query = query.where(Product.category == category)
    pagination = paginate(query)
    return render_template(
        'admin/products.html',
        pagination=pagination,
        selected_category=category,
    )


def read_product_form():
    category = request.form.get('category', '').strip()
    if category not in PRODUCT_CATEGORIES:
        return None, 'Выберите категорию товара.'
    name = form_value('name', 200)
    description = form_value('description', 5000)
    if not name or not description:
        return None, 'Заполните название и описание товара.'
    raw_price = request.form.get('price', '').strip()
    try:
        price = int(raw_price)
    except ValueError:
        return None, 'Цена должна быть целым числом.'
    if price < 0:
        return None, 'Цена не может быть отрицательной.'
    subcategory = form_optional_value('subcategory', 80)
    if subcategory and subcategory not in PRODUCT_SUBCATEGORIES:
        return None, 'Выберите подкатегорию из списка.'
    podzagolovok = form_optional_value('podzagolovok', 255)
    if podzagolovok is None and request.form.get('podzagolovok', '').strip():
        return None, 'Подзаголовок не должен превышать 255 символов.'
    return {
        'category': category,
        'subcategory': subcategory,
        'name': name,
        'podzagolovok': podzagolovok,
        'description': description,
        'full_description': form_optional_value('full_description', 10000),
        'materials': form_optional_value('materials', 255),
        'dimensions': form_optional_value('dimensions', 120),
        'price': price,
    }, None


@admin_bp.route('/products/add', methods=['GET', 'POST'])
@admin_required
def product_add():
    if request.method == 'POST':
        values, error = read_product_form()
        photo_name = form_value('photo', 255)
        if error:
            flash(error, 'error')
        elif not photo_name and not any(
            file.filename for file in request.files.getlist('main_photo')
        ):
            flash('Укажите файл или имя основной фотографии товара.', 'error')
        else:
            product = Product(**values, photo=photo_name or '')
            db.session.add(product)
            db.session.flush()
            try:
                main_photo = next(
                    (
                        file
                        for file in request.files.getlist('main_photo')
                        if file and file.filename
                    ),
                    None,
                )
                if main_photo is not None:
                    product.photo = save_entity_photo('products', product.id, main_photo)
                apply_uploaded_photos(product, 'products', product.id)
            except ValueError as upload_error:
                db.session.rollback()
                flash(str(upload_error), 'error')
                return render_template('admin/product_form.html', product=None, form=request.form)
            db.session.commit()
            flash(f'Товар «{product.name}» добавлен.', 'success')
            return redirect(url_for('admin.products'))
    return render_template('admin/product_form.html', product=None, form=request.form)


@admin_bp.route('/products/<int:product_id>/edit', methods=['GET', 'POST'])
@admin_required
def product_edit(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    if request.method == 'POST':
        values, error = read_product_form()
        photo_name = form_optional_value('photo', 255)
        if error:
            flash(error, 'error')
        else:
            try:
                remove_marked_photos(product)
                apply_uploaded_photos(product, 'products', product.id)
                main_photo = next(
                    (
                        file
                        for file in request.files.getlist('main_photo')
                        if file and file.filename
                    ),
                    None,
                )
                if main_photo is not None:
                    product.photo = save_entity_photo('products', product.id, main_photo)
                elif photo_name:
                    product.photo = photo_name
            except ValueError as upload_error:
                flash(str(upload_error), 'error')
                return render_template('admin/product_form.html', product=product, form=request.form)
            if not product.photo:
                flash('У товара должна быть основная фотография.', 'error')
                return render_template('admin/product_form.html', product=product, form=request.form)
            for field, value in values.items():
                setattr(product, field, value)
            db.session.commit()
            flash(f'Товар «{product.name}» обновлён.', 'success')
            return redirect(url_for('admin.products'))
    return render_template('admin/product_form.html', product=product, form=request.form)


@admin_bp.route('/products/<int:product_id>/delete', methods=['POST'])
@admin_required
def product_delete(product_id):
    product = db.session.get(Product, product_id)
    if product is None:
        abort(404)
    photos = [product.photo] if product.photo else []
    photos.extend(product.photos if isinstance(product.photos, list) else [])
    db.session.delete(product)
    db.session.commit()
    for photo in photos:
        if '/' in photo:
            delete_entity_photo(photo)
    flash(f'Товар «{product.name}» удалён.', 'success')
    return redirect(url_for('admin.products'))


# ========================================================
# СОБЫТИЯ
# ========================================================
@admin_bp.route('/events')
@admin_required
def events():
    pagination = paginate(select(Event).order_by(Event.starts_at.desc(), Event.id.desc()))
    return render_template('admin/events.html', pagination=pagination)


def read_event_form():
    starts_at, error = parse_future_event_date(request.form.get('starts_at', ''))
    if error:
        return None, None, error
    game_ids = request.form.getlist('game_ids')
    games = (
        Game.query.filter(Game.id.in_(game_ids)).order_by(Game.id).all()
        if game_ids
        else []
    )
    if not games:
        return None, None, 'Выберите хотя бы одну игру для события.'
    return starts_at, games, None


@admin_bp.route('/events/add', methods=['GET', 'POST'])
@admin_required
def event_add():
    all_games = Game.query.order_by(Game.id).all()
    if request.method == 'POST':
        starts_at, selected_games, error = read_event_form()
        if error:
            flash(error, 'error')
        else:
            event = Event(
                date=format_event_datetime(starts_at),
                starts_at=starts_at,
                games=selected_games,
            )
            db.session.add(event)
            db.session.commit()
            flash(f'Событие «{event.date}» добавлено.', 'success')
            return redirect(url_for('admin.events'))
    return render_template(
        'admin/event_form.html',
        event=None,
        all_games=all_games,
        selected_game_ids=set(request.form.getlist('game_ids')),
        form=request.form,
    )


@admin_bp.route('/events/<int:event_id>/edit', methods=['GET', 'POST'])
@admin_required
def event_edit(event_id):
    event = db.session.get(Event, event_id)
    if event is None:
        abort(404)
    all_games = Game.query.order_by(Game.id).all()
    if request.method == 'POST':
        starts_at, selected_games, error = read_event_form()
        if error:
            flash(error, 'error')
        else:
            event.starts_at = starts_at
            event.date = format_event_datetime(starts_at)
            event.games = selected_games
            db.session.commit()
            flash(f'Событие обновлено: «{event.date}».', 'success')
            return redirect(url_for('admin.events'))
        selected_game_ids = set(request.form.getlist('game_ids'))
    else:
        selected_game_ids = {game.id for game in event.games}
    return render_template(
        'admin/event_form.html',
        event=event,
        all_games=all_games,
        selected_game_ids=selected_game_ids,
        form=request.form,
    )


@admin_bp.route('/events/<int:event_id>/delete', methods=['POST'])
@admin_required
def event_delete(event_id):
    event = db.session.get(Event, event_id)
    if event is None:
        abort(404)
    if event_has_bookings(event.id):
        flash(
            'Нельзя удалить событие с зарезервированными или оплаченными местами.',
            'error',
        )
        return redirect(url_for('admin.events'))
    db.session.delete(event)
    db.session.commit()
    flash(f'Событие «{event.date}» удалено.', 'success')
    return redirect(url_for('admin.events'))


# ========================================================
# ОТЗЫВЫ
# ========================================================
@admin_bp.route('/reviews')
@admin_required
def reviews():
    pagination = paginate(select(Review).order_by(Review.id.desc()))
    return render_template('admin/reviews.html', pagination=pagination)


def read_review_form():
    author = form_value('author', 100)
    body = form_value('body', 4000)
    if not author:
        return None, 'Имя автора должно содержать от 1 до 100 символов.'
    if not body:
        return None, 'Текст отзыва должен содержать от 1 до 4000 символов.'
    return {'author': author, 'body': body}, None


@admin_bp.route('/reviews/add', methods=['GET', 'POST'])
@admin_required
def review_add():
    if request.method == 'POST':
        values, error = read_review_form()
        if error:
            flash(error, 'error')
        else:
            review = Review(
                **values,
                is_published=request.form.get('is_published') == 'on',
            )
            db.session.add(review)
            db.session.commit()
            flash(f'Отзыв #{review.id} сохранён.', 'success')
            return redirect(url_for('admin.reviews'))
    return render_template('admin/review_form.html', review=None, form=request.form)


@admin_bp.route('/reviews/<int:review_id>/edit', methods=['GET', 'POST'])
@admin_required
def review_edit(review_id):
    review = db.session.get(Review, review_id)
    if review is None:
        abort(404)
    if request.method == 'POST':
        values, error = read_review_form()
        if error:
            flash(error, 'error')
        else:
            review.author = values['author']
            review.body = values['body']
            review.is_published = request.form.get('is_published') == 'on'
            db.session.commit()
            flash(f'Отзыв #{review.id} обновлён.', 'success')
            return redirect(url_for('admin.reviews'))
    return render_template('admin/review_form.html', review=review, form=request.form)


@admin_bp.route('/reviews/<int:review_id>/publish', methods=['POST'])
@admin_required
def review_toggle_publish(review_id):
    review = db.session.get(Review, review_id)
    if review is None:
        abort(404)
    review.is_published = not review.is_published
    db.session.commit()
    flash(
        'Отзыв опубликован.' if review.is_published else 'Отзыв снят с публикации.',
        'success',
    )
    return redirect(url_for('admin.reviews'))


@admin_bp.route('/reviews/<int:review_id>/delete', methods=['POST'])
@admin_required
def review_delete(review_id):
    review = db.session.get(Review, review_id)
    if review is None:
        abort(404)
    db.session.delete(review)
    db.session.commit()
    flash(f'Отзыв #{review_id} удалён.', 'success')
    return redirect(url_for('admin.reviews'))
