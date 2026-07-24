def test_health_check_ok(client):
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"


def test_health_check_uses_injected_db_not_real_database_file(client, tmp_path, monkeypatch):
    """Regression test: /health must go through Depends(get_db) so the test's
    dependency_overrides apply, instead of touching the real data/defect_tracker.db
    directly via a module-level engine reference."""
    from app import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    assert not (tmp_path / "defect_tracker.db").exists()
