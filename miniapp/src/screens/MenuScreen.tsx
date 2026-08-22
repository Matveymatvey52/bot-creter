import { Icon, iconForResource } from '../components/Icon'
import type { ResourceDisplay } from '../lib/displaySchema'

/* Экран «Все разделы» — то, куда ведёт «⋯» в шапке (вариант I,
   design/mockups/miniapp_mockup_I.html). Нижняя навигация показывает только
   первые разделы, лента прокручивается вбок; этот экран — единственное место,
   где видно СРАЗУ все разделы бота с количеством записей. */
export function MenuScreen({
  resources,
  counts,
  onOpen,
  onBack,
}: {
  resources: Record<string, ResourceDisplay>
  counts: Record<string, number>
  onOpen: (resource: string) => void
  onBack: () => void
}) {
  const names = Object.keys(resources)
  return (
    <div className="screen">
      <div className="sol-sheet">
        <div className="sol-sheet-h">
          <button className="sol-crumb" onClick={onBack}>
            <Icon name="back" size={13} />
            Назад
          </button>
          <h1>Все разделы</h1>
        </div>
        {names.map((name) => (
          <button className="sol-mrow" key={name} onClick={() => onOpen(name)}>
            <Icon name={iconForResource(name, resources[name].title)} size={15} />
            <span className="sol-mrow-n">{resources[name].title}</span>
            {counts[name] !== undefined && (
              <span className="sol-mrow-c">записей: {counts[name]}</span>
            )}
            <Icon name="chevron" size={14} />
          </button>
        ))}
      </div>
    </div>
  )
}
