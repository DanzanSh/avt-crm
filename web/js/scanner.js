/* Общий слой захвата скана (FEATURES-PLAN.md, этап 4.2).
 *
 * Раньше приём скана держался на постоянном фокусе одного <input> (receiving.html) —
 * ломалось ровно там, где мешает больше всего: оператор кликает в поле количества,
 * сканирует ячейку — код уходит не туда. Здесь перехват идёт на уровне document по
 * ТАЙМИНГУ нажатий (тот же приём, что в scan-layout.js: несколько символов подряд
 * быстрее SCANNER_MAX_GAP_MS — это сканер, не человек с клавиатурой), поэтому не важно,
 * куда именно был направлен фокус в момент скана.
 *
 * Даёт: Scanner.onScan(cb), не зависящий от фокуса; звуковой сигнал разной формы на
 * успех/ошибку (оператор не смотрит в экран — DEV-PLAN.md); захват разделителя GS
 * (ASCII 29) для кодов «Честного знака».
 */
(function () {
  'use strict';

  const SCANNER_MAX_GAP_MS = 40;
  const SCANNER_MIN_FAST_CHARS = 4; // минимальная длина кода, чтобы не путать со случайным набором
  const FLUSH_AFTER_MS = 80; // код считается завершённым, если пауза больше этого времени

  let buffer = '';
  let hasGs = false;
  let lastCharAt = 0;
  let firstCharAt = 0;
  let flushTimer = null;
  const listeners = [];
  let enabled = true;

  function reset() {
    buffer = '';
    hasGs = false;
    firstCharAt = 0;
    lastCharAt = 0;
    clearTimeout(flushTimer);
  }

  function looksLikeScanner() {
    if (buffer.length < SCANNER_MIN_FAST_CHARS) return false;
    const elapsed = lastCharAt - firstCharAt;
    const avgGap = elapsed / (buffer.length - 1 || 1);
    return avgGap <= SCANNER_MAX_GAP_MS;
  }

  function emit() {
    const code = buffer;
    const meta = { hasGs, raw: code, durationMs: lastCharAt - firstCharAt };
    reset();
    if (!code) return;
    listeners.forEach((cb) => {
      try {
        cb(code, meta);
      } catch (e) {
        /* один упавший обработчик не должен ронять остальных */
      }
    });
  }

  function scheduleFlush() {
    clearTimeout(flushTimer);
    flushTimer = setTimeout(() => {
      if (looksLikeScanner()) emit();
      else reset();
    }, FLUSH_AFTER_MS);
  }

  document.addEventListener(
    'keydown',
    (e) => {
      if (!enabled) return;
      // Разделитель GS (ASCII 29) — код «Честного знака» между частями составного кода.
      // HID-сканеры в разных режимах отдают его как keyCode 29, символ '\x1d' либо F8.
      if (e.keyCode === 29 || e.key === '\x1d' || (e.key === 'F8' && buffer.length > 0)) {
        hasGs = true;
        lastCharAt = performance.now();
        scheduleFlush();
        return;
      }

      if (e.key === 'Enter') {
        if (buffer.length > 0) {
          clearTimeout(flushTimer);
          if (looksLikeScanner()) {
            // Подавляем Enter-обработчик поля целиком — скан уже обработан здесь,
            // иначе тот же код обработался бы дважды (см. receiving.html: резервный
            // ручной ввод слушает то же событие).
            e.preventDefault();
            e.stopImmediatePropagation();
            emit();
          } else {
            reset();
          }
        }
        return;
      }

      if (e.key.length !== 1) return; // игнорируем Shift/Tab/стрелки и т.п.

      const now = performance.now();
      if (buffer.length === 0) firstCharAt = now;
      const gapOk = buffer.length === 0 || now - lastCharAt <= SCANNER_MAX_GAP_MS * 3;
      if (!gapOk) reset(); // пауза слишком большая — это не одна посылка сканера, начинаем заново
      buffer += e.key;
      lastCharAt = now;
      scheduleFlush();
    },
    true // capture — видим скан раньше любого обработчика конкретного поля
  );

  function onScan(cb) {
    listeners.push(cb);
    return () => {
      const i = listeners.indexOf(cb);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  function setEnabled(value) {
    enabled = value;
    if (!enabled) reset();
  }

  /** Звуковой сигнал разной формы на событие скана — оператор смотрит на товар, не на
   * экран (DEV-PLAN.md). Короткий высокий тон — успех, низкий двойной — ошибка. */
  function beep(kind) {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      const ctx = beep._ctx || (beep._ctx = new Ctx());
      const now = ctx.currentTime;
      const notes = kind === 'error' ? [220, 180] : [880];
      notes.forEach((freq, i) => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = freq;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.15, now + i * 0.12);
        gain.gain.exponentialRampToValueAtTime(0.001, now + i * 0.12 + 0.1);
        osc.connect(gain).connect(ctx.destination);
        osc.start(now + i * 0.12);
        osc.stop(now + i * 0.12 + 0.11);
      });
    } catch (e) {
      /* звук — не критичен для работы экрана */
    }
  }

  window.Scanner = { onScan, setEnabled, beep };
})();
