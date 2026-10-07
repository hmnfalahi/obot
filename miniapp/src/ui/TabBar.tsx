import { useI18n } from '../i18n'
import { TABS, type Tab } from '../nav'

const ICONS: Record<Tab, string> = { bots: '🤖', tasks: '⏰', credits: '💳', settings: '⚙️' }

/** Bottom tab bar, shown on tab roots only (inner pages give the space to Telegram's buttons). */
export function TabBar({ active, onSelect }: { active: Tab; onSelect: (tab: Tab) => void }) {
  const { t } = useI18n()
  return (
    <nav className="tabbar" role="tablist">
      {TABS.map((tab) => (
        <button
          key={tab}
          role="tab"
          aria-selected={tab === active}
          className={tab === active ? 'active' : undefined}
          onClick={() => onSelect(tab)}
        >
          <span className="tabbar-icon" aria-hidden>
            {ICONS[tab]}
          </span>
          {t(`tab.${tab}`)}
        </button>
      ))}
    </nav>
  )
}
