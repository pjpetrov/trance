"""Sample FastAPI backend. Deliberately has a multi-hop call chain so the
curator has something non-trivial to walk."""

from fastapi import APIRouter, HTTPException

from .services import OrderService
from .db import get_session

router = APIRouter()
order_service = OrderService()


@router.get("/api/users/{user_id}/orders")
def get_user_orders(user_id: int, page: int = 1):
    """Entry point used by the frontend's fetchUserOrders()."""
    session = get_session()
    user = load_user(session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    orders = order_service.list_for_user(session, user_id, page)
    return {"user": serialize_user(user), "orders": [serialize_order(o) for o in orders]}


def load_user(session, user_id: int):
    row = session.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    return row


def serialize_user(user) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user["name"]}


def serialize_order(order) -> dict:
    return {
        "id": order["id"],
        "total": format_currency(order["total_cents"]),
        "status": order["status"],
    }


def format_currency(cents: int) -> str:
    return f"${cents / 100:,.2f}"


@router.post("/api/users/{user_id}/orders")
def create_order(user_id: int, payload: dict):
    session = get_session()
    order = order_service.create(session, user_id, payload)
    return serialize_order(order)
