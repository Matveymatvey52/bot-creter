/* Монолинейные иконки варианта I (design/mockups/miniapp_mockup_I.html).
   Эмодзи в интерфейсе сознательно не используются: владелец отдельно
   отметил, что они удешевляют вид. */

const PATHS = {
  plus: 'M12 5.5v13M5.5 12h13',
  back: 'M14.5 5.5L8 12l6.5 6.5',
  chevron: 'M9.5 5.5l6.5 6.5-6.5 6.5',
  // Обобщённая иконка раздела: набор ресурсов у каждого бота свой, поэтому
  // семантическую иконку под каждый подобрать нельзя — одна нейтральная
  // честнее, чем случайная.
  section: 'M8.5 6h11M8.5 12h11M8.5 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01',
  chart: 'M4.5 19.5h15M7 16.5V11M12 16.5V6.5M17 16.5v-7',
  trip: 'M3.5 7.5h17v12h-17zM8.5 7.5V6a1.6 1.6 0 0 1 1.6-1.6h3.8A1.6 1.6 0 0 1 15.5 6v1.5',
  people: 'M9.2 5.9a3.1 3.1 0 1 1 0 6.2 3.1 3.1 0 0 1 0-6.2M3.6 19c.7-2.8 2.9-4.3 5.6-4.3s4.9 1.5 5.6 4.3M16.4 6.4a3 3 0 0 1 0 5.4',
  building: 'M4.5 20V6.4L12 3.4l7.5 3V20M9.5 20v-4.2h5V20M8 9.4h2M14 9.4h2',
  money: 'M3.5 7.5h17v11h-17zM3.5 11h17M16.6 14.8h.01',
  box: 'M3.5 7.6 12 4l8.5 3.6v8.8L12 20l-8.5-3.6zM3.5 7.6 12 11.2l8.5-3.6M12 11.2V20',
  dots: 'M5 12h.01M12 12h.01M19 12h.01',
  file: 'M6 3.5h7.5L18 8v12.5H6zM13.5 3.5V8H18M9 12.5h6M9 16h4',
  image: 'M4 5.5h16v13H4zM4 15l4.5-4 3.5 3 3-2.5L20 16M15.5 9.2h.01',
  receipt: 'M6.5 3.5h11v17l-2-1.4-1.8 1.4-1.7-1.4-1.7 1.4-1.8-1.4-2 1.4zM9.5 8.5h5M9.5 12h5',
  download: 'M12 4.5v10M8 11l4 4 4-4M5 19.5h14',
  share: 'M7 13.5a2.2 2.2 0 1 1 0-4.4 2.2 2.2 0 0 1 0 4.4M17 8.1a2.2 2.2 0 1 1 0-4.4 2.2 2.2 0 0 1 0 4.4M17 20.3a2.2 2.2 0 1 1 0-4.4 2.2 2.2 0 0 1 0 4.4M8.9 10.3l6.2-3.1M8.9 13.7l6.2 3.1',
  check: 'M4.5 12.5l4.5 4.5 10.5-10.5',
} as const

/* Иконка раздела подбирается по смыслу имени ресурса из miniapp_config.
   Набор ресурсов у каждого бота свой, поэтому это эвристика по ключевым
   словам с нейтральным запасным вариантом — но она покрывает типовые
   шаблоны (туры, гости, отели, платежи, заказы, задачи). */
const BY_KEYWORD: Array<[RegExp, IconName]> = [
  [/tour|trip|travel|route|тур|поезд/i, 'trip'],
  [/guest|client|customer|member|student|people|person|гост|клиент|участник/i, 'people'],
  [/hotel|room|property|apartment|object|venue|отел|номер|объект/i, 'building'],
  [/pay|price|invoice|finance|money|cash|expense|budget|оплат|плат|счёт|счет|расход|бюджет/i, 'money'],
  [/order|product|item|good|stock|заказ|товар|склад/i, 'box'],
  [/task|todo|job|check|задач|дело/i, 'check'],
]

export function iconForResource(name: string, title?: string): IconName {
  const haystack = `${name} ${title ?? ''}`
  for (const [re, icon] of BY_KEYWORD) {
    if (re.test(haystack)) return icon
  }
  return 'section'
}

export type IconName = keyof typeof PATHS

export function Icon({ name, size = 16 }: { name: IconName; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ display: 'block', flex: '0 0 auto' }}
    >
      <path d={PATHS[name]} />
    </svg>
  )
}
