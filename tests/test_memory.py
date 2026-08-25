import tempfile
from pathlib import Path

from app import storage


def setup_temp_storage(tmp: str):
    storage.DATA_DIR = Path(tmp)
    storage.LIBRARY_DIR = storage.DATA_DIR / "library"
    storage.SESSIONS_DIR = storage.DATA_DIR / "sessions"
    storage.ensure_dirs()


def test_character_memory_stays_separate_and_survives_handoff():
    with tempfile.TemporaryDirectory() as tmp:
        setup_temp_storage(tmp)

        novel = {
            "novel_id": "memory_test",
            "title": "Memory Test",
            "version": 1,
            "novel": {},
            "characters": [],
            "lore": {},
        }
        meta = storage.create_session(novel)
        session_id = meta["session_id"]

        first = storage.commit_turn(
            session_id,
            {
                "user_input": "Rina tells Liam where she was born. Aiden is absent.",
                "scene_output": "Liam hears the answer.",
                "extracted": {
                    "knowledge_add": [
                        {
                            "character_id": "liam",
                            "fact_id": "rina_birthplace",
                            "content": "Rina said she was born in the West",
                            "source": "rina",
                        }
                    ],
                    "experiences_add": [
                        {
                            "character_id": "liam",
                            "event_id": "heard_birthplace_answer",
                            "summary": "Liam heard Rina answer where she was born",
                            "role": "heard",
                        }
                    ],
                    "dialogue_memory_add": [
                        {
                            "topic_id": "rina_birthplace_question",
                            "participants": ["liam", "rina"],
                            "asked_by": "liam",
                            "asked_to": "rina",
                            "question": "Where were you born?",
                            "answer": "In the West",
                            "status": "answered",
                        }
                    ],
                },
            },
        )
        assert first["turn_number"] == 1

        liam = storage.get_character_memory(session_id, "liam")
        aiden = storage.get_character_memory(session_id, "aiden")

        assert any(x["fact_id"] == "rina_birthplace" for x in liam["knowledge"])
        assert any(x["event_id"] == "heard_birthplace_answer" for x in liam["experiences"])
        assert any(x["topic_id"] == "rina_birthplace_question" and x["status"] == "answered" for x in liam["dialogue_memory"])
        assert not any(x.get("fact_id") == "rina_birthplace" for x in aiden["knowledge"])

        for turn in range(2, 61):
            result = storage.commit_turn(
                session_id,
                {
                    "user_input": f"user {turn}",
                    "scene_output": f"scene {turn}",
                    "extracted": {},
                },
            )
            if result["audit_due"]:
                start_turn, end_turn = result["audit_range"]
                storage.commit_audit(
                    session_id,
                    {
                        "start_turn": start_turn,
                        "end_turn": end_turn,
                        "repairs": {},
                        "notes": [],
                    },
                )

        package = storage.build_resume_package(session_id)
        assert any(x["fact_id"] == "rina_birthplace" for x in package["memory"]["liam"]["knowledge"])
        assert not any(x.get("fact_id") == "rina_birthplace" for x in package["memory"].get("aiden", {}).get("knowledge", []))
        assert any(x["topic_id"] == "rina_birthplace_question" for x in package["memory"]["liam"]["dialogue_memory"])

        storage.confirm_resume(session_id, package["resume_token"])
        liam_after = storage.get_character_memory(session_id, "liam")
        assert any(x["fact_id"] == "rina_birthplace" for x in liam_after["knowledge"])
