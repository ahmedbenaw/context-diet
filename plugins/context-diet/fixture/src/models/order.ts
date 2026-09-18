import { makeIdKey } from "../util/idKey.ts";

export interface OrderLine {
  sku: string;
  quantity: number;
  unitPriceCents: number;
}

export interface Order {
  id: string;
  customerId: string;
  lines: OrderLine[];
  discountPercent: number;
}

export function createOrder(
  id: string,
  customerId: string,
  lines: readonly OrderLine[],
  discountPercent = 0,
): Order {
  return { id, customerId, lines: [...lines], discountPercent };
}

export function orderKey(order: Order): string {
  return makeIdKey("order", order.id);
}

export function lineCount(order: Order): number {
  return order.lines.length;
}
