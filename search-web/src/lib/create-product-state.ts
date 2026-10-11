import { batch, createSignal } from "solid-js";
import { schemaEquivalent } from "./merge-characteristics";
import type {
  AutofillEvidence,
  CatalogSpecialization,
  CatalogCharacteristic,
  CategoryPairDecision,
  DirTreeNode,
  DraftCharacteristic,
  KtruTreeNode,
  ProductHit,
  VotedCode,
} from "./types";

export type DraftPhoto = { id: string; url: string; name: string };

/** Module-level draft — survives route changes (create ↔ directories ↔ catalog). */
export const [name, setName] = createSignal("");
export const [sku, setSku] = createSignal("");
export const [model, setModel] = createSignal("");
export const [brand, setBrand] = createSignal("");
/** Показывать ли проценты проверки Jev рядом со значениями характеристик (переключатель в заголовке блока); по умолчанию скрыты. */
export const [showConfidence, setShowConfidence] = createSignal(false);
const [descriptionValue, updateDescription] = createSignal("");
export const description = descriptionValue;
export const [selected, setSelected] = createSignal<KtruTreeNode | null>(null);
export const [okpd2, setOkpd2] = createSignal<VotedCode | null>(null);
export const [kpgz, setKpgz] = createSignal<VotedCode | null>(null);
export const [spgz, setSpgz] = createSignal<VotedCode | null>(null);
export const [ktruVote, setKtruVote] = createSignal<VotedCode | null>(null);
export const [ktruLocked, setKtruLocked] = createSignal(false);
export const [spgzLocked, setSpgzLocked] = createSignal(false);
export const [okpdKtruDecision, setOkpdKtruDecision] = createSignal<CategoryPairDecision | null>(null);
export const [kpgzSpgzDecision, setKpgzSpgzDecision] = createSignal<CategoryPairDecision | null>(null);

export function clearCategoryDecisions() {
  setOkpdKtruDecision(null);
  setKpgzSpgzDecision(null);
}
export const [suggesting, setSuggesting] = createSignal(false);
/** Visible loading time of last successful category suggest, including debounce and category updates, ms. */
export const [suggestDurationMs, setSuggestDurationMs] = createSignal<number | null>(null);
export const [similar, setSimilar] = createSignal<ProductHit[]>([]);
export const [similarDurationMs, setSimilarDurationMs] = createSignal<number | null>(null);
export const [characteristics, setCharacteristics] = createSignal<DraftCharacteristic[]>([]);
export const [catalogCharacteristicSuggestions, setCatalogCharacteristicSuggestions] = createSignal<CatalogCharacteristic[]>([]);
export const [lastAutofillKey, setLastAutofillKey] = createSignal("");
export const manuallyEditedCharacteristics = new Set<string>();
export const manuallyEditedCharacteristicValues = new Map<string, DraftCharacteristic>();
export const autofilledCharacteristicValues = new Map<string, { value: string; rangeOption: string }>();
export const manuallyEditedProductFields = new Set<"name" | "model" | "manufacturer" | "article">();
export const autofilledProductValues = new Map<"name" | "model" | "manufacturer" | "article", string>();
export const autofilledProductEvidence = new Map<"name" | "model" | "manufacturer" | "article", AutofillEvidence>();

export function setDescription(text: string): string {
  if (text === description()) return text;
  batch(() => {
    setName("");
    manuallyEditedProductFields.delete("name");
    autofilledProductValues.delete("name");
    autofilledProductEvidence.delete("name");
    const isQueryField = (item: DraftCharacteristic) => !!item.webSource || item.id.startsWith("additional:");
    for (const item of [...characteristics(), ...manuallyEditedCharacteristicValues.values()]) {
      if (!isQueryField(item)) continue;
      const key = characteristicEditKey(item);
      manuallyEditedCharacteristics.delete(key);
      manuallyEditedCharacteristicValues.delete(key);
      autofilledCharacteristicValues.delete(key);
    }
    setCharacteristics((items) => items.filter((item) => !isQueryField(item)));
    updateDescription(text);
  });
  return text;
}

export function characteristicEditKey(item: DraftCharacteristic): string {
  if (item.webSource) return item.id;
  if (item.source === "catalog") return `catalog:${item.id}:${item.okeiCode ?? item.unit}`;
  const name = item.name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  return `${item.source}:${name}:${item.okeiCode ?? item.unit}`;
}

export function preserveManualCharacteristics(fields: DraftCharacteristic[], previous: DraftCharacteristic[]): DraftCharacteristic[] {
  return fields.map((field) => {
    const key = characteristicEditKey(field);
    if (!manuallyEditedCharacteristics.has(key)) return field;
    const prior = previous.find((item) => characteristicEditKey(item) === key && schemaEquivalent(item, field))
      ?? manuallyEditedCharacteristicValues.get(key);
    return prior && schemaEquivalent(prior, field)
      ? { ...field, value: prior.value, rangeOption: prior.rangeOption, autofill: undefined,
        autofillDiagnostics: field.autofillDiagnostics ?? prior.autofillDiagnostics } : field;
  });
}
export const [charStandards, setCharStandards] = createSignal<string[]>([]);
export const [noNewFeatures, setNoNewFeatures] = createSignal<boolean | null>(null);
export const [noNewFeaturesReason, setNoNewFeaturesReason] = createSignal("");
export const [generatedFor, setGeneratedFor] = createSignal("");
export const [catalogSpecialization, setCatalogSpecialization] = createSignal<CatalogSpecialization | null>(null);
/** Template selected before refining characteristics to a child KTRU position. */
export const [initialTemplateKtruCode, setInitialTemplateKtruCode] = createSignal<string | null>(null);
export const [photos, setPhotos] = createSignal<DraftPhoto[]>([]);
export const [photoIndex, setPhotoIndex] = createSignal(0);
/** Last description that finished neighbor suggest — skip remount re-fetch. */
export const [lastSuggestQuery, setLastSuggestQuery] = createSignal("");

export const [ktruPickerSelected, setKtruPickerSelected] = createSignal<KtruTreeNode | null>(null);
export const [ktruPickerPath, setKtruPickerPath] = createSignal<string[]>([]);
export const [spgzPickerSelected, setSpgzPickerSelected] = createSignal<DirTreeNode | null>(null);
export const [spgzPickerPath, setSpgzPickerPath] = createSignal<string[]>([]);

let refineSeq = 0;
export const nextRefineSeq = () => ++refineSeq;
export const currentRefineSeq = () => refineSeq;

export function revokePhotos(items: DraftPhoto[]) {
  for (const item of items) URL.revokeObjectURL(item.url);
}

export function clearPhotos() {
  revokePhotos(photos());
  setPhotos([]);
  setPhotoIndex(0);
}

export function clearCharMeta() {
  setCatalogCharacteristicSuggestions([]);
  setCharStandards([]);
  setNoNewFeatures(null);
  setNoNewFeaturesReason("");
  setCatalogSpecialization(null);
  setInitialTemplateKtruCode(null);
}

/** Keep editable results independent when switching between batch/history requests. */
export function captureQueryDraft() {
  return structuredClone({
    name: name(), sku: sku(), model: model(), brand: brand(), selected: selected(),
    okpd2: okpd2(), kpgz: kpgz(), spgz: spgz(), ktruVote: ktruVote(),
    ktruLocked: ktruLocked(), spgzLocked: spgzLocked(), similar: similar(), similarDurationMs: similarDurationMs(),
    okpdKtruDecision: okpdKtruDecision(), kpgzSpgzDecision: kpgzSpgzDecision(),
    characteristics: characteristics(), catalogCharacteristicSuggestions: catalogCharacteristicSuggestions(),
    charStandards: charStandards(),
    noNewFeatures: noNewFeatures(), noNewFeaturesReason: noNewFeaturesReason(),
    generatedFor: generatedFor(), catalogSpecialization: catalogSpecialization(),
    initialTemplateKtruCode: initialTemplateKtruCode(), lastSuggestQuery: lastSuggestQuery(),
    suggestDurationMs: suggestDurationMs(), lastAutofillKey: lastAutofillKey(),
    manuallyEdited: [...manuallyEditedCharacteristics],
    manuallyEditedValues: [...manuallyEditedCharacteristicValues],
    autofilledValues: [...autofilledCharacteristicValues],
    manuallyEditedProduct: [...manuallyEditedProductFields],
    autofilledProduct: [...autofilledProductValues],
    autofilledProductEvidence: [...autofilledProductEvidence],
  });
}

export type QueryDraft = ReturnType<typeof captureQueryDraft>;

export function restoreQueryDraft(draft: QueryDraft) {
  batch(() => {
    setName(draft.name); setSku(draft.sku); setModel(draft.model); setBrand(draft.brand);
    setSelected(draft.selected); setOkpd2(draft.okpd2); setKpgz(draft.kpgz);
    setSpgz(draft.spgz); setKtruVote(draft.ktruVote);
    setKtruLocked(draft.ktruLocked); setSpgzLocked(draft.spgzLocked);
    setOkpdKtruDecision(draft.okpdKtruDecision); setKpgzSpgzDecision(draft.kpgzSpgzDecision);
    nextRefineSeq();
    setSimilar(draft.similar); setSimilarDurationMs(draft.similarDurationMs); setCharacteristics(draft.characteristics);
    setCatalogCharacteristicSuggestions(draft.catalogCharacteristicSuggestions);
    setCharStandards(draft.charStandards); setNoNewFeatures(draft.noNewFeatures);
    setNoNewFeaturesReason(draft.noNewFeaturesReason); setGeneratedFor(draft.generatedFor);
    setCatalogSpecialization(draft.catalogSpecialization);
    setInitialTemplateKtruCode(draft.initialTemplateKtruCode);
    setLastSuggestQuery(draft.lastSuggestQuery); setSuggestDurationMs(draft.suggestDurationMs);
    setLastAutofillKey(draft.lastAutofillKey); setSuggesting(false);
    manuallyEditedCharacteristics.clear();
    for (const key of draft.manuallyEdited) manuallyEditedCharacteristics.add(key);
    manuallyEditedCharacteristicValues.clear();
    for (const [key, value] of draft.manuallyEditedValues) manuallyEditedCharacteristicValues.set(key, value);
    autofilledCharacteristicValues.clear();
    for (const [key, value] of draft.autofilledValues) autofilledCharacteristicValues.set(key, value);
    manuallyEditedProductFields.clear();
    for (const key of draft.manuallyEditedProduct ?? []) manuallyEditedProductFields.add(key);
    autofilledProductValues.clear();
    for (const [key, value] of draft.autofilledProduct ?? []) autofilledProductValues.set(key, value);
    autofilledProductEvidence.clear();
    for (const [key, value] of draft.autofilledProductEvidence ?? []) autofilledProductEvidence.set(key, value);
  });
}
