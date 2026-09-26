import time

from conftest import png
from fastapi.testclient import TestClient

from safety_mask.api import create_app


def test_api_authentication_origin_validation_and_export(store):
    app = create_app("test-token", store)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.get("/api/images").status_code == 401
        assert client.get("/api/performance").status_code == 401
        headers = {"X-Session-Token": "test-token"}
        assert client.get("/api/performance", headers=headers).json()["requested_mode"] == "low_impact"
        assert client.put("/api/performance", headers=headers, json={"mode": "invalid"}).status_code == 422
        assert (
            client.get("/api/images", headers={**headers, "Origin": "https://external.invalid"}).status_code
            == 403
        )
        assert client.get("/api/images", headers={**headers, "Host": "malicious.invalid"}).status_code == 403
        assert client.get("/api/images", headers=headers).headers["cache-control"].startswith("no-store")
        bad = client.put("/api/settings", headers=headers, json={"categories": ["PRIVATE-INVALID-VALUE"]})
        assert bad.status_code == 422 and "PRIVATE-INVALID-VALUE" not in bad.text
        upload = client.post("/api/images", headers=headers, content=png())
        assert upload.status_code == 201
        image_id = upload.json()["id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            image = client.get("/api/images", headers=headers).json()[0]
            if image["status"] in {"review", "error"}:
                break
            time.sleep(0.01)
        request = {"images": [{"id": image_id, "revision": image["revision"]}], "format": "png"}
        assert client.post("/api/export", headers=headers, json=request).status_code == 409
        confirm = client.post(
            f"/api/images/{image_id}/confirm",
            headers=headers,
            json={"revision": image["revision"], "reviewed": True},
        )
        assert confirm.status_code == 200
        result = client.post("/api/export", headers=headers, json=request)
        assert result.status_code == 200 and result.headers["content-type"] == "image/png"
        assert "sanitized_001.png" in result.headers["content-disposition"]
        assert (
            client.post("/api/export", headers=headers, json={**request, "format": "zip"}).status_code == 422
        )
        assert (
            client.post(
                "/api/export", headers=headers, json={**request, "images": request["images"] * 2}
            ).status_code
            == 422
        )
        assert client.post("/api/images", headers=headers, content=b"invalid").status_code == 400
