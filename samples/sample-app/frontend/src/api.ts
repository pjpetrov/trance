// Frontend API layer. The fetch() URLs here are what the PHASE 2 linker will
// match against the backend's route decorators.

const BASE = "/api";

export interface Order {
  id: number;
  total: string;
  status: string;
}

export async function fetchUserOrders(userId: number, page = 1): Promise<Order[]> {
  const res = await fetch(`${BASE}/users/${userId}/orders?page=${page}`);
  const body = await handleResponse(res);
  return body.orders.map(normalizeOrder);
}

export async function createOrder(userId: number, totalCents: number): Promise<Order> {
  const res = await fetch(`${BASE}/users/${userId}/orders`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ total_cents: totalCents }),
  });
  return normalizeOrder(await handleResponse(res));
}

async function handleResponse(res: Response): Promise<any> {
  if (!res.ok) {
    throw new ApiError(res.status, await res.text());
  }
  return res.json();
}

function normalizeOrder(raw: any): Order {
  return { id: raw.id, total: raw.total, status: raw.status ?? "pending" };
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}
