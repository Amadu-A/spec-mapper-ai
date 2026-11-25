// tests/frontend/job-lifecycle.test.js
/** Правила logical операции и правдивых метрик; видимый UI проверяется Chromium E2E. */
import test from "node:test";
import assert from "node:assert/strict";
import {SubmissionState,fileProgress,jobStage,knownState,progressView} from "../../src/web/static/js/job-state.js";

test("Десять кликов и 202 не снимают logical lock и не создают новый ключ",()=>{
  let count=0;const state=new SubmissionState(()=>`key-${++count}`),key=state.begin("same-package");
  for(let i=0;i<10;i++)assert.equal(state.begin("same-package"),null);
  state.accepted();assert.equal(state.key,key);assert.equal(state.begin("same-package"),null);
  state.reset();assert.notEqual(state.begin("same-package"),key);
});
test("Потерянный ответ повторяется прежним ключом, другой payload требует явного нового",()=>{
  let count=0;const state=new SubmissionState(()=>`key-${++count}`),key=state.begin("file-a");
  state.failed(0);assert.throws(()=>state.begin("file-b"));assert.equal(state.begin("file-a"),key);
  state.failed(503);assert.equal(state.begin("file-a"),key);
  state.failed(422);state.changed();assert.notEqual(state.begin("file-b"),key);
});
test("Неизвестные totals не становятся 0/0 или фиктивным процентом",()=>{
  const view=progressView({status:"queued",extracting_applicable:true,total_pages:null,total_items:null});
  assert.equal(view.percent,null);assert.ok(!view.text.includes("0/0"));
  assert.equal(progressView({status:"extracting",extracting_applicable:true,processed_pages:6,total_pages:12}).percent,50);
  assert.equal(progressView({status:"matching",total_items:10,processed_items:3}).percent,30);
  assert.equal(progressView({status:"matching",total_items:0,processed_items:0}).percent,null);
});
test("JSON пропускает extractor; очередь Mapper и upstream различимы",()=>{
  assert.match(progressView({extracting_applicable:false,total_items:2}).text,/не требуется/);
  assert.notEqual(jobStage({status:"queued",stage:"queued"}),jobStage({status:"extracting",stage:"upstream_queued"}));
  assert.equal(knownState("unrecognized"),false);assert.match(jobStage({status:"unrecognized"}),/Неизвестное/);
});
test("Per-file progress считает выбранные физические страницы и не скрывает failed",()=>{
  const file={selected_pages:[2,4,5],pages:[{pdf_page:1,state:"completed"},{pdf_page:2,state:"completed"},{pdf_page:4,state:"running"}]};
  assert.deepEqual(fileProgress(file),{done:1,total:3,state:"running"});
  file.pages.push({pdf_page:5,state:"failed"});assert.equal(fileProgress(file).state,"failed");
});
