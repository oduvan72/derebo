(function () {
    var root = document.querySelector('[data-calendar]');
    if (!root) return;

    var rows = Array.prototype.slice.call(root.querySelectorAll('.event-row'));
    var weekEl = root.querySelector('[data-calendar-week]');
    var rangeEl = root.querySelector('[data-calendar-range]');
    var filterEl = root.querySelector('[data-calendar-filter]');
    var emptyEl = root.querySelector('[data-calendar-empty]');

    var WEEKDAY_SHORT = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];
    var MONTH_GENITIVE = [
        'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
        'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'
    ];

    function toISO(date) {
        var month = String(date.getMonth() + 1).padStart(2, '0');
        var day = String(date.getDate()).padStart(2, '0');
        return date.getFullYear() + '-' + month + '-' + day;
    }

    function fromISO(value) {
        var parts = value.split('-').map(Number);
        return new Date(parts[0], parts[1] - 1, parts[2], 12);
    }

    function mondayOf(date) {
        var day = new Date(date.getFullYear(), date.getMonth(), date.getDate(), 12);
        day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
        return day;
    }

    function addDays(date, offset) {
        var next = new Date(date.getTime());
        next.setDate(next.getDate() + offset);
        return next;
    }

    var todayISO = toISO(new Date());
    var selectedDay = null;
    var selectedGame = 'all';
    var firstDay = rows.length ? rows[0].dataset.day : null;
    var monday = firstDay ? mondayOf(fromISO(firstDay)) : mondayOf(new Date());

    rows.forEach(function (row) {
        var label = row.querySelector('[data-weekday]');
        if (label && row.dataset.day) {
            label.textContent = WEEKDAY_SHORT[fromISO(row.dataset.day).getDay()];
        }
    });

    function renderWeek() {
        weekEl.textContent = '';
        for (var index = 0; index < 7; index += 1) {
            var date = addDays(monday, index);
            var iso = toISO(date);
            var button = document.createElement('button');
            button.type = 'button';
            button.className = 'calendar__day';
            button.dataset.date = iso;
            button.setAttribute('aria-pressed', iso === selectedDay ? 'true' : 'false');
            if (iso === selectedDay) button.classList.add('is-active');
            if (iso === todayISO) button.classList.add('is-today');

            var name = document.createElement('span');
            name.className = 'calendar__day-name';
            name.textContent = WEEKDAY_SHORT[date.getDay()];
            var num = document.createElement('span');
            num.className = 'calendar__day-num';
            num.textContent = date.getDate();
            button.appendChild(name);
            button.appendChild(num);
            weekEl.appendChild(button);
        }

        var sunday = addDays(monday, 6);
        if (monday.getMonth() === sunday.getMonth()) {
            rangeEl.textContent = monday.getDate() + '–' + sunday.getDate() + ' ' + MONTH_GENITIVE[monday.getMonth()];
        } else {
            rangeEl.textContent = monday.getDate() + ' ' + MONTH_GENITIVE[monday.getMonth()]
                + ' – ' + sunday.getDate() + ' ' + MONTH_GENITIVE[sunday.getMonth()];
        }
    }

    function applyFilter() {
        var weekStart = toISO(monday);
        var weekEnd = toISO(addDays(monday, 6));
        var visible = 0;
        rows.forEach(function (row) {
            var day = row.dataset.day;
            var dayOk;
            if (selectedDay) {
                dayOk = !day || day === selectedDay;
            } else {
                dayOk = !day || (day >= weekStart && day <= weekEnd);
            }
            var gameOk = selectedGame === 'all' || row.dataset.game === selectedGame;
            var show = dayOk && gameOk;
            row.hidden = !show;
            if (show) visible += 1;
        });
        root.dataset.mode = selectedDay ? 'day' : 'week';
        emptyEl.textContent = selectedDay
            ? 'В этот день встреч нет. Выберите другую дату или игру.'
            : 'На этой неделе встреч нет. Выберите другую неделю или игру.';
        emptyEl.hidden = visible > 0;
    }

    weekEl.addEventListener('click', function (event) {
        var button = event.target.closest('.calendar__day');
        if (!button) return;
        selectedDay = selectedDay === button.dataset.date ? null : button.dataset.date;
        renderWeek();
        applyFilter();
    });

    root.querySelector('[data-calendar-prev]').addEventListener('click', function () {
        monday = addDays(monday, -7);
        selectedDay = null;
        renderWeek();
        applyFilter();
    });

    root.querySelector('[data-calendar-next]').addEventListener('click', function () {
        monday = addDays(monday, 7);
        selectedDay = null;
        renderWeek();
        applyFilter();
    });

    root.querySelector('[data-calendar-today]').addEventListener('click', function () {
        monday = mondayOf(new Date());
        selectedDay = null;
        renderWeek();
        applyFilter();
    });

    filterEl.addEventListener('change', function () {
        selectedGame = filterEl.value;
        applyFilter();
    });

    renderWeek();
    applyFilter();
})();
