from app.runtime_access import runtime_documents


def test_scene_time_density_controls_pacing():
    builder = runtime_documents()["scene_builder"]
    assert "Сжимай время, когда ничего содержательно не меняется." in builder
    assert "Если ситуация, отношения, разговор или взаимодействие развиваются, веди сцену непрерывно и не проматывай её." in builder
    assert "Чем плотнее взаимодействие и изменения внутри сцены, тем меньше скачок времени." in builder
