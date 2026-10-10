import { Meta, Title } from "@solidjs/meta";
import { A } from "@solidjs/router";
import CircleAlert from "lucide-solid/icons/circle-alert";
import FileText from "lucide-solid/icons/file-text";
import ImagePlus from "lucide-solid/icons/image-plus";
import Lightbulb from "lucide-solid/icons/lightbulb";
import Pencil from "lucide-solid/icons/pencil";
import Plus from "lucide-solid/icons/plus";
import RotateCcw from "lucide-solid/icons/rotate-ccw";
import Sparkles from "lucide-solid/icons/sparkles";
import Tags from "lucide-solid/icons/tags";
import X from "lucide-solid/icons/x";
import { batch, createEffect, createMemo, createSignal, For, Index, on, onCleanup, Show } from "solid-js";
import { directoryHref, ProductCard } from "~/components/catalog";
import { CategoryPairs } from "~/components/category-pairs";
import { KtruPickerActions, KtruPickerBody } from "~/components/ktru-picker";
import { SpgzPickerActions, SpgzPickerBody } from "~/components/spgz-picker";
import {
  isRangeCharacteristic,
  isYesNoOptions,
  matchCharacteristicOption,
  matchNumberToRange,
  parseCharacteristicNumber,
  splitCharacteristicValues,
  yesNoOptionPair,
} from "~/lib/characteristic-options";
import { getCatalogSpecialization, getDirectoryNode, getDirectoryRecord, getDirectoryRevealPath, getKtruNode, getKtruRecord, getKtruRevealPath, suggestFromNeighbors } from "~/lib/rpc";
import {
  buildDisplayCharacteristics,
  characteristicSourceLabel,
  mapValueToOptions,
} from "~/lib/merge-characteristics";
import {
  brand,
  catalogSpecialization,
  characteristics,
  charStandards,
  clearCharMeta,
  clearPhotos,
  currentRefineSeq,
  description,
  type DraftPhoto,
  generatedFor,
  kpgz,
  ktruLocked,
  ktruPickerPath,
  ktruPickerSelected,
  ktruVote,
  lastSuggestQuery,
  model,
  name,
  nameTouched,
  nextRefineSeq,
  noNewFeatures,
  noNewFeaturesReason,
  okpd2,
  photoIndex,
  photos,
  selected,
  setBrand,
  setCatalogSpecialization,
  setCharacteristics,
  setCharStandards,
  setDescription,
  setGeneratedFor,
  setKpgz,
  setKtruLocked,
  setKtruPickerPath,
  setKtruPickerSelected,
  setKtruVote,
  setLastSuggestQuery,
  setModel,
  setName,
  setNameTouched,
  setNoNewFeatures,
  setNoNewFeaturesReason,
  setOkpd2,
  setPhotoIndex,
  setPhotos,
  setSelected,
  setSimilar,
  setSku,
  setSpgz,
  setSpgzLocked,
  setSpgzPickerPath,
  setSpgzPickerSelected,
  setSuggestDurationMs,
  setSuggesting,
  similar,
  sku,
  spgz,
  spgzLocked,
  spgzPickerPath,
  spgzPickerSelected,
  suggestDurationMs,
  suggesting,
} from "~/lib/create-product-state";
import { draftFromSpgzCharacteristic } from "~/lib/spgz-characteristics";
import type { CatalogSpecialization, DirTreeNode, DraftCharacteristic, KtruTreeNode, VotedCode } from "~/lib/types";
import { Badge, Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Skeleton, TextField, TextFieldInput, TextFieldLabel, TextFieldTextarea } from "~/ui-kit";
import "./create-product.css";
import "./directories.css";
import "./ktru.css";

const MAX_PHOTOS = 8;

function nameFromDescription(text: string): string {
  return text.trim().split(/[.!?\n]/)[0]?.trim().slice(0, 200) ?? "";
}

function formatSuggestTiming(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} мс` : `${(ms / 1000).toFixed(2)} с`;
}

function normCharName(name: string): string {
  return name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е");
}

function withCatalogSpecialization(
  fields: DraftCharacteristic[],
  spec: CatalogSpecialization | null,
  selectedValue?: string | null,
): DraftCharacteristic[] {
  if (!spec) return fields.map((item) => ({ ...item, refinesKtru: false }));
  const options = spec.options.map((item) => item.value);
  // Only an explicit choice (user / refine) fills the discriminator — never auto from current KTRU.
  const preferred = (selectedValue ?? "").trim();
  const value = preferred ? mapValueToOptions(preferred, options) || preferred : "";
  const hasKtru = spec.options.some((item) => item.ktruCode);
  const hasSpgz = spec.options.some((item) => item.spgzCode);
  const hint = hasKtru && hasSpgz
    ? "Выбор уточняет позиции КТРУ и СПГЗ"
    : hasSpgz
      ? "Выбор уточняет позицию СПГЗ среди вариантов КПГЗ"
      : "Выбор уточняет позицию КТРУ среди вариантов шаблона";
  const previousRefine = new Map(
    fields
      .filter((item) => item.refinesKtru)
      .map((item) => [normCharName(item.name), item.applyPositionRefine !== false]),
  );
  const refineIds = new Set<string>();
  const mapped = fields.map((item) => {
    if (normCharName(item.name) !== normCharName(spec.characteristicName)) {
      return { ...item, refinesKtru: false };
    }
    if (item.source !== "ktru" && item.source !== "spgz" && item.source !== "both") {
      return { ...item, refinesKtru: false };
    }
    refineIds.add(item.id);
    return {
      ...item,
      options,
      value: value || item.value,
      rangeOption: "",
      required: true,
      multiple: false,
      ranges: [],
      badges: item.badges.filter((entry) => !/^уточняет /.test(entry)),
      hint: item.hint || hint,
      refinesKtru: true,
      applyPositionRefine: previousRefine.get(normCharName(item.name)) ?? item.applyPositionRefine !== false,
      // Keep ktru/spgz so buildDisplayCharacteristics can merge siblings; only
      // injected rows (no prior source) use "both".
      source: item.source === "both" ? "both" : item.source,
    };
  });
  if (!refineIds.size) {
    mapped.unshift({
      id: spec.characteristicCode || `refine:${spec.characteristicName}`,
      name: spec.characteristicName,
      value,
      rangeOption: "",
      required: true,
      badges: [],
      hint,
      options,
      optionLabels: {},
      ranges: [],
      unit: "",
      multiple: false,
      source: hasKtru && hasSpgz ? "both" : hasSpgz ? "spgz" : "ktru",
      refinesKtru: true,
      applyPositionRefine: previousRefine.get(normCharName(spec.characteristicName)) ?? true,
    });
  }
  return [
    ...mapped.filter((item) => item.refinesKtru),
    ...mapped.filter((item) => !item.refinesKtru),
  ];
}

type RefinedPositionRef = {
  tab: "ktru" | "spgz";
  code: string;
  name: string;
  kindLabel: string;
};

function CreateCharRow(props: {
  item: () => DraftCharacteristic;
  choiceOptions: (item: DraftCharacteristic) => string[];
  optionLabel: (item: DraftCharacteristic, value: string) => string;
  selectedOptions: (value: string) => string[];
  refinedRefs?: RefinedPositionRef[];
  onUpdate: (id: string, value: string) => void;
  onUpdateName: (id: string, name: string) => void;
  onUpdateRange: (id: string, rangeOption: string) => void;
  onTogglePositionRefine?: (id: string, enabled: boolean) => void;
  onRemove: (id: string) => void;
  onPickOption: (item: DraftCharacteristic, option: string) => void;
}) {
  const item = () => props.item();
  const refineEnabled = () => item().applyPositionRefine !== false;
  return (
            <div class="create-char-row" classList={{ "create-char-row-detached": !!item().refinesKtru && !refineEnabled() }}>
              <dt>
                <Show when={item().custom} fallback={
                  <span class="dir-char-name">
                    {item().name}
                    <Show when={item().required}><span class="create-char-req" aria-label="обязательная">*</span></Show>
                    <span class="create-char-source">{characteristicSourceLabel(item().source)}</span>
                  </span>
                }>
                  <div class="create-char-custom-name">
                    <TextField class="create-inline-field" value={item().name} onChange={(value) => props.onUpdateName(item().id, value)}>
                      <TextFieldLabel class="sr-only">Название характеристики</TextFieldLabel>
                      <TextFieldInput placeholder="Название характеристики" />
                    </TextField>
                    <span class="create-char-source">{characteristicSourceLabel("custom")}</span>
                    <button type="button" class="create-char-remove" aria-label="Удалить характеристику" onClick={() => props.onRemove(item().id)}>
                      <X size={14} />
                    </button>
                  </div>
                </Show>
                <Show when={item().badges.length}>
                  <span class="dir-char-badges">
                    <For each={item().badges}>{badge => <span class="dir-char-required">{badge}</span>}</For>
                  </span>
                </Show>
              </dt>
              <dd>
                <Show when={isRangeCharacteristic(item())} fallback={
                  <Show when={(item().options ?? []).length} fallback={
                    <TextField class="create-inline-field" value={item().value} onChange={(value) => props.onUpdate(item().id, value)}>
                      <TextFieldLabel class="sr-only">{item().name || "Значение характеристики"}</TextFieldLabel>
                      <TextFieldInput
                        placeholder={item().custom ? "Значение" : item().hint ? `Напр.: ${item().hint.split(" · ")[0]}` : "Укажите значение"}
                      />
                    </TextField>
                  }>
                    <Show when={isYesNoOptions(props.choiceOptions(item()))} fallback={
                      <Show when={item().multiple} fallback={
                        <Select<string>
                          class="create-char-select"
                          options={props.choiceOptions(item())}
                          value={item().value || null}
                          onChange={(value) => {
                            if (!item().required && value == null) {
                              props.onUpdate(item().id, "");
                              return;
                            }
                            if (value && value === item().value) {
                              if (!item().required) props.onUpdate(item().id, "");
                              return;
                            }
                            props.onUpdate(item().id, value ?? "");
                          }}
                          placeholder="Выберите значение"
                          disallowEmptySelection={item().required}
                          selectionBehavior={item().required ? "replace" : "toggle"}
                          itemComponent={(selectProps) => <SelectItem item={selectProps.item}>{props.optionLabel(item(), selectProps.item.rawValue)}</SelectItem>}
                        >
                          <SelectTrigger class="create-char-select-trigger" aria-label={item().name}>
                            <SelectValue<string>>{(state) => {
                              const selected = state.selectedOption();
                              return selected ? props.optionLabel(item(), selected) : "Выберите значение";
                            }}</SelectValue>
                          </SelectTrigger>
                          <SelectContent />
                        </Select>
                      }>
                        <Select<string>
                          class="create-char-select"
                          multiple
                          options={props.choiceOptions(item())}
                          value={props.selectedOptions(item().value)}
                          onChange={(values) => { props.onUpdate(item().id, values.join(" · ")); }}
                          placeholder="Выберите значения"
                          disallowEmptySelection={item().required}
                          itemComponent={(selectProps) => <SelectItem item={selectProps.item}>{props.optionLabel(item(), selectProps.item.rawValue)}</SelectItem>}
                        >
                          <SelectTrigger class="create-char-select-trigger" aria-label={item().name}>
                            <SelectValue<string>>{(state) => {
                              const selected = state.selectedOptions();
                              return selected.length ? selected.map((value) => props.optionLabel(item(), value)).join(" · ") : "Выберите значения";
                            }}</SelectValue>
                          </SelectTrigger>
                          <SelectContent />
                        </Select>
                      </Show>
                    }>
                      <div class="create-char-toggle" role="group" aria-label={item().name}>
                        <For each={(() => {
                          const pair = yesNoOptionPair(props.choiceOptions(item()));
                          return [pair.yes, pair.no];
                        })()}>{(option) =>
                          <button
                            type="button"
                            class="create-char-toggle-btn"
                            classList={{ active: item().value === option }}
                            aria-pressed={item().value === option}
                            onClick={() => props.onPickOption(item(), option)}
                          >
                            {props.optionLabel(item(), option)}
                          </button>
                        }</For>
                      </div>
                    </Show>
                  </Show>
                }>
                  <div class="create-char-quantity">
                    <label class="create-char-quantity-input">
                      <span class="sr-only">{item().name}</span>
                      <input
                        type="text"
                        inputmode="decimal"
                        value={item().value}
                        placeholder="Число"
                        onInput={(event) => props.onUpdate(item().id, event.currentTarget.value)}
                      />
                      <Show when={item().unit}><span class="create-char-unit">{item().unit}</span></Show>
                    </label>
                    <Select<string>
                      class="create-char-select create-char-range-select"
                      options={item().options}
                      value={item().rangeOption || null}
                      onChange={(value) => {
                        if (!item().required && value == null) {
                          props.onUpdateRange(item().id, "");
                          return;
                        }
                        if (!item().required && value && value === item().rangeOption) {
                          props.onUpdateRange(item().id, "");
                          return;
                        }
                        props.onUpdateRange(item().id, value ?? "");
                      }}
                      placeholder="Категория КТРУ"
                      disallowEmptySelection={item().required}
                      selectionBehavior={item().required ? "replace" : "toggle"}
                      itemComponent={(selectProps) => <SelectItem item={selectProps.item}>{props.optionLabel(item(), selectProps.item.rawValue)}</SelectItem>}
                    >
                      <SelectTrigger class="create-char-select-trigger" aria-label={`${item().name}: категория КТРУ`}>
                        <SelectValue<string>>{(state) => props.optionLabel(item(), state.selectedOption())}</SelectValue>
                      </SelectTrigger>
                      <SelectContent />
                    </Select>
                  </div>
                </Show>
                <Show when={item().refinesKtru && props.onTogglePositionRefine} fallback={
                  <Show when={item().hint}><p class="create-char-hint">{item().hint}</p></Show>
                }>
                  <div class="create-char-hint-row" classList={{ detached: !refineEnabled() }}>
                    <Show when={item().hint}><p class="create-char-hint">{item().hint}</p></Show>
                    <label class="create-char-refine-toggle">
                      <input
                        type="checkbox"
                        checked={refineEnabled()}
                        onChange={(event) => props.onTogglePositionRefine?.(item().id, event.currentTarget.checked)}
                      />
                      <span>Уточнять позицию</span>
                    </label>
                  </div>
                  <Show when={props.refinedRefs?.length}>
                    <div class="create-char-refine-bar" classList={{ detached: !refineEnabled() }}>
                      <div class="create-char-ktru-ref">
                        <For each={props.refinedRefs}>{ref =>
                          <A class="create-char-ktru-ref-link" href={directoryHref(ref.tab, ref.code)} title={`Открыть ${ref.kindLabel} в справочнике`}>
                            <span class="create-char-ktru-ref-code">{ref.kindLabel} {ref.code}</span>
                            <Show when={ref.name}><span class="create-char-ktru-ref-name">{ref.name}</span></Show>
                          </A>
                        }</For>
                      </div>
                    </div>
                  </Show>
                </Show>
              </dd>
            </div>
  );
}

function CategorySuggestSkeleton() {
  return (
    <div class="create-suggest-status" role="status" aria-live="polite" aria-label="Ищем ближайшие товары и голосуем за категории">
      <p class="create-live-hint create-live-hint-pulse">Ищем ближайшие товары и голосуем за категории…</p>
      <div class="create-vote-card create-vote-card-skeleton" aria-hidden="true">
        <For each={["ОКПД2 и КТРУ", "КПГЗ и СПГЗ"]}>{title =>
          <div class="create-vote-group create-vote-group-skeleton">
            <div class="create-vote-group-head">
              <Skeleton class="create-skel-title" />
              <span class="sr-only">{title}</span>
            </div>
            <div class="create-skel-rows">
              <div><Skeleton class="create-skel-label" /><Skeleton class="create-skel-value" /></div>
              <div><Skeleton class="create-skel-label" /><Skeleton class="create-skel-value wide" /></div>
            </div>
          </div>
        }</For>
      </div>
    </div>
  );
}

function SimilarSuggestSkeleton() {
  return (
    <div class="create-suggest-status" role="status" aria-live="polite" aria-label="Ищем похожие позиции в каталоге">
      <p class="create-live-hint create-live-hint-pulse">Ищем похожие позиции в каталоге…</p>
      <div class="create-similar-skel" aria-hidden="true">
        <For each={[1, 2, 3]}>{() =>
          <div class="create-similar-skel-card">
            <Skeleton class="create-skel-thumb" />
            <Skeleton class="create-skel-line" />
            <Skeleton class="create-skel-line short" />
          </div>
        }</For>
      </div>
    </div>
  );
}

export default function NewProduct() {
  const [ktruPickerOpen, setKtruPickerOpen] = createSignal(false);
  const [spgzPickerOpen, setSpgzPickerOpen] = createSignal(false);
  const [photoDragging, setPhotoDragging] = createSignal(false);
  let photoInput: HTMLInputElement | undefined;

  const fingerprint = () => `${selected()?.code ?? ""}\u0000${description()}`;
  const outdated = () => !!generatedFor() && generatedFor() !== fingerprint();
  const displayCharacteristics = createMemo(() => buildDisplayCharacteristics(characteristics()));
  const filledCount = () => displayCharacteristics().filter((item) => item.value.trim() || item.rangeOption.trim()).length;
  const linkedIdsFor = (id: string) =>
    displayCharacteristics().find((row) => row.id === id)?.linkedIds ?? [id];
  const hasDraft = () => !!(description().trim() || name().trim() || selected() || similar().length || photos().length);
  const hasCategory = () => !!(okpd2() || ktruVote() || kpgz() || spgz());
  const hasKtruPosition = () => !!(ktruVote()?.code || selected()?.kind === "ktru");
  const activePhoto = () => photos()[photoIndex()] ?? photos()[0] ?? null;

  const loadSpecialization = async (input?: {
    ktruCode?: string | null;
    spgzCode?: string | null;
    kpgzCode?: string | null;
  }) => getCatalogSpecialization({
    ktruCode: input?.ktruCode ?? ktruVote()?.code ?? (selected()?.kind === "ktru" ? selected()?.code : null),
    spgzCode: input?.spgzCode ?? spgz()?.code ?? null,
    kpgzCode: input?.kpgzCode ?? kpgz()?.code ?? null,
  });

  const refinedPositionRefs = (item: DraftCharacteristic): RefinedPositionRef[] => {
    if (!item.refinesKtru) return [];
    const spec = catalogSpecialization();
    if (!spec) return [];
    const match = item.value.trim()
      ? spec.options.find((option) => option.value === item.value)
      : null;
    if (!match) return [];
    const refs: RefinedPositionRef[] = [];
    if (match.ktruCode) {
      const isTemplate = match.ktruCode === spec.templateKtruCode;
      refs.push({
        tab: "ktru",
        code: match.ktruCode,
        name: match.ktruName ? `${match.ktruName}${isTemplate ? " (Шаблон)" : ""}` : (isTemplate ? "(Шаблон)" : ""),
        kindLabel: "КТРУ",
      });
    }
    if (match.spgzCode) {
      refs.push({
        tab: "spgz",
        code: match.spgzCode,
        name: match.spgzName || "",
        kindLabel: "СПГЗ",
      });
    }
    return refs;
  };

  const addPhotoFiles = (list: FileList | File[] | null) => {
    if (!list?.length) return;
    const room = MAX_PHOTOS - photos().length;
    if (room <= 0) return;
    const next: DraftPhoto[] = [];
    for (const file of [...list].slice(0, room)) {
      if (!file.type.startsWith("image/")) continue;
      next.push({ id: `${file.name}-${file.size}-${file.lastModified}-${Math.random().toString(36).slice(2, 8)}`, url: URL.createObjectURL(file), name: file.name });
    }
    if (!next.length) return;
    setPhotos((current) => {
      const merged = [...current, ...next];
      setPhotoIndex(current.length);
      return merged;
    });
  };

  const removePhoto = (id: string) => {
    setPhotos((current) => {
      const index = current.findIndex((item) => item.id === id);
      if (index < 0) return current;
      URL.revokeObjectURL(current[index]!.url);
      const next = current.filter((item) => item.id !== id);
      setPhotoIndex((selected) => {
        if (!next.length) return 0;
        if (selected > index) return selected - 1;
        if (selected === index) return Math.min(index, next.length - 1);
        return selected;
      });
      return next;
    });
  };

  const applyOkpd = async (code: string, locked: boolean) => {
    const node = await getKtruNode(code);
    if (!node || node.kind !== "okpd") return;
    const text = description();
    batch(() => {
      setSelected(node);
      setOkpd2({ code: node.code, name: node.name, score: okpd2()?.score ?? 1 });
      setKtruVote(null);
      setKtruLocked(locked);
      setCharacteristics((current) => current.filter((item) => item.source !== "ktru"));
      clearCharMeta();
      setGeneratedFor(`\u0000${text}`);
      if (!nameTouched() || !name().trim()) {
        setName(nameFromDescription(text) || node.name);
        setNameTouched(false);
      }
    });
  };

  const buildKtruDraftFields = (
    record: NonNullable<Awaited<ReturnType<typeof getKtruRecord>>>,
    previous: DraftCharacteristic[],
    keepPrevious: boolean,
  ): DraftCharacteristic[] => {
    const previousByName = new Map(
      previous
        .filter((item) => item.source === "ktru" || item.source === "both")
        .map((item) => [normCharName(item.name), item]),
    );
    return (record.characteristics ?? []).map((item) => {
      const options = item.options ?? [];
      const ranges = item.ranges ?? [];
      const id = item.code || item.name;
      const prior = keepPrevious ? previousByName.get(normCharName(item.name)) : undefined;
      if (isRangeCharacteristic(item)) {
        return {
          id,
          name: item.name,
          value: prior?.value.trim() ? prior.value : "",
          rangeOption: prior?.rangeOption.trim()
            ? mapValueToOptions(prior.rangeOption, options) || prior.rangeOption
            : "",
          required: item.required,
          badges: item.badges.filter((badge) => badge !== "обязательная"),
          hint: item.hint || "",
          options,
          optionLabels: item.optionLabels ?? {},
          ranges,
          unit: item.unit || "",
          multiple: !!item.multiple,
          source: "ktru" as const,
        };
      }
      return {
        id,
        name: item.name,
        value: prior?.value.trim()
          ? (options.length ? mapValueToOptions(prior.value, options) || prior.value : prior.value)
          : "",
        rangeOption: "",
        required: item.required,
        badges: item.badges.filter((badge) => badge !== "обязательная"),
        hint: item.hint || "",
        options,
        optionLabels: item.optionLabels ?? {},
        ranges: [],
        unit: item.unit || "",
        multiple: !!item.multiple,
        source: "ktru" as const,
      };
    });
  };

  const buildSpgzDraftFields = (
    record: NonNullable<Awaited<ReturnType<typeof getDirectoryRecord>>>,
    previous: DraftCharacteristic[],
    keepPrevious: boolean,
  ): DraftCharacteristic[] => {
    const previousByName = new Map(
      previous
        .filter((item) => item.source === "spgz" || item.source === "both")
        .map((item) => [normCharName(item.name), item]),
    );
    return (record.characteristics ?? []).map((item) => {
      const draft = draftFromSpgzCharacteristic(item);
      const prior = keepPrevious ? previousByName.get(normCharName(draft.name)) : undefined;
      if (prior?.value.trim()) {
        return {
          ...draft,
          value: draft.options.length ? mapValueToOptions(prior.value, draft.options) || prior.value : prior.value,
        };
      }
      return draft;
    });
  };

  /** Fetch KTRU/SPGZ once, then commit votes + characteristics in a single batch (no table flicker). */
  const applyCatalogPositions = async (input: {
    ktruCode?: string | null;
    spgzCode?: string | null;
    clearSpgz?: boolean;
    ktruLocked?: boolean;
    spgzLocked?: boolean;
    discriminatorValue?: string | null;
    applyPositionRefine?: boolean;
    /** Reuse already-loaded specialization (refine path) — skips a heavy RPC. */
    reuseSpecialization?: boolean;
  }) => {
    const text = description();
    const keepPrevious = input.discriminatorValue !== undefined;
    const previous = characteristics();
    const customKeep = previous.filter((item) => item.source === "custom" || item.custom);
    const wantKtru = !!input.ktruCode;
    const wantSpgz = !input.clearSpgz && !!input.spgzCode;
    const keepSpgzRows = !input.clearSpgz && !input.spgzCode;
    const reuseSpec = input.reuseSpecialization ? catalogSpecialization() : null;

    const [ktruNode, ktruRecord, spgzNode, spgzRecord, spec] = await Promise.all([
      wantKtru ? getKtruNode(input.ktruCode!) : Promise.resolve(null),
      wantKtru ? getKtruRecord(input.ktruCode!) : Promise.resolve(null),
      wantSpgz ? getDirectoryNode("spgz", input.spgzCode!) : Promise.resolve(null),
      wantSpgz ? getDirectoryRecord("spgz", input.spgzCode!) : Promise.resolve(null),
      reuseSpec
        ? Promise.resolve(reuseSpec)
        : getCatalogSpecialization({
          ktruCode: input.ktruCode ?? (selected()?.kind === "ktru" ? selected()?.code : ktruVote()?.code) ?? null,
          spgzCode: input.clearSpgz ? null : (input.spgzCode ?? spgz()?.code ?? null),
          kpgzCode: kpgz()?.code ?? null,
        }),
    ]);

    const ktruFields = (wantKtru && ktruRecord && ktruNode?.kind === "ktru")
      ? buildKtruDraftFields(ktruRecord, previous, keepPrevious)
      : previous.filter((item) => item.source === "ktru");
    const spgzFields = (wantSpgz && spgzRecord && spgzNode?.kind === "spgz")
      ? buildSpgzDraftFields(spgzRecord, previous, keepPrevious)
      : keepSpgzRows
        ? previous.filter((item) => item.source === "spgz")
        : [];

    let nextChars = withCatalogSpecialization(
      [...ktruFields, ...spgzFields, ...customKeep],
      spec,
      input.discriminatorValue,
    );
    if (input.applyPositionRefine === false) {
      nextChars = nextChars.map((item) => item.refinesKtru ? { ...item, applyPositionRefine: false } : item);
    }

    const kpgzLink = spgzRecord?.links.find((link) => link.tab === "kpgz");

    batch(() => {
      if (wantKtru && ktruNode?.kind === "ktru" && ktruRecord) {
        setSelected(ktruNode);
        setKtruVote({
          code: ktruNode.code,
          name: ktruNode.name,
          score: ktruVote()?.score ?? 1,
          isTemplate: ktruNode.isTemplate,
        });
        if (input.ktruLocked != null) setKtruLocked(input.ktruLocked);
        setCharStandards(ktruRecord.standards ?? []);
        setNoNewFeatures(ktruRecord.noNewFeatures);
        setNoNewFeaturesReason(ktruRecord.noNewFeaturesReason ?? "");
        setGeneratedFor(`${ktruNode.code}\u0000${text}`);
        if (ktruRecord.okpd2) {
          setOkpd2({
            code: ktruRecord.okpd2.code,
            name: ktruRecord.okpd2.name || ktruRecord.okpd2.code,
            score: okpd2()?.score ?? 1,
          });
        }
        if (!nameTouched() || !name().trim()) {
          setName(nameFromDescription(text) || ktruNode.name);
          setNameTouched(false);
        }
      }
      if (input.clearSpgz) {
        setSpgz(null);
        if (input.spgzLocked != null) setSpgzLocked(input.spgzLocked);
      } else if (wantSpgz && spgzNode?.kind === "spgz") {
        setSpgz({
          code: spgzNode.code,
          name: spgzNode.name || spgzRecord?.name || spgzNode.code,
          score: spgz()?.score ?? 1,
        });
        if (input.spgzLocked != null) setSpgzLocked(input.spgzLocked);
        if (kpgzLink) {
          const label = kpgzLink.label.replace(/^КПГЗ\s+/, "");
          const sep = label.indexOf(" — ");
          setKpgz({
            code: kpgzLink.code,
            name: sep >= 0 ? label.slice(sep + 3) : label,
            score: kpgz()?.score ?? 1,
          });
        }
      }
      setCatalogSpecialization(spec);
      setCharacteristics(nextChars);
    });
  };

  const applyKtru = async (code: string, locked: boolean, discriminatorValue?: string | null) => {
    await applyCatalogPositions({
      ktruCode: code,
      ktruLocked: locked,
      discriminatorValue,
    });
  };

  const refineFromDiscriminator = (value: string) => {
    const spec = catalogSpecialization();
    if (!spec) return;
    const match = spec.options.find((option) => option.value === value);
    const nextKtru = match?.ktruCode ?? spec.templateKtruCode;
    const nextSpgz = match?.spgzCode ?? null;
    const ktruSame = !nextKtru || ktruVote()?.code === nextKtru;
    const spgzSame = !nextSpgz || spgz()?.code === nextSpgz;
    if (ktruSame && spgzSame) {
      setCharacteristics((items) => items.map((item) =>
        item.refinesKtru ? { ...item, value } : item));
      return;
    }
    const seq = nextRefineSeq();
    void applyCatalogPositions({
      ktruCode: !ktruLocked() && nextKtru && !ktruSame ? nextKtru : (ktruVote()?.code ?? null),
      spgzCode: !spgzLocked() && nextSpgz && !spgzSame ? nextSpgz : undefined,
      clearSpgz: !spgzLocked() && !nextSpgz && !!spgz(),
      discriminatorValue: value || null,
      applyPositionRefine: true,
      reuseSpecialization: true,
    }).then(() => {
      if (seq !== currentRefineSeq()) return;
    });
  };

  const applyKpgz = async (code: string, locked: boolean) => {
    const node = await getDirectoryNode("kpgz", code);
    if (!node || node.kind !== "kpgz") return;
    const spec = await loadSpecialization({ kpgzCode: code, spgzCode: null });
    batch(() => {
      setKpgz({ code: node.code, name: node.name, score: kpgz()?.score ?? 1 });
      setSpgz(null);
      setSpgzLocked(locked);
      setCatalogSpecialization(spec);
      setCharacteristics((current) => withCatalogSpecialization(
        current.filter((item) => item.source !== "spgz"),
        spec,
      ));
    });
  };

  const applySpgz = async (code: string, locked: boolean, discriminatorValue?: string | null) => {
    await applyCatalogPositions({
      spgzCode: code,
      spgzLocked: locked,
      ktruCode: ktruVote()?.code ?? (selected()?.kind === "ktru" ? selected()?.code : null),
      discriminatorValue,
    });
  };

  const openKtruPicker = () => {
    setKtruPickerSelected(selected());
    setKtruPickerPath([]);
    setKtruPickerOpen(true);
    const code = selected()?.code || ktruVote()?.code || okpd2()?.code;
    if (!code) return;
    void getKtruRevealPath(code).then((result) => {
      if (result.node) setKtruPickerSelected(result.node);
      setKtruPickerPath(result.pathIds);
    });
  };

  const openSpgzPicker = () => {
    setSpgzPickerSelected(null);
    setSpgzPickerPath([]);
    setSpgzPickerOpen(true);
    const code = spgz()?.code || kpgz()?.code;
    if (!code) return;
    void getDirectoryRevealPath("kpgz", code).then((result) => {
      if (result.node) setSpgzPickerSelected(result.node);
      setSpgzPickerPath(result.pathIds);
    });
  };

  const confirmKtruPicker = (node: KtruTreeNode) => {
    setKtruPickerOpen(false);
    if (node.kind === "okpd") void applyOkpd(node.code, true);
    else void applyKtru(node.code, true);
  };

  const confirmSpgzPicker = (node: DirTreeNode) => {
    setSpgzPickerOpen(false);
    if (node.kind === "kpgz") void applyKpgz(node.code, true);
    else void applySpgz(node.code, true);
  };

  const fillExample = () => {
    batch(() => {
      setName("");
      setNameTouched(false);
      setSku("MOL-500-DRV");
      setModel("");
      setBrand("");
      setSelected(null);
      setOkpd2(null);
      setKpgz(null);
      setSpgz(null);
      setKtruVote(null);
      setKtruLocked(false);
      setSpgzLocked(false);
      setSimilar([]);
      setLastSuggestQuery("");
      setSuggestDurationMs(null);
      setDescription("Молоток слесарный 500 г, боёк стальной круглый, рукоятка деревянная. Для слесарных и монтажных работ.");
      setCharacteristics([]);
      clearCharMeta();
      setGeneratedFor("");
      clearPhotos();
    });
  };

  const reset = () => {
    batch(() => {
      setName(""); setNameTouched(false); setSku(""); setModel(""); setBrand(""); setDescription("");
      setSelected(null); setOkpd2(null); setKpgz(null); setSpgz(null); setKtruVote(null);
      setKtruLocked(false); setSpgzLocked(false);
      setSimilar([]); setSuggesting(false); setLastSuggestQuery(""); setSuggestDurationMs(null);
      setCharacteristics([]); clearCharMeta(); setGeneratedFor("");
      clearPhotos();
    });
  };

  createEffect(on(description, (text) => {
    const query = text.trim().replace(/\s+/g, " ");
    if (query.length < 12) {
      batch(() => {
        setSimilar([]);
        setSuggesting(false);
        setLastSuggestQuery("");
        setSuggestDurationMs(null);
        if (!ktruLocked()) {
          setSelected(null);
          setOkpd2(null);
          setKtruVote(null);
          setCharacteristics((current) => current.filter((item) => item.source !== "ktru"));
          clearCharMeta();
          setGeneratedFor("");
        }
        if (!spgzLocked()) {
          setKpgz(null);
          setSpgz(null);
          setCharacteristics((current) => current.filter((item) => item.source !== "spgz"));
        }
      });
      return;
    }
    if (query === lastSuggestQuery()) {
      setSuggesting(false);
      return;
    }
    setSuggesting(true);
    let alive = true;
    const timer = setTimeout(() => {
      const started = performance.now();
      void suggestFromNeighbors(query).then(async (draft) => {
        if (!alive) return;
        try {
          const wantSpec = (!ktruLocked() || !spgzLocked()) && !!(draft.ktru || draft.spgz || draft.kpgz);
          const spec = wantSpec
            ? await getCatalogSpecialization({
              ktruCode: (!ktruLocked() ? draft.ktru?.code : ktruVote()?.code) ?? null,
              spgzCode: (!spgzLocked() ? draft.spgz?.code : spgz()?.code) ?? null,
              kpgzCode: (!spgzLocked() ? draft.kpgz?.code : kpgz()?.code) ?? null,
            })
            : null;
          if (!alive) return;
          setLastSuggestQuery(query);
          setSuggestDurationMs(Math.round(performance.now() - started));

          const draftKtru = draft.characteristics.filter((item) => item.source === "ktru");
          const draftSpgz = draft.characteristics.filter((item) => item.source === "spgz");
          const customKeep = characteristics().filter((item) => item.source === "custom" || item.custom);
          let nextChars: DraftCharacteristic[];
          if (!ktruLocked() && !spgzLocked()) nextChars = [...draftKtru, ...draftSpgz, ...customKeep];
          else if (!ktruLocked()) nextChars = [...draftKtru, ...characteristics().filter((item) => item.source !== "ktru")];
          else if (!spgzLocked()) nextChars = [...characteristics().filter((item) => item.source !== "spgz"), ...draftSpgz];
          else nextChars = characteristics();
          nextChars = withCatalogSpecialization(nextChars, spec);

          const refineItem = nextChars.find((item) =>
            item.refinesKtru && item.applyPositionRefine !== false && item.value.trim());
          const refineMatch = refineItem && spec
            ? spec.options.find((option) => option.value === refineItem.value)
            : undefined;
          // Resolve child codes before painting categories — avoid template → child jump.
          const resolvedKtruCode = (!ktruLocked() && refineMatch?.ktruCode)
            ? refineMatch.ktruCode
            : (!ktruLocked() ? draft.ktru?.code : null);
          const resolvedSpgzCode = (!spgzLocked() && refineMatch?.spgzCode)
            ? refineMatch.spgzCode
            : (!spgzLocked() ? draft.spgz?.code : null);

          batch(() => {
            setSimilar(draft.neighbors);
            setSuggesting(false);
            setCatalogSpecialization(spec);
            if (!ktruLocked()) {
              setOkpd2(draft.okpd2);
              setCharStandards(draft.standards);
              setNoNewFeatures(draft.noNewFeatures);
              setNoNewFeaturesReason(draft.noNewFeaturesReason);
            }
            if (!spgzLocked() && !refineMatch?.spgzCode) {
              setKpgz(draft.kpgz);
              setSpgz(draft.spgz);
            }
            if (!refineMatch) setCharacteristics(nextChars);
            // Sole OKPD2 template (or any draft KTRU without a discriminator pick) must still land in the vote card.
            if (!ktruLocked() && !refineMatch) {
              setKtruVote(draft.ktru);
              if (draft.ktru) {
                void getKtruNode(draft.ktru.code).then((node) => {
                  if (!alive || ktruLocked() || !node) return;
                  setSelected(node);
                  setGeneratedFor(`${node.code}\u0000${query}`);
                });
              } else {
                setSelected(null);
                setGeneratedFor(`\u0000${query}`);
              }
            }
            if (!nameTouched()) {
              setName(draft.productName || nameFromDescription(query));
              setNameTouched(false);
            }
          });

          if (refineMatch) {
            await applyCatalogPositions({
              ktruCode: resolvedKtruCode && !ktruLocked() ? resolvedKtruCode : null,
              spgzCode: resolvedSpgzCode && !spgzLocked() ? resolvedSpgzCode : null,
              discriminatorValue: refineItem?.value ?? null,
              applyPositionRefine: true,
              reuseSpecialization: true,
            });
          }
        } catch {
          if (alive) setSuggesting(false);
        }
      }, () => { if (alive) setSuggesting(false); });
    }, 420);
    onCleanup(() => { alive = false; clearTimeout(timer); });
  }));

  const updateCharacteristic = (id: string, value: string) => {
    const ids = new Set(linkedIdsFor(id));
    const target = characteristics().find((item) => ids.has(item.id));
    const next = target?.options.length ? mapValueToOptions(value, target.options) : value;
    setCharacteristics((items) => items.map((item) => {
      if (!ids.has(item.id)) return item;
      if (!isRangeCharacteristic(item)) return { ...item, value: next };
      const number = parseCharacteristicNumber(next);
      return {
        ...item,
        value: next,
        rangeOption: number == null ? "" : (matchNumberToRange(number, item.ranges) ?? mapValueToOptions(next, item.options)),
      };
    }));
    if (target?.refinesKtru && target.applyPositionRefine !== false) {
      refineFromDiscriminator(next);
    }
  };

  const togglePositionRefine = (id: string, enabled: boolean) => {
    const ids = new Set(linkedIdsFor(id));
    const keptValue = characteristics().find((item) => ids.has(item.id))?.value ?? "";
    if (enabled) {
      setCharacteristics((items) => items.map((item) =>
        ids.has(item.id) ? { ...item, applyPositionRefine: true } : item));
      if (keptValue.trim()) refineFromDiscriminator(keptValue);
      return;
    }
    const template = catalogSpecialization()?.templateKtruCode;
    if (!template) {
      setCharacteristics((items) => items.map((item) =>
        ids.has(item.id) ? { ...item, applyPositionRefine: false } : item));
      return;
    }
    const seq = nextRefineSeq();
    void applyCatalogPositions({
      ktruCode: !ktruLocked() ? template : null,
      clearSpgz: !spgzLocked() && !!spgz(),
      discriminatorValue: keptValue || null,
      applyPositionRefine: false,
    }).then(() => {
      if (seq !== currentRefineSeq()) return;
    });
  };

  const updateCharacteristicName = (id: string, name: string) => {
    const ids = new Set(linkedIdsFor(id));
    setCharacteristics((items) => items.map((item) => ids.has(item.id) ? { ...item, name } : item));
  };

  const updateCharacteristicRange = (id: string, rangeOption: string) => {
    const ids = new Set(linkedIdsFor(id));
    setCharacteristics((items) => items.map((item) => {
      if (!ids.has(item.id)) return item;
      return { ...item, rangeOption: mapValueToOptions(rangeOption, item.options) || rangeOption };
    }));
  };

  const removeCharacteristic = (id: string) => {
    const ids = new Set(linkedIdsFor(id));
    setCharacteristics((items) => items.filter((item) => !ids.has(item.id)));
  };

  const addCustomCharacteristic = () => {
    if (noNewFeatures()) return;
    const id = `custom-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
    setCharacteristics((items) => [...items, {
      id,
      name: "",
      value: "",
      rangeOption: "",
      required: false,
      badges: [],
      hint: "",
      options: [],
      optionLabels: {},
      ranges: [],
      unit: "",
      multiple: false,
      source: "custom",
      custom: true,
    }]);
  };

  const selectedOptions = (value: string) => splitCharacteristicValues(value);

  const choiceOptions = (item: DraftCharacteristic) => {
    const options = item.options ?? [];
    if (!item.value.trim()) return options;
    const extras = (item.multiple ? selectedOptions(item.value) : [item.value])
      .filter((entry) => !matchCharacteristicOption(entry, options));
    return extras.length ? [...extras, ...options] : options;
  };

  const optionLabel = (item: DraftCharacteristic, value: string) =>
    item.optionLabels?.[value] ?? value;

  const pickCharacteristicOption = (item: DraftCharacteristic, option: string) => {
    if (!item.required && item.value === option) {
      updateCharacteristic(item.id, "");
      return;
    }
    updateCharacteristic(item.id, option);
  };

  return <main id="main-content" class="page-width create-main">
    <Title>Новый товар — Каталог</Title>
    <Meta name="description" content="Создайте карточку товара: ближайшие СТЕ подскажут ОКПД2, КТРУ, КПГЗ, СПГЗ и характеристики." />

    <section class="create-hero">
      <div class="create-hero-row">
        <h1>Сначала<br />опишите <em>товар</em><span class="hero-asterisk" aria-hidden="true"><Sparkles strokeWidth={1.1} /></span></h1>
        <div class="create-top-actions">
          <Button variant="outline" size="sm" type="button" onClick={fillExample}><Lightbulb size={15} />Пример</Button>
          <Button variant="ghost" size="sm" type="button" onClick={reset} disabled={!hasDraft()}><RotateCcw size={14} />Очистить</Button>
        </div>
      </div>
    </section>

    <section class="create-describe" aria-labelledby="describe-heading">
      <div class="section-heading"><FileText size={20} strokeWidth={1.6} /><h2 id="describe-heading">Описание</h2></div>
      <TextField class="create-describe-field" name="product-description" value={description()} onChange={setDescription}>
        <TextFieldLabel class="sr-only">Описание товара</TextFieldLabel>
        <TextFieldTextarea id="product-description" rows={5} placeholder="Опишите товар своими словами: назначение, материал, размеры, комплектацию…" maxlength={3000} />
      </TextField>
      <div class="create-describe-meta"><span>Начните с типа товара, затем отличительные признаки: материал, размер, назначение. Без рекламных формулировок.</span><span>{description().length} / 3000</span></div>
    </section>

    <Show when={description().trim().length >= 12}>
      <section class="create-live" aria-live="polite">
        <div class="create-live-category">
          <div class="section-heading">
            <Tags size={18} strokeWidth={1.6} />
            <h2>Категории</h2>
            <Show when={!suggesting() && suggestDurationMs() != null}>
              <span>{formatSuggestTiming(suggestDurationMs()!)}</span>
            </Show>
          </div>
          <Show when={!suggesting()} fallback={<CategorySuggestSkeleton />}>
            <Show when={hasCategory()} fallback={<p class="create-live-hint">Пока не удалось вывести категории — уточните описание или выберите коды вручную.</p>}>
              <CategoryPairs
                okpd2={okpd2()}
                ktru={ktruVote()}
                kpgz={kpgz()}
                spgz={spgz()}
                onEditKtru={openKtruPicker}
                onEditSpgz={openSpgzPicker}
              />
            </Show>
          </Show>
        </div>
        <div class="create-live-similar">
          <div class="section-heading"><Sparkles size={18} strokeWidth={1.6} /><h2>Похожие товары</h2></div>
          <Show when={!suggesting()} fallback={<SimilarSuggestSkeleton />}>
            <Show when={similar().length} fallback={<p class="create-live-hint">Похожих товаров пока нет — уточните описание.</p>}>
              <div class="create-similar-row"><For each={similar()}>{product => <ProductCard product={product} returnTo="/" />}</For></div>
            </Show>
          </Show>
        </div>
      </section>
    </Show>

    <Show when={hasDraft()}>
      <section class="product-detail create-detail">
        <div class="product-gallery create-gallery">
          <input
            ref={photoInput}
            type="file"
            accept="image/*"
            multiple
            class="sr-only"
            onChange={(event) => {
              addPhotoFiles(event.currentTarget.files);
              event.currentTarget.value = "";
            }}
          />
          <div
            class="gallery-main create-gallery-main"
            classList={{ "create-gallery-dragging": photoDragging(), "create-gallery-filled": !!activePhoto() }}
            onDragEnter={(event) => { event.preventDefault(); setPhotoDragging(true); }}
            onDragOver={(event) => { event.preventDefault(); setPhotoDragging(true); }}
            onDragLeave={(event) => {
              if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
              setPhotoDragging(false);
            }}
            onDrop={(event) => {
              event.preventDefault();
              setPhotoDragging(false);
              addPhotoFiles(event.dataTransfer?.files ?? null);
            }}
          >
            <Show when={activePhoto()} fallback={
              <button type="button" class="create-gallery-empty" onClick={() => photoInput?.click()}>
                <ImagePlus size={42} strokeWidth={1.2} />
                <span>Перетащите фото или нажмите, чтобы загрузить</span>
                <em>до {MAX_PHOTOS} изображений</em>
              </button>
            }>
              {photo => <>
                <img src={photo().url} alt={photo().name} />
                <div class="create-gallery-toolbar">
                  <Button type="button" variant="outline" size="sm" onClick={() => photoInput?.click()} disabled={photos().length >= MAX_PHOTOS}>
                    <ImagePlus size={14} />Добавить
                  </Button>
                  <Button type="button" variant="ghost" size="sm" onClick={() => removePhoto(photo().id)}>
                    <X size={14} />Удалить
                  </Button>
                </div>
              </>}
            </Show>
          </div>
          <Show when={photos().length}>
            <div class="gallery-thumbnails create-gallery-thumbs" aria-label="Загруженные фото">
              <For each={photos()}>{(photo, index) =>
                <button type="button" classList={{ selected: photoIndex() === index() }} aria-label={`Фото ${index() + 1}`} aria-pressed={photoIndex() === index()} onClick={() => setPhotoIndex(index())}>
                  <img src={photo.url} alt="" />
                </button>
              }</For>
              <Show when={photos().length < MAX_PHOTOS}>
                <button type="button" class="create-gallery-add-thumb" aria-label="Добавить фото" onClick={() => photoInput?.click()}>
                  <ImagePlus size={18} strokeWidth={1.4} />
                </button>
              </Show>
            </div>
          </Show>
        </div>
        <div class="detail-summary create-summary">
          <div class="detail-category">{selected()?.name || ktruVote()?.name || spgz()?.name || "Категория не выбрана"}</div>
          <div class="create-name-wrap">
            <TextField class="create-name-field" name="product-name" value={name()} onChange={(value) => { setName(value); setNameTouched(true); }} required>
              <TextFieldLabel class="sr-only">Наименование</TextFieldLabel>
              <TextFieldTextarea id="product-name" required placeholder="Наименование товара" maxlength={200} rows={2} />
            </TextField>
            <span class="create-name-edit" aria-hidden="true" title="Можно изменить">
              <Pencil size={15} strokeWidth={1.6} />
            </span>
          </div>
          <dl class="detail-facts">
            <div>
              <dt>Производитель</dt>
              <dd><TextField class="create-inline-field" name="product-brand" value={brand()} onChange={setBrand}><TextFieldLabel class="sr-only">Производитель</TextFieldLabel><TextFieldInput id="product-brand" placeholder="Производитель или бренд" maxlength={120} /></TextField></dd>
            </div>
            <div>
              <dt>Артикул</dt>
              <dd><TextField class="create-inline-field" name="product-sku" value={sku()} onChange={setSku} required><TextFieldLabel class="sr-only">Артикул</TextFieldLabel><TextFieldInput id="product-sku" required placeholder="Ваш код" maxlength={80} /></TextField></dd>
            </div>
            <div>
              <dt>Модель</dt>
              <dd><TextField class="create-inline-field" name="product-model" value={model()} onChange={setModel}><TextFieldLabel class="sr-only">Модель</TextFieldLabel><TextFieldInput id="product-model" placeholder="Необязательно" maxlength={120} /></TextField></dd>
            </div>
          </dl>
          <Show when={outdated()}><div class="create-summary-actions"><Badge variant="outline">Описание изменилось</Badge></div></Show>
          <input type="hidden" name="product-category" value={selected()?.code ?? ""} />
        </div>
      </section>

      <section class="characteristics-section create-characteristics" aria-labelledby="create-chars-heading">
        <div class="section-heading"><Tags size={21} strokeWidth={1.6} /><h2 id="create-chars-heading">Характеристики</h2><Show when={displayCharacteristics().length}><span>{filledCount()} / {displayCharacteristics().length}</span></Show></div>
        <Show when={charStandards().length}>
          <p class="create-char-standards">{charStandards().join(", ")}</p>
        </Show>
        <Show when={displayCharacteristics().length} fallback={<p class="no-characteristics">Выберите КТРУ или СПГЗ — появятся поля из справочников.</p>}>
          <dl class="characteristics-table create-char-fields dir-characteristics-table"><Index each={displayCharacteristics()}>{item =>
            <CreateCharRow
              item={item}
              choiceOptions={choiceOptions}
              optionLabel={optionLabel}
              selectedOptions={selectedOptions}
              refinedRefs={refinedPositionRefs(item())}
              onUpdate={updateCharacteristic}
              onUpdateName={updateCharacteristicName}
              onUpdateRange={updateCharacteristicRange}
              onTogglePositionRefine={togglePositionRefine}
              onRemove={removeCharacteristic}
              onPickOption={pickCharacteristicOption}
            />
          }</Index></dl>
        </Show>
        <Show when={hasKtruPosition()}>
          <Show when={noNewFeatures()} fallback={
            <div class="create-char-footer">
              <button type="button" class="create-char-add" onClick={addCustomCharacteristic}>
                <Plus size={13} strokeWidth={1.8} />Добавить характеристику
              </button>
            </div>
          }>
            <div class="create-char-alert" role="alert">
              <CircleAlert size={18} strokeWidth={1.6} />
              <div>
                <p>Эта позиция КТРУ не допускает новых характеристик — доступны только поля из справочника.</p>
                <Show when={noNewFeaturesReason()}><p class="create-char-alert-reason">{noNewFeaturesReason()}</p></Show>
              </div>
            </div>
          </Show>
        </Show>
      </section>
    </Show>

    <Dialog open={ktruPickerOpen()} onOpenChange={setKtruPickerOpen}>
      <Show when={ktruPickerOpen()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>ОКПД2 и КТРУ</DialogTitle>
            <DialogDescription>Можно выбрать группу ОКПД2 без КТРУ или конкретную позицию КТРУ.</DialogDescription>
          </DialogHeader>
          <KtruPickerBody selected={ktruPickerSelected()} onSelect={setKtruPickerSelected} pathIds={ktruPickerPath()} />
          <DialogFooter>
            <KtruPickerActions selected={ktruPickerSelected()} onCancel={() => setKtruPickerOpen(false)} onConfirm={confirmKtruPicker} />
          </DialogFooter>
        </DialogContent>
      </Show>
    </Dialog>

    <Dialog open={spgzPickerOpen()} onOpenChange={setSpgzPickerOpen}>
      <Show when={spgzPickerOpen()}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>КПГЗ и СПГЗ</DialogTitle>
            <DialogDescription>Можно выбрать категорию КПГЗ без СПГЗ или конкретную позицию СПГЗ.</DialogDescription>
          </DialogHeader>
          <SpgzPickerBody selected={spgzPickerSelected()} onSelect={setSpgzPickerSelected} pathIds={spgzPickerPath()} />
          <DialogFooter>
            <SpgzPickerActions selected={spgzPickerSelected()} onCancel={() => setSpgzPickerOpen(false)} onConfirm={confirmSpgzPicker} />
          </DialogFooter>
        </DialogContent>
      </Show>
    </Dialog>
  </main>;
}
