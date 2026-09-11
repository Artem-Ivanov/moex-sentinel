from trading_automaton.services.active_intent_gate import ActiveIntentGateService


def test_wrong_intent_id_does_not_clear_active_gate() -> None:
    gate = ActiveIntentGateService()
    gate.activate("automation", "intent-1")

    cleared = gate.clear("automation", "intent-2")

    assert cleared is False
    assert gate.has_active_intent("automation") is True
