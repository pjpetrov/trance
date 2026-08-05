import { fetchUserOrders, Order } from "./api";

export function OrderList({ userId }: { userId: number }) {
  const orders = useOrders(userId);
  return orders.map(renderRow);
}

function useOrders(userId: number): Order[] {
  const [orders, setOrders] = useState<Order[]>([]);
  useEffect(() => {
    fetchUserOrders(userId).then(setOrders);
  }, [userId]);
  return orders;
}

function renderRow(order: Order) {
  return { key: order.id, label: `${order.total} — ${order.status}` };
}

declare function useState<T>(init: T): [T, (v: T) => void];
declare function useEffect(fn: () => void, deps: unknown[]): void;
