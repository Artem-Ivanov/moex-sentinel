from trading_automaton.services.active_intent_gate import ActiveIntentGateService


def test_tracking_claim_is_exclusive_until_matching_owner_releases_it():
    gate = ActiveIntentGateService()

    assert gate.claim_tracking("intent-1") is True
    assert gate.is_tracked("intent-1") is True
    assert gate.claim_tracking("intent-1") is False
    assert gate.release_tracking("intent-2") is False
    assert gate.release_tracking("intent-1") is True
    assert gate.is_tracked("intent-1") is False
    assert gate.claim_tracking("intent-1") is True
