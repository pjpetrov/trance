"""Business logic layer — one hop below the route handlers."""

PAGE_SIZE = 25


class OrderService:
    def list_for_user(self, session, user_id: int, page: int = 1):
        offset = self._offset(page)
        rows = session.query(
            "SELECT * FROM orders WHERE user_id = ? LIMIT ? OFFSET ?",
            (user_id, PAGE_SIZE, offset),
        )
        return [self._hydrate(row) for row in rows]

    def create(self, session, user_id: int, payload: dict):
        validate_payload(payload)
        row = session.insert("orders", {"user_id": user_id, **payload})
        return self._hydrate(row)

    def _offset(self, page: int) -> int:
        return max(0, (page - 1) * PAGE_SIZE)

    def _hydrate(self, row: dict) -> dict:
        row = dict(row)
        row["status"] = row.get("status", "pending")
        return row


def validate_payload(payload: dict) -> None:
    if "total_cents" not in payload:
        raise ValueError("total_cents is required")
