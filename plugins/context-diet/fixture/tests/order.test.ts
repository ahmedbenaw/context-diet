import { describe, expect, it } from "vitest";
import { createOrder, lineCount, orderKey } from "../src/models/order.ts";
import { demoQuote } from "../src/index.ts";

describe("createOrder", () => {
  it("copies the lines it is given", () => {
    const lines = [{ sku: "a", quantity: 1, unitPriceCents: 100 }];
    const order = createOrder("A-4", "c-2", lines);
    lines.push({ sku: "b", quantity: 1, unitPriceCents: 100 });
    expect(lineCount(order)).toBe(1);
  });

  it("builds a normalised key", () => {
    expect(orderKey(createOrder(" A-5 ", "c-2", []))).toBe("order:a-5");
  });
});

describe("demoQuote", () => {
  it("returns the fixed sample quote", () => {
    expect(demoQuote()).toEqual({
      orderKey: "order:a-100",
      customerKey: "customer:cust-7",
      totalCents: 4500,
      display: "45.00",
      reservable: true,
    });
  });
});
