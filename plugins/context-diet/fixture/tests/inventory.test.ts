import { describe, expect, it } from "vitest";
import { createOrder } from "../src/models/order.ts";
import {
  availableUnits,
  createInventory,
  reserveOrder,
} from "../src/services/inventory.ts";

const stock = () =>
  createInventory([
    { sku: "widget", onHand: 10, reserved: 2 },
    { sku: "gizmo", onHand: 1, reserved: 0 },
  ]);

describe("availableUnits", () => {
  it("subtracts reserved from on hand", () => {
    expect(availableUnits(stock(), "widget")).toBe(8);
  });

  it("normalises the sku before lookup", () => {
    expect(availableUnits(stock(), " WIDGET ")).toBe(8);
  });

  it("reports zero for an unknown sku", () => {
    expect(availableUnits(stock(), "nope")).toBe(0);
  });
});

describe("reserveOrder", () => {
  it("reserves every line when stock allows", () => {
    const inventory = stock();
    const order = createOrder("A-2", "c-1", [
      { sku: "widget", quantity: 4, unitPriceCents: 100 },
    ]);
    const result = reserveOrder(inventory, order);
    expect(result.ok).toBe(true);
    expect(availableUnits(inventory, "widget")).toBe(4);
  });

  it("reserves nothing and reports shortages", () => {
    const inventory = stock();
    const order = createOrder("A-3", "c-1", [
      { sku: "widget", quantity: 1, unitPriceCents: 100 },
      { sku: "gizmo", quantity: 5, unitPriceCents: 100 },
    ]);
    const result = reserveOrder(inventory, order);
    expect(result).toEqual({ ok: false, shortages: ["gizmo"] });
    expect(availableUnits(inventory, "widget")).toBe(8);
  });
});
