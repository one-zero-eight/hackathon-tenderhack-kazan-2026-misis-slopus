import { Meta, Title } from "@solidjs/meta";
import { A } from "@solidjs/router";
import * as Tooltip from "@kobalte/core/tooltip";
import CircleAlert from "lucide-solid/icons/circle-alert";
import ClipboardList from "lucide-solid/icons/clipboard-list";
import Check from "lucide-solid/icons/check";
import LoaderCircle from "lucide-solid/icons/loader-circle";
import FileText from "lucide-solid/icons/file-text";
import ImagePlus from "lucide-solid/icons/image-plus";
import Lightbulb from "lucide-solid/icons/lightbulb";
import History from "lucide-solid/icons/history";
import MousePointer from "lucide-solid/icons/mouse-pointer";
import Upload from "lucide-solid/icons/upload";
import Pencil from "lucide-solid/icons/pencil";
import Plus from "lucide-solid/icons/plus";
import RotateCcw from "lucide-solid/icons/rotate-ccw";
import Sparkles from "lucide-solid/icons/sparkles";
import Tags from "lucide-solid/icons/tags";
import X from "lucide-solid/icons/x";
import { batch, createEffect, createMemo, createSignal, For, Index, on, onCleanup, onMount, Show } from "solid-js";
import { directoryHref, ProductCard } from "~/components/catalog";
import { CategoryPairs, PairChoices } from "~/components/category-pairs";
import { CatalogCharacteristicInput } from "~/components/catalog-characteristic-input";
import { CharacteristicEditor } from "~/components/characteristic-editor";
import { characteristicNameKey, isVisibleCharacteristic } from "~/lib/characteristic-editor";
import { QueryDropdown } from "~/components/query-dropdown";
import { KtruPickerActions, KtruPickerBody } from "~/components/ktru-picker";
import { SpgzPickerActions, SpgzPickerBody } from "~/components/spgz-picker";
import { apiAutofillCharacteristics, apiSuggestCategoriesStream, isAbortError, type NeighborSuggestion } from "~/lib/api";
import {
  characteristicRangeProblem,
  isRangeCharacteristic,
  isYesNoOptions,
  matchCharacteristicOption,
  matchNumberToRange,
  parseCharacteristicNumber,
  splitCharacteristicValues,
  yesNoOptionPair,
} from "~/lib/characteristic-options";
import { getCatalogCharacteristics, getCatalogSpecialization, getDirectoryNode, getDirectoryRecord, getDirectoryRevealPath, getKtruNode, getKtruRecord, getKtruRevealPath } from "~/lib/rpc";
import {
  appendCatalogCharacteristics,
  autofillConfidencePresentation,
  buildDisplayCharacteristics,
  catalogFrequencyLabel,
  catalogOptionPopularity,
  characteristicSourceLabel,
  mapValueToOptions,
  retainTemplateCharacteristics,
} from "~/lib/merge-characteristics";
import {
  cascadingSpecializationValues,
  filterSpecializationOptions,
  normSpecName,
  specializationOptionLabel,
} from "~/lib/specialization";
import {
  autofilledCharacteristicValues,
  autofilledProductEvidence,
  autofilledProductValues,
  captureQueryDraft,
  restoreQueryDraft,
  clearCategoryDecisions,
  manuallyEditedProductFields,
  okpdKtruDecision,
  setOkpdKtruDecision,
  kpgzSpgzDecision,
  setKpgzSpgzDecision,
  brand,
  catalogSpecialization,
  catalogCharacteristicSuggestions,
  setCatalogCharacteristicSuggestions,
  initialTemplateKtruCode,
  setInitialTemplateKtruCode,
  characteristicEditKey,
  characteristics,
  preserveManualCharacteristics,
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
  lastAutofillKey,
  lastSuggestQuery,
  manuallyEditedCharacteristics,
  manuallyEditedCharacteristicValues,
  model,
  name,
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
  setLastAutofillKey,
  setLastSuggestQuery,
  setModel,
  setName,
  setNoNewFeatures,
  setNoNewFeaturesReason,
  setOkpd2,
  setPhotoIndex,
  setPhotos,
  setSelected,
  setSimilar,
  setSimilarDurationMs,
  setSku,
  setSpgz,
  setSpgzLocked,
  setSpgzPickerPath,
  setSpgzPickerSelected,
  setSuggestDurationMs,
  setSuggesting,
  similar,
  similarDurationMs,
  sku,
  spgz,
  spgzLocked,
  spgzPickerPath,
  spgzPickerSelected,
  suggestDurationMs,
  suggesting,
} from "~/lib/create-product-state";
import { draftFromSpgzCharacteristic } from "~/lib/spgz-characteristics";
import { normalizeQuery, parseQueryFile, parseQueryHistory, QUERY_HISTORY_KEY, QueryQueue, rememberQuery, type QueuedQuery, type QueryHistoryItem, type QueryStatus } from "~/lib/query-queue";
import {
  collectEvidenceSpans,
  evidenceFromField,
  evidenceSourceLabel,
  formatEvidenceConversion,
  highlightDescription,
} from "~/lib/evidence-highlight";
import type { AutofillEvidence, CatalogSpecialization, CategoryPairCandidate, CategoryPairDecision, DirTreeNode, DraftCharacteristic, KtruTreeNode, VotedCode } from "~/lib/types";
import { checkSpelling, spellerErrorLabel, type SpellerError } from "~/lib/yandex-speller";
import { Badge, Button, Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Skeleton, TextField, TextFieldInput, TextFieldLabel, TextFieldTextarea } from "~/ui-kit";
import type { WebEnrichmentSuggestion } from "~/lib/types";
import "./create-product.css";
import "./directories.css";
import "./ktru.css";

const MAX_PHOTOS = 8;

function formatSuggestTiming(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} мс` : `${(ms / 1000).toFixed(2)} с`;
}

/** Split suggested description so the appended web facts can be highlighted. */
function enrichmentDescriptionParts(original: string, suggested: string): { kept: string; added: string } {
  const next = suggested.trim();
  const raw = original.trim();
  const base = raw.replace(/\.+\s*$/, "");
  if (base && next.startsWith(base)) return { kept: base, added: next.slice(base.length) };
  if (raw && next.startsWith(raw)) return { kept: raw, added: next.slice(raw.length) };
  return { kept: "", added: next };
}

function normCharName(name: string): string {
  return normSpecName(name);
}

function selectedRefineValues(
  fields: DraftCharacteristic[],
  override?: Record<string, string> | null,
): Record<string, string> {
  if (override !== undefined) {
    if (!override) return {};
    return Object.fromEntries(
      Object.entries(override)
        .map(([name, value]) => [name, value.trim()])
        .filter(([, value]) => value),
    );
  }
  const selected: Record<string, string> = {};
  for (const item of fields) {
    if (!item.refinesKtru || !item.value.trim()) continue;
    selected[item.name] = item.value.trim();
  }
  return selected;
}

function withCatalogSpecialization(
  fields: DraftCharacteristic[],
  spec: CatalogSpecialization | null,
  selectedValues?: Record<string, string> | null,
): DraftCharacteristic[] {
  if (!spec) return fields.map((item) => ({ ...item, refinesKtru: false }));
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
  const previousValues = new Map(
    fields.map((item) => [normCharName(item.name), item.value]),
  );
  // Only an explicit choice (user / refine) fills discriminators — never auto from current KTRU.
  const preferred = selectedValues === undefined
    ? null
    : selectedRefineValues(fields, selectedValues);
  const refineNames = new Map(
    spec.characteristics.map((item) => [normCharName(item.name), item]),
  );
  const refineIds = new Set<string>();
  const mapped = fields.map((item): DraftCharacteristic => {
    const characteristic = refineNames.get(normCharName(item.name));
    if (!characteristic) return { ...item, refinesKtru: false };
    if (item.source !== "ktru" && item.source !== "spgz" && item.source !== "both") {
      return { ...item, refinesKtru: false };
    }
    refineIds.add(item.id);
    const selected = preferred ?? selectedRefineValues(
      fields.map((entry) => entry.id === item.id ? { ...entry, refinesKtru: true } : entry),
    );
    const options = cascadingSpecializationValues(spec.options, characteristic.name, selected);
    const raw = preferred
      ? (preferred[characteristic.name] ?? preferred[item.name] ?? "")
      : (previousValues.get(normCharName(characteristic.name)) ?? item.value);
    const value = raw.trim() ? mapValueToOptions(raw, options) || raw : "";
    return {
      ...item,
      name: characteristic.name,
      options,
      optionLabels: {},
      value,
      rangeOption: "",
      required: true,
      multiple: false,
      numeric: false,
      unit: "",
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
  const present = new Set(
    mapped.filter((item) => item.refinesKtru).map((item) => normCharName(item.name)),
  );
  const selected = preferred ?? selectedRefineValues(mapped);
  for (const characteristic of [...spec.characteristics].reverse()) {
    if (present.has(normCharName(characteristic.name))) continue;
    const options = cascadingSpecializationValues(spec.options, characteristic.name, selected);
    const raw = preferred?.[characteristic.name] ?? "";
    const value = raw.trim() ? mapValueToOptions(raw, options) || raw : "";
    mapped.unshift({
      id: characteristic.code || `refine:${characteristic.name}`,
      name: characteristic.name,
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
      applyPositionRefine: previousRefine.get(normCharName(characteristic.name)) ?? true,
    });
  }
  // Refresh cascading option lists after all refine rows exist.
  const withRefine = mapped.map((item) => {
    if (!item.refinesKtru) return item;
    const options = cascadingSpecializationValues(spec.options, item.name, selected);
    return { ...item, options };
  });
  return [
    ...withRefine.filter((item) => item.refinesKtru),
    ...withRefine.filter((item) => !item.refinesKtru),
  ];
}

type RefinedPositionRef = {
  tab: "ktru" | "spgz";
  code: string;
  name: string;
  kindLabel: string;
};

function AutofillConfidence(props: { item: DraftCharacteristic; inSelect?: boolean }) {
  const score = () => props.item.autofill;
  const diagnostics = () => props.item.autofillDiagnostics;
  const presentation = () => autofillConfidencePresentation(score()!.confidence);
  return <Show when={score() && props.item.value.trim()}>
    <Tooltip.Root placement="top" openDelay={200} closeDelay={100} gutter={8}>
      <Tooltip.Trigger
        as="span"
        class={`create-char-confidence ${presentation().level}`}
        tabindex={props.inSelect ? -1 : 0}
        aria-label={`Уверенность проверки Jev: ${presentation().percent}%`}
      >
        {presentation().percent}%
      </Tooltip.Trigger>
      <Tooltip.Portal>
        <Tooltip.Content class="create-char-confidence-tooltip">
          <strong>{presentation().label} · {presentation().percent}%</strong>
          <p>Так Jev оценивает подтверждённость выбранного значения по описанию товара. Это не гарантия правильности.</p>
          <Show when={diagnostics()?.quote}><p class="create-char-quote">«{diagnostics()!.quote}»</p></Show>
          <Show when={diagnostics()?.conversion}>
            <p>Пересчёт: {diagnostics()!.conversion!.sourceValue} {diagnostics()!.conversion!.from} → {diagnostics()!.conversion!.result} {diagnostics()!.conversion!.to}</p>
          </Show>
          <Tooltip.Arrow />
        </Tooltip.Content>
      </Tooltip.Portal>
    </Tooltip.Root>
  </Show>;
}

function AutofillLocate(props: {
  evidence: AutofillEvidence | null | undefined;
  label?: string;
  onLocate: () => void;
}) {
  return <Show when={props.evidence}>
    <button
      type="button"
      class="create-char-locate"
      aria-label={props.label ?? "Показать цитату в описании"}
      title="Показать в описании"
      onClick={props.onLocate}
    >
      <MousePointer size={14} strokeWidth={1.8} />
    </button>
  </Show>;
}

function AutofillRejected(props: { item: DraftCharacteristic }) {
  const diagnostics = () => props.item.autofillDiagnostics;
  return <Show when={diagnostics()?.status === "rejected" && !props.item.value.trim()}>
    <div class="create-char-rejected" title={diagnostics()!.errors?.join("; ") || "Отклонено"}>
      Отклонено{diagnostics()?.confidence != null ? ` · ${Math.round(diagnostics()!.confidence! * 100)}%` : ""}
      <Show when={diagnostics()?.errors?.[0]}><span>: {diagnostics()!.errors![0]}</span></Show>
      <Show when={diagnostics()?.suggestion}>{(hint) =>
        <p class="create-char-scale-hint" role="note" title={`${hint().value} ${hint().unit} → ${hint().converted} в единицах схемы`}>
          Подсказка: {hint().reason}
        </p>
      }</Show>
    </div>
  </Show>;
}

function OptionPopularity(props: { item: DraftCharacteristic; option: string }) {
  const popularity = createMemo(() => catalogOptionPopularity(props.item, props.option));
  return <Show when={popularity()}>{(stats) =>
    <span
      class="create-char-combobox-popularity"
      title={props.option === "—"
        ? `Каталог: характеристика не указана у ${stats().count} из ${stats().total} товаров`
        : `Каталог: ${stats().count} из ${stats().total} товаров с этой характеристикой`}
      aria-label={`Популярность в каталоге: ${stats().percent}%`}
    >
      Популярность {stats().percent}%
    </span>
  }</Show>;
}

function CreateCharRow(props: {
  item: () => DraftCharacteristic;
  choiceOptions: (item: DraftCharacteristic) => string[];
  optionLabel: (item: DraftCharacteristic, value: string) => string;
  selectedOptions: (value: string) => string[];
  refinedRefs?: RefinedPositionRef[];
  onUpdate: (id: string, value: string) => void;
  onEditSchema: (item: DraftCharacteristic) => void;
  onUpdateRange: (id: string, rangeOption: string) => void;
  onTogglePositionRefine?: (id: string, enabled: boolean) => void;
  onRemove: (id: string) => void;
  onPickOption: (item: DraftCharacteristic, option: string) => void;
  onLocateEvidence?: (id: string) => void;
}) {
  const item = () => props.item();
  const rangeProblem = createMemo(() => characteristicRangeProblem(item()));
  const refineEnabled = () => item().applyPositionRefine !== false;
  const locate = () => <Show when={item().value.trim() && evidenceFromField(item()) && props.onLocateEvidence}>
    <AutofillLocate evidence={evidenceFromField(item())} onLocate={() => props.onLocateEvidence!(item().id)} />
  </Show>;
  return (
            <div class="create-char-row" classList={{ "create-char-row-detached": !!item().refinesKtru && !refineEnabled() }}>
              <dt>
                <Show when={item().custom} fallback={
                  <span class="dir-char-name">
                    {item().name}
                    <Show when={item().required}><span class="create-char-req" aria-label="обязательная">*</span></Show>
                    <span class="create-char-source" title={item().catalogFrequency ? catalogFrequencyLabel(item().catalogFrequency!) : item().templateKtruCode ? `Шаблон КТРУ ${item().templateKtruCode}` : undefined}>{characteristicSourceLabel(item().source, item().templateKtruCode)}</span>
                    {locate()}
                    <Show when={item().source === "catalog"}>
                      <button type="button" class="create-char-remove" aria-label={`Изменить схему: ${item().name}`} title="Изменить тип, единицы и схему" onClick={() => props.onEditSchema(item())}>
                        <Pencil size={14} />
                      </button>
                    </Show>
                  </span>
                }>
                  <div class="create-char-custom-name">
                    <span class="dir-char-name">
                      {item().name}
                      <Show when={item().required}><span class="create-char-req" aria-label="обязательная">*</span></Show>
                    </span>
                    <span class="create-char-source">{characteristicSourceLabel("custom")}</span>
                    <button type="button" class="create-char-remove" aria-label={`Изменить схему: ${item().name}`} title="Изменить тип, единицы и схему" onClick={() => props.onEditSchema(item())}>
                      <Pencil size={14} />
                    </button>
                    {locate()}
                    <button type="button" class="create-char-remove" aria-label="Удалить характеристику" onClick={() => props.onRemove(item().id)}>
                      <X size={14} />
                    </button>
                  </div>
                </Show>
                <Show when={item().badges.length}>
                  <span class="create-char-badges">
                    <For each={item().badges}>{badge => <span class="create-char-badge">{badge}</span>}</For>
                  </span>
                </Show>
              </dt>
              <dd>
                <div class="create-char-value" classList={{ "has-confidence": !!item().autofill && !!item().value.trim() }}>
                <Show when={item().source === "catalog" || (item().custom && !!item().catalogFrequency && !item().options.length && !item().ranges.length)} fallback={
                <Show when={isRangeCharacteristic(item())} fallback={
                  <Show when={(item().options ?? []).length} fallback={
                    <TextField class="create-inline-field" value={item().value} onChange={(value) => props.onUpdate(item().id, value)}>
                      <TextFieldLabel class="sr-only">{item().name || "Значение характеристики"}</TextFieldLabel>
                      <div class="create-char-text-input" classList={{ "has-confidence": !!item().autofill && !!item().value.trim() }}>
                        <TextFieldInput
                          inputmode={item().numeric ? "decimal" : "text"}
                          placeholder={item().numeric ? "Число" : item().custom ? "Значение" : item().hint ? `Напр.: ${item().hint.split(" · ")[0]}` : "Укажите значение"}
                        />
                        <Show when={item().unit}><span class="create-char-unit">{item().unit}</span></Show>
                        <AutofillConfidence item={item()} />
                      </div>
                    </TextField>
                  }>
                    <Show when={isYesNoOptions(props.choiceOptions(item()))} fallback={
                      <Show when={item().multiple} fallback={
                        <Select<string>
                          class="create-char-select"
                          options={["—", ...props.choiceOptions(item())]}
                          value={item().value || "—"}
                          onChange={(value) => props.onUpdate(item().id, value === "—" ? "" : value ?? "")}
                          placeholder="Выберите значение"
                          selectionBehavior="replace"
                          itemComponent={(selectProps) => <SelectItem class="create-char-option-item" item={selectProps.item}>
                            <span class="create-char-option-label" classList={{ "create-char-placeholder": selectProps.item.rawValue === "—" }}>{props.optionLabel(item(), selectProps.item.rawValue)}</span>
                            <OptionPopularity item={item()} option={selectProps.item.rawValue} />
                          </SelectItem>}
                        >
                          <SelectTrigger class="create-char-select-trigger" aria-label={item().name}>
                            <span class="create-char-selection">
                              <SelectValue<string>>{(state) => {
                                const selected = state.selectedOption();
                                return selected === "—"
                                  ? <span class="create-char-placeholder">—</span>
                                  : selected ? props.optionLabel(item(), selected) : "Выберите значение";
                              }}</SelectValue>
                              <AutofillConfidence item={item()} inSelect />
                            </span>
                          </SelectTrigger>
                          <SelectContent />
                        </Select>
                      }>
                        <Select<string>
                          class="create-char-select"
                          multiple
                          options={["—", ...props.choiceOptions(item())]}
                          value={item().value ? props.selectedOptions(item().value) : ["—"]}
                          onChange={(values) => {
                            const cleared = values.includes("—") && !!item().value;
                            props.onUpdate(item().id, cleared ? "" : values.filter((value) => value !== "—").join(" · "));
                          }}
                          placeholder="Выберите значения"
                          itemComponent={(selectProps) => <SelectItem class="create-char-option-item" item={selectProps.item}>
                            <span class="create-char-option-label" classList={{ "create-char-placeholder": selectProps.item.rawValue === "—" }}>{props.optionLabel(item(), selectProps.item.rawValue)}</span>
                            <OptionPopularity item={item()} option={selectProps.item.rawValue} />
                          </SelectItem>}
                        >
                          <SelectTrigger class="create-char-select-trigger" aria-label={item().name}>
                            <span class="create-char-selection">
                              <SelectValue<string>>{(state) => {
                                const selected = state.selectedOptions();
                                return selected.length === 1 && selected[0] === "—"
                                  ? <span class="create-char-placeholder">—</span>
                                  : selected.length ? selected.map((value) => props.optionLabel(item(), value)).join(" · ") : "Выберите значения";
                              }}</SelectValue>
                              <AutofillConfidence item={item()} inSelect />
                            </span>
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
                        <AutofillConfidence item={item()} />
                      </div>
                    </Show>
                  </Show>
                }>
                  <div class="create-char-quantity">
                    <div class="create-char-number" classList={{ "has-confidence": !!item().autofill && !!item().value.trim() }}>
                    <div
                      class="create-char-quantity-input"
                      onClick={(event) => event.currentTarget.querySelector("input")?.focus()}
                    >
                      <span class="sr-only">{item().name}</span>
                      <Tooltip.Root placement="top" openDelay={200} closeDelay={100} gutter={8} disabled={!rangeProblem()}>
                        <Tooltip.Trigger as="span" class="create-char-range-trigger" tabindex={-1}>
                          <input
                            type="text"
                            inputmode="decimal"
                            classList={{ "create-char-range-problem": !!rangeProblem() }}
                            aria-label={[item().name, item().unit].filter(Boolean).join(", ")}
                            aria-invalid={rangeProblem() ? "true" : undefined}
                            style={{ width: `${item().value ? Math.max(3, item().value.length) : 7}ch` }}
                            value={item().value}
                            placeholder="Число"
                            onInput={(event) => props.onUpdate(item().id, event.currentTarget.value)}
                          />
                        </Tooltip.Trigger>
                        <Tooltip.Portal>
                          <Tooltip.Content class="create-char-confidence-tooltip">
                            <strong>Проверьте значение</strong>
                            <p>{rangeProblem()}</p>
                            <Tooltip.Arrow />
                          </Tooltip.Content>
                        </Tooltip.Portal>
                      </Tooltip.Root>
                      <Show when={item().unit}><span class="create-char-unit">{item().unit}</span></Show>
                      <AutofillConfidence item={item()} />
                    </div>
                    </div>
                    <Select<string>
                      class="create-char-select create-char-range-select"
                      options={["—", ...item().options]}
                      value={item().rangeOption || "—"}
                      onChange={(value) => {
                        if (value === "—") {
                          props.onUpdate(item().id, "");
                          props.onUpdateRange(item().id, "");
                          return;
                        }
                        props.onUpdateRange(item().id, value ?? "");
                      }}
                      placeholder="Диапазон"
                      selectionBehavior="replace"
                      itemComponent={(selectProps) => <SelectItem class="create-char-option-item" item={selectProps.item}>
                        <span class="create-char-option-label" classList={{ "create-char-placeholder": selectProps.item.rawValue === "—" }}>{props.optionLabel(item(), selectProps.item.rawValue)}</span>
                        <OptionPopularity item={item()} option={selectProps.item.rawValue} />
                      </SelectItem>}
                    >
                      <SelectTrigger class="create-char-select-trigger" aria-label={`${item().name}: диапазон`}>
                        <SelectValue<string>>{(state) =>
                          !state.selectedOption() || state.selectedOption() === "—"
                            ? <span class="create-char-placeholder">Диапазон</span>
                            : props.optionLabel(item(), state.selectedOption())
                        }</SelectValue>
                      </SelectTrigger>
                      <SelectContent />
                    </Select>
                  </div>
                </Show>
                }>
                  <CatalogCharacteristicInput
                    name={item().name}
                    value={item().value}
                    examples={item().catalogFrequency!.examples}
                    exampleCounts={item().catalogFrequency!.exampleCounts}
                    count={item().catalogFrequency!.count}
                    unit={item().unit}
                    numeric={item().numeric}
                    onChange={(value) => props.onUpdate(item().id, value)}
                  >
                    <AutofillConfidence item={item()} />
                  </CatalogCharacteristicInput>
                </Show>
                </div>
                <AutofillRejected item={item()} />
                <Show when={item().refinesKtru && props.onTogglePositionRefine} fallback={
                  <Show when={item().hint}>
                    <p class="create-char-hint">
                      <Show when={/^ГОСТ(?:\s|$)/i.test(item().hint)} fallback={item().hint}>
                        <a href={`https://yandex.ru/search/?text=${encodeURIComponent(item().hint)}`} target="_blank" rel="noopener noreferrer" title="Найти ГОСТ в Яндексе">
                          {item().hint}
                        </a>
                      </Show>
                    </p>
                  </Show>
                }>
                  <div class="create-char-hint-row" classList={{ detached: !refineEnabled() }}>
                    <Show when={item().hint}>
                      <p class="create-char-hint">
                        <Show when={/^ГОСТ(?:\s|$)/i.test(item().hint)} fallback={item().hint}>
                          <a href={`https://yandex.ru/search/?text=${encodeURIComponent(item().hint)}`} target="_blank" rel="noopener noreferrer" title="Найти ГОСТ в Яндексе">
                            {item().hint}
                          </a>
                        </Show>
                      </p>
                    </Show>
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
                          <div class="create-char-ktru-ref-row">
                            <A class="create-char-ktru-ref-link" href={directoryHref(ref.tab, ref.code)} title={`Открыть ${ref.kindLabel} в справочнике`}>
                              <span class="create-char-ktru-ref-code">{ref.kindLabel} {ref.code}</span>
                            </A>
                            <Show when={ref.name}><span class="create-char-ktru-ref-name">{ref.name}</span></Show>
                          </div>
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
    <div class="create-suggest-status" role="status" aria-live="polite" aria-label="Ищем подходящую категорию">
      <p class="create-live-hint create-live-hint-pulse">Ищем подходящую категорию</p>
      <div class="create-vote-card create-vote-card-skeleton" aria-hidden="true">
        <For each={["ОКПД2 и КТРУ", "Из каталога Портала Поставщиков"]}>{title =>
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
  type Suggestion = NeighborSuggestion;
  const [queuedQueries, setQueuedQueries] = createSignal<QueuedQuery<Suggestion>[]>([]);
  const [batchIds, setBatchIds] = createSignal<string[]>([]);
  const [activeQueryId, setActiveQueryId] = createSignal<string | null>(null);
  const [querySelectionSeq, setQuerySelectionSeq] = createSignal(0);
  const [queryHistory, setQueryHistory] = createSignal<QueryHistoryItem[]>([]);
  const [queryFileError, setQueryFileError] = createSignal("");
  const [historyError, setHistoryError] = createSignal("");
  const [suggestError, setSuggestError] = createSignal("");
  const [queryQuality, setQueryQuality] = createSignal<NonNullable<Suggestion["queryQuality"]> | null>(null);
  const [similarReady, setSimilarReady] = createSignal(false);
  const [forceSuggestQuery, setForceSuggestQuery] = createSignal<string | null>(null);
  const [suggestAcceptedQuery, setSuggestAcceptedQuery] = createSignal("");
  const [readingQueries, setReadingQueries] = createSignal(false);
  const queryDrafts = new Map<string, {
    draft: ReturnType<typeof captureQueryDraft>;
    autofillCompletedText: string | null;
    autofillDurationMs: number | null;
  }>();
  let queryFileInput: HTMLInputElement | undefined;
  let mounted = true;
  const saveHistory = (text: string) => {
    if (!mounted) return;
    const next = rememberQuery(queryHistory(), text);
    setQueryHistory(next);
    try {
      localStorage.setItem(QUERY_HISTORY_KEY, JSON.stringify(next));
      setHistoryError("");
    } catch {
      setHistoryError("Не удалось сохранить историю: localStorage недоступен или переполнен.");
    }
  };
  const queryQueue = new QueryQueue<Suggestion>(async (text, signal, reportProgress) => {
    const query = normalizeQuery(text);
    if (query.length < 3) throw new Error("Для поиска нужно не менее 3 символов.");
    return apiSuggestCategoriesStream(query, { force: forceSuggestQuery() === query }, signal, reportProgress);
  }, (items) => { if (mounted) setQueuedQueries(items); }, saveHistory);
  createEffect(() => {
    const item = queuedQueries().find((entry) => entry.id === activeQueryId());
    if (!item || normalizeQuery(item.text) !== normalizeQuery(description())) return;
    const progress = item.result ?? item.progress;
    if (!progress) return;
    batch(() => {
      if (progress.queryQuality !== undefined) setQueryQuality(progress.queryQuality);
      if (progress.neighbors !== undefined) {
        setSimilar(progress.neighbors);
        setSimilarDurationMs(progress.neighborsDurationMs ?? null);
        setSimilarReady(true);
      }
      if (progress.neighborError) setSuggestError(progress.neighborError);
    });
  });
  onMount(() => {
    try { setQueryHistory(parseQueryHistory(localStorage.getItem(QUERY_HISTORY_KEY))); }
    catch { setHistoryError("Не удалось прочитать историю запросов."); }
  });
  onCleanup(() => { mounted = false; queryQueue.stop(); });
  const batchQueries = createMemo(() => {
    const ids = new Set(batchIds());
    return queuedQueries().filter((item) => ids.has(item.id));
  });
  const batchCompleted = () => batchQueries().filter((item) => item.status === "done").length;
  const historyQueries = createMemo(() => queryHistory().map((item) => {
    const query = queuedQueries().find((entry) => normalizeQuery(entry.text) === normalizeQuery(item.text));
    return {
      id: item.text, text: item.text, status: query?.status ?? "pending" as QueryStatus,
      detail: new Date(item.requestedAt).toLocaleString("ru-RU"),
    };
  }));
  const [ktruPickerOpen, setKtruPickerOpen] = createSignal(false);
  const [spgzPickerOpen, setSpgzPickerOpen] = createSignal(false);
  const [photoDragging, setPhotoDragging] = createSignal(false);
  const [autofilling, setAutofilling] = createSignal(false);
  const [autofillError, setAutofillError] = createSignal("");
  const [catalogCharacteristicsLoading, setCatalogCharacteristicsLoading] = createSignal(false);
  const [catalogCharacteristicsError, setCatalogCharacteristicsError] = createSignal("");
  const [characteristicEditorOpen, setCharacteristicEditorOpen] = createSignal(false);
  const [editingCharacteristic, setEditingCharacteristic] = createSignal<DraftCharacteristic>();
  const [autofillDurationMs, setAutofillDurationMs] = createSignal<number | null>(null);
  const [autofillRequestedText, setAutofillRequestedText] = createSignal<string | null>(null);
  const [autofillCompletedText, setAutofillCompletedText] = createSignal<string | null>(null);
  const [autofillStatus, setAutofillStatus] = createSignal<"accepted" | "needs_review" | "no_match" | null>(null);
  const [webEnrichment, setWebEnrichment] = createSignal<WebEnrichmentSuggestion | null>(null);
  const autofillBusy = () => autofilling() || autofillRequestedText() !== null;
  const autofillCompleted = () => autofillCompletedText() === description().trim();
  const autofillNeedsReview = () => autofillCompleted() && autofillStatus() === "needs_review";
  let categorySection: HTMLDivElement | undefined;
  let descriptionSection: HTMLElement | undefined;
  const [refinedCategories, setRefinedCategories] = createSignal({ ktru: false, spgz: false });
  const [categoriesBeforeAutofill, setCategoriesBeforeAutofill] = createSignal<{ ktru: VotedCode | null; spgz: VotedCode | null }>({ ktru: null, spgz: null });
  const clearCategoryHighlight = () => {
    setRefinedCategories({ ktru: false, spgz: false });
  };
  const scrollToResult = (section: HTMLElement) => {
    const content = section.closest<HTMLElement>(".app-content")!;
    const offset = parseFloat(window.getComputedStyle(content).scrollPaddingTop);
    const translation = new DOMMatrixReadOnly(window.getComputedStyle(section).transform).m42;
    content.scrollTo({
      top: content.scrollTop + section.getBoundingClientRect().top - translation - content.getBoundingClientRect().top - offset,
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth",
    });
  };
  createEffect(on(description, clearCategoryHighlight, { defer: true }));
  onCleanup(clearCategoryHighlight);
  let autofillController: AbortController | undefined;
  let catalogPositionsSeq = 0;
  const [spellErrors, setSpellErrors] = createSignal<SpellerError[]>([]);
  const [backdropTip, setBackdropTip] = createSignal<
    { kind: "spell" | "evidence"; index: number; left: number; top: number } | null
  >(null);
  const [evidenceFlashId, setEvidenceFlashId] = createSignal<string | null>(null);
  const [productEvidenceVersion, setProductEvidenceVersion] = createSignal(0);
  let photoInput: HTMLInputElement | undefined;
  let describeTextarea: HTMLTextAreaElement | undefined;
  let spellBackdrop: HTMLDivElement | undefined;
  let spellWrap: HTMLDivElement | undefined;
  let spellTipHideTimer: ReturnType<typeof setTimeout> | undefined;
  let evidenceFlashTimer: ReturnType<typeof setTimeout> | undefined;

  const fingerprint = () => `${selected()?.code ?? ""}\u0000${description()}`;
  const outdated = () => !!generatedFor() && generatedFor() !== fingerprint();
  const displayCharacteristics = createMemo(() => buildDisplayCharacteristics(characteristics()));
  const visibleCharacteristics = createMemo(() => displayCharacteristics().filter(isVisibleCharacteristic));
  const linkedIdsFor = (id: string) =>
    displayCharacteristics().find((row) => row.id === id)?.linkedIds ?? [id];
  const hasDraft = () => !!(description().trim() || name().trim() || selected() || similar().length || photos().length);
  const hasCategory = () => !!(okpd2() || ktruVote() || kpgz() || spgz());
  // Схемы нет, пока не выбрана позиция КТРУ/СПГЗ: тогда «Характеристики разобраны» было бы неверным, а варианты
  // выбора показываем прямо в блоке характеристик.
  // Только варианты с самой позицией: пара «ОКПД2 / без КТРУ» схемы характеристик не даёт.
  const leafChoices = (decision: CategoryPairDecision | null | undefined, leaf: "ktru" | "spgz"): CategoryPairDecision | null =>
    decision?.needsChoice ? { ...decision, alternatives: (decision.alternatives ?? []).filter((pair) => !!pair[leaf]) } : null;
  const reportCategoryError = (error: unknown) => {
    if (mounted) setSuggestError(error instanceof Error ? error.message : "Не удалось применить выбранную категорию.");
  };
  const pickOkpdKtruAlternative = (pair: CategoryPairCandidate) => {
    void (async () => {
      const applied = pair.ktru
        ? await applyCatalogPositions({ ktruCode: pair.ktru.code, ktruLocked: true, discriminatorValues: null })
        : pair.okpd2 ? await applyOkpd(pair.okpd2.code, true) : false;
      if (!applied) return;
      const rerun = autofillCompleted() && displayCharacteristics().length === 0;
      batch(() => {
        setOkpd2(pair.okpd2);
        setKtruVote(pair.ktru);
      });
      saveActiveQueryDraft();
      if (rerun) requestAutofill();  // разбор уже был без схемы — повторяем, когда поля появились
    })().catch(reportCategoryError);
  };
  const pickKpgzSpgzAlternative = (pair: CategoryPairCandidate) => {
    void (async () => {
      const applied = pair.spgz
        ? await applyCatalogPositions({ spgzCode: pair.spgz.code, kpgzCode: pair.kpgz?.code ?? null, spgzLocked: true, discriminatorValues: null })
        : pair.kpgz ? await applyKpgz(pair.kpgz.code, true) : false;
      if (!applied) return;
      const rerun = autofillCompleted() && displayCharacteristics().length === 0;
      batch(() => {
        setKpgz(pair.kpgz);
        setSpgz(pair.spgz);
      });
      saveActiveQueryDraft();
      if (rerun) requestAutofill();
    })().catch(reportCategoryError);
  };
  const activePhoto = () => photos()[photoIndex()] ?? photos()[0] ?? null;

  const loadSpecialization = async (input?: {
    ktruCode?: string | null;
    spgzCode?: string | null;
    kpgzCode?: string | null;
  }) => getCatalogSpecialization({
    ktruCode: input?.ktruCode ?? ktruVote()?.code ?? (selected()?.kind === "ktru" ? selected()?.code : null),
    spgzCode: input?.spgzCode !== undefined ? input.spgzCode : spgz()?.code ?? null,
    kpgzCode: input?.kpgzCode !== undefined ? input.kpgzCode : kpgz()?.code ?? null,
  });

  const refinedPositionRefs = (item: DraftCharacteristic): RefinedPositionRef[] => {
    if (!item.refinesKtru) return [];
    const spec = catalogSpecialization();
    if (!spec) return [];
    const selected = selectedRefineValues(characteristics());
    if (!Object.keys(selected).length) return [];
    const matches = filterSpecializationOptions(spec.options, selected);
    if (matches.length !== 1) return [];
    const match = matches[0]!;
    const refs: RefinedPositionRef[] = [];
    if (match.ktruCode) {
      const isTemplate = match.ktruCode === spec.templateKtruCode;
      const baseName = match.ktruName ? `${match.ktruName}${isTemplate ? " (Шаблон)" : ""}` : (isTemplate ? "(Шаблон)" : "");
      const variant = !isTemplate
        ? specializationOptionLabel(spec.characteristics, match.values)
        : "";
      refs.push({
        tab: "ktru",
        code: match.ktruCode,
        name: variant ? (baseName ? `${baseName} · ${variant}` : variant) : baseName,
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
    const seq = ++catalogPositionsSeq;
    const text = description();
    const node = await getKtruNode(code);
    if (!mounted || seq !== catalogPositionsSeq || text !== description() || !node || node.kind !== "okpd") return false;
    batch(() => {
      setSelected(node);
      setOkpd2({ code: node.code, name: node.name, score: okpd2()?.score ?? 1 });
      setKtruVote(null);
      setKtruLocked(locked);
      if (locked) setOkpdKtruDecision(null);
      setCharacteristics((current) => current.filter((item) => item.source !== "ktru"));
      setCharStandards([]);
      setNoNewFeatures(null);
      setNoNewFeaturesReason("");
      setInitialTemplateKtruCode(null);
      setGeneratedFor(`\u0000${text}`);
    });
    return true;
  };

  const buildKtruDraftFields = (
    record: Pick<NonNullable<Awaited<ReturnType<typeof getKtruRecord>>>, "characteristics">,
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
          badges: item.badges.filter((badge) =>
            !/^(обязательная|качественная|количественная)$/i.test(badge.trim())),
          hint: item.hint || "",
          options,
          optionLabels: item.optionLabels ?? {},
          ranges,
          unit: item.unit || "",
          numeric: true,
          multiple: !!item.multiple,
          source: "ktru" as const,
          autofill: prior?.value.trim() ? prior.autofill : undefined,
          autofillDiagnostics: prior?.value.trim() ? prior.autofillDiagnostics : undefined,
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
        badges: item.badges.filter((badge) =>
          !/^(обязательная|качественная|количественная)$/i.test(badge.trim())),
        hint: item.hint || "",
        options,
        optionLabels: item.optionLabels ?? {},
        ranges: [],
        unit: item.unit || "",
        numeric: /количественная/i.test(item.badges.join(" ")) || (!!item.unit && !(item.options ?? []).length),
        multiple: !!item.multiple,
        source: "ktru" as const,
        autofill: prior?.value.trim() ? prior.autofill : undefined,
        autofillDiagnostics: prior?.value.trim() ? prior.autofillDiagnostics : undefined,
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
      return {
        ...draft,
        value: prior?.value.trim()
          ? draft.options.length ? mapValueToOptions(prior.value, draft.options) || prior.value : prior.value
          : draft.value,
        autofill: prior?.value.trim() ? prior.autofill : undefined,
        autofillDiagnostics: prior?.value.trim() ? prior.autofillDiagnostics : undefined,
      };
    });
  };

  /** Fetch KTRU/SPGZ once, then commit votes + characteristics in a single batch (no table flicker). */
  const applyCatalogPositions = async (input: {
    ktruCode?: string | null;
    spgzCode?: string | null;
    kpgzCode?: string | null;
    clearSpgz?: boolean;
    ktruLocked?: boolean;
    spgzLocked?: boolean;
    discriminatorValues?: Record<string, string> | null;
    applyPositionRefine?: boolean;
    /** Reuse already-loaded specialization (refine path) — skips a heavy RPC. */
    reuseSpecialization?: boolean;
  }) => {
    clearCategoryHighlight();
    const seq = ++catalogPositionsSeq;
    const text = description();
    const keepPrevious = input.discriminatorValues !== undefined;
    const previous = characteristics();
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
          kpgzCode: input.kpgzCode !== undefined ? input.kpgzCode : kpgz()?.code ?? null,
        }),
    ]);

    if (!mounted || seq !== catalogPositionsSeq || text !== description()) return false;

    const templateCode = spec?.templateKtruCode;
    const templateRecord = wantKtru && ktruRecord && templateCode && templateCode !== input.ktruCode
      ? await getKtruRecord(templateCode)
      : null;
    if (!mounted || seq !== catalogPositionsSeq || text !== description()) return false;

    let ktruFields = (wantKtru && ktruRecord && ktruNode?.kind === "ktru")
      ? buildKtruDraftFields(ktruRecord, previous, keepPrevious)
      : previous.filter((item) => item.source === "ktru");
    if (templateRecord && templateCode) {
      ktruFields = retainTemplateCharacteristics(
        ktruFields,
        preserveManualCharacteristics(buildKtruDraftFields(templateRecord, previous, keepPrevious), previous),
        templateCode,
      );
    }
    const spgzFields = (wantSpgz && spgzRecord && spgzNode?.kind === "spgz")
      ? buildSpgzDraftFields(spgzRecord, previous, keepPrevious)
      : keepSpgzRows
        ? previous.filter((item) => item.source === "spgz")
        : [];

    if ((wantKtru && (!ktruRecord || ktruNode?.kind !== "ktru")) || (wantSpgz && (!spgzRecord || spgzNode?.kind !== "spgz"))) {
      throw new Error("Выбранная позиция не найдена в справочнике.");
    }
    const kpgzLink = spgzRecord?.links.find((link) => link.tab === "kpgz");
    // The untouched pair may have changed while records were loading; never replace its schema with the captured snapshot.
    const latest = characteristics();
    let nextChars = withCatalogSpecialization([
      ...(wantKtru ? ktruFields : latest.filter((item) => item.source === "ktru")),
      ...(wantSpgz ? spgzFields : keepSpgzRows ? latest.filter((item) => item.source === "spgz") : []),
      ...latest.filter((item) => item.source === "custom" || item.source === "catalog" || item.custom),
    ], spec, input.discriminatorValues);
    if (input.applyPositionRefine === false) {
      nextChars = nextChars.map((item) => item.refinesKtru ? { ...item, applyPositionRefine: false } : item);
    }
    nextChars = preserveManualCharacteristics(nextChars, latest);

    batch(() => {
      if (wantKtru && ktruNode?.kind === "ktru" && ktruRecord) {
        if (!keepPrevious) setInitialTemplateKtruCode(ktruNode.isTemplate ? ktruNode.code : null);
        setSelected(ktruNode);
        setKtruVote({
          code: ktruNode.code,
          name: ktruNode.name,
          score: ktruVote()?.score ?? 1,
          isTemplate: ktruNode.isTemplate,
          variantLabel: ktruNode.variantLabel ?? null,
        });
        if (input.ktruLocked != null) setKtruLocked(input.ktruLocked);
        if (input.ktruLocked) setOkpdKtruDecision(null);
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
      }
      if (input.clearSpgz) {
        setSpgz(null);
        if (input.spgzLocked != null) setSpgzLocked(input.spgzLocked);
        if (input.spgzLocked) setKpgzSpgzDecision(null);
      } else if (wantSpgz && spgzNode?.kind === "spgz") {
        setSpgz({
          code: spgzNode.code,
          name: spgzNode.name || spgzRecord?.name || spgzNode.code,
          score: spgz()?.score ?? 1,
        });
        if (input.spgzLocked != null) setSpgzLocked(input.spgzLocked);
        if (input.spgzLocked) setKpgzSpgzDecision(null);
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
    return true;
  };

  const applyKtru = async (code: string, locked: boolean, discriminatorValues?: Record<string, string> | null) => {
    return applyCatalogPositions({
      ktruCode: code,
      ktruLocked: locked,
      discriminatorValues,
    });
  };

  const refineFromDiscriminators = (override?: { name: string; value: string }) => {
    const spec = catalogSpecialization();
    if (!spec) return;
    const selected: Record<string, string> = {};
    for (const item of characteristics()) {
      if (!item.refinesKtru || item.applyPositionRefine === false) continue;
      const raw = override && normCharName(override.name) === normCharName(item.name)
        ? override.value
        : item.value;
      if (!raw.trim()) continue;
      const canonical = spec.characteristics.find((entry) =>
        normCharName(entry.name) === normCharName(item.name))?.name ?? item.name;
      selected[canonical] = raw.trim();
    }
    const matches = filterSpecializationOptions(spec.options, selected);
    const complete = Object.keys(selected).length === spec.characteristics.length;
    const unique = complete && matches.length === 1 ? matches[0]! : null;
    const nextSpgz = unique?.spgzCode ?? null;
    const targetKtru = unique?.ktruCode
      ?? (spec.templateKtruCode || ktruVote()?.code || null);
    const ktruSame = !targetKtru || ktruVote()?.code === targetKtru;
    const spgzSame = unique
      ? (!nextSpgz || spgz()?.code === nextSpgz)
      : true;
    if (ktruSame && (unique ? spgzSame : !spgz() || spgzLocked() || !spec.templateKtruCode)) {
      setCharacteristics((items) => withCatalogSpecialization(
        items.map((item) => {
          if (!item.refinesKtru) return item;
          if (override && normCharName(override.name) === normCharName(item.name)) {
            return { ...item, value: override.value };
          }
          return item;
        }),
        spec,
        selected,
      ));
      return;
    }
    const seq = nextRefineSeq();
    return applyCatalogPositions({
      ktruCode: !ktruLocked() && targetKtru && !ktruSame ? targetKtru : (ktruVote()?.code ?? null),
      spgzCode: !spgzLocked() && unique && nextSpgz && !spgzSame ? nextSpgz : undefined,
      clearSpgz: !spgzLocked() && !unique && !!spgz(),
      discriminatorValues: selected,
      applyPositionRefine: true,
      reuseSpecialization: true,
    }).then(() => {
      if (seq !== currentRefineSeq()) return;
    });
  };

  const applyKpgz = async (code: string, locked: boolean) => {
    const text = description();
    const seq = ++catalogPositionsSeq;
    const node = await getDirectoryNode("kpgz", code);
    if (!mounted || seq !== catalogPositionsSeq || text !== description() || !node || node.kind !== "kpgz") return false;
    const spec = await loadSpecialization({ kpgzCode: code, spgzCode: null });
    if (!mounted || seq !== catalogPositionsSeq || text !== description()) return false;
    batch(() => {
      setKpgz({ code: node.code, name: node.name, score: kpgz()?.score ?? 1 });
      setSpgz(null);
      setSpgzLocked(locked);
      if (locked) setKpgzSpgzDecision(null);
      setCatalogSpecialization(spec);
      setCharacteristics((current) => preserveManualCharacteristics(
        withCatalogSpecialization(current.filter((item) => item.source !== "spgz" && item.source !== "catalog"), spec), current,
      ));
    });
    return true;
  };

  const applySpgz = async (code: string, locked: boolean, discriminatorValues?: Record<string, string> | null) => {
    return applyCatalogPositions({
      spgzCode: code,
      spgzLocked: locked,
      discriminatorValues,
    });
  };

  const openKtruPicker = () => {
    setKtruPickerSelected(selected());
    setKtruPickerPath([]);
    setKtruPickerOpen(true);
    const code = selected()?.code || ktruVote()?.code || okpd2()?.code;
    if (!code) return;
    const text = description();
    const selection = ktruPickerSelected();
    void getKtruRevealPath(code).then((result) => {
      if (!mounted || !ktruPickerOpen() || text !== description() || selection !== ktruPickerSelected()) return;
      if (result.node) setKtruPickerSelected(result.node);
      setKtruPickerPath(result.pathIds);
    }).catch(reportCategoryError);
  };

  const openSpgzPicker = () => {
    setSpgzPickerSelected(null);
    setSpgzPickerPath([]);
    setSpgzPickerOpen(true);
    const code = spgz()?.code || kpgz()?.code;
    if (!code) return;
    const text = description();
    const selection = spgzPickerSelected();
    void getDirectoryRevealPath(spgz() ? "spgz" : "kpgz", code).then((result) => {
      if (!mounted || !spgzPickerOpen() || text !== description() || selection !== spgzPickerSelected()) return;
      if (result.node) setSpgzPickerSelected(result.node);
      setSpgzPickerPath(result.pathIds);
    }).catch(reportCategoryError);
  };

  const confirmKtruPicker = (node: KtruTreeNode) => {
    setKtruPickerOpen(false);
    void (node.kind === "okpd" ? applyOkpd(node.code, true) : applyKtru(node.code, true, null))
      .then((applied) => { if (applied) saveActiveQueryDraft(); }).catch(reportCategoryError);
  };

  const confirmSpgzPicker = (node: DirTreeNode) => {
    setSpgzPickerOpen(false);
    void (node.kind === "kpgz" ? applyKpgz(node.code, true) : applySpgz(node.code, true, null))
      .then((applied) => { if (applied) saveActiveQueryDraft(); }).catch(reportCategoryError);
  };

  const clearAutofill = () => {
    clearCategoryHighlight();
    autofillController?.abort();
    setAutofillRequestedText(null);
    setAutofilling(false);
    catalogPositionsSeq++;
    manuallyEditedCharacteristics.clear();
    manuallyEditedCharacteristicValues.clear();
    autofilledCharacteristicValues.clear();
    manuallyEditedProductFields.clear();
    autofilledProductValues.clear();
    autofilledProductEvidence.clear();
    setLastAutofillKey("");
    setAutofillError("");
    setAutofillDurationMs(null);
    setAutofillCompletedText(null);
    setAutofillStatus(null);
    setWebEnrichment(null);
    setEvidenceFlashId(null);
    clearTimeout(evidenceFlashTimer);
    setProductEvidenceVersion((version) => version + 1);
  };

  const resetCharacteristics = async () => {
    const templateCode = initialTemplateKtruCode();
    batch(() => {
      clearAutofill();
      nextRefineSeq();
      setCharacteristics((items) => items
        .filter((item) => !item.templateKtruCode)
        .map((item) => ({ ...item, value: "", rangeOption: "", autofill: undefined, autofillDiagnostics: undefined })));
    });
    if (templateCode && ktruVote()?.code !== templateCode) {
      await applyCatalogPositions({
        ktruCode: templateCode,
        discriminatorValues: null,
        clearSpgz: !spgzLocked(),
        reuseSpecialization: true,
      });
    }
  };

  const fillExample = () => {
    saveActiveQueryDraft();
    setActiveQueryId(null);
    clearAutofill();
    batch(() => {
      setName("");
      setSku("");
      setModel("");
      setBrand("");
      setSelected(null);
      setOkpd2(null);
      setKpgz(null);
      setSpgz(null);
      setKtruVote(null);
      setKtruLocked(false);
      setSpgzLocked(false);
      clearCategoryDecisions();
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
    saveActiveQueryDraft();
    setActiveQueryId(null);
    setSuggestError("");
    setQueryQuality(null);
    setForceSuggestQuery(null);
    setSuggestAcceptedQuery("");
    clearAutofill();
    batch(() => {
      setName(""); setSku(""); setModel(""); setBrand(""); setDescription("");
      setSelected(null); setOkpd2(null); setKpgz(null); setSpgz(null); setKtruVote(null);
      setKtruLocked(false); setSpgzLocked(false);
      clearCategoryDecisions();
      setSimilar([]); setSuggesting(false); setLastSuggestQuery(""); setSuggestDurationMs(null);
      setCharacteristics([]); clearCharMeta(); setGeneratedFor("");
      clearPhotos();
    });
  };

  const forceSuggestDespiteQuality = () => {
    const query = normalizeQuery(description());
    if (query.length < 3) return;
    queryQueue.invalidate(query);
    batch(() => {
      setForceSuggestQuery(query);
      setQueryQuality(null);
      setSuggestError("");
      setLastSuggestQuery("");
      setQuerySelectionSeq((seq) => seq + 1);
    });
  };

  const saveActiveQueryDraft = () => {
    const id = activeQueryId();
    const item = queryQueue.items.find((entry) => entry.id === id);
    if (!id || !item || suggesting() || lastSuggestQuery() !== normalizeQuery(item.text)) return;
    queryDrafts.set(id, {
      draft: captureQueryDraft(),
      autofillCompletedText: autofillCompletedText(),
      autofillDurationMs: autofillDurationMs(),
    });
  };

  const selectQuery = (id: string) => {
    const item = queryQueue.items.find((entry) => entry.id === id);
    if (!item) return;
    saveActiveQueryDraft();
    const saved = queryDrafts.get(id);
    batch(() => {
      reset();
      setActiveQueryId(id);
      setDescription(item.text);
      setQuerySelectionSeq((seq) => seq + 1);
      if (saved && item.status === "done") {
        restoreQueryDraft(saved.draft);
        setAutofillCompletedText(saved.autofillCompletedText);
        setAutofillDurationMs(saved.autofillDurationMs);
        setProductEvidenceVersion((version) => version + 1);
      }
    });
  };

  const selectHistoryQuery = (text: string) => {
    const item = queryQueue.find(text) ?? queryQueue.add([text])[0]!;
    selectQuery(item.id);
  };

  const editDescription = (text: string) => {
    saveActiveQueryDraft();
    batch(() => {
      setActiveQueryId(null);
      setDescription(text);
      if (forceSuggestQuery() && forceSuggestQuery() !== normalizeQuery(text)) setForceSuggestQuery(null);
      if (suggestAcceptedQuery() && suggestAcceptedQuery() !== normalizeQuery(text)) setSuggestAcceptedQuery("");
      if (queryQuality()) setQueryQuality(null);
    });
  };

  const importQueries = async (file: File | undefined) => {
    if (!file) return;
    setReadingQueries(true);
    setQueryFileError("");
    try {
      const texts = parseQueryFile(await file.text());
      if (!mounted) return;
      if (!texts.length) throw new Error("Файл пуст: добавьте по одному запросу на строку.");
      const added = queryQueue.add(texts);
      setBatchIds((ids) => [...ids, ...added.map((item) => item.id)]);
      if (!activeQueryId()) selectQuery(added[0]!.id);
      queryQueue.resume();
    } catch (error) {
      if (mounted) setQueryFileError(error instanceof Error ? error.message : "Не удалось прочитать файл.");
    } finally {
      if (mounted) setReadingQueries(false);
    }
  };

  createEffect(on(description, (text) => {
    setSpellErrors((prev) => {
      if (!text.trim() || !prev.length) return [];
      return prev.filter((err) => text.slice(err.pos, err.pos + err.len) === err.word);
    });
    if (!text.trim()) return;
    let alive = true;
    const ctrl = new AbortController();
    const timer = setTimeout(() => {
      void checkSpelling(text, ctrl.signal)
        .then((errors) => { if (alive) setSpellErrors(errors); })
        .catch((err: unknown) => {
          if (!alive || (err instanceof DOMException && err.name === "AbortError")) return;
          setSpellErrors([]);
        });
    }, 450);
    onCleanup(() => {
      alive = false;
      clearTimeout(timer);
      ctrl.abort();
    });
  }));

  const evidenceSpans = createMemo(() => {
    productEvidenceVersion();
    if (!autofillCompleted()) return [];
    const text = description();
    const product = ([
      { id: "product:manufacturer", key: "manufacturer" as const, name: "Производитель", value: brand() },
      { id: "product:article", key: "article" as const, name: "Артикул", value: sku() },
      { id: "product:model", key: "model" as const, name: "Модель", value: model() },
    ]).flatMap((item) => {
      const evidence = autofilledProductEvidence.get(item.key) ?? null;
      if (!evidence || autofilledProductValues.get(item.key) !== item.value) return [];
      return [{ id: item.id, name: item.name, value: item.value, evidence }];
    });
    return collectEvidenceSpans(text, characteristics(), product);
  });

  const spellHighlight = createMemo(() => highlightDescription(
    description(), spellErrors(), evidenceSpans(), evidenceFlashId(),
  ));

  const syncSpellScroll = () => {
    if (!describeTextarea || !spellBackdrop) return;
    spellBackdrop.scrollTop = describeTextarea.scrollTop;
    spellBackdrop.scrollLeft = describeTextarea.scrollLeft;
    setBackdropTip(null);
  };

  const clearSpellTipSoon = () => {
    clearTimeout(spellTipHideTimer);
    spellTipHideTimer = setTimeout(() => setBackdropTip(null), 120);
  };

  const showBackdropTip = (kind: "spell" | "evidence", index: number, anchor: HTMLElement) => {
    if (!spellWrap) return;
    clearTimeout(spellTipHideTimer);
    const wrapRect = spellWrap.getBoundingClientRect();
    const rect = anchor.getBoundingClientRect();
    const left = Math.min(
      Math.max(8, rect.left - wrapRect.left),
      Math.max(8, wrapRect.width - 220),
    );
    const top = rect.bottom - wrapRect.top + 6;
    setBackdropTip({ kind, index, left, top });
  };

  const onSpellBackdropOver = (event: MouseEvent) => {
    const raw = event.target as HTMLElement | null;
    if (!raw) return;
    const evidence = raw.closest?.(".autofill-evidence") as HTMLElement | null;
    if (evidence) {
      const index = Number(evidence.dataset.evidenceIndex);
      if (Number.isFinite(index)) showBackdropTip("evidence", index, evidence);
      return;
    }
    const spell = raw.closest?.(".spell-error") as HTMLElement | null;
    if (spell) {
      const index = Number(spell.dataset.spellIndex);
      if (Number.isFinite(index)) showBackdropTip("spell", index, spell);
    }
  };

  const applySpellSuggestion = (suggestion: string) => {
    const tip = backdropTip();
    const err = tip?.kind === "spell" ? spellErrors()[tip.index] : null;
    if (!err || !describeTextarea) return;
    const text = description();
    const next = `${text.slice(0, err.pos)}${suggestion}${text.slice(err.pos + err.len)}`;
    editDescription(next);
    setBackdropTip(null);
    const caret = err.pos + suggestion.length;
    queueMicrotask(() => {
      describeTextarea?.focus();
      describeTextarea?.setSelectionRange(caret, caret);
    });
  };

  const locateEvidence = (id: string) => {
    const span = evidenceSpans().find((item) => item.id === id)
      ?? (() => {
        const item = characteristics().find((row) => row.id === id);
        const evidence = item ? evidenceFromField(item) : null;
        if (!item || !evidence) return null;
        return { id, start: evidence.start, end: evidence.end };
      })();
    descriptionSection?.scrollIntoView({ behavior: "smooth", block: "center" });
    clearTimeout(evidenceFlashTimer);
    setEvidenceFlashId(id);
    evidenceFlashTimer = setTimeout(() => setEvidenceFlashId(null), 3000);
    if (span && describeTextarea) {
      const before = description().slice(0, span.start);
      const line = before.split("\n").length;
      const approx = Math.max(0, (line - 1) * 24);
      describeTextarea.scrollTop = approx;
      syncSpellScroll();
    }
  };

  createEffect(() => {
    spellHighlight();
    const tip = backdropTip();
    if (!spellBackdrop) return;
    for (const node of spellBackdrop.querySelectorAll(".spell-error.is-active")) {
      node.classList.remove("is-active");
    }
    if (tip?.kind !== "spell") return;
    spellBackdrop.querySelector(`.spell-error[data-spell-index="${tip.index}"]`)?.classList.add("is-active");
  });

  onCleanup(() => {
    clearTimeout(spellTipHideTimer);
    clearTimeout(evidenceFlashTimer);
  });

  createEffect(on([description, querySelectionSeq], ([text]) => {
    catalogPositionsSeq++;
    nextRefineSeq();
    const query = normalizeQuery(text);
    const entry = queryQueue.items.find((item) => item.id === activeQueryId());
    const queued = entry && normalizeQuery(entry.text) === query ? entry : undefined;
    setSuggestError("");
    setQueryQuality(null);
    if (query.length < 3) {
      if (queued) {
        setSuggestError("Для поиска нужно не менее 3 символов.");
        void queryQueue.run(queued.id).catch(() => {});
      }
      batch(() => {
        setSimilar([]);
        setSimilarDurationMs(null);
        setSuggesting(false);
        setLastSuggestQuery("");
        setSuggestAcceptedQuery("");
        setSuggestDurationMs(null);
        if (!ktruLocked()) {
          setOkpdKtruDecision(null);
          setSelected(null);
          setOkpd2(null);
          setKtruVote(null);
          setCharacteristics((current) => current.filter((item) => item.source !== "ktru"));
          clearCharMeta();
          setGeneratedFor("");
        }
        if (!spgzLocked()) {
          setKpgzSpgzDecision(null);
          setKpgz(null);
          setSpgz(null);
          setCharacteristics((current) => current.filter((item) => item.source !== "spgz" && item.source !== "catalog"));
        }
      });
      return;
    }
    if (query === lastSuggestQuery()) {
      const quality = queued?.result?.queryQuality ?? null;
      const gated = forceSuggestQuery() !== query
        && (quality?.verdict === "reject" || quality?.verdict === "suspicious");
      setQueryQuality(quality);
      setSuggestAcceptedQuery(gated ? "" : query);
      if (queued?.result?.neighborsDurationMs !== undefined) {
        setSimilarDurationMs(queued.result.neighborsDurationMs);
      }
      setSimilarReady(true);
      setSuggesting(false);
      return;
    }
    const progress = queued?.status === "done" ? queued.result
      : queued?.status === "processing" ? queued.progress : undefined;
    setQueryQuality(progress?.queryQuality ?? null);
    setSimilarReady(progress?.neighbors !== undefined);
    setSimilar(progress?.neighbors ?? []);
    setSimilarDurationMs(progress?.neighborsDurationMs ?? null);
    setSuggesting(true);
    if (!ktruLocked()) setOkpdKtruDecision(null);
    if (!spgzLocked()) setKpgzSpgzDecision(null);
    const started = performance.now();
    setSuggestDurationMs(null);
    let alive = true;
    const queuedRequest = queued ? queryQueue.run(queued.id) : undefined;
    // Observe rejection immediately, even if the user switches before the render timer fires.
    if (queuedRequest) void queuedRequest.catch(() => {});
    const timer = setTimeout(() => {
      if (!alive) return;
      setSuggesting(true);
      const requestEntry = queued ?? queryQueue.find(query) ?? queryQueue.add([text])[0]!;
      if (!queued) setActiveQueryId(requestEntry.id);
      const request = queuedRequest ?? queryQueue.run(requestEntry.id);
      void request.then(async (draft) => {
        if (!alive) return;
        try {
          const forced = forceSuggestQuery() === query;
          const quality = draft.queryQuality;
          batch(() => {
            setQueryQuality(quality);
            setSimilar(draft.neighbors);
            setSimilarDurationMs(draft.neighborsDurationMs ?? null);
            setSimilarReady(true);
          });
          const gated = !forced && !!quality && (quality.verdict === "reject" || quality.verdict === "suspicious");
          if (gated) {
            batch(() => {
              setQueryQuality(quality);
              setSuggestAcceptedQuery("");
              setSimilar([]);
              setSuggesting(false);
              setLastSuggestQuery(query);
              setSuggestDurationMs(queued
                ? queryQueue.items.find((item) => item.id === queued.id)?.durationMs ?? Math.round(performance.now() - started)
                : Math.round(performance.now() - started));
              if (!ktruLocked()) {
                setOkpdKtruDecision(null);
                setSelected(null);
                setOkpd2(null);
                setKtruVote(null);
                setCharacteristics((current) => current.filter((item) => item.source !== "ktru"));
                clearCharMeta();
                setGeneratedFor("");
              }
              if (!spgzLocked()) {
                setKpgzSpgzDecision(null);
                setKpgz(null);
                setSpgz(null);
                setCharacteristics((current) => current.filter((item) => item.source !== "spgz" && item.source !== "catalog"));
              }
            });
            return;
          }
          const wantSpec = (!ktruLocked() || !spgzLocked()) && !!(draft.ktru || draft.spgz || draft.kpgz);
          const spec = !wantSpec ? catalogSpecialization() : !ktruLocked() && !spgzLocked()
            ? draft.specialization
            : await getCatalogSpecialization({
              ktruCode: (!ktruLocked() ? draft.ktru?.code : ktruVote()?.code) ?? null,
              spgzCode: (!spgzLocked() ? draft.spgz?.code : spgz()?.code) ?? null,
              kpgzCode: (!spgzLocked() ? draft.kpgz?.code : kpgz()?.code) ?? null,
            });
          if (!alive) return;
          let draftKtru = draft.characteristics.filter((item) => item.source === "ktru");
          if (!ktruLocked() && draft.ktru && spec?.templateKtruCode && spec.templateKtruCode !== draft.ktru.code) {
            const template = spec.templateKtruCode === draft.specialization?.templateKtruCode
              ? draft.templateCharacteristics ? { characteristics: draft.templateCharacteristics } : null
              : await getKtruRecord(spec.templateKtruCode);
            if (!alive) return;
            if (template) {
              draftKtru = retainTemplateCharacteristics(
                draftKtru,
                preserveManualCharacteristics(buildKtruDraftFields(template, characteristics(), false), characteristics()),
                spec.templateKtruCode,
              );
            }
          }
          const draftSpgz = draft.characteristics.filter((item) => item.source === "spgz");
          const customKeep = characteristics().filter((item) => item.source === "custom" || item.custom);
          let nextChars: DraftCharacteristic[];
          if (!ktruLocked() && !spgzLocked()) nextChars = [...draftKtru, ...draftSpgz, ...customKeep];
          else if (!ktruLocked()) nextChars = [...draftKtru, ...characteristics().filter((item) => item.source !== "ktru")];
          else if (!spgzLocked()) nextChars = [...characteristics().filter((item) => item.source !== "spgz"), ...draftSpgz];
          else nextChars = characteristics();
          nextChars = preserveManualCharacteristics(
            withCatalogSpecialization(nextChars, spec), characteristics(),
          );

          const refineSelected = selectedRefineValues(
            nextChars.filter((item) => item.refinesKtru && item.applyPositionRefine !== false),
          );
          const refineMatches = spec
            ? filterSpecializationOptions(spec.options, refineSelected)
            : [];
          const refineMatch = Object.keys(refineSelected).length === (spec?.characteristics.length ?? 0)
            && refineMatches.length === 1
            ? refineMatches[0]
            : undefined;
          // Resolve child codes before painting categories — avoid template → child jump.
          const resolvedKtruCode = (!ktruLocked() && refineMatch?.ktruCode)
            ? refineMatch.ktruCode
            : (!ktruLocked() ? draft.ktru?.code : null);
          const resolvedSpgzCode = (!spgzLocked() && refineMatch?.spgzCode)
            ? refineMatch.spgzCode
            : (!spgzLocked() ? draft.spgz?.code : null);

          const draftNode = !ktruLocked() && !refineMatch ? draft.ktruNode : null;
          if (!alive) return;

          batch(() => {
            setCatalogSpecialization(spec);
            if (!ktruLocked()) setOkpdKtruDecision(draft.okpdKtruDecision);
            if (!spgzLocked()) setKpgzSpgzDecision(draft.kpgzSpgzDecision);
            if (draft.classifierError || draft.neighborError) {
              setSuggestError([draft.neighborError, draft.classifierError].filter(Boolean).join(" · "));
            }
            if (!ktruLocked()) {
              setInitialTemplateKtruCode(draft.ktru?.isTemplate ? draft.ktru.code : null);
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
                if (draftNode) {
                  setSelected(draftNode);
                  setGeneratedFor(`${draftNode.code}\u0000${query}`);
                }
              } else {
                setSelected(null);
                setGeneratedFor(`\u0000${query}`);
              }
            }
          });

          if (refineMatch) {
            // Retain current values while the refined schema is loaded; manual
            // edits must survive the intermediate template-only schema.
            setCharacteristics(nextChars);
            await applyCatalogPositions({
              ktruCode: resolvedKtruCode && !ktruLocked() ? resolvedKtruCode : null,
              spgzCode: resolvedSpgzCode && !spgzLocked() ? resolvedSpgzCode : null,
              discriminatorValues: refineSelected,
              applyPositionRefine: true,
              reuseSpecialization: true,
            });
          }
          if (!alive) return;
          batch(() => {
            setLastSuggestQuery(query);
            setSuggestAcceptedQuery(query);
            setSuggestDurationMs(queued
              ? queryQueue.items.find((item) => item.id === queued.id)?.durationMs ?? Math.round(performance.now() - started)
              : Math.round(performance.now() - started));
            setSuggesting(false);
          });
          saveActiveQueryDraft();
        } catch (error) {
          if (alive && !isAbortError(error)) {
            setSuggestError(error instanceof Error ? error.message : "Не удалось показать результаты запроса.");
            setSuggestAcceptedQuery("");
            setSuggesting(false);
          }
        }
      }, (error: unknown) => {
        if (alive && !isAbortError(error)) {
          setSuggestError(error instanceof Error ? error.message : "Не удалось обработать запрос.");
          setSuggestAcceptedQuery("");
          setSuggesting(false);
        }
      });
    }, queued ? 0 : 200);
    onCleanup(() => {
      alive = false;
      clearTimeout(timer);
      queryQueue.abortInFlight();
      // Cancelled debounce/in-flight run must not leave the skeleton stuck on.
      setSuggesting(false);
    });
  }));

  // Catalog statistics can require a cold snapshot build; never block category votes on it.
  createEffect(on([description, kpgz, suggesting, ktruVote, spgz], ([text, category, categoryLoading]) => {
    setCatalogCharacteristicsError("");
    setCatalogCharacteristicsLoading(false);
    setCatalogCharacteristicSuggestions([]);
    if (categoryLoading || !category?.code || text.trim().length < 3) return;
    let alive = true;
    setCatalogCharacteristicsLoading(true);
    void getCatalogCharacteristics(category.code).then((stats) => {
      if (!alive) return;
      setCatalogCharacteristicSuggestions(stats);
      // Official КПГЗ schema gaps only while СПГЗ is unset — эталонные already come from СПГЗ.
      const catalogStats = spgz()?.code
        ? stats.filter((item) => item.scope !== "schema")
        : stats;
      setCharacteristics((current) => preserveManualCharacteristics(
        appendCatalogCharacteristics(current, catalogStats, current), current,
      ));
      saveActiveQueryDraft();
    }).catch((error: unknown) => {
      if (alive) setCatalogCharacteristicsError(error instanceof Error ? error.message : "Не удалось загрузить характеристики каталога.");
    }).finally(() => {
      if (alive) setCatalogCharacteristicsLoading(false);
    });
    onCleanup(() => { alive = false; });
  }));

  const autofillInput = createMemo(() => JSON.stringify({
    text: description().trim(),
    characteristics: displayCharacteristics()
      .filter((item) => !item.custom && item.source !== "custom")
      .map(({ id, name, options, optionLabels, ranges, unit, numeric, multiple, required, hint, source, catalogFrequency }) =>
        ({ id, name, options, optionLabels, ranges, unit, numeric, multiple, required, hint,
          source: source === "catalog" ? "catalog" : source === "spgz" ? "spgz" : source === "both" ? "both" : "ktru",
          examples: catalogFrequency?.examples ?? [] })),
    standards: charStandards(),
    ktruCode: ktruVote()?.code ?? null,
    additionalAllowed: noNewFeatures() == null ? null : !noNewFeatures()!,
    excludedNames: characteristics().filter((item) => item.custom || item.source === "custom").map((item) => item.name),
  }));

  const requestAutofill = () => {
    const text = description().trim();
    if (text.length < 12 || autofilling() || autofillRequestedText() !== null) return;
    if (suggestAcceptedQuery() !== normalizeQuery(text)) return;
    clearCategoryHighlight();
    setCategoriesBeforeAutofill({ ktru: ktruVote(), spgz: spgz() });
    batch(() => {
      setLastAutofillKey("");
      setAutofillError("");
      setAutofillCompletedText(null);
      setAutofillStatus(null);
      setWebEnrichment(null);
      setAutofillRequestedText(text);
    });
  };

  createEffect(on(description, () => {
    if (autofillRequestedText() === null && !autofilling()) return;
    autofillController?.abort();
    batch(() => {
      setAutofillRequestedText(null);
      setAutofilling(false);
    });
  }, { defer: true }));
  onCleanup(() => autofillController?.abort());

  createEffect(on([suggesting, autofillRequestedText, catalogCharacteristicsLoading], ([categoryLoading, requestedText, catalogLoading]) => {
    if (requestedText === null || autofilling()) return;
    const key = autofillInput();
    const input = JSON.parse(key) as Parameters<typeof apiAutofillCharacteristics>[0];
    if (requestedText !== input.text || input.text.length < 12) {
      setAutofillRequestedText(null);
      return;
    }
    if (categoryLoading || catalogLoading) return;
    if (key === lastAutofillKey()) {
      setAutofillRequestedText(null);
      return;
    }
    const snapshot = new Map(characteristics().map((item) => [item.id, item]));
    const productSnapshot = { name: name(), brand: brand(), sku: sku(), model: model() };
    const rows = displayCharacteristics();
    const ctrl = new AbortController();
    autofillController = ctrl;
    const isCurrent = () => mounted && !ctrl.signal.aborted && description().trim() === input.text;
    setAutofilling(true);
    setAutofillDurationMs(null);
    void apiAutofillCharacteristics(input, ctrl.signal).then(async (result) => {
        if (!isCurrent()) return;
        if (autofillInput() !== key) throw new Error("Схема характеристик изменилась во время разбора. Запустите разбор заново.");
        const byId = new Map(result.characteristics.map((item) => [item.id, item]));
        type FieldUpdate = {
          value: string; rangeOption: string;
          autofill: DraftCharacteristic["autofill"];
          autofillDiagnostics: DraftCharacteristic["autofillDiagnostics"];
        };
        const updates = new Map<string, FieldUpdate>();
        for (const row of rows) {
          const answer = byId.get(row.id);
          for (const id of row.linkedIds) {
            const prior = snapshot.get(id);
            const current = characteristics().find((item) => item.id === id);
            if (!prior || !current || manuallyEditedCharacteristics.has(characteristicEditKey(current))) continue;
            if (current.value !== prior.value || current.rangeOption !== prior.rangeOption) continue;
            const previousAuto = autofilledCharacteristicValues.get(characteristicEditKey(current));
            const isEmpty = !current.value.trim() && !current.rangeOption.trim();
            const isPreviousAuto = previousAuto?.value === current.value && previousAuto?.rangeOption === current.rangeOption;
            if (!isEmpty && !isPreviousAuto) continue;
            const accepted = answer?.status === "accepted" && answer.value != null && String(answer.value).trim();
            const value = accepted ? String(answer.value) : "";
            const rangeOption = accepted
              ? (answer.rangeOption || (isRangeCharacteristic(current)
                ? (value ? matchNumberToRange(Number(value), current.ranges) ?? "" : "")
                : ""))
              : "";
            const diagnostics = answer ? {
              status: answer.status,
              confidence: answer.confidence,
              quote: answer.quote,
              errors: answer.errors ?? [],
              conversion: answer.conversion ?? null,
              suggestion: answer.suggestion ?? null,
              evidence: answer.evidence ?? null,
              model: result.model,
            } : {
              status: "missing" as const, confidence: null, quote: null, errors: [],
              conversion: null, suggestion: null, evidence: null, model: result.model,
            };
            updates.set(id, {
              value, rangeOption,
              autofill: accepted ? {
                confidence: answer!.confidence ?? 0, model: result.model,
                evidence: answer!.evidence ?? null,
              } : undefined,
              autofillDiagnostics: diagnostics,
            });
            autofilledCharacteristicValues.set(characteristicEditKey(current), { value, rangeOption });
          }
        }
        autofilledProductEvidence.clear();
        const applyProduct = (
          keyName: "name" | "model" | "manufacturer" | "article",
          current: string,
          setter: (value: string) => void,
        ) => {
          const extracted = result.product?.[keyName];
          if (!extracted || extracted.status !== "accepted" || !extracted.value?.trim()) return;
          if (manuallyEditedProductFields.has(keyName)) return;
          const previousAuto = autofilledProductValues.get(keyName);
          if (current.trim() && previousAuto !== current) return;
          setter(extracted.value.trim());
          autofilledProductValues.set(keyName, extracted.value.trim());
          if (extracted.evidence) autofilledProductEvidence.set(keyName, extracted.evidence);
        };
        if (name() === productSnapshot.name) applyProduct("name", name(), setName);
        applyProduct("manufacturer", productSnapshot.brand, setBrand);
        applyProduct("article", productSnapshot.sku, setSku);
        applyProduct("model", productSnapshot.model, setModel);
        setProductEvidenceVersion((version) => version + 1);

        const existingNames = new Set(characteristics().map((item) => item.name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е")));
        const additions = (result.additional_fields ?? [])
          .filter((item) => item.status === "accepted" && item.value != null && String(item.value).trim())
          .filter((item) => !existingNames.has(item.name.trim().toLocaleLowerCase("ru").replaceAll("ё", "е")))
          .map((item, index) => {
            const value = String(item.value);
            const id = `additional:${item.name}:${item.unit ?? ""}:${index}`;
            return {
              id, name: item.name, value, rangeOption: "", required: false, badges: [],
              options: [], optionLabels: {}, ranges: [], unit: item.unit ?? "", multiple: false,
              source: "custom" as const, custom: true, hint: "",
              autofill: item.confidence != null ? {
                confidence: item.confidence, model: result.model, evidence: item.evidence ?? null,
              } : undefined,
              autofillDiagnostics: {
                status: item.status, confidence: item.confidence, quote: item.quote,
                errors: item.errors ?? [], conversion: item.conversion ?? null, suggestion: null,
                evidence: item.evidence ?? null, model: result.model,
              },
            };
          });

        setLastAutofillKey(key);
        setCharacteristics((items) => [
          ...items.map((item) => {
            const update = updates.get(item.id);
            return update ? { ...item, ...update } : item;
          }).filter((item) => !item.templateKtruCode || item.value.trim() || item.rangeOption.trim()),
          ...additions,
        ]);
        setAutofillDurationMs(result.durationMs);
        setAutofillStatus(result.status);
        setWebEnrichment(result.webEnrichment ?? null);
        const filledRefine = characteristics().some((item) =>
          updates.has(item.id) && item.refinesKtru && item.applyPositionRefine !== false && item.value.trim());
        if (filledRefine) await refineFromDiscriminators();
        if (isCurrent()) {
          batch(() => {
            setAutofillCompletedText(input.text);
            setAutofillRequestedText(null);
            setAutofilling(false);
          });
          saveActiveQueryDraft();
          const changed = {
            ktru: !!ktruVote()?.code && ktruVote()!.code !== categoriesBeforeAutofill().ktru?.code,
            spgz: !!spgz()?.code && spgz()!.code !== categoriesBeforeAutofill().spgz?.code,
          };
          setRefinedCategories(changed);
          requestAnimationFrame(() => {
            if (!autofillCompleted() || autofillBusy() || !categorySection?.isConnected) return;
            scrollToResult(categorySection);
          });
        }
      }).catch((error: unknown) => {
        if (!isCurrent()) return;
        batch(() => {
          setAutofilling(false);
          setAutofillRequestedText(null);
          setAutofillError(error instanceof Error ? error.message : "Не удалось заполнить характеристики");
        });
      });
  }));

  const markCharacteristicEdited = (ids: Set<string>) => {
    for (const item of characteristics()) {
      if (ids.has(item.id)) {
        manuallyEditedCharacteristics.add(characteristicEditKey(item));
        manuallyEditedCharacteristicValues.set(characteristicEditKey(item), { ...item });
      }
    }
  };

  const updateCharacteristic = (id: string, value: string) => {
    const ids = new Set(linkedIdsFor(id));
    const target = characteristics().find((item) => ids.has(item.id));
    const next = target?.options.length ? mapValueToOptions(value, target.options) : value;
    if (!target || target.value === next) return;
    markCharacteristicEdited(ids);
    setCharacteristics((items) => items.map((item) => {
      if (!ids.has(item.id)) return item;
      if (!isRangeCharacteristic(item)) return { ...item, value: next, autofill: undefined };
      const number = parseCharacteristicNumber(next);
      return {
        ...item,
        value: next,
        autofill: undefined,
        rangeOption: number == null ? "" : (matchNumberToRange(number, item.ranges) ?? ""),
      };
    }));
    markCharacteristicEdited(ids);
    setCharacteristics((items) => items.filter((item) => !item.templateKtruCode || item.value.trim() || item.rangeOption.trim()));
    if (target?.refinesKtru && target.applyPositionRefine !== false) {
      refineFromDiscriminators({ name: target.name, value: next });
    }
  };

  const togglePositionRefine = (id: string, enabled: boolean) => {
    const ids = new Set(linkedIdsFor(id));
    const keptValues = selectedRefineValues(characteristics());
    if (enabled) {
      setCharacteristics((items) => items.map((item) =>
        ids.has(item.id) ? { ...item, applyPositionRefine: true } : item));
      if (Object.keys(keptValues).length) refineFromDiscriminators();
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
      discriminatorValues: keptValues,
      applyPositionRefine: false,
    }).then(() => {
      if (seq !== currentRefineSeq()) return;
    });
  };

  const editCharacteristicSchema = (item: DraftCharacteristic) => {
    setEditingCharacteristic(item);
    setCharacteristicEditorOpen(true);
  };

  const saveCharacteristicSchema = (field: DraftCharacteristic) => {
    if (noNewFeatures()) return;
    const prior = characteristics().find((item) => item.id === field.id);
    if (prior) {
      manuallyEditedCharacteristics.delete(characteristicEditKey(prior));
      manuallyEditedCharacteristicValues.delete(characteristicEditKey(prior));
      autofilledCharacteristicValues.delete(characteristicEditKey(prior));
    }
    setCharacteristics((items) => [
      ...items.filter((item) => item.id !== field.id
        && !(item.source === "catalog" && characteristicNameKey(item.name) === characteristicNameKey(field.name))),
      field,
    ]);
    markCharacteristicEdited(new Set([field.id]));
    setCharacteristicEditorOpen(false);
    saveActiveQueryDraft();
  };

  const updateCharacteristicRange = (id: string, rangeOption: string) => {
    const ids = new Set(linkedIdsFor(id));
    const target = characteristics().find((item) => ids.has(item.id));
    if (!target || target.rangeOption === rangeOption) return;
    markCharacteristicEdited(ids);
    setCharacteristics((items) => items.map((item) => {
      if (!ids.has(item.id)) return item;
      return { ...item, rangeOption: mapValueToOptions(rangeOption, item.options) || rangeOption, autofill: undefined };
    }));
    markCharacteristicEdited(ids);
    setCharacteristics((items) => items.filter((item) => !item.templateKtruCode || item.value.trim() || item.rangeOption.trim()));
  };

  const removeCharacteristic = (id: string) => {
    const ids = new Set(linkedIdsFor(id));
    setCharacteristics((items) => items.filter((item) => !ids.has(item.id)));
  };

  const addCustomCharacteristic = () => {
    if (noNewFeatures()) return;
    setEditingCharacteristic(undefined);
    setCharacteristicEditorOpen(true);
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
        <div class="create-query-actions">
          <input ref={queryFileInput} type="file" accept=".csv,.txt,text/csv,text/plain" class="sr-only" tabindex={-1} aria-label="Файл с запросами"
            onChange={(event) => {
              const file = event.currentTarget.files?.[0];
              event.currentTarget.value = "";
              void importQueries(file);
            }} />
          <Button variant="secondary" size="sm" type="button" disabled={readingQueries()} onClick={() => queryFileInput?.click()}>
            <Upload size={14} />{readingQueries() ? "Загрузка…" : "Загрузить CSV"}
          </Button>
          <QueryDropdown button hidePendingStatus label={<><History size={14} />История запросов</>} items={historyQueries()}
            selectedId={description().trim()} onSelect={selectHistoryQuery} emptyLabel="История пока пуста" />
        </div>
      </div>
    </section>

    <section ref={descriptionSection} class="create-describe" aria-labelledby="describe-heading">
      <div class="section-heading create-describe-heading">
        <FileText size={20} strokeWidth={1.6} /><h2 id="describe-heading">Описание</h2>
        <Show when={queryQuality()}>{quality =>
          <span
            class="create-query-quality-status"
            classList={{
              "is-ok": quality().verdict === "ok",
              "is-failed": quality().verdict === "reject" || quality().verdict === "suspicious",
            }}
            role="status"
            title={quality().verdict === "ok" ? "Проверка описания пройдена"
              : quality().verdict === "unchecked" ? "Описание не проверено"
              : "Проверка описания не пройдена"}
          >
            <Show when={(quality().durationMs ?? 0) > 0}>
              {formatSuggestTiming(quality().durationMs!)}
            </Show>
            <Show when={quality().verdict !== "unchecked"} fallback={<CircleAlert size={14} aria-label="Описание не проверено" />}>
              <Show when={quality().verdict === "ok"} fallback={<X size={14} aria-label="Проверка описания не пройдена" />}>
                <Check size={14} aria-label="Проверка описания пройдена" />
              </Show>
            </Show>
          </span>
        }</Show>
        <div class="create-top-actions">
          <Button variant="ghost" size="sm" type="button" onClick={reset} disabled={!hasDraft()}><RotateCcw size={14} />Очистить</Button>
          <Button variant="secondary" size="sm" class="create-example-button" type="button" onClick={fillExample}><Lightbulb size={15} />Пример</Button>
        </div>
      </div>
      <Show when={batchQueries().length}>
        <div class="create-query-batch">
          <QueryDropdown class="query-dropdown-batch" label={<>
            <span class="query-dropdown-current">{batchQueries().find((item) => item.id === activeQueryId())?.text ?? "Выбрать запрос из файла"}</span>
            <span class="query-dropdown-progress">{batchCompleted()} / {batchQueries().length}</span>
          </>} items={batchQueries().map((item) => ({ ...item, detail: item.error }))} selectedId={activeQueryId()} onSelect={selectQuery} />
          <p class="create-live-hint">Запросы обрабатываются по очереди. Выберите ожидающий запрос, чтобы запустить его сразу.</p>
        </div>
      </Show>
      <Show when={queryFileError() || historyError()}>
        <p class="create-query-error" role="alert">{queryFileError() || historyError()}</p>
      </Show>
      <TextField class="create-describe-field" name="product-description" value={description()} onChange={editDescription}>
        <TextFieldLabel class="sr-only">Описание товара</TextFieldLabel>
        <div class="create-describe-spell" ref={spellWrap}>
          <TextFieldTextarea
            id="product-description"
            ref={(el) => { describeTextarea = el; }}
            rows={5}
            placeholder="Опишите товар своими словами: назначение, материал, размеры, комплектацию…"
            spellcheck={false}
            onScroll={syncSpellScroll}
          />
          <div
            ref={spellBackdrop}
            class="create-describe-spell-backdrop"
            aria-hidden="true"
            innerHTML={spellHighlight()}
            onMouseOver={onSpellBackdropOver}
            onMouseLeave={clearSpellTipSoon}
          />
          <Show when={backdropTip()}>
            {(tip) => (
              <Show
                when={tip().kind === "spell" ? spellErrors()[tip().index] : evidenceSpans()[tip().index]}
              >
                {(payload) => (
                  <div
                    class="create-describe-spell-tip"
                    role="tooltip"
                    style={{ left: `${tip().left}px`, top: `${tip().top}px` }}
                    onMouseEnter={() => clearTimeout(spellTipHideTimer)}
                    onMouseLeave={clearSpellTipSoon}
                  >
                    <Show
                      when={tip().kind === "evidence"}
                      fallback={<>
                        <p class="create-describe-spell-tip-label">{spellerErrorLabel((payload() as SpellerError).code)}</p>
                        <p class="create-describe-spell-tip-word">«{(payload() as SpellerError).word}»</p>
                        <Show
                          when={(payload() as SpellerError).s.length}
                          fallback={<p class="create-describe-spell-tip-empty">Вариантов нет</p>}
                        >
                          <ul class="create-describe-spell-tip-list">
                            <For each={(payload() as SpellerError).s.slice(0, 5)}>
                              {(suggestion) => (
                                <li>
                                  <button type="button" onClick={() => applySpellSuggestion(suggestion)}>
                                    {suggestion}
                                  </button>
                                </li>
                              )}
                            </For>
                          </ul>
                        </Show>
                      </>}
                    >
                      {(() => {
                        const span = payload() as ReturnType<typeof evidenceSpans>[number];
                        const conversion = formatEvidenceConversion(span.conversion);
                        return <>
                          <p class="create-describe-spell-tip-label">{evidenceSourceLabel(span.source)}</p>
                          <p class="create-describe-spell-tip-word">{span.name}</p>
                          <p class="create-describe-evidence-value">
                            → {span.value}{span.unit ? ` ${span.unit}` : ""}
                          </p>
                          <Show when={conversion}>
                            <p class="create-describe-evidence-conversion">{conversion}</p>
                          </Show>
                        </>;
                      })()}
                    </Show>
                  </div>
                )}
              </Show>
            )}
          </Show>
        </div>
      </TextField>
      <div class="create-describe-meta"><span>Начните с типа товара, затем отличительные признаки: материал, размер, назначение. Без рекламных формулировок.</span><span>{description().length} символов</span></div>
      <Show when={suggestError()}>
        <div class="create-autofill-error" role="alert">
          <span>{suggestError()}</span>
          <Button type="button" variant="outline" size="sm" onClick={() => {
            const id = activeQueryId();
            if (id) selectQuery(id);
            else setQuerySelectionSeq((seq) => seq + 1);
          }}>Повторить</Button>
        </div>
      </Show>
      <Show when={(queryQuality()?.verdict === "reject" || queryQuality()?.verdict === "suspicious")
        && suggestAcceptedQuery() !== normalizeQuery(description())}>
        <div
          class="create-autofill-error"
          classList={{ "create-query-quality-warning": queryQuality()?.verdict === "suspicious" }}
          role="alert"
        >
          <span>{(queryQuality()?.reasons.length
            ? queryQuality()!.reasons
            : ["описание товара выглядит противоречивым, абсурдным или не похоже на товар"]
          ).join("; ")}</span>
          <Show
            when={queryQuality()?.verdict === "suspicious"}
            fallback={
              <Button type="button" variant="outline" size="sm" onClick={() => {
                queryQueue.invalidate(description());
                setLastSuggestQuery("");
                setQuerySelectionSeq((seq) => seq + 1);
              }}>Повторить</Button>
            }
          >
            <Button type="button" variant="outline" size="sm" onClick={forceSuggestDespiteQuality}>
              Искать всё равно
            </Button>
          </Show>
        </div>
      </Show>
      <Show when={webEnrichment() && autofillCompleted() ? webEnrichment() : null}>
        {(enrichment) => {
          const parts = () => enrichmentDescriptionParts(description(), enrichment().suggestedDescription);
          return (
          <Card class="create-web-enrichment-card">
            <CardHeader>
              <CardTitle>Уточните описание по данным из интернета</CardTitle>
              <CardDescription>{enrichment().message}</CardDescription>
            </CardHeader>
            <CardContent>
              <p class="create-web-enrichment-label">Предлагаемое описание</p>
              <p class="create-web-enrichment-text">
                {parts().kept}
                <Show when={parts().added || false}>
                  <span class="create-web-enrichment-added">{parts().added}</span>
                </Show>
              </p>
            </CardContent>
            <CardFooter class="create-web-enrichment-footer">
              <Button
                type="button"
                variant="default"
                size="sm"
                onClick={() => {
                  const next = enrichment().suggestedDescription;
                  setDescription(next);
                  setWebEnrichment(null);
                  if (enrichment().brand && !brand().trim()) setBrand(enrichment().brand);
                  if (enrichment().model && !model().trim()) setModel(enrichment().model);
                }}
              >
                Применить к описанию
              </Button>
              <Button type="button" variant="outline" size="sm" onClick={() => setWebEnrichment(null)}>
                Скрыть
              </Button>
            </CardFooter>
          </Card>
          );
        }}
      </Show>
    </section>

    <Show when={description().trim().length >= 3}>
      <section class="create-live" aria-live="polite">
        <div class="create-live-similar">
          <div class="section-heading">
            <Sparkles size={18} strokeWidth={1.6} /><h2>Похожие товары</h2>
            <Show when={similarDurationMs() != null}>
              <span title="Время до появления первых похожих товаров, включая проверку описания">
                {formatSuggestTiming(similarDurationMs()!)}
              </span>
            </Show>
          </div>
          <Show when={similarReady() || !suggesting()} fallback={<SimilarSuggestSkeleton />}>
            <Show when={similar().length} fallback={<p class="create-live-hint">Похожих товаров пока нет — уточните описание.</p>}>
              <div class="create-similar-row"><For each={similar()}>{product => <ProductCard product={product} returnTo="/" />}</For></div>
            </Show>
          </Show>
        </div>
        <div ref={categorySection} class="create-live-category">
          <div class="section-heading">
            <Tags size={18} strokeWidth={1.6} />
            <h2>Категории</h2>
            <Show when={!suggesting() && suggestDurationMs() != null}>
              <span>{formatSuggestTiming(suggestDurationMs()!)}</span>
            </Show>
          </div>
          <Show when={!suggesting()} fallback={<CategorySuggestSkeleton />}>
            <CategoryPairs
              okpd2={okpd2()}
              ktru={ktruVote()}
              kpgz={kpgz()}
              spgz={spgz()}
              highlightedKtru={refinedCategories().ktru}
              highlightedSpgz={refinedCategories().spgz}
              previousKtru={categoriesBeforeAutofill().ktru}
              previousSpgz={categoriesBeforeAutofill().spgz}
              onEditKtru={openKtruPicker}
              onEditSpgz={openSpgzPicker}
              okpdKtruDecision={okpdKtruDecision()}
              kpgzSpgzDecision={kpgzSpgzDecision()}
              onPickOkpdKtru={pickOkpdKtruAlternative}
              onPickKpgzSpgz={pickKpgzSpgzAlternative}
            />
          </Show>
        </div>
      </section>
    </Show>

    <Show when={hasDraft()}>
      <section class="characteristics-section create-characteristics" aria-labelledby="create-chars-heading">
        <div class="section-heading create-char-heading">
          <Tags size={21} strokeWidth={1.6} /><h2 id="create-chars-heading">Характеристики</h2>
          <Show when={!autofillBusy() && autofillDurationMs() != null}>
            <span>{formatSuggestTiming(autofillDurationMs()!)}</span>
          </Show>
          <div class="create-char-actions">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              class="create-char-reset-button"
              disabled={!characteristics().some((item) => item.value.trim() || item.rangeOption.trim() || item.autofill || item.autofillDiagnostics) && !autofilling() && autofillRequestedText() === null}
              onClick={resetCharacteristics}
            >
              <RotateCcw size={14} />Сбросить
            </Button>
            <Button
              type="button"
              variant="default"
              size="sm"
              class="create-char-autofill-button"
              disabled={description().trim().length < 12 || autofillBusy() || suggestAcceptedQuery() !== normalizeQuery(description())}
              aria-busy={autofillBusy()}
              onClick={requestAutofill}
            >
              <Show when={autofillBusy()} fallback={
                <Show when={autofillCompleted() && displayCharacteristics().length > 0} fallback={<Sparkles size={16} />}><Check size={16} /></Show>
              }><LoaderCircle size={16} class="create-autofill-spinner" /></Show>
              {autofillBusy() ? "Разбираем характеристики…" : autofillCompleted() && !displayCharacteristics().length ? "Выберите КТРУ или СПГЗ" : autofillCompleted() ? (autofillNeedsReview() ? "Требует проверки" : "Характеристики разобраны") : "Разобрать характеристики"}
            </Button>
          </div>
        </div>
        <Show when={autofillError()}>
          <div class="create-autofill-error" role="alert">
            <span>{autofillError()}</span>
            <Button type="button" variant="outline" size="sm" onClick={requestAutofill}>Повторить</Button>
          </div>
        </Show>
        <Show when={catalogCharacteristicsLoading()}>
          <p class="create-live-hint" role="status">Загружаем дополнительные характеристики каталога…</p>
        </Show>
        <Show when={catalogCharacteristicsError()}>
          <p class="create-autofill-error" role="alert">{catalogCharacteristicsError()}</p>
        </Show>
        <Show when={charStandards().length}>
          <p class="create-char-standards">
            <For each={charStandards()}>{(standard, index) => <>
              {index() > 0 && ", "}
              <a href={`https://yandex.ru/search/?text=${encodeURIComponent(standard)}`} target="_blank" rel="noopener noreferrer" title="Найти ГОСТ в Яндексе">
                {standard}
              </a>
            </>}</For>
          </p>
        </Show>
        <Show when={visibleCharacteristics().length} fallback={
          <div class="create-char-empty">
            <p class="no-characteristics">Выберите КТРУ или СПГЗ — появятся поля из справочников.</p>
            <Show when={leafChoices(okpdKtruDecision(), "ktru")?.alternatives.length} fallback={
              <Button type="button" variant="outline" size="sm" class="create-char-empty-pick" onClick={openKtruPicker}>
                <ClipboardList size={14} />Выбрать КТРУ из справочника
              </Button>
            }>
              <p class="create-char-empty-label">Возможные позиции КТРУ:</p>
              <PairChoices decision={leafChoices(okpdKtruDecision(), "ktru")} parent="okpd2" child="ktru" onPick={pickOkpdKtruAlternative} />
            </Show>
            <Show when={leafChoices(kpgzSpgzDecision(), "spgz")?.alternatives.length} fallback={
              <Button type="button" variant="outline" size="sm" class="create-char-empty-pick" onClick={openSpgzPicker}>
                <ClipboardList size={14} />Выбрать СПГЗ из справочника
              </Button>
            }>
              <p class="create-char-empty-label">Возможные позиции СПГЗ:</p>
              <PairChoices decision={leafChoices(kpgzSpgzDecision(), "spgz")} parent="kpgz" child="spgz" onPick={pickKpgzSpgzAlternative} />
            </Show>
          </div>
        }>
          <dl class="characteristics-table create-char-fields dir-characteristics-table"><Index each={visibleCharacteristics()}>{item =>
            <CreateCharRow
              item={item}
              choiceOptions={choiceOptions}
              optionLabel={optionLabel}
              selectedOptions={selectedOptions}
              refinedRefs={refinedPositionRefs(item())}
              onUpdate={updateCharacteristic}
              onEditSchema={editCharacteristicSchema}
              onUpdateRange={updateCharacteristicRange}
              onTogglePositionRefine={togglePositionRefine}
              onRemove={removeCharacteristic}
              onPickOption={pickCharacteristicOption}
              onLocateEvidence={locateEvidence}
            />
          }</Index></dl>
        </Show>
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
      </section>

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
            <TextField class="create-name-field" name="product-name" value={name()} onChange={(value) => { manuallyEditedProductFields.add("name"); autofilledProductEvidence.delete("name"); setProductEvidenceVersion((v) => v + 1); setName(value); }} required>
              <TextFieldLabel class="sr-only">Наименование</TextFieldLabel>
              <TextFieldTextarea id="product-name" required placeholder="Наименование товара" rows={2} />
            </TextField>
            <span class="create-name-edit" aria-hidden="true" title="Можно изменить">
              <Pencil size={15} strokeWidth={1.6} />
            </span>
          </div>
          <dl class="detail-facts">
            <div>
              <dt>
                Производитель
                <AutofillLocate
                  evidence={productEvidenceVersion() >= 0 && autofilledProductValues.get("manufacturer") === brand()
                    ? autofilledProductEvidence.get("manufacturer") : null}
                  onLocate={() => locateEvidence("product:manufacturer")}
                />
              </dt>
              <dd><TextField class="create-inline-field" name="product-brand" value={brand()} onChange={(value) => { manuallyEditedProductFields.add("manufacturer"); autofilledProductEvidence.delete("manufacturer"); setProductEvidenceVersion((v) => v + 1); setBrand(value); }}><TextFieldLabel class="sr-only">Производитель</TextFieldLabel><TextFieldInput id="product-brand" placeholder="Производитель или бренд" maxlength={120} /></TextField></dd>
            </div>
            <div>
              <dt>
                Артикул
                <AutofillLocate
                  evidence={productEvidenceVersion() >= 0 && autofilledProductValues.get("article") === sku()
                    ? autofilledProductEvidence.get("article") : null}
                  onLocate={() => locateEvidence("product:article")}
                />
              </dt>
              <dd><TextField class="create-inline-field" name="product-sku" value={sku()} onChange={(value) => { manuallyEditedProductFields.add("article"); autofilledProductEvidence.delete("article"); setProductEvidenceVersion((v) => v + 1); setSku(value); }} required><TextFieldLabel class="sr-only">Артикул</TextFieldLabel><TextFieldInput id="product-sku" required placeholder="Ваш код" maxlength={80} /></TextField></dd>
            </div>
            <div>
              <dt>
                Модель
                <AutofillLocate
                  evidence={productEvidenceVersion() >= 0 && autofilledProductValues.get("model") === model()
                    ? autofilledProductEvidence.get("model") : null}
                  onLocate={() => locateEvidence("product:model")}
                />
              </dt>
              <dd><TextField class="create-inline-field" name="product-model" value={model()} onChange={(value) => { manuallyEditedProductFields.add("model"); autofilledProductEvidence.delete("model"); setProductEvidenceVersion((v) => v + 1); setModel(value); }}><TextFieldLabel class="sr-only">Модель</TextFieldLabel><TextFieldInput id="product-model" placeholder="Необязательно" maxlength={120} /></TextField></dd>
            </div>
          </dl>
          <input type="hidden" name="product-category" value={selected()?.code ?? ""} />
        </div>
      </section>

    </Show>

    <Dialog open={characteristicEditorOpen()} onOpenChange={setCharacteristicEditorOpen}>
      <Show when={characteristicEditorOpen()}>
        <CharacteristicEditor
          item={editingCharacteristic()}
          statistics={catalogCharacteristicSuggestions()}
          existing={visibleCharacteristics()}
          onSave={saveCharacteristicSchema}
          onCancel={() => setCharacteristicEditorOpen(false)}
        />
      </Show>
    </Dialog>

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
