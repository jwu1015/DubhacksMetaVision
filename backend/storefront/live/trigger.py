"""When an object has been on screen long enough: two timers per uid, one for its quick label and one
for its Look. Each fires once per uid; leaving the screen resets both. Pure: no network, no clock of its own."""


class Dwell:
    def __init__(self, label_ms: int, look_ms: int):
        self.label_s, self.look_s = label_ms / 1000, look_ms / 1000
        self._since: dict[str, float] = {}  # uid -> when it came on screen, this time
        self._labeled: set[str] = set()
        self._looked: set[str] = set()

    def update(self, on_screen: list[str], now: float) -> tuple[list[str], list[str]]:
        """Take the uids on screen in a tracking result; return those due a quick label, and those due a Look."""
        self._since = {uid: self._since.get(uid, now) for uid in on_screen}
        label = [u for u in on_screen if u not in self._labeled and now - self._since[u] >= self.label_s]
        look = [u for u in on_screen if u not in self._looked and now - self._since[u] >= self.look_s]
        self._labeled.update(label)
        self._looked.update(look)
        return label, look

    def looked(self, uid: str):
        """Its Look started some other way (the user picked it): the timer won't start another."""
        self._looked.add(uid)

    def reset(self):
        self._since.clear()
        self._labeled.clear()
        self._looked.clear()
