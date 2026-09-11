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

  /** Русское склонение по числу: plural(2, 'место', 'места', 'мест') -> 'места'.
   * Было размножено по страницам (fbs/orders.html и т.п.) — вынесено сюда (п.0.2). */
  function plural(n, one, few, many) {
    const d10 = n % 10;
    const d100 = n % 100;
    if (d10 === 1 && d100 !== 11) return one;
    if (d10 >= 2 && d10 <= 4 && (d100 < 12 || d100 > 14)) return few;
    return many;
  }

  /** Общая модалка формы взамен prompt() (п.1.2 жалоб заказчика: голые prompt()
   * без подписей и текущих значений). Динамически создаёт .modal-overlay/.modal
   * поверх existующих классов из theme.css, возвращает Promise со значениями
   * полей или null при отмене/Esc.
   *
   * fields: [{ id, label, type: 'number'|'text'|'textarea', value, min, hint, required }]
   * onChange(values, api) — необязательный колбэк для живого предпросмотра
   * (debounce 300мс), где api = { setPreview(html), setSubmitEnabled(bool) }. */
  function formModal({ title, fields, submitLabel, onChange }) {
    return new Promise((resolve) => {
      const overlay = document.createElement('div');
      overlay.className = 'modal-overlay open';

      const fieldsHtml = fields
        .map((f) => {
          const value = f.value === undefined || f.value === null ? '' : f.value;
          const hintHtml = f.hint ? `<div class="field-hint">${esc(f.hint)}</div>` : '';
          const reqAttr = f.required ? 'required' : '';
          if (f.type === 'textarea') {
            return `
              <div>
                <label class="field-label" for="fm_${f.id}">${esc(f.label)}</label>
                ${hintHtml}
                <textarea class="input" id="fm_${f.id}" ${reqAttr}>${esc(value)}</textarea>
              </div>`;
          }
          const typeAttrs =
            f.type === 'number'
              ? `type="number"${f.min !== undefined ? ` min="${esc(f.min)}"` : ''}`
              : 'type="text"';
          return `
            <div>
              <label class="field-label" for="fm_${f.id}">${esc(f.label)}</label>
              ${hintHtml}
              <input class="input" id="fm_${f.id}" ${typeAttrs} value="${esc(value)}" ${reqAttr} />
            </div>`;
        })
        .join('');

      const form = document.createElement('form');
      form.className = 'modal';
      form.innerHTML = `
        <h2>${esc(title)}</h2>
        ${fieldsHtml}
        <div class="form-modal-preview"></div>
        <div class="field-row" style="margin-top:8px;">
          <button type="button" class="btn" data-action="cancel">Отмена</button>
          <button type="submit" class="btn btn-primary" data-action="submit">${esc(submitLabel || 'Сохранить')}</button>
        </div>`;
      overlay.appendChild(form);
      document.body.appendChild(overlay);

      const submitBtn = form.querySelector('[data-action="submit"]');
      const previewEl = form.querySelector('.form-modal-preview');
      if (typeof onChange === 'function') submitBtn.disabled = true; // до первого предпросмотра

      function fieldEl(id) {
        return form.querySelector('#fm_' + id);
      }

      function readValues() {
        const values = {};
        fields.forEach((f) => {
          const el = fieldEl(f.id);
          if (f.type === 'number') {
            values[f.id] = el.value === '' ? null : Number(el.value);
          } else {
            values[f.id] = el.value;
          }
        });
        return values;
      }

      function isValid() {
        for (const f of fields) {
          const el = fieldEl(f.id);
          if (f.required && !el.value) return false;
          if (f.type === 'number' && el.value !== '') {
            const n = Number(el.value);
            if (Number.isNaN(n)) return false;
            if (f.min !== undefined && n < f.min) return false;
          }
        }
        return true;
      }

      function cleanup(result) {
        document.removeEventListener('keydown', onKeydown);
        overlay.remove();
        resolve(result);
      }

      function onKeydown(e) {
        if (e.key === 'Escape') cleanup(null);
      }
      document.addEventListener('keydown', onKeydown);

      form.querySelector('[data-action="cancel"]').addEventListener('click', () => cleanup(null));
      overlay.addEventListener('click', (e) => {
        if (e.target === overlay) cleanup(null);
      });

      form.addEventListener('submit', (e) => {
        e.preventDefault();
        if (!isValid() || submitBtn.disabled) return;
        cleanup(readValues());
      });

      if (typeof onChange === 'function') {
        const api = {
          setPreview: (html) => {
            previewEl.innerHTML = html;
          },
          setSubmitEnabled: (enabled) => {
            submitBtn.disabled = !enabled;
          },
        };
        let timer = null;
        const trigger = () => onChange(readValues(), api);
        fields.forEach((f) => {
          fieldEl(f.id).addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(trigger, 300);
          });
        });
        trigger(); // первый расчёт сразу с предзаполненными значениями
      }
    });
  }

  window.Fulfil = {
    api, getToken, setToken, logout, decodeJwt, esc, toast, errorText, requireAuth, newIdempotencyKey, thumb,
    plural, formModal,
  };
})();
