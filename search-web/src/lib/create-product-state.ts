import { createSignal } from "solid-js";
import { schemaEquivalent } from "./merge-characteristics";
import type {
  CatalogSpecialization,
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
export const [description, setDescription] = createSignal("");
export const [selected, setSelected] = createSignal<KtruTreeNode | null>(null);
export const [okpd2, setOkpd2] = createSignal<VotedCode | null>(null);
export const [kpgz, setKpgz] = createSignal<VotedCode | null>(null);
export const [spgz, setSpgz] = createSignal<VotedCode | null>(null);
export const [ktruVote, setKtruVote] = createSignal<VotedCode | null>(null);
export const [ktruLocked, setKtruLocked] = createSignal(false);
export const [spgzLocked, setSpgzLocked] = createSignal(false);
export const [suggesting, setSuggesting] = createSignal(false);
/** Visible loading time of last successful category suggest, including debounce and category updates, ms. */
export const [suggestDurationMs, setSuggestDurationMs] = createSignal<number | null>(null);
export const [similar, setSimilar] = createSignal<ProductHit[]>([]);
export const [characteristics, setCharacteristics] = createSignal<DraftCharacteristic[]>([]);
export const [lastAutofillKey, setLastAutofillKey] = createSignal("");
export const manuallyEditedCharacteristics = new Set<string>();
export const manuallyEditedCharacteristicValues = new Map<string, DraftCharacteristic>();
export const autofilledCharacteristicValues = new Map<string, { value: string; rangeOption: string }>();

export function characteristicEditKey(item: DraftCharacteristic): string {
  const name = item.name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
  return `${item.source}:${name}:${item.unit}`;
}

export function preserveManualCharacteristics(fields: DraftCharacteristic[], previous: DraftCharacteristic[]): DraftCharacteristic[] {
  return fields.map((field) => {
    const key = characteristicEditKey(field);
    if (!manuallyEditedCharacteristics.has(key)) return field;
    const prior = previous.find((item) => characteristicEditKey(item) === key && schemaEquivalent(item, field))
      ?? manuallyEditedCharacteristicValues.get(key);
    return prior && schemaEquivalent(prior, field)
      ? { ...field, value: prior.value, rangeOption: prior.rangeOption, autofill: undefined,
        optionProbabilities: field.optionProbabilities ?? prior.optionProbabilities } : field;
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
  setCharStandards([]);
  setNoNewFeatures(null);
  setNoNewFeaturesReason("");
  setCatalogSpecialization(null);
  setInitialTemplateKtruCode(null);
}
