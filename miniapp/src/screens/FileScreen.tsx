import { useState } from 'react'
import { Icon } from '../components/Icon'
import { isLinkValue } from '../lib/displaySchema'
import type { FieldDisplay } from '../lib/displaySchema'

/* Экран одного вложения (вариант I). Превью не рисуем: движок хранит только
   значение поля — ссылку или имя файла, — и что внутри, он не знает.

   «Скачать» открывает вложение в новой вкладке, «Поделиться» отдаёт ссылку
   системному share-меню, а где его нет (десктоп) — кладёт в буфер обмена.
   Обе кнопки появляются только если значение действительно ссылка: у файла,
   сохранённого одним именем, скачивать нечего, и мёртвую кнопку рисовать
   нельзя. */
export function FileScreen({
  field,
  value,
  onBack,
}: {
  field: FieldDisplay
  value: unknown
  onBack: () => void
}) {
  const [note, setNote] = useState<string | null>(null)
  const text = String(value ?? '')
  const url = isLinkValue(field, value) ? text : null
  const name = url ? decodeURIComponent(url.split('/').pop() || text) : text

  const share = async () => {
    if (!url) return
    if (navigator.share) {
      try {
        await navigator.share({ url })
        return
      } catch {
        // Пользователь закрыл системное меню — это не ошибка, просто выходим.
        return
      }
    }
    try {
      await navigator.clipboard.writeText(url)
      setNote('Ссылка скопирована')
    } catch {
      setNote('Не удалось скопировать ссылку')
    }
  }

  return (
    <div className="screen">
      <div className="sol-sheet">
        <div className="sol-sheet-h">
          <button className="sol-crumb" onClick={onBack}>
            <Icon name="back" size={13} />
            Назад к записи
          </button>
          <h1>{name}</h1>
          <div className="sol-sheet-sub">{field.label}</div>
        </div>
      </div>

      <div className="sol-fview">
        <Icon name="file" size={56} />
      </div>

      {note && <div className="state-message">{note}</div>}

      {url && (
        <div className="sol-acts">
          <div className="sol-acts-row">
            <a className="sol-btn" href={url} target="_blank" rel="noopener noreferrer">
              <Icon name="download" size={15} />
              Скачать
            </a>
            <button className="sol-btn" onClick={share}>
              <Icon name="share" size={15} />
              Поделиться
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
