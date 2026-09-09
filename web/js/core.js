/* Общий слой: авторизация, вызов API, toast, экранирование.
 * Единственная реализация на весь проект — страницы не копируют свой getToken/api/esc/toast
 * (в отличие от эталона, где это было размножено по 15-40 страницам). */
(function () {
  'use strict';

  function getToken() {
    try {
      return JSON.parse(localStorage.getItem('authState') || '{}').accessToken || '';
    } catch (e) {
      return '';
    }
  }

  function setToken(token) {
    localStorage.setItem('authState', JSON.stringify({ accessToken: token }));
  }

  function logout() {
    localStorage.removeItem('authState');
    location.href = '/login.html';
  }

  function decodeJwt(token) {
    try {
      return JSON.parse(atob(token.split('.')[1]));
    } catch (e) {
      return null;
    }
  }

  async function api(method, path, body, extraHeaders) {
    const token = getToken();
    const opts = {
      method,
      headers: Object.assign({ 'Content-Type': 'application/json' }, extraHeaders || {}),
    };
    if (token) opts.headers.Authorization = 'Bearer ' + token;
    if (body !== undefined) opts.body = JSON.stringify(body);

    let resp;
    try {
      resp = await fetch('/api/v1' + path, opts);
    } catch (e) {
      throw { status: 0, detail: 'Ошибка сети. Проверьте подключение.' };
    }

    if (resp.status === 401) {
      logout();
      throw { status: 401, detail: 'Сессия истекла' };
    }

    if (resp.status === 204) return null;

    let data = null;
    try {
      data = await resp.json();
    } catch (e) {
      /* тело могло быть пустым (например, у PDF-эндпоинтов, которые сюда не ходят) */
    }

    if (!resp.ok) {
      throw Object.assign({ status: resp.status }, data);
    }
    return data;
  }

  function esc(s) {
    const d = document.createElement('div');
    d.textContent = s === undefined || s === null ? '' : String(s);
    return d.innerHTML;
  }

  function toast(message, type) {
    type = type || 'success';
    let el = document.getElementById('__toast');
    if (!el) {
      el = document.createElement('div');
      el.id = '__toast';
      document.body.appendChild(el);
    }
    el.className = 'toast ' + type;
    el.textContent = message;
    requestAnimationFrame(() => el.classList.add('show'));
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.remove('show'), 3200);
  }

  /** Миниатюра фото товара: URL от WB вида
   * https://basket-27.wbbasket.ru/vol.../images/big/1.webp — замена /images/big/
   * на /images/tm/ даёт лёгкий thumbnail. Если URL пустой или не подходит под
   * шаблон — возвращаем как есть (миграция и повторный синк не нужны). */
  function thumb(url) {
    if (!url || typeof url !== 'string') return url || '';
    return url.indexOf('/images/big/') !== -1
      ? url.replace('/images/big/', '/images/tm/')
      : url;
  }

  /** Единая обработка ошибки app-error: {detail, reasonCode, whatToDo, suggestions?, blockingCells?}. */
  function errorText(err) {
    if (!err) return 'Неизвестная ошибка';
    // 422 от FastAPI: detail — массив объектов {loc, msg, type}, а не строка.
    if (Array.isArray(err.detail)) {
      const msgs = err.detail
        .map((d) => (d && d.msg ? d.msg : null))
        .filter(Boolean);
      return msgs.length ? msgs.join('; ') : 'Ошибка ' + (err.status || '');
    }
    let text = err.detail || 'Ошибка ' + (err.status || '');
    if (err.whatToDo) text += ' ' + err.whatToDo;
    if (err.suggestions && err.suggestions.length) {
      text += ' Свободна: ' + err.suggestions[0].address;
    }
    if (err.blockingCells && err.blockingCells.length) {
      text += ' (' + err.blockingCells.map((c) => c.address).join(', ') + ')';
    }
    if (err.actualQty !== undefined) {
      text += ' Актуальный остаток: ' + err.actualQty + '.';
    }
    return text;
  }

  /** Идемпотентный ключ для мутирующих складских операций (X-Idempotency-Key) —
   * см. FEATURES-PLAN.md, этап 4.4. Один ключ на одну попытку операции с фронта. */
  function newIdempotencyKey() {
    if (window.crypto && window.crypto.randomUUID) return window.crypto.randomUUID();
    return 'idem-' + Date.now() + '-' + Math.random().toString(36).slice(2);
  }

  function requireAuth() {
    if (!getToken()) {
      location.href = '/login.html';
    }
  }

  window.Fulfil = {
    api, getToken, setToken, logout, decodeJwt, esc, toast, errorText, requireAuth, newIdempotencyKey, thumb,
  };
})();
