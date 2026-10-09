import { createHash } from "node:crypto";
import type { Characteristic, Product } from "../types";

interface RawCharacteristic {
  productCharacteristicValue?: {
    productCharacteristicName?: string;
    productCharacteristicUnitDesignationNational?: string;
  };
  characteristicValueStringValue?: string | null;
  characteristicValueIntValue?: number | null;
  characteristicValueDecimalValue?: number | null;
  characteristicValueBoolValue?: boolean | null;
  characteristicValueDateTimeValue?: string | null;
  rangeValue?: string | null;
}

export interface SourceCard {
  id: number;
  name: string;
  description?: string | null;
  productionDirectoryName?: string | null;
  productionDirectoryId?: number | null;
  manufacturerName?: string | null;
  oksmName?: string | null;
  okeiShortName?: string | null;
  okeiName?: string | null;
  partNumber?: string | null;
  modelName?: string | null;
  production?: { code?: string | null; name?: string | null; okpd?: { code?: string | null; name?: string | null } | null } | null;
  spgzId?: number | null;
  referencePrice?: number | null;
  medianPrice?: number | null;
  avgPrice?: number | null;
  minPrice?: number | null;
  images?: { fileStorageId: number }[];
  skuCharacteristics?: RawCharacteristic[];
  updatedDate?: string;
}

export function cleanText(value: string | null | undefined): string {
  return (value ?? "").replace(/<[^>]*>/g, " ").replace(/&nbsp;/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ").trim();
}

export function normalizeProduct(raw: SourceCard): Product {
  if (!Number.isSafeInteger(raw.id) || raw.id <= 0 || typeof raw.name !== "string" || !raw.name.trim()) {
    throw new Error("Некорректная карточка в исходной базе");
  }
  const characteristics: Characteristic[] = (raw.skuCharacteristics ?? []).flatMap((item) => {
    const value = item.characteristicValueStringValue ?? item.characteristicValueIntValue ?? item.characteristicValueDecimalValue ?? item.characteristicValueBoolValue ?? item.characteristicValueDateTimeValue ?? item.rangeValue;
    const name = cleanText(item.productCharacteristicValue?.productCharacteristicName);
    if (!name || value === null || value === undefined) return [];
    return [{ name, value: typeof value === "boolean" ? (value ? "Да" : "Нет") : cleanText(String(value)), unit: cleanText(item.productCharacteristicValue?.productCharacteristicUnitDesignationNational) }];
  });
  const price = [raw.referencePrice, raw.medianPrice, raw.avgPrice, raw.minPrice].find((p) => typeof p === "number" && Number.isFinite(p) && p > 0) ?? null;
  return {
    id: raw.id, name: cleanText(raw.name), description: cleanText(raw.description),
    category: cleanText(raw.productionDirectoryName) || "Без категории", categoryId: raw.productionDirectoryId ?? 0,
    manufacturer: cleanText(raw.manufacturerName), country: cleanText(raw.oksmName),
    unit: cleanText(raw.okeiShortName ?? raw.okeiName), partNumber: cleanText(raw.partNumber),
    modelName: cleanText(raw.modelName),
    okpd2: cleanText(raw.production?.okpd?.code), okpd2Name: cleanText(raw.production?.okpd?.name),
    kpgzCode: cleanText(raw.production?.code), kpgzName: cleanText(raw.production?.name),
    spgzId: Number.isSafeInteger(raw.spgzId) && raw.spgzId! > 0 ? raw.spgzId! : null, price,
    imageIds: [...new Set((raw.images ?? []).map((image) => image.fileStorageId).filter((id) => Number.isSafeInteger(id) && id > 0))],
    characteristics, sourceUpdatedAt: raw.updatedDate ?? "",
  };
}

export function embeddingDocument(product: Product): string {
  const content = [
    `Категория: ${product.category}`,
    product.manufacturer && `Производитель: ${product.manufacturer}`,
    product.partNumber && `Артикул: ${product.partNumber}`,
    product.okpd2 && `ОКПД2: ${product.okpd2}`,
    ...product.characteristics.map((c) => `${c.name}: ${c.value}${c.unit ? ` ${c.unit}` : ""}`),
    product.description.slice(0, 2000),
  ].filter(Boolean).join("\n");
  // BERTA has a 512-token context: keep the title and structured properties first.
  return `${product.name}\n${content}`;
}

export function documentHash(document: string): string {
  return createHash("sha256").update("berta:768:search_document:document-v1\0").update(document).digest("hex");
}
