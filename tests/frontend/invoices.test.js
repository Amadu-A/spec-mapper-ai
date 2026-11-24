// tests/frontend/invoices.test.js
/** Invoice UI: отдельные состояния, lossless отображение и multipart без JSON Content-Type. */
import test from "node:test";
import assert from "node:assert/strict";
import {api} from "../../src/web/static/js/api.js";
import {invoiceApprovalPayload,invoiceCanApprove,invoiceSelectionSource,invoiceValue} from "../../src/web/static/js/invoice-state.js";

test("Invoice approve требует каждое серверное предупреждение и не меняет источник",()=>{
  const warnings=[{document_id:"0:0"},{document_id:"1:0"}];
  assert.equal(invoiceCanApprove("awaiting_review",warnings,new Set(["0:0"])),false);
  const acknowledged=new Set(["1:0","0:0"]);
  assert.equal(invoiceCanApprove("awaiting_review",warnings,acknowledged),true);
  assert.equal(invoiceCanApprove("approved",warnings,acknowledged),false);
  assert.equal(invoiceCanApprove("awaiting_review",warnings,acknowledged,true),false);
  assert.deepEqual(invoiceApprovalPayload(4,acknowledged),{revision:4,acknowledged_document_warnings:["0:0","1:0"]});
});

test("Общий dialog показывает подтверждённые invoice-поля и сохраняет нули/null",()=>{
  const row={source:{caption:"Товар",quantity:"001.00",units:null,brand:null,manufacturer:"Марка",article:null,article_candidate:"000999",source_pages:[2]},file:{source_file:"one.pdf"},document:{number:"0001"}};
  const before=structuredClone(row),view=invoiceSelectionSource(row);
  assert.equal(view.Article,"");assert.equal(view.Brand,"Марка");assert.equal(view.Quantity,"001.00");
  assert.equal(view.Units,"");assert.equal(view.Pages,"2");assert.deepEqual(row,before);
  assert.equal(invoiceValue(null),"—");assert.equal(invoiceValue("000123"),"000123");
});

test("Общий API различает multipart и JSON, сохраняя стандартные ошибки",async()=>{
  const original=globalThis.fetch,calls=[];
  globalThis.fetch=async(path,options)=>{calls.push(options);return new Response(JSON.stringify({ok:true}),{headers:{"Content-Type":"application/json"}});};
  try{
    const body=new FormData();body.append("page_selection","{}");
    await api("/api/v1/invoices",{method:"POST",body});
    assert.equal(calls[0].headers.has("Content-Type"),false);
    await api("/api/v1/invoices/import-json",{method:"POST",body:"{}"});
    assert.equal(calls[1].headers.get("Content-Type"),"application/json");
    assert.equal(calls[1].credentials,"same-origin");
  }finally{globalThis.fetch=original;}
});
