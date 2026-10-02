/* Исправление русской раскладки при сканировании штрихкодов.
 * USB/Bluetooth-сканер печатает как клавиатура: при включённой русской раскладке
 * ОС "A" превращается в "Ф" и т.д., и штрихкод приходит нечитаемой кириллицей.
 *
 * data-scan="auto" — поле общего назначения (может содержать осмысленную кириллицу,
 *   например адрес ячейки "А-1-10"): исправляем только если ввод пришёл со скоростью
 *   сканера (несколько символов подряд быстрее SCANNER_MAX_GAP_MS).
 * data-scan (без значения) — поле только для машинных кодов: исправляем всегда.
 * data-scan="mark" — код «Честный знак»: символ берётся по ФИЗИЧЕСКОЙ клавише
 *   (event.code), а не по раскладке ОС, поэтому верно приходят и символы криптохвоста
 *   (@ # $ ^ & / …), которые по кириллице не восстановить. Плюс GS-разделитель.
 */
(function () {
  'use strict';

  const RU_TO_EN = {
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p',
    'х':'[','ъ':']','ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k',
    'д':'l','ж':';','э':"'",'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m',
    'б':',','ю':'.','ё':'`',
  };
  // Заглавные: у символьных клавиш Shift даёт другой символ, а не toUpperCase().
  const SHIFTED_SYMBOL = { '[': '{', ']': '}', ';': ':', "'": '"', ',': '<', '.': '>', '`': '~' };
  Object.keys(RU_TO_EN).forEach((k) => {
    const v = RU_TO_EN[k];
    RU_TO_EN[k.toUpperCase()] = SHIFTED_SYMBOL[v] || v.toUpperCase();
  });

  // Символы, которые русская раскладка печатает на месте латинских (Shift+цифры,
  // клавиши / и \). Применяются только в полях марок: в обычных полях . , ; : — это
  // осмысленные символы, а не следы раскладки.
  const RU_SYMBOL_TO_EN = {
    '"': '@', '№': '#', ';': '$', ':': '^', '?': '&', '.': '/', ',': '?', '/': '|',
  };

  function hasCyrillic(s) {
    return /[а-яё]/i.test(s);
  }

  function fromRu(s, withSymbols) {
    let out = '';
    for (const ch of s) {
      if (RU_TO_EN[ch] !== undefined) out += RU_TO_EN[ch];
      else if (withSymbols && RU_SYMBOL_TO_EN[ch] !== undefined) out += RU_SYMBOL_TO_EN[ch];
      else out += ch;
    }
    return out;
  }

  // Символ US-раскладки по физической клавише: [без Shift, с Shift].
  const US_BY_CODE = {
    Backquote: ['`', '~'], Minus: ['-', '_'], Equal: ['=', '+'],
    BracketLeft: ['[', '{'], BracketRight: [']', '}'], Backslash: ['\\', '|'],
    Semicolon: [';', ':'], Quote: ["'", '"'], Comma: [',', '<'], Period: ['.', '>'],
    Slash: ['/', '?'], Space: [' ', ' '],
  };
  '1234567890'.split('').forEach((d, i) => {
    US_BY_CODE['Digit' + d] = [d, '!@#$%^&*()'[i]];
  });

  function usCharFor(e) {
    if (/^Key[A-Z]$/.test(e.code)) {
      const lower = e.code.slice(3).toLowerCase();
      const caps = e.getModifierState && e.getModifierState('CapsLock');
      return e.shiftKey !== !!caps ? lower.toUpperCase() : lower;
    }
    const pair = US_BY_CODE[e.code];
    return pair ? pair[e.shiftKey ? 1 : 0] : null;
  }

  function insertText(el, text) {
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? el.value.length;
    el.setRangeText(text, start, end, 'end');
    el.dispatchEvent(new Event('input', { bubbles: true }));
  }

  document.addEventListener(
    'keydown',
    (e) => {
      const el = e.target;
      if (!el || !el.matches || !el.matches('[data-scan="mark"]')) return;
      if (e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;

      // GS (ASCII 29) — разделитель частей кода ЧЗ; сканеры отдают его как keyCode 29,
      // символ '\x1d' либо F8 (та же логика, что в scanner.js).
      if (e.keyCode === 29 || e.key === '\x1d' || e.key === 'F8') {
        e.preventDefault();
        insertText(el, '\x1d');
        return;
      }

      const ch = usCharFor(e);
      if (ch === null || ch === e.key) return; // латинская раскладка — не вмешиваемся
      e.preventDefault();
      insertText(el, ch);
      showBanner(e.key, ch);
    },
    true
  );

  const SCANNER_MAX_GAP_MS = 40;
  const SCANNER_MIN_FAST_GAPS = 3;
  const timings = new WeakMap();

  function looksLikeScannerBurst(el) {
    const t = timings.get(el);
    if (!t || t.length < SCANNER_MIN_FAST_GAPS + 1) return false;
    let fast = 0;
    for (let i = 1; i < t.length; i++) {
      if (t[i] - t[i - 1] <= SCANNER_MAX_GAP_MS) fast++;
    }
    return fast >= SCANNER_MIN_FAST_GAPS;
  }

  function showBanner(original, fixed) {
    let el = document.getElementById('__scanLayoutBanner');
    if (!el) {
      el = document.createElement('div');
      el.id = '__scanLayoutBanner';
      el.className = 'scan-layout-banner';
      document.body.appendChild(el);
    }
    el.textContent = `Раскладка клавиатуры — русская. Исправлено: «${original}» → «${fixed}»`;
    el.style.display = 'block';
    clearTimeout(el._timer);
    el._timer = setTimeout(() => (el.style.display = 'none'), 8000);
  }

  document.addEventListener(
    'input',
    (e) => {
      const el = e.target;
      if (!el || !el.matches) return;
      if (!el.matches('[data-scan]')) return;

      const now = performance.now();
      const arr = timings.get(el) || [];
      arr.push(now);
      if (arr.length > 10) arr.shift();
      timings.set(el, arr);

      if (!hasCyrillic(el.value)) return;

      const mode = el.getAttribute('data-scan');
      const shouldFix = mode !== 'auto' || looksLikeScannerBurst(el);
      if (!shouldFix) return;

      const original = el.value;
      const fixed = fromRu(original, mode === 'mark');
      el.value = fixed;
      showBanner(original, fixed);
    },
    true // capture — значение исправлено ДО того, как его увидит обработчик страницы
  );
})();
