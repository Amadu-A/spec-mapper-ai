// src/web/static/js/swagger.js
/** Инициализация официального локального Swagger UI; без CDN, внешнего validator и server secrets. */
function initializeSwagger() {
  /** Использует фиксированную same-origin схему и не сохраняет введённые пользователем ключи. */
  if(!globalThis.SwaggerUIBundle)return;
  globalThis.SwaggerUIBundle({
    url:"/api/openapi.json",dom_id:"#swagger-ui",deepLinking:true,
    validatorUrl:null,persistAuthorization:false,queryConfigEnabled:false,
    docExpansion:"none",defaultModelsExpandDepth:-1,displayRequestDuration:true,
  });
}
if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",initializeSwagger,{once:true});
else initializeSwagger();
