import assert from 'node:assert/strict'
import test from 'node:test'
import { createBottomButtons, type BottomButtonAdapter, type Slot } from '../src/bottomButtons.ts'

function recorder() {
  const calls: string[] = []
  const adapter: BottomButtonAdapter = {
    show: (slot: Slot, o) => calls.push(`show ${slot} ${o.text}${o.disabled ? ' disabled' : ''}${o.loading ? ' loading' : ''}`),
    hide: (slot: Slot) => calls.push(`hide ${slot}`),
  }
  return { calls, adapter }
}

test('set shows the button and null hides it', () => {
  const { calls, adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', null)
  assert.deepEqual(calls, ['show main Save', 'hide main'])
  assert.equal(buttons.get('main'), null)
})

test('unchanged options do not touch Telegram again', () => {
  const { calls, adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', { text: 'Save', onClick: () => {} })
  buttons.set('main', { text: 'Save', onClick: () => {}, loading: true })
  assert.deepEqual(calls, ['show main Save', 'show main Save loading'])
})

test('click runs the latest handler', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  const hits: string[] = []
  buttons.set('secondary', { text: 'Back', onClick: () => hits.push('old') })
  buttons.set('secondary', { text: 'Back', onClick: () => hits.push('new') })
  buttons.click('secondary')
  assert.deepEqual(hits, ['new'])
})

test('click is ignored while disabled, loading or hidden', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  let hits = 0
  buttons.set('main', { text: 'Go', onClick: () => hits++, disabled: true })
  buttons.click('main')
  buttons.set('main', { text: 'Go', onClick: () => hits++, loading: true })
  buttons.click('main')
  buttons.set('main', null)
  buttons.click('main')
  assert.equal(hits, 0)
})

test('subscribers hear every change and can leave', () => {
  const { adapter } = recorder()
  const buttons = createBottomButtons(adapter)
  let heard = 0
  const leave = buttons.subscribe(() => heard++)
  buttons.set('main', { text: 'A', onClick: () => {} })
  leave()
  buttons.set('main', null)
  assert.equal(heard, 1)
})
