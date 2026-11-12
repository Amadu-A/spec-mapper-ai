# tests/fixtures/onec_mock_server.py
"""Изолированный контрактный mock 1С для Linux integration; не публиковать вне тестовой сети."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()
INITIAL = [
    {
        "guid": f"10000000-0000-4000-8000-{i:012d}",
        "code": f"TEST-{i:03}",
        "name": name,
        "full_name": name,
        "unit": "шт",
        "article": "",
        "brand": "",
        "manufacturer": "",
        "deleted": False,
    }
    for i, name in enumerate(["Клапан DN50 PN16", "Клапан DN40 PN16", "Насос 40 Вт"], 1)
]
RECORDS = {row["guid"]: row.copy() for row in INITIAL}


class Handler(BaseHTTPRequestHandler):
    """Уже существующий XML mock и согласованные JSON export/batch; control только для тестов."""

    def reply(self, payload, status=200, full=False):
        """Возвращает UTF-8 JSON; completion только у полного export."""
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if full:
            self.send_header("X-Catalog-Complete", "true")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        """Поддерживает XML регрессии и полный JSON без групп и pagination."""
        if self.path == "/catalog":
            body = (
                "<СправочникНоменклатура>"
                + "".join(
                    f'<Позиция код="{row["code"]}"><Наименование>{row["name"]}</Наименование>'
                    "<ЕдиницаИзмерения>шт</ЕдиницаИзмерения>"
                    "<ПометкаУдаления>Нет</ПометкаУдаления></Позиция>"
                    for row in INITIAL
                )
                + "</СправочникНоменклатура>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Export-Complete", "yes")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/catalog/export":
            with LOCK:
                self.reply({"items": list(RECORDS.values())}, full=True)
        else:
            self.reply({"error": "test_route_not_found"}, 404)

    def do_POST(self):
        """Batch возвращает deleted; отсутствие записи не обозначает физическое удаление."""
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= 100000:
            self.reply({"error": "test_body_limit"}, 413)
            return
        payload = json.loads(self.rfile.read(length))
        with LOCK:
            if self.path == "/catalog/items/batch":
                ids = payload["guids"]
                if len(ids) > 50:
                    self.reply({"error": "test_batch_limit"}, 422)
                else:
                    self.reply({"items": [RECORDS[guid] for guid in ids if guid in RECORDS]})
            elif self.path == "/test-control":
                if payload.get("reset"):
                    RECORDS.clear()
                    RECORDS.update({row["guid"]: row.copy() for row in INITIAL})
                for row in payload.get("upsert", []):
                    RECORDS[row["guid"]] = row
                for guid in payload.get("delete", []):
                    RECORDS.pop(guid, None)
                self.reply({"ok": True})
            else:
                self.reply({"error": "test_route_not_found"}, 404)

    def log_message(self, *args):
        """Не журналирует приватные реквизиты и HTTP тело даже в mock сервере."""


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
