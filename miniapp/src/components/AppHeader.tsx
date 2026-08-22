import type { SchemaBot } from '../lib/api'
import { Icon } from './Icon'

/* Инициалы для квадрата слева. Имя бота владелец пишет как хочет —
   «TravelOps», «Тур Ателье», «bot_dostavka», — поэтому границей слова
   считаем и пробел/подчёркивание/дефис, и переход строчная→заглавная
   (TravelOps → TO). Одно слово без границ даёт две первые буквы. */
function initials(name: string): string {
  const words = name
    .replace(/([a-zа-яё])([A-ZА-ЯЁ])/g, '$1 $2')
    .split(/[\s_\-·.]+/)
    .filter(Boolean)
  if (words.length === 0) return '?'
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase()
  return (words[0][0] + words[1][0]).toUpperCase()
}

/* Шапка приложения — вариант I (эталон design/mockups/miniapp_mockup_I.html):
   «назад», квадрат с инициалами, название и род занятий, «⋯».

   «Назад» рисуется только когда есть куда возвращаться (onBack), иначе на его
   месте пустой отступ той же ширины — чтобы шапка не дёргалась при переходах.
   Если бэкенд не отдал имя бота, шапка не рисуется вовсе: пустой квадрат с
   прочерком хуже, чем её отсутствие. */
export function AppHeader({
  bot,
  onBack,
  onMenu,
}: {
  bot: SchemaBot | null
  onBack?: () => void
  onMenu?: () => void
}) {
  if (!bot?.name) return null
  return (
    <div className="sol-top">
      {onBack ? (
        <button className="sol-tb" onClick={onBack} aria-label="Назад">
          <Icon name="back" size={15} />
        </button>
      ) : (
        <div className="sol-tb-gap" />
      )}
      <div className="sol-mark">{initials(bot.name)}</div>
      <div className="sol-brand">
        <div className="sol-brand-name">{bot.name}</div>
        {bot.subtitle && <div className="sol-brand-sub">{bot.subtitle}</div>}
      </div>
      {onMenu && (
        <button className="sol-tb" onClick={onMenu} aria-label="Все разделы">
          <Icon name="dots" size={15} />
        </button>
      )}
    </div>
  )
}
