import { createSignal } from "solid-js";
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
/** Wall time of last successful category suggest (neighbors + specialization), ms. */
export const [suggestDurationMs, setSuggestDurationMs] = createSignal<number | null>(null);
export const [similar, setSimilar] = createSignal<ProductHit[]>([]);
export const [characteristics, setCharacteristics] = createSignal<DraftCharacteristic[]>([]);
export const [charStandards, setCharStandards] = createSignal<string[]>([]);
export const [noNewFeatures, setNoNewFeatures] = createSignal<boolean | null>(null);
export const [noNewFeaturesReason, setNoNewFeaturesReason] = createSignal("");
export const [generatedFor, setGeneratedFor] = createSignal("");
export const [catalogSpecialization, setCatalogSpecialization] = createSignal<CatalogSpecialization | null>(null);
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
}
