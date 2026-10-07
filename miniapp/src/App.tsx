import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api, type Lang, type Me } from './api'
import { I18n, translator } from './i18n'
import { currentTab, initialStack, isTab, reduce, type Route, type Tab } from './nav'
import { BotPage } from './pages/BotPage'
import { BotsPage } from './pages/BotsPage'
import { CreditsPage } from './pages/CreditsPage'
import { MySettingsPage } from './pages/MySettingsPage'
import { NewBotPage } from './pages/NewBotPage'
import { SettingsPage } from './pages/SettingsPage'
import { TasksPage } from './pages/TasksPage'
import { cloudGet, cloudSet, onSettingsButton, selectionHaptic, startParam, webApp } from './telegram'
import { BottomButtonsFallback, ErrorBox, Loading, TabBar } from './ui'

const LAST_TAB = 'last_tab'

export default function App() {
  const [stack, dispatch] = useReducer(reduce, undefined, () =>
    initialStack(window.location.search, startParam()),
  )
  const [me, setMe] = useState<Me | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadMe = useCallback(() => {
    api.me().then(
      (value) => {
        setMe(value)
        setError(null)
      },
      (e: Error) => setError(e.message),
    )
  }, [])
  useEffect(loadMe, [loadMe])

  const lang: Lang = me?.language ?? (webApp?.initDataUnsafe.user?.language_code === 'fa' ? 'fa' : 'en')
  const i18n = useMemo(() => ({ lang, t: translator(lang) }), [lang])

  useEffect(() => {
    document.documentElement.lang = lang
    document.documentElement.dir = lang === 'fa' ? 'rtl' : 'ltr'
  }, [lang])

  // Set once the stored tab was applied or skipped; until then LAST_TAB is not written.
  const [restored, setRestored] = useState(false)
  // Set when the user navigates before the stored tab arrives; their choice wins.
  const navigated = useRef(false)

  const navigate = useCallback((route: Route) => {
    navigated.current = true
    dispatch({ type: 'push', route })
  }, [])
  const selectTab = useCallback((tab: Tab) => {
    navigated.current = true
    selectionHaptic()
    dispatch({ type: 'tab', tab })
  }, [])

  // Reopen on the tab used last time, unless Telegram opened a specific page.
  useEffect(() => {
    if (stack.length > 1) {
      setRestored(true)
      return
    }
    cloudGet(LAST_TAB).then((tab) => {
      if (!navigated.current && tab && isTab(tab) && tab !== 'bots') dispatch({ type: 'tab', tab })
      setRestored(true)
    })
    // only on first load
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const tab = currentTab(stack)
  useEffect(() => {
    if (restored) cloudSet(LAST_TAB, tab)
  }, [tab, restored])

  // Telegram's back button goes one page back; hidden on tab roots.
  useEffect(() => {
    const back = webApp?.BackButton
    if (!back) return
    const goBack = () => dispatch({ type: 'back' })
    if (stack.length > 1) back.show()
    else back.hide()
    back.onClick(goBack)
    return () => back.offClick(goBack)
  }, [stack.length])

  // "Settings" in the Mini App's own menu opens the settings tab.
  useEffect(() => onSettingsButton(() => dispatch({ type: 'tab', tab: 'settings' })), [])

  const route = stack[stack.length - 1]
  const onTabRoot = stack.length === 1

  let content
  if (error) content = <ErrorBox message={error} onRetry={loadMe} />
  else if (!me) content = <Loading />
  else if (route.page === 'bot') content = <BotPage id={route.id} me={me} navigate={navigate} />
  else if (route.page === 'mine') content = <MySettingsPage id={route.id} navigate={navigate} />
  else if (route.page === 'new') content = <NewBotPage navigate={navigate} />
  else if (route.page === 'settings') content = <SettingsPage me={me} onChange={setMe} />
  else if (route.page === 'credits') content = <CreditsPage me={me} onChange={setMe} />
  else if (route.page === 'tasks') content = <TasksPage me={me} />
  else content = <BotsPage me={me} navigate={navigate} />

  return (
    <I18n.Provider value={i18n}>
      <div className={onTabRoot ? 'has-tabbar' : undefined}>{content}</div>
      <BottomButtonsFallback />
      {onTabRoot && <TabBar active={tab} onSelect={selectTab} />}
    </I18n.Provider>
  )
}
