// tests/frontend/state.test.js
/** Frontend regression: terminal states, UUID, pagination и явный отказ в выборе. */
import test from "node:test";
import assert from "node:assert/strict";
import { newIdempotencyKey, pageInfo, selectionPayload, terminal, validUuid } from "../../src/web/static/js/state.js";
import { writeOptions } from "../../src/web/static/js/api.js";

test("Ревью завершает технический polling, ошибка не считается успехом", () => {
  assert.equal(terminal("ready_for_review"), true); assert.equal(terminal("failed"), true);
  assert.equal(terminal("matching"), false);
});
test("Нет подходящего: null сохраняется явно вместе с revision", () => {
  assert.deepEqual(selectionPayload(null, 5), { catalog_item_guid: null, revision: 5 });
  assert.throws(() => selectionPayload(undefined, 5));
});
test("UUID должен быть полным; короткий ID из макета не отправляется", () => {
  assert.equal(validUuid("a81f52e7-…-d9b3"), false);
  assert.equal(validUuid("5e2cfd2a-51e3-44ba-88bd-914a93aa4a1c"), true);
});
test("Пагинация использует filtered total и корректно завершает последнюю страницу", () => {
  assert.equal(pageInfo(25, 1, 26, 25).next, false);
  assert.equal(pageInfo(25, 1, 26, 25).previous, true);
  assert.equal(pageInfo(0, 0, 0, 25).next, false);
});
test("Создание задания сохраняет исходный JSON и передаёт Idempotency-Key", () => {
  const source = { items: [{ Caption: " <script>  DN50 ", Quantity: " 2,50 " }] };
  const request = writeOptions(source, "stable");
  assert.deepEqual(JSON.parse(request.body), source);
  assert.equal(request.headers["Idempotency-Key"], "stable");
});
test("Обычный HTTP в LAN не требует crypto.randomUUID", () => {
  const source = { getRandomValues: buffer => buffer.fill(42) };
  const key = newIdempotencyKey(source); assert.equal(validUuid(key), true);
  assert.equal(key[14], "4"); assert.equal(key[19], "a");
});
