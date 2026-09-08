from app.main import app
from app.runtime_access import runtime_documents


def test_runtime_documents_expose_only_real_runtime_files():
    documents = runtime_documents()
    assert set(documents) == {"rules", "scene_builder"}
    assert documents["rules"]
    assert documents["scene_builder"]


def test_removed_batch_routes_are_not_registered_in_fastapi():
    paths = {route.path for route in app.routes}
    assert "/sessions/{session_id}/turn-packet-batch/{packet_id}" not in paths
    assert "/sessions/{session_id}/audit-snapshot-batch/{audit_id}" not in paths
    assert "/sessions/{session_id}/turn-packet/{packet_id}/{chunk_index}" in paths
    assert "/sessions/{session_id}/audit-snapshot/{audit_id}/{chunk_index}" in paths
