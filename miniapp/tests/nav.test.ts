import assert from 'node:assert/strict'
import test from 'node:test'
import { currentTab, initialStack, reduce, type Stack } from '../src/nav.ts'

const home: Stack = [{ page: 'bots' }]

test('push adds a page on top', () => {
  assert.deepEqual(reduce(home, { type: 'push', route: { page: 'bot', id: 3 } }), [
    { page: 'bots' },
    { page: 'bot', id: 3 },
  ])
})

test('pushing a tab root switches tabs instead of stacking', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'mine', id: 2 }]
  assert.deepEqual(reduce(deep, { type: 'push', route: { page: 'credits' } }), [{ page: 'credits' }])
})

test('back pops one page and never empties the stack', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'bot', id: 3 }, { page: 'mine', id: 3 }]
  assert.deepEqual(reduce(deep, { type: 'back' }), [{ page: 'bots' }, { page: 'bot', id: 3 }])
  assert.deepEqual(reduce(home, { type: 'back' }), home)
})

test('replace swaps the top page', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'new' }]
  assert.deepEqual(reduce(deep, { type: 'replace', route: { page: 'bot', id: 9 } }), [
    { page: 'bots' },
    { page: 'bot', id: 9 },
  ])
})

test('replace on a tab root keeps a tab root', () => {
  assert.deepEqual(reduce(home, { type: 'replace', route: { page: 'bot', id: 1 } }), [
    { page: 'bots' },
    { page: 'bot', id: 1 },
  ])
})

test('tab resets to that tab root', () => {
  const deep: Stack = [{ page: 'bots' }, { page: 'bot', id: 3 }]
  assert.deepEqual(reduce(deep, { type: 'tab', tab: 'tasks' }), [{ page: 'tasks' }])
})

test('currentTab is the root of the stack', () => {
  assert.equal(currentTab([{ page: 'settings' }]), 'settings')
  assert.equal(currentTab([{ page: 'bots' }, { page: 'new' }]), 'bots')
})

test('initialStack honours the existing deep links', () => {
  assert.deepEqual(initialStack('', undefined), home)
  assert.deepEqual(initialStack('?bot=4', undefined), [{ page: 'bots' }, { page: 'bot', id: 4 }])
  assert.deepEqual(initialStack('?mine=5', undefined), [{ page: 'bots' }, { page: 'mine', id: 5 }])
  assert.deepEqual(initialStack('', 'bot_6'), [{ page: 'bots' }, { page: 'bot', id: 6 }])
  assert.deepEqual(initialStack('?bot=abc', 'nonsense'), home)
})
