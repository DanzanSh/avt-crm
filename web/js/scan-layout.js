/* Исправление русской раскладки при сканировании штрихкодов.
 * USB/Bluetooth-сканер печатает как клавиатура: при включённой русской раскладке
 * ОС "A" превращается в "Ф" и т.д., и штрихкод приходит нечитаемой кириллицей.
 *
 * data-scan="auto" — поле общего назначения (может содержать осмысленную кириллицу,
 *   например адрес ячейки "А-1-10"): исправляем только если ввод пришёл со скоростью
 *   сканера (несколько символов подряд быстрее SCANNER_MAX_GAP_MS).
 * data-scan (без значения) — поле только для машинных кодов: исправляем всегда.
 */
(function () {
  'use strict';

  const RU_TO_EN = {
    'й':'q','ц':'w','у':'e','к':'r','е':'t','н':'y','г':'u','ш':'i','щ':'o','з':'p',
    'х':'[','ъ':']','ф':'a','ы':'s','в':'d','а':'f','п':'g','р':'h','о':'j','л':'k',
    'д':'l','ж':';','э':"'",'я':'z','ч':'x','с':'c','м':'v','и':'b','т':'n','ь':'m',
    'б':',','ю':'.','ё':'`',
  };
  Object.keys(RU_TO_EN).forEach((k) => {
    RU_TO_EN[k.toUpperCase()] = RU_TO_EN[k].toUpperCase();
  });

  function hasCyrillic(s) {
    return /[а-яё]/i.test(s);
  }

  function fromRu(s) {
    let out = '';
    for (const ch of s) out += RU_TO_EN[ch] !== undefined ? RU_TO_EN[ch] : ch;
    return out;
  }

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
      const fixed = fromRu(original);
      el.value = fixed;
      showBanner(original, fixed);
    },
    true // capture — значение исправлено ДО того, как его увидит обработчик страницы
  );
})();
