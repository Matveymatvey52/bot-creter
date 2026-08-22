/* Экранная клавиатура и способ её убрать.

   В WebView нет своей кнопки «свернуть», и единственный способ спрятать
   клавиатуру — снять фокус с поля. Без этого человек, начав печатать, оставался
   с поднятой клавиатурой до самой отправки или отмены экрана.

   Живёт отдельным модулем, а не копией в каждом экране: правило одно и должно
   вести себя одинаково всюду — на форме, в поиске, в списке и в пикере
   родителя. */

/** Снимает фокус с активного поля — этого достаточно, чтобы клавиатура ушла. */
export function blurActive(): void {
  const active = document.activeElement
  if (active instanceof HTMLElement) active.blur()
}

/* Элементы, тап по которым НЕ должен гасить клавиатуру: у полей это перевод
   фокуса между строками формы, у кнопок, подписей и ссылок — их собственное
   действие. Гасим только тап «мимо». */
const KEEPS_FOCUS = 'input, select, textarea, button, label, a'

/** Обработчик для контейнера экрана: тап мимо поля прячет клавиатуру. */
export function dismissKeyboardOnBackdrop(event: { target: EventTarget | null }): void {
  const target = event.target
  if (target instanceof HTMLElement && target.closest(KEEPS_FOCUS)) return
  blurActive()
}
