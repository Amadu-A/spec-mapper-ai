// src/web/static/js/docs.js
/** Локальный OpenAPI viewer: схемы и маршруты реального backend, без внешних ресурсов и секретов. */
import { element } from "./dom.js";
const list = document.querySelector("[data-api-routes]");
const status = document.querySelector("[data-api-status]");
let routes = [];
function render() {
  /** Фильтрует документированные операции без вставки недоверенного HTML. */
  const query = document.querySelector("[data-api-filter]").value.toLowerCase();
  const found = routes.filter(route => `${route.method} ${route.path} ${route.operation.description || ""}`.toLowerCase().includes(query));
  list.replaceChildren(...found.map(route => {
    const details = element("details", "api-docs__endpoint");
    details.append(element("summary", "", `${route.method.toUpperCase()} ${route.path} · ${route.operation.summary || ""}`));
    details.append(element("p", "", route.operation.description || ""));
    details.append(element("pre", "", JSON.stringify({ parameters: route.operation.parameters || [], requestBody: route.operation.requestBody, responses: route.operation.responses, security: route.operation.security }, null, 2)));
    return details;
  }));
  status.textContent = `Показано операций: ${found.length} из ${routes.length}`;
}
document.querySelector("[data-api-filter]").addEventListener("input", render);
try {
  const response = await fetch("/api/openapi.json", { credentials: "same-origin" });
  if (!response.ok) throw new Error("Схема API недоступна");
  const schema = await response.json();
  routes = Object.entries(schema.paths).flatMap(([path, methods]) => Object.entries(methods).filter(([method]) => ["get", "post", "put", "delete", "patch"].includes(method)).map(([method, operation]) => ({ path, method, operation })));
  document.querySelector("[data-api-schemas]").textContent = JSON.stringify(schema.components?.schemas || {}, null, 2);
  render();
} catch (error) { status.textContent = error.message; }
