// src/web/static/js/api.js
/** Единый HTTP-клиент: cookie/CSRF, безопасная диагностика и отмена устаревших запросов. */
let csrf = "";
let operatorCsrf = "";

export class ApiError extends Error {
  /** Сохраняет безопасный код backend без вывода исходного запроса. */
  constructor(message, code, status) { super(message); this.code = code; this.status = status; }
}

export async function api(path, options = {}) {
  /** Выполняет запрос с HttpOnly cookie; секретные API-ключи в frontend отсутствуют. */
  const headers = new Headers(options.headers);
  if (options.body !== undefined) headers.set("Content-Type", "application/json");
  if (csrf) headers.set("X-CSRF-Token", csrf);
  if (operatorCsrf) headers.set("X-Operator-CSRF", operatorCsrf);
  let response;
  try { response = await fetch(path, { ...options, headers, credentials: "same-origin" }); }
  catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError("Нет соединения с сервисом. Проверьте сеть и повторите действие.", "network", 0);
  }
  const data = await response.json();
  if (!response.ok) throw new ApiError(data.error?.message || "Запрос не выполнен", data.error?.code, response.status);
  return data;
}

export async function initializeSession() {
  /** Получает только CSRF своей сессии; upstream credentials остаются на сервере. */
  csrf = (await api("/api/v1/session")).csrf;
}

export async function initializeOperator() {
  /** Получает короткую серверную роль без API-ключей в браузере. */
  const session = await api("/api/v1/operator/session");
  operatorCsrf = session.csrf || "";
  return session;
}

export function writeOptions(body, idempotencyKey) {
  /** Строит единый payload записи и явно передаёт ключ идемпотентности нового задания. */
  return { method: "POST", body: JSON.stringify(body), headers: { "Idempotency-Key": idempotencyKey } };
}
