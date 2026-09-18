import { describe, expect, it } from "vitest";
import { createOrder } from "../src/models/order.ts";
import {
  applyDiscountPercent,
  computeLineTotalCents,
  computeOrderTotalCents,
  formatCents,
} from "../src/services/pricing.ts";

describe("computeLineTotalCents", () => {
  it("multiplies unit price by quantity", () => {
    expect(computeLineTotalCents({ sku: "a", quantity: 3, unitPriceCents: 1250 }))
      .toBe(3750);
  });
});

describe("applyDiscountPercent", () => {
  it("returns the amount unchanged for a zero discount", () => {
    expect(applyDiscountPercent(999, 0)).toBe(999);
  });

  it("applies a discount that lands on a whole cent", () => {
    expect(applyDiscountPercent(1000, 10)).toBe(900);
  });

  it("rounds a half-cent result half up", () => {
    expect(applyDiscountPercent(1250, 15)).toBe(1063);
  });

  it("caps the discount at one hundred percent", () => {
    expect(applyDiscountPercent(5000, 150)).toBe(0);
  });
});

describe("computeOrderTotalCents", () => {
  it("sums the lines then discounts the subtotal", () => {
    const order = createOrder(
      "A-1",
      "c-1",
      [
        { sku: "a", quantity: 3, unitPriceCents: 1250 },
        { sku: "b", quantity: 1, unitPriceCents: 1250 },
      ],
      20,
    );
    expect(computeOrderTotalCents(order)).toBe(4000);
  });
});

describe("formatCents", () => {
  it("pads the minor units", () => {
    expect(formatCents(4005)).toBe("40.05");
  });
});
