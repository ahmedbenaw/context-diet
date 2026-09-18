import type { Order } from "../models/order.ts";
import { makeIdKey } from "../util/idKey.ts";

export interface StockRecord {
  sku: string;
  onHand: number;
  reserved: number;
}

export type Inventory = Map<string, StockRecord>;

export type ReserveResult =
  | { ok: true; reservedSkus: string[] }
  | { ok: false; shortages: string[] };

export function createInventory(records: readonly StockRecord[]): Inventory {
  const inventory: Inventory = new Map();
  for (const record of records) {
    inventory.set(makeIdKey("sku", record.sku), { ...record });
  }
  return inventory;
}

export function availableUnits(inventory: Inventory, sku: string): number {
  const record = inventory.get(makeIdKey("sku", sku));
  return record ? record.onHand - record.reserved : 0;
}

/** Reserves every line or none. Returns a result object, never throws. */
export function reserveOrder(inventory: Inventory, order: Order): ReserveResult {
  const shortages = order.lines
    .filter((line) => availableUnits(inventory, line.sku) < line.quantity)
    .map((line) => line.sku);

  if (shortages.length > 0) return { ok: false, shortages };

  const reservedSkus: string[] = [];
  for (const line of order.lines) {
    const record = inventory.get(makeIdKey("sku", line.sku));
    if (record) {
      record.reserved += line.quantity;
      reservedSkus.push(line.sku);
    }
  }
  return { ok: true, reservedSkus };
}
