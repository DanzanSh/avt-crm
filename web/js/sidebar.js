/* Само-монтирующийся сайдбар: оборачивает содержимое страницы, строит навигацию
 * из GET /api/v1/settings/pages — единственного источника правды (fulfil.pages.PAGES).
 * Страницы не хранят собственной копии карты page->href.
 *
 * Оформление — по макету framer-export/component-Navigation.json: 196px, плоский
 * список без заголовков секций (порядок в PAGES уже совпадает с макетом), шапка
 * с фирменным знаком. Секции приходят с бэкенда и остаются в данных — просто
 * не рисуются.
 *
 * Каркас (обёртка контента + панель) строится СИНХРОННО, до сетевого запроса —
 * иначе между загрузкой страницы и ответом API контент успевает отрисоваться
 * без раскладки (голые flex-элементы в ряд), а потом «прыгает» на место. Список
 * ссылок — единственное, что ждёт ответа: он дорисовывается в готовую панель. */
(function () {
  'use strict';

  if (location.pathname === '/login.html') return;

  const MARK = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4' +
    'a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z"/><path d="m3.3 7 8.7 5 8.7-5"/><path d="M12 22V12"/></svg>';

  const style = document.createElement('style');
  style.textContent = `
    body { display: flex; min-height: 100vh; }
    .sb-nav {
      width: 196px;
      flex: none;
      background: var(--bg);
      border-right: 1px solid var(--border);
      padding: 20px 14px;
      display: flex;
      flex-direction: column;
      gap: 14px;
    }
    .sb-brand { display: flex; align-items: center; gap: 9px; }
    .sb-mark {
      width: 28px;
      height: 28px;
      flex: none;
      border-radius: 8px;
      background: rgba(53, 96, 189, 0.8);
      color: #fff;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .sb-brand-name { font-size: 18px; font-weight: 700; line-height: 1.1; color: var(--text); }
    .sb-link {
      font-size: 18px;
      font-weight: 600;
      line-height: 1.2;
      color: var(--text-2);
      text-decoration: none;
      border-radius: var(--radius-sm);
      margin: 0 -8px;
      padding: 4px 8px;
    }
    .sb-link:hover { background: var(--row-hover); }
    .sb-link.active { color: var(--blue); background: var(--blue-soft); font-weight: 700; }
    .sb-logout {
      margin-top: auto;
      padding: 11px 14px;
      border: 1px solid var(--red);
      border-radius: var(--radius-sm);
      background: var(--red-soft);
      color: var(--red);
      font-family: var(--font);
      font-size: 16px;
      font-weight: 700;
      text-align: center;
      cursor: pointer;
    }
    .sb-logout:hover { background: var(--red); color: #fff; }
    .sb-content { flex: 1; min-width: 0; padding: 28px; overflow: auto; }
    @media (max-width: 768px) {
      body { flex-direction: column; }
      .sb-nav {
        width: 100%;
        flex-direction: row;
        align-items: center;
        gap: 8px;
        overflow-x: auto;
        padding: 8px 12px;
        border-right: none;
        border-bottom: 1px solid var(--border);
      }
      .sb-brand-name { display: none; }
      .sb-logout { margin-top: 0; padding: 8px 12px; }
      .sb-link { margin: 0; white-space: nowrap; }
      .sb-content { padding: 16px; }
    }
  `;
  document.head.appendChild(style);

  // ── Каркас: синхронно, в том же кадре, что и инъекция стилей ────────
  const wrapper = document.createElement('div');
  wrapper.className = 'sb-content';
  while (document.body.firstChild) {
    wrapper.appendChild(document.body.firstChild);
  }

  const nav = document.createElement('nav');
  nav.className = 'sb-nav';
  nav.innerHTML =
    '<div class="sb-brand">' +
      '<span class="sb-mark">' + MARK + '</span>' +
      '<span class="sb-brand-name">AVT fulfil</span>' +
    '</div>' +
    '<button class="sb-logout" id="sbLogout">Выйти</button>';

  document.body.appendChild(nav);
  document.body.appendChild(wrapper);
  document.body.classList.add('sb-ready');

  document.getElementById('sbLogout').addEventListener('click', () => Fulfil.logout());

  // ── Ссылки: дорисовываются между шапкой и «Выйти», когда придёт карта ─
  function fillLinks(pages) {
    const logout = document.getElementById('sbLogout');
    nav.querySelectorAll('.sb-link').forEach((el) => el.remove());
    pages.forEach((p) => {
      const a = document.createElement('a');
      a.className = 'sb-link' + (location.pathname === p.href ? ' active' : '');
      a.href = p.href;
      a.textContent = p.label;
      nav.insertBefore(a, logout);
    });
  }

  Fulfil.requireAuth();
  Fulfil.api('GET', '/settings/pages')
    .then(fillLinks)
    .catch(() => fillLinks([]));
})();
