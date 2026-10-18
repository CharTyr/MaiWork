import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../maiwork/console/static/js/goal-chat.js', import.meta.url), 'utf8');
let listeners = {}, sends = 0, resolveSend;
const input = {value: '保留草稿', dataset: {goalId: 'G-1'}, closest: () => input};
const box = {dataset: {goalId: 'G-1', groupId: 'one'}, querySelector: () => input};
const state = {g: 'one', detail: {type: 'goal', id: 'G-1'}, me: {role: 'admin'}};
const context = {state, admin: () => true, esc: s => String(s).replaceAll('<', '&lt;').replaceAll('"', '&quot;'),
  document: {addEventListener: (n, f) => listeners[n] = f, getElementById: () => box, querySelector: () => box},
  setInterval: () => 1, setTimeout: () => 1, console,
  api: async (method) => {if (method === 'POST') {sends++; await new Promise(r => resolveSend = r); return {};}
    return {messages: [{role: 'assistant', content: '<script>unsafe</script>'}], pending: [{id: 7, status: 'pending', summary: '暂停目标'}], running: false};}};
vm.createContext(context);
vm.runInContext(source.replace(/^import .*;$/gm, '').replaceAll('export ', '') + '\nthis.ui = {goalChatHTML, loadGoalChat, preserveGoalChatInput};', context);
listeners.input({target: input});
assert.match(context.ui.goalChatHTML({id: 'G-1'}), /保留草稿/);
await context.ui.loadGoalChat('G-1', 'one');
const html = context.ui.goalChatHTML({id: 'G-1'});
assert.ok(!html.includes('<script>'));
assert.match(html, /goal-chat-confirm/);
assert.match(html, /暂停目标/);
let prevented = false;
listeners.keydown({key: 'Enter', isComposing: true, target: input, preventDefault: () => prevented = true});
assert.equal(sends, 0);
assert.equal(prevented, false);
const button = {dataset: {act: 'goal-chat-send', id: 'G-1'}, closest: () => button};
const first = listeners.click({target: button});
await listeners.click({target: button});
assert.equal(sends, 1);
resolveSend();
await first;
assert.ok(!context.ui.goalChatHTML({id: 'G-1'}).includes('value="保留草稿"'));
context.document.activeElement = input;
input.selectionStart = 2;
input.selectionEnd = 4;
let focused = false, selection, redraws = 0;
input.focus = () => focused = true;
input.setSelectionRange = (...args) => selection = args;
context.ui.preserveGoalChatInput(() => redraws++);
assert.equal(redraws, 1);
assert.equal(focused, true);
assert.deepEqual(selection, [2, 4]);
listeners.compositionstart({target: input});
context.ui.preserveGoalChatInput(() => redraws++);
assert.equal(redraws, 1);
listeners.compositionend({target: input});
context.ui.preserveGoalChatInput(() => redraws++);
assert.equal(redraws, 2);
state.g = 'two';
assert.ok(!context.ui.goalChatHTML({id: 'G-1'}).includes('暂停目标'));
console.log('goal chat browser-state checks passed');
