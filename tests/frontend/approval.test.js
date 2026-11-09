// tests/frontend/approval.test.js
/** Завершение подбора допускает общий approve; очистка использует versioned DELETE. */
import test from "node:test";
import assert from "node:assert/strict";
import {canApprove,clearSelectionRequest} from "../../src/web/static/js/state.js";

test("общее подтверждение доступно для готового подбора и спорных строк",()=>{
  assert.equal(canApprove("complete"),true);
  assert.equal(canApprove("ready_for_review"),true);
  for(const state of ["queued","polling","matching","failed","approved","expired",undefined])assert.equal(canApprove(state),false);
});
test("утверждение блокируется на время записи выбора",()=>{
  assert.equal(canApprove("ready_for_review",true),false);
  assert.equal(canApprove("complete",true),false);
});
test("крестик вызывает DELETE только соответствия с исходной revision",()=>{
  const request=clearSelectionRequest("e5c8176c-a2ef-46b0-b391-8e993b56083f",3,7);
  assert.equal(request.path,"/api/v1/mappings/e5c8176c-a2ef-46b0-b391-8e993b56083f/rows/3/selection?revision=7");
  assert.deepEqual(request.options,{method:"DELETE"});
});
