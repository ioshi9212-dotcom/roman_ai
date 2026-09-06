from pathlib import Path


def test_custom_gpt_parenthetical_input_is_never_spoken():
    text = Path("gpt/custom_gpt_instructions.md").read_text(encoding="utf-8")
    assert "В `( )` = закрытый неразговорный канал" in text
    assert "НИКОГДА не речь" in text
    assert "дословно" in text
    assert "перефразирован" in text
    assert "Самостоятельные реплики POV не могут раскрывать содержание текущих скобок" in text


def test_pov_contract_blocks_literal_and_paraphrased_parenthetical_dialogue():
    text = Path("runtime/pov_contract.md").read_text(encoding="utf-8")
    assert "Содержимое текущих `( )` НИКОГДА не становится репликой POV" in text
    assert "повторять его вслух дословно" in text
    assert "перефразировать его в реплику" in text
    assert "использовать разрешение на самостоятельные бытовые реплики POV как способ озвучить смысл текущих скобок" in text
    assert "NPC могут реагировать на наблюдаемое действие из скобок" in text


def test_parenthetical_boundary_is_checked_before_commit():
    text = Path("runtime/pov_contract.md").read_text(encoding="utf-8")
    assert "содержимое текущих `( )` не стало речью POV ни дословно, ни перефразированно" in text
    assert "NPC не отреагировал на закрытую мысль/оценку/намерение из скобок" in text
