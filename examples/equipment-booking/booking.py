class SlotTaken(Exception):
    pass


class BookingSlot:
    # One fixed slot, exercised through sequential calls.

    def __init__(self):
        self._statuses: dict[int, str] = {}

    def submit(self) -> tuple[int, str]:
        request_id = len(self._statuses) + 1
        self._statuses[request_id] = "pending"
        return request_id, self._statuses[request_id]

    def status(self, request_id: int) -> str:
        return self._statuses[request_id]

    def confirm(self, request_id: int, *, is_admin: bool) -> None:
        if not is_admin:
            raise PermissionError("Only an administrator can confirm a request")
        if self.status(request_id) == "confirmed":
            return
        if "confirmed" in self._statuses.values():
            raise SlotTaken("The slot is already booked")
        self._statuses[request_id] = "confirmed"
