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
  ktru?: { code?: string | null; name?: string | null; id?: number | null } | string | null;
  spgzId?: number | null;
  referencePrice?: number | null;
  medianPrice?: number | null;
  avgPrice?: number | null;
  minPrice?: number | null;
  images?: { fileStorageId: number }[];
  skuCharacteristics?: RawCharacteristic[];
  updatedDate?: string;
}

function extractKtru(raw: SourceCard, characteristics: Characteristic[]): { ktruCode: string; ktruName: string } {
  let ktruCode = "";
  let ktruName = "";
  if (typeof raw.ktru === "string") ktruCode = cleanText(raw.ktru);
  else if (raw.ktru && typeof raw.ktru === "object") {
    ktruCode = cleanText(raw.ktru.code);
    ktruName = cleanText(raw.ktru.name);
  }
  if (!ktruCode) {
    const characteristic = characteristics.find((item) => item.name.toLocaleLowerCase("ru") === "ктру");
    if (characteristic?.value) ktruCode = cleanText(characteristic.value);
  }
  return { ktruCode, ktruName };
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
  const { ktruCode, ktruName } = extractKtru(raw, characteristics);
  return {
    id: raw.id, name: cleanText(raw.name), description: cleanText(raw.description),
    category: cleanText(raw.productionDirectoryName) || "Без категории", categoryId: raw.productionDirectoryId ?? 0,
    manufacturer: cleanText(raw.manufacturerName), country: cleanText(raw.oksmName),
    unit: cleanText(raw.okeiShortName ?? raw.okeiName), partNumber: cleanText(raw.partNumber),
    modelName: cleanText(raw.modelName),
    okpd2: cleanText(raw.production?.okpd?.code), okpd2Name: cleanText(raw.production?.okpd?.name),
    ktruCode, ktruName,
    kpgzCode: cleanText(raw.production?.code), kpgzName: cleanText(raw.production?.name),
    spgzId: Number.isSafeInteger(raw.spgzId) && raw.spgzId! > 0 ? raw.spgzId! : null, price,
    imageIds: [...new Set((raw.images ?? []).map((image) => image.fileStorageId).filter((id) => Number.isSafeInteger(id) && id > 0))],
    characteristics, sourceUpdatedAt: raw.updatedDate ?? "",
  };
}

const MAX_CHARACTERISTICS = 24;
const MAX_CHAR_VALUE_LEN = 120;

function formatCharacteristic(c: { name: string; value: string; unit?: string }): string {
  const value = c.value.length > MAX_CHAR_VALUE_LEN ? `${c.value.slice(0, MAX_CHAR_VALUE_LEN - 1)}…` : c.value;
  return `${c.name}: ${value}${c.unit ? ` ${c.unit}` : ""}`;
}

export function embeddingDocument(product: Product): string {
  const content = [
    `Категория: ${product.category}`,
    product.manufacturer && `Производитель: ${product.manufacturer}`,
    product.partNumber && `Артикул: ${product.partNumber}`,
    product.okpd2 && `ОКПД2: ${product.okpd2}`,
    product.ktruCode && `КТРУ: ${product.ktruCode}${product.ktruName ? ` ${product.ktruName}` : ""}`,
    ...product.characteristics.slice(0, MAX_CHARACTERISTICS).map(formatCharacteristic),
    product.description.slice(0, 2000),
  ].filter(Boolean).join("\n");
  // EmbeddingGemma SearchQuery/Document: titled docs as `title: … | text: …`.
  return `title: ${product.name} | text: ${content}`;
}

export function documentHash(document: string): string {
  return createHash("sha256").update("embeddinggemma-2:768:search-result:document-v2\0").update(document).digest("hex");
}
