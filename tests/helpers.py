from app import storage


def commit_with_packet(session_id: str, user_input: str, scene_output: str, extracted=None):
    packet = storage.prepare_turn_packet(session_id, user_input)
    for index in range(packet["chunk_count"]):
        chunk = storage.get_turn_packet_chunk(session_id, packet["packet_id"], index)
    assert chunk["all_chunks_read"] is True
    return storage.commit_turn(
        session_id,
        {
            "user_input": user_input,
            "scene_output": scene_output,
            "extracted": extracted or {},
        },
    )


def audit_if_due(session_id: str, result):
    if result.get("audit_due"):
        start_turn, end_turn = result["audit_range"]
        snapshot = storage.get_audit_snapshot(session_id)
        assert snapshot["audit_range"] == [start_turn, end_turn]
        return storage.commit_audit(
            session_id,
            {"start_turn": start_turn, "end_turn": end_turn, "repairs": {}, "notes": []},
        )
    return None
