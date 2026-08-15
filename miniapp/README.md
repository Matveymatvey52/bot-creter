# Bot mini-app SPA

Generic Telegram Mini App front-end for bots created by this project. It talks to
`runtime/miniapp_api.py`'s REST layer (`GET/POST /api/{bot_id}/{resource}`) and renders
list / detail / create screens for whatever resources a bot's template declares.

The SPA itself has no per-bot logic in it. All bot-specific content — which resources
exist, what their fields are called, how they're labeled and formatted — lives in one
file: [`src/lib/resources.ts`](src/lib/resources.ts).

## Adding a new bot

1. Make sure the bot's template module (e.g. `templates/car_rental.py`) exports a
   `miniapp_config` dict describing its resources, matching the shape already used by
   `templates/tour_operator.py`:

   ```python
   miniapp_config = {
       "resources": [
           {
               "name": "cars",
               "table": "cars",
               "order_by": "created_at DESC",
               "creatable": True,
               "fields": [
                   {"name": "make", "required": True},
                   {"name": "status"},
                   {"name": "created_at", "creatable": False},
               ],
           },
           ...
       ],
   }
   ```

   This is the server-side contract: it controls which columns exist, which are
   writable, and which are required. The generic REST layer in `miniapp_api.py` reads
   this at request time — no backend code changes needed per bot.

2. Edit `src/lib/resources.ts` and add one entry to the `RESOURCES` map per resource
   in `miniapp_config`, keyed by the same resource `name`:

   ```ts
   cars: {
     name: 'cars',
     title: 'Машины',        // tab label
     titleField: 'make',     // which field is shown as the item's heading
     listFields: [ { name: 'status', label: 'Статус', kind: 'status' } ],
     detailFields: [ /* fields shown on the detail screen, in order */ ],
     createFields: [ /* fields shown on the create form, in order */ ],
   },
   ```

   `kind` is one of `'text' | 'number' | 'date' | 'status'` — it picks the form input
   type on the create screen and the display formatting elsewhere. `status` values in
   `statusTone()` (same file) control which statuses render as "success" (green chip)
   vs neutral; adjust that set if a new bot uses different status vocabulary.

3. That's it. `App.tsx`, `api.ts`, and all three screens (`ListScreen`, `DetailScreen`,
   `CreateFormScreen`) read resource names and field lists entirely from `RESOURCES` —
   nothing else in the SPA needs to change. The whole `miniapp/` directory can be
   copied as-is between bots; only `resources.ts` differs.

## What NOT to hardcode elsewhere

If you find yourself adding a resource or field name to any file other than
`resources.ts` (or a bot-specific `miniapp_config` in the template), that's a sign the
change belongs in one of those two places instead — it keeps the SPA reusable across
templates without code changes.

## Development

```sh
npm install
npm run dev       # dev server; see vite.config.ts for the API proxy
npm run build      # outputs to dist/, served by combined_app.py at /app/{bot_id}
```
