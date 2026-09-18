/**
 * Canonical key builder. Every lookup key in this service is produced here so
 * that "SKU-1 " and "sku-1" can never end up as two different map entries.
 */
export function makeIdKey(namespace: string, id: string): string {
  return `${namespace}:${id.trim().toLowerCase()}`;
}
