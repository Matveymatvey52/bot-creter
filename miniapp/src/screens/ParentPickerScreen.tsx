/* Экран выбора родителя для scoped-раздела — «по какому туру» из
   docs/SCOPE_AUDIT_STAGE_A.md.

   Разметка и классы взяты из утверждённого эталона
   design/mockups/miniapp_mockup_I.html (.pick / .pick.on / .pick-t / .pick-s),
   там это и есть экран выбора контекста. Своих стилей нет.

   Экран существует только для scoped-ресурса: ListScreen держит его за той же
   веткой `scope !== null`, что и всё остальное про родителя, а глобальному
   ресурсу передавать нечего. */

import { PARENT_ALL, PARENT_NONE, type ListScope } from '../lib/api'
import { Icon } from '../components/Icon'
import { dismissKeyboardOnBackdrop } from '../lib/keyboard'

export function ParentPickerScreen({
  scope,
  sectionTitle,
  selected,
  onSelect,
  onCancel,
}: {
  scope: ListScope
  sectionTitle: string
  /** null — сводный режим: все родители сразу. */
  selected: string | null
  onSelect: (parent: string | null) => void
  onCancel: () => void
}) {
  const rows: Array<{ key: string; value: string | null; title: string; sub?: string }> = [
    {
      key: PARENT_ALL,
      value: null,
      title: `Все ${scope.parentTitle.toLowerCase()}`,
      sub: 'сводно, с колонкой-различителем',
    },
    ...scope.options.map((option) => ({
      key: option.id,
      value: option.id as string | null,
      title: option.label ?? `#${option.id}`,
    })),
    {
      key: PARENT_NONE,
      value: PARENT_NONE as string | null,
      title: 'Не привязано',
      sub: 'записи без привязки — назначить можно вручную',
    },
  ]

  return (
    /* Тап мимо поля прячет клавиатуру — то же правило, что на форме создания и
       в поиске: одно поведение на всех экранах. */
    <div className="screen" onPointerDown={dismissKeyboardOnBackdrop}>
      <div className="sol-head">
        <div>
          <h1>{scope.parentTitle}</h1>
          <div className="sol-head-count">раздел «{sectionTitle}»</div>
        </div>
        <button className="sol-add" onClick={onCancel} aria-label="Закрыть">
          <Icon name="back" />
        </button>
      </div>

      {rows.map((row) => (
        <button
          key={row.key}
          className={`pick${row.value === selected ? ' on' : ''}`}
          onClick={() => onSelect(row.value)}
        >
          <div>
            <div className="pick-t">{row.title}</div>
            {row.sub && <div className="pick-s">{row.sub}</div>}
          </div>
          {row.value === selected && <Icon name="check" />}
        </button>
      ))}

      {scope.options.length === 0 && (
        <div className="state-message">
          Пока не из чего выбирать — сначала создайте запись в разделе «{scope.parentTitle}».
        </div>
      )}
    </div>
  )
}
