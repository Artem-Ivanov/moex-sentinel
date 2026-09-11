"""Keep locally persisted active intents across position-state hydration."""


class ActiveIntentGateService:
    def __init__(self) -> None:
        self._intent_by_automation: dict[str, str] = {}

    def activate(self, automation_id: str, intent_id: str) -> None:
        self._intent_by_automation[automation_id] = intent_id

    def has_active_intent(self, automation_id: str) -> bool:
        return automation_id in self._intent_by_automation

    def clear(self, automation_id: str, intent_id: str) -> bool:
        if self._intent_by_automation.get(automation_id) != intent_id:
            return False
        del self._intent_by_automation[automation_id]
        return True
