# tests/test_v4_live.py
"""Реальные PostgreSQL/Qdrant/RabbitMQ/shared embedding/HTTP API с отдельным mock 1С."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from src.application.catalog_management import CONFIG_FIELDS
from src.bootstrap import build_container
from src.config import Settings

pytestmark = pytest.mark.live


async def test_live_v4_json_guid_webhook_and_extended_specification():
    """Только временный stack: JSON → стабильные GUID → durable webhook → готовый extended JSON."""
    settings = Settings()
    if not settings.database_host.startswith("mapper-v2-test-v3-v4-"):
        pytest.skip("V4 mutation live разрешён только в изолированном mapper-v2-test-v3-v4-* stack")
    container = build_container(settings)
    admin = {"X-Catalog-Key": settings.catalog_admin_key.get_secret_value()}
    api_key = settings.api_keys["onec"].get_secret_value()
    try:
        async with httpx.AsyncClient(
            base_url=settings.app_public_base_url, timeout=60, trust_env=False
        ) as client:
            reset = await client.post("http://onec-fixture:8000/test-control", json={"reset": True})
            assert reset.status_code == 200
            response = await client.get("/api/v1/catalog/sync-settings", headers=admin)
            assert response.status_code == 200
            previous = response.json()
            config = {key: previous[key] for key in CONFIG_FIELDS} | {
                "revision": previous["revision"],
                "enabled": False,
                "source_type": "http_onec",
                "url": "http://onec-fixture:8000/catalog/export",
                "expected_format": "json",
                "auth_type": "none",
                "completion_header": "X-Catalog-Complete",
                "completion_value": "true",
            }
            assert (
                await client.put("/api/v1/catalog/sync-settings", json=config, headers=admin)
            ).status_code == 200
            probe = await client.post("/api/v1/catalog/connection-test", headers=admin)
            assert probe.status_code == 200 and probe.json()["probe"]["state"] == "confirmed"
            before = (
                await client.get("/api/v1/catalog/status", headers={"X-API-Key": api_key})
            ).json()
            old_ids = await container.catalog.guids(before["version"])
            imported = await client.post(
                "/api/v1/catalog/imports",
                json={"source_type": "http_onec"},
                headers=admin | {"Idempotency-Key": str(uuid4())},
            )
            assert imported.status_code == 202, imported.text
            for _ in range(180):
                state = (
                    await client.get(
                        "/api/v1/catalog/imports/" + imported.json()["job_id"], headers=admin
                    )
                ).json()
                if state["status"] in {"complete", "failed"}:
                    break
                await asyncio.sleep(0.25)
            assert state["status"] == "complete", state
            status = await container.catalog.status()
            assert sorted(await container.catalog.guids(status["version"])) == sorted(old_ids)
            assert state["metrics"]["embedded_count"] == 0
            assert state["metrics"]["metadata_only_count"] == 3
            cards = (await client.get("http://onec-fixture:8000/catalog/export")).json()["items"]
            changed = {**cards[0], "article": "00000006836", "brand": "ROSMA-" + uuid4().hex[:8]}
            assert (
                await client.post(
                    "http://onec-fixture:8000/test-control", json={"upsert": [changed]}
                )
            ).status_code == 200
            event = {
                "event_id": str(uuid4()),
                "occurred_at": datetime.now(UTC).isoformat(),
                "changes": [{"guid": changed["guid"], "action": "upsert"}],
            }
            headers = {"X-Webhook-Key": settings.onec_catalog_webhook_key.get_secret_value()}
            accepted = await client.post("/api/v1/catalog/events", json=event, headers=headers)
            assert accepted.status_code == 202
            assert (
                await client.post("/api/v1/catalog/events", json=event, headers=headers)
            ).status_code == 200
            for _ in range(200):
                journal = (await client.get("/api/v1/catalog/events", headers=admin)).json()
                matched = [row for row in journal["items"] if row["event_id"] == event["event_id"]]
                if matched and matched[0]["state"] == "applied":
                    break
                await asyncio.sleep(0.25)
            assert matched and matched[0]["state"] == "applied", journal
            status = await container.catalog.status()
            assert status["embedded_count"] == 1 and status["count"] == 3
            assert await container.index.count(status["collection"]) == 3
            found = await container.catalog.search("00000006836", 20)
            assert len(found) == 1 and found[0]["onec_guid"] == changed["guid"]
            assert found[0]["guid"] in old_ids
            row = {
                "Caption": changed["full_name"],
                "Units": "шт",
                "Quantity": "0,5",
                "UserComment1": "LIVE-V4",
                "Pages": "1",
                "Brand": changed["brand"],
                "Article": "00000006836",
                "Note": "Уточнить при монтаже",
            }
            document = {"file_name": "live-v4.pdf", "items": [row, row.copy()]}
            owner = {"X-API-Key": api_key, "Idempotency-Key": str(uuid4())}
            submitted = await client.post(
                "/api/v1/mappings/import-json", json=document, headers=owner
            )
            assert submitted.status_code == 202
            prefix = "/api/v1/mappings/" + submitted.json()["job_id"]
            for _ in range(180):
                mapped = (await client.get(prefix, headers=owner)).json()
                if mapped["status"] in {"complete", "ready_for_review", "failed"}:
                    break
                await asyncio.sleep(0.25)
            assert mapped["status"] != "failed", mapped
            assert (await client.get(prefix + "/source.json", headers=owner)).json() == document
            result = (await client.get(prefix + "/result", headers=owner)).json()
            assert (
                result["file_name"] == document["file_name"] and result["items"][0]["source"] == row
            )
            assert (
                await client.post(
                    prefix + "/approve", json={"revision": mapped["revision"]}, headers=owner
                )
            ).status_code == 200
            approved = (await client.get(prefix + "/approved.json", headers=owner)).json()
            assert (
                approved["file_name"] == document["file_name"]
                and approved["items"][0]["Article"] == "00000006836"
            )
            assert (await client.get(prefix + "/result.xlsx", headers=owner)).status_code == 200
    finally:
        await container.close()
