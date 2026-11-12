// tests/frontend/source-fields.test.js
/** Новые исходные поля и legacy отображаются дословно, без HTML преобразований. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {sourceAttributes} from '../../src/web/static/js/source-fields.js';
test('Сохраняет нули, переносы, HTML и формулы как исходный текст',()=>{
  assert.deepEqual(sourceAttributes({Brand:' ROSMA ',Article:'000007',Note:'<img>\n=SUM(A1)'}),['Марка:  ROSMA ','Артикул: 000007','Примечание: <img>\n=SUM(A1)']);
  assert.deepEqual(sourceAttributes({Caption:'Legacy'}),[]);
  assert.deepEqual(sourceAttributes({Brand:'',Article:'',Note:''}),[]);
});
