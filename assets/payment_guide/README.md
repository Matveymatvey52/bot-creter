# Скриншоты для гайда подключения оплаты

Сюда владелец кладёт статичные PNG-скриншоты BotFather / @YooKassaBot для
пошагового wizard'а `PaymentConnectFlow` (`handlers/manage_bots.py`,
`_PAYMENT_STEP_SCREENSHOTS`).

Ожидаемые файлы (шаг 1 — без скриншота, там нет ещё экрана BotFather):

- `step2_botfather_payments.png` — `/mybots` → Bot Settings → Payments в BotFather
- `step3_choose_provider.png` — список провайдеров с выбранной ЮKassa / экран
  подтверждения в @YooKassaBot
- `step4_token_message.png` — сообщение от @YooKassaBot с токеном (реальный
  токен на референсе должен быть замазан, важен только формат)

Если файла нет — wizard automatически показывает тот же шаг текстом, без
скриншота (см. `_show_payment_step`). Ничего в коде менять не нужно, просто
положить файл с точным именем сюда.
