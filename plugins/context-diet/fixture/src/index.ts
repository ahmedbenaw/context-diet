import { createOrder, orderKey } from "./models/order.ts";
import {
  createInventory,
  reserveOrder,
  type Inventory,
} from "./services/inventory.ts";
import { computeOrderTotalCents, formatCents } from "./services/pricing.ts";
import type { Order } from "./models/order.ts";
import { makeIdKey } from "./util/idKey.ts";

export interface Quote {
  orderKey: string;
  customerKey: string;
  totalCents: number;
  display: string;
  reservable: boolean;
}

export function quoteOrder(order: Order, inventory: Inventory): Quote {
  const totalCents = computeOrderTotalCents(order);
  return {
    orderKey: orderKey(order),
    customerKey: makeIdKey("customer", order.customerId),
    totalCents,
    display: formatCents(totalCents),
    reservable: reserveOrder(inventory, order).ok,
  };
}

/** Fixed sample used by the smoke script and by tests. No randomness, no clock. */
export function demoQuote(): Quote {
  const order = createOrder(
    "A-100",
    "Cust-7",
    [
      { sku: "widget", quantity: 2, unitPriceCents: 1500 },
      { sku: "gizmo", quantity: 1, unitPriceCents: 2000 },
    ],
    10,
  );
  const inventory = createInventory([
    { sku: "widget", onHand: 10, reserved: 0 },
    { sku: "gizmo", onHand: 4, reserved: 0 },
  ]);
  return quoteOrder(order, inventory);
}

if (process.argv[1]?.endsWith("index.ts") ?? false) {
  console.log(JSON.stringify(demoQuote()));
}
