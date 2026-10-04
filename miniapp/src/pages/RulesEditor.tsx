import { useState } from 'react'
import { api, RULE_KINDS, type BotDetail, type ReplyRule, type RuleKind } from '../api'
import { useI18n } from '../i18n'
import { haptic } from '../telegram'

export function RulesEditor({ bot, onSaved }: { bot: BotDetail; onSaved: (b: BotDetail) => void }) {
  const { t } = useI18n()
  const [rules, setRules] = useState<ReplyRule[]>(bot.reply_rules)
  const [status, setStatus] = useState<'idle' | 'saving' | 'saved' | string>('idle')
  const isAdminSomewhere = bot.chats.some((c) => c.is_admin)

  const update = (index: number, patch: Partial<ReplyRule>) =>
    setRules((current) => current.map((rule, i) => (i === index ? { ...rule, ...patch } : rule)))

  const save = async () => {
    setStatus('saving')
    try {
      const saved = await api.updateBot(bot.id, { reply_rules: rules })
      onSaved(saved)
      setRules(saved.reply_rules)
      setStatus('saved')
      haptic('success')
    } catch (e) {
      setStatus((e as Error).message)
      haptic('error')
    }
  }

  return (
    <>
      <h2>{t('rules')}</h2>
      <div className="card">
        <p className="hint">{t('rulesHint')}</p>
        {!isAdminSomewhere && <p className="error">{t('rulesNoAdmin')}</p>}
        {rules.map((rule, index) => (
          <div key={index} className="card" style={{ background: 'var(--bg2)' }}>
            <label>{t('ruleKind')}</label>
            <select value={rule.kind} onChange={(e) => update(index, { kind: e.target.value as RuleKind })}>
              {RULE_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {t(`rule.${kind}`)}
                </option>
              ))}
            </select>
            {rule.kind === 'topic' && (
              <>
                <label>{t('ruleTopic')}</label>
                <input maxLength={300} value={rule.topic ?? ''} onChange={(e) => update(index, { topic: e.target.value })} />
              </>
            )}
            {rule.kind === 'keyword' && (
              <>
                <label>{t('ruleKeywords')}</label>
                <input
                  value={(rule.keywords ?? []).join(', ')}
                  onChange={(e) => update(index, { keywords: e.target.value.split(/[,،]/).map((k) => k.trim()) })}
                />
              </>
            )}
            {rule.kind === 'regex' && (
              <>
                <label>{t('rulePattern')}</label>
                <input dir="ltr" maxLength={200} value={rule.pattern ?? ''} onChange={(e) => update(index, { pattern: e.target.value })} />
              </>
            )}
            <label>{t('ruleInstruction')}</label>
            <input maxLength={500} value={rule.instruction ?? ''} onChange={(e) => update(index, { instruction: e.target.value })} />
            <button className="btn danger" onClick={() => setRules(rules.filter((_, i) => i !== index))}>
              {t('delete')}
            </button>
          </div>
        ))}
        {rules.length < 10 && (
          <button className="btn secondary" onClick={() => setRules([...rules, { kind: 'topic', topic: '', instruction: '' }])}>
            + {t('addRule')}
          </button>
        )}
        <button className="btn block" disabled={status === 'saving'} onClick={save}>
          {status === 'saved' ? t('saved') : t('save')}
        </button>
        {!['idle', 'saving', 'saved'].includes(status) && <p className="error">{status}</p>}
      </div>
    </>
  )
}
