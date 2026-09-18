import type { Order, OrderLine } from "../models/order.ts";

/** Extended price for a single line, in integer cents. */
export function computeLineTotalCents(line: OrderLine): number {
  return line.unitPriceCents * line.quantity;
}

/**
 * Apply a whole-percent discount to an amount in cents.
 * Half-cent results round half up, so 1250 at 15% is 1063, not 1062.
 */
export function applyDiscountPercent(cents: number, percent: number): number {
  if (percent <= 0) return cents;
  const capped = Math.min(percent, 100);
  return Math.round((cents * (100 - capped)) / 100);
}

/** Subtotal of every line, then the order-level discount. */
export function computeOrderTotalCents(order: Order): number {
  const subtotal = order.lines.reduce(
    (sum, line) => sum + computeLineTotalCents(line),
    0,
  );
  return applyDiscountPercent(subtotal, order.discountPercent);
}

/** Display-only. Never feed the result of this back into arithmetic. */
export function formatCents(cents: number): string {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  const minor = String(abs % 100).padStart(2, "0");
  return `${sign}${Math.floor(abs / 100)}.${minor}`;
}
