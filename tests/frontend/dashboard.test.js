// tests/frontend/dashboard.test.js
/** Честные отсутствующие данные, UTC и незелёные неподтверждённые интеграции. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {percent,date,tone,changeText} from '../../src/web/static/js/dashboard-state.js';
test('нет данных отличается от нулевого процента',()=>{assert.equal(percent(null),'Нет данных');assert.notEqual(percent(0),'Нет данных');});
test('формат времени явно UTC',()=>{assert.match(date(1793977200),/UTC$/);assert.equal(date(null),'—');});
test('ожидание контракта и отсутствие обращений не зелёные',()=>{for(const state of ['awaiting_contract','no_activity','key_missing','check_required'])assert.equal(tone(state),'pending');assert.equal(tone('confirmed'),'good');});
test('нулевые изменения отличаются от отсутствующего отчёта',()=>{assert.match(changeText({}),/Нет отчёта/);assert.match(changeText({added_count:0,changed_count:0,removed_count:0,unchanged_count:4}),/Добавлено: 0/);});
