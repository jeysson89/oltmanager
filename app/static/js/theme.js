/**
 * OLT Manager - Theme Manager v3
 * Переключение тёмной/светлой темы + авто-контраст текста для цветных строк таблиц.
 *
 * ВАЖНО: applyRowContrast объявлена как function declaration (hoisting) —
 * её можно безопасно вызывать из applyTheme в любой момент выполнения IIFE.
 */

(function () {
    'use strict';

    var THEME_KEY = 'olt_theme';

    /* ---------- Глобальная функция получения CSS-переменной ---------- */
    window.cssVar = function (name, fallback) {
        try {
            var v = getComputedStyle(document.body).getPropertyValue(name).trim();
            return v || fallback;
        } catch (e) {
            return fallback;
        }
    };

    /* ---------- Авто-контраст для цветных строк таблиц (hoisted) ---------- */
    function luminance(hex) {
        var c = hex.replace('#', '');
        if (c.length === 3) c = c.split('').map(function (x) { return x + x; }).join('');
        var r = parseInt(c.substr(0, 2), 16) / 255;
        var g = parseInt(c.substr(2, 2), 16) / 255;
        var b = parseInt(c.substr(4, 2), 16) / 255;
        function lin(v) { return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
    }

    function bestTextColor(bgHex) {
        if (!bgHex || !/^#[0-9a-fA-F]{3,6}$/.test(bgHex)) return null;
        try {
            return luminance(bgHex) > 0.45 ? '#1e293b' : '#f1f5f9';
        } catch (e) {
            return null;
        }
    }

    // function declaration — hoisted, доступна везде внутри IIFE
    function applyRowContrast() {
        var body = document.body;
        if (!body) return;
        var isLight = body.classList.contains('light-theme');

        // Выбираем цвета из data-атрибутов в зависимости от активной темы
        var hasSignal = isLight
            ? (body.getAttribute('data-light-signal-has-color') || body.getAttribute('data-signal-has-color'))
            : (body.getAttribute('data-dark-signal-has-color') || body.getAttribute('data-signal-has-color'));
        var noSignal = isLight
            ? (body.getAttribute('data-light-signal-nosignal-color') || body.getAttribute('data-signal-nosignal-color'))
            : (body.getAttribute('data-dark-signal-nosignal-color') || body.getAttribute('data-signal-nosignal-color'));

        // Классы трендов из signal_stats: фон из CSS-переменных, контраст по яркости
        function rowBgVar(cls) {
            var probe = document.querySelector('tr.' + cls);
            if (!probe) return null;
            var bg = getComputedStyle(probe).backgroundColor;
            return (bg && bg !== 'rgba(0, 0, 0, 0)') ? bg : null;
        }
        var trendRows = [];
        ['row-danger', 'row-improved', 'row-new'].forEach(function (cls) {
            var rows = document.querySelectorAll('tr.' + cls);
            if (!rows.length) return;
            var bg = rowBgVar(cls);
            if (!bg) return;
            var m = bg.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
            if (!m) return;
            var hex = '#' + [1, 2, 3].map(function (i) {
                var v = parseInt(m[i]).toString(16);
                return v.length === 1 ? '0' + v : v;
            }).join('');
            var tc = bestTextColor(hex) || '';
            rows.forEach(function (row) {
                row.querySelectorAll('td, th').forEach(function (cell) {
                    if (tc) cell.style.color = tc;
                });
            });
        });

        [['row-has-signal', hasSignal], ['row-no-signal', noSignal]].forEach(function (pair) {
            var cls = pair[0], bg = pair[1];
            var rows = document.querySelectorAll('tr.' + cls);
            if (!rows.length || !bg) return;
            var textColor = bestTextColor(bg) || '';
            rows.forEach(function (row) {
                row.style.setProperty('--row-bg-color', bg);
                if (textColor) {
                    row.querySelectorAll('td, th').forEach(function (cell) {
                        cell.style.color = textColor;
                    });
                }
            });
        });
    }

    window.applyRowContrast = applyRowContrast;

    /* ---------- Применение темы ---------- */
    function applyTheme(theme) {
        var body = document.body;
        if (!body) return;
        if (theme === 'light') {
            body.classList.add('light-theme');
        } else {
            body.classList.remove('light-theme');
        }
        var btn = document.getElementById('themeToggle');
        if (btn) {
            var icon = btn.querySelector('i');
            if (icon) icon.className = theme === 'light' ? 'fas fa-moon' : 'fas fa-sun';
            btn.title = theme === 'light' ? 'Переключить на тёмную тему' : 'Переключить на светлую тему';
        }
        // Безопасный вызов (таблицы могут ещё не существовать — ок)
        try { applyRowContrast(); } catch (e) {}
    }

    function currentTheme() {
        try {
            return localStorage.getItem(THEME_KEY) || 'dark';
        } catch (e) {
            return 'dark';
        }
    }

    function toggleTheme() {
        var next = currentTheme() === 'light' ? 'dark' : 'light';
        try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
        applyTheme(next);
    }

    /* ---------- Раннее применение (body уже существует — скрипт в конце body) ---------- */
    try {
        applyTheme(currentTheme());
    } catch (e) {
        // Любая ошибка не должна сломать переключение
        console.error('Theme init error:', e);
    }

    /* ---------- Обработчики после полной загрузки ---------- */
    document.addEventListener('DOMContentLoaded', function () {
        try {
            applyTheme(currentTheme());
            var btn = document.getElementById('themeToggle');
            if (btn) {
                btn.addEventListener('click', function (ev) {
                    ev.preventDefault();
                    toggleTheme();
                });
            }
            applyRowContrast();

            // Наблюдение за изменениями: добавление/удаление узлов и СМЕНА КЛАССОВ строк
            if ('MutationObserver' in window) {
                var pending = null;
                var observer = new MutationObserver(function () {
                    clearTimeout(pending);
                    pending = setTimeout(applyRowContrast, 150);
                });
                observer.observe(document.body, {
                    childList: true,
                    subtree: true,
                    attributes: true,
                    attributeFilter: ['class']
                });
            }
        } catch (e) {
            console.error('Theme DOMContentLoaded error:', e);
        }
    });
})();
