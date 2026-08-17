// Emoji-by-template mapping for the "Мои боты" dashboard card's icon plate
// (see docs bot-card redesign mockup, approved 2026-08-17). Purely a display
// concern — mirrors the template id list in templates/*.py 1:1, but this
// file has no business importing Python; a new template just needs a line
// added here (falls back to a neutral icon otherwise, never breaks).

const TEMPLATE_ICONS: Record<string, string> = {
  accountant: '🧾',
  booking_beauty: '💇',
  booking_fitness: '🏋️',
  booking_medical: '🩺',
  booking_restaurant: '🍽️',
  campaign_tracker: '📣',
  car_rental: '🚙',
  channel_monitor: '📡',
  coworking_space: '🧑‍💻',
  debtors: '💳',
  delivery_tracker: '📦',
  event_manager: '🎪',
  event_rsvp: '📅',
  expense_tracker: '💸',
  feedback_survey: '📝',
  habit_tracker: '✅',
  inventory: '📊',
  loyalty_program: '🎁',
  manager_secretary: '🗂️',
  moderator: '🛡️',
  orders_tracker: '🧺',
  referral_program: '🔗',
  rental_equipment: '🛠️',
  repair_tracker: '🔧',
  shop_catalog: '🛍️',
  staff_scheduler: '🗓️',
  support_tickets: '🎫',
  tour_operator: '🧭',
  tourist_documents: '🛂',
  trip_manager: '✈️',
  vehicle_service: '🚗',
}

// From-scratch bots (no template) or a template this map hasn't caught up
// with yet — a neutral, still-on-brand icon rather than a blank plate.
const FALLBACK_ICON = '✨'

export function iconForTemplate(template: string | null): string {
  if (!template) return FALLBACK_ICON
  return TEMPLATE_ICONS[template] ?? FALLBACK_ICON
}
