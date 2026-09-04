from app.diagnostic_main import app


def test_diagnostic_route_is_registered_without_replacing_normal_app():
    paths = {route.path for route in app.routes}
    assert "/health" in paths
    assert "/sessions/{session_id}/turn-packet" in paths
    assert "/sessions/{session_id}/context-stats" in paths
