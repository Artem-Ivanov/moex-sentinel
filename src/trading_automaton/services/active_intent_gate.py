"""Keep locally persisted active intents across position-state hydration."""


class ActiveIntentGateService:
    def __init__(self) -> None:
        self._intent_by_automation: dict[str, str] = {}
        self._tracked_intents: set[str] = set()

    def activate(self, automation_id: str, intent_id: str) -> None:
        self._intent_by_automation[automation_id] = intent_id

    def has_active_intent(self, automation_id: str) -> bool:
        return automation_id in self._intent_by_automation

    def clear(self, automation_id: str, intent_id: str) -> bool:
        if self._intent_by_automation.get(automation_id) != intent_id:
            return False
        del self._intent_by_automation[automation_id]
        return True

    def claim_tracking(self, intent_id: str) -> bool:
        if intent_id in self._tracked_intents:
            return False
        self._tracked_intents.add(intent_id)
        return True

    def is_tracked(self, intent_id: str) -> bool:
        return intent_id in self._tracked_intents

    def release_tracking(self, intent_id: str) -> bool:
        if intent_id not in self._tracked_intents:
            return False
        self._tracked_intents.remove(intent_id)
        return True
