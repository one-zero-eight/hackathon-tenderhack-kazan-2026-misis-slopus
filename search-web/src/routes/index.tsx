import { Meta, Title } from "@solidjs/meta";
import { A } from "@solidjs/router";
import CircleAlert from "lucide-solid/icons/circle-alert";
import ClipboardList from "lucide-solid/icons/clipboard-list";
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
import { KtruPickerActions, KtruPickerBody } from "~/components/ktru-picker";
import { SpgzPickerActions, SpgzPickerBody } from "~/components/spgz-picker";
import {
  extractCharacteristicNumber,
  isRangeCharacteristic,
  isYesNoOptions,
  matchCharacteristicOption,
  matchNumberToRange,
  parseCharacteristicNumber,
  resolveCharacteristicValue,
  splitCharacteristicValues,
  yesNoOptionPair,
} from "~/lib/characteristic-options";
import { getDirectoryNode, getDirectoryRecord, getDirectoryRevealPath, getKtruNode, getKtruRecord, getKtruRevealPath, suggestFromNeighbors } from "~/lib/rpc";
import {
  buildDisplayCharacteristics,
  characteristicSourceLabel,
  mapValueToOptions,
} from "~/lib/merge-characteristics";
import { draftFromSpgzCharacteristic } from "~/lib/spgz-characteristics";
import type { DirTreeNode, DraftCharacteristic, KtruTreeNode, ProductHit, VotedCode } from "~/lib/types";
import { Badge, Button, Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, TextField, TextFieldInput, TextFieldLabel, TextFieldTextarea } from "~/ui-kit";
import "./create-product.css";
import "./directories.css";
import "./ktru.css";

type DraftPhoto = { id: string; url: string; name: string };

const MAX_PHOTOS = 8;

function nameFromDescription(text: string): string {
  return text.trim().split(/[.!?\n]/)[0]?.trim().slice(0, 200) ?? "";
}

function revokePhotos(items: DraftPhoto[]) {
  for (const item of items) URL.revokeObjectURL(item.url);
}

function guessFromText(name: string, description: string): string {
  const text = description.toLocaleLowerCase("ru");
  const n = name.toLocaleLowerCase("ru");
  if (/формат/.test(n)) return (text.match(/\b(а[345])\b/i)?.[1] ?? "").toUpperCase();
  if (/плотност/.test(n)) {
    const value = text.match(/(\d{2,3}(?:[.,]\d+)?)\s*(?:г\s*\/\s*м\s*(?:²|2)|г\/м)/i)?.[1];
    return value ? `${value.replace(",", ".")} г/м²` : "";
  }
  if (/лист/.test(n)) {
    const value = text.match(/(\d{1,4})\s*лист/i)?.[1];
    return value ? `${value} шт.` : "";
  }
  if (/цвет/.test(n)) {
    if (/бел/.test(text)) return "Белый";
    if (/ч[её]рн/.test(text)) return "Чёрный";
  }
  if (/материал/.test(n)) return text.match(/(металл(?:ический)?|пластик(?:овый)?|дерев(?:о|янный)|ткань|кожа)/i)?.[1] ?? "";
  return "";
}


function CreateCharRow(props: {
  item: () => DraftCharacteristic;
  choiceOptions: (item: DraftCharacteristic) => string[];
  optionLabel: (item: DraftCharacteristic, value: string) => string;
  selectedOptions: (value: string) => string[];
  onUpdate: (id: string, value: string) => void;
  onUpdateName: (id: string, name: string) => void;
  onUpdateRange: (id: string, rangeOption: string) => void;
  onRemove: (id: string) => void;
  onPickOption: (item: DraftCharacteristic, option: string) => void;
}) {
  const item = () => props.item();
  return (
            <div class="create-char-row">
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
                            if (!item().required && value && value === item().value) {
                              props.onUpdate(item().id, "");
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
                <Show when={item().hint}><p class="create-char-hint">{item().hint}</p></Show>
              </dd>
            </div>
  );
}

function VoteRow(props: { label: string; tab: "okpd" | "ktru" | "kpgz" | "spgz"; vote: VotedCode | null | undefined }) {
  return <Show when={props.vote} fallback={
    <div>
      <dt>{props.label}</dt>
      <dd><span class="create-vote-name">—</span></dd>
    </div>
  }>{vote =>
    <div>
      <dt>{props.label}</dt>
      <dd>
        <A class="create-vote-code" href={directoryHref(props.tab, vote().code)}>{vote().code}</A>
        <span class="create-vote-name">{vote().name}</span>
      </dd>
    </div>
  }</Show>;
}

function CategoryPairs(props: {
  okpd2: VotedCode | null;
  ktru: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
  onEditKtru: () => void;
  onEditSpgz: () => void;
  class?: string;
}) {
  return <div class={`create-vote-card ${props.class ?? ""}`.trim()}>
    <div class="create-vote-group">
      <div class="create-vote-group-head">
        <h3>ОКПД2 и КТРУ</h3>
        <Button type="button" variant="outline" size="xs" onClick={props.onEditKtru}><ClipboardList size={12} />Изменить</Button>
      </div>
      <dl class="create-vote-rows">
        <VoteRow label="ОКПД2" tab="okpd" vote={props.okpd2} />
        <VoteRow label="КТРУ" tab="ktru" vote={props.ktru} />
      </dl>
    </div>
    <div class="create-vote-group">
      <div class="create-vote-group-head">
        <h3>КПГЗ и СПГЗ</h3>
        <Button type="button" variant="outline" size="xs" onClick={props.onEditSpgz}><ClipboardList size={12} />Изменить</Button>
      </div>
      <dl class="create-vote-rows">
        <VoteRow label="КПГЗ" tab="kpgz" vote={props.kpgz} />
        <VoteRow label="СПГЗ" tab="spgz" vote={props.spgz} />
      </dl>
    </div>
  </div>;
}

export default function NewProduct() {
  const [name, setName] = createSignal("");
  const [nameTouched, setNameTouched] = createSignal(false);
  const [sku, setSku] = createSignal("");
  const [model, setModel] = createSignal("");
  const [brand, setBrand] = createSignal("");
  const [description, setDescription] = createSignal("");
  const [selected, setSelected] = createSignal<KtruTreeNode | null>(null);
  const [okpd2, setOkpd2] = createSignal<VotedCode | null>(null);
  const [kpgz, setKpgz] = createSignal<VotedCode | null>(null);
  const [spgz, setSpgz] = createSignal<VotedCode | null>(null);
  const [ktruVote, setKtruVote] = createSignal<VotedCode | null>(null);
  const [ktruLocked, setKtruLocked] = createSignal(false);
  const [spgzLocked, setSpgzLocked] = createSignal(false);
  const [suggesting, setSuggesting] = createSignal(false);
  const [similar, setSimilar] = createSignal<ProductHit[]>([]);
  const [ktruPickerSelected, setKtruPickerSelected] = createSignal<KtruTreeNode | null>(null);
  const [ktruPickerOpen, setKtruPickerOpen] = createSignal(false);
  const [ktruPickerPath, setKtruPickerPath] = createSignal<string[]>([]);
  const [spgzPickerSelected, setSpgzPickerSelected] = createSignal<DirTreeNode | null>(null);
  const [spgzPickerOpen, setSpgzPickerOpen] = createSignal(false);
  const [spgzPickerPath, setSpgzPickerPath] = createSignal<string[]>([]);
  const [characteristics, setCharacteristics] = createSignal<DraftCharacteristic[]>([]);
  const [charStandards, setCharStandards] = createSignal<string[]>([]);
  const [noNewFeatures, setNoNewFeatures] = createSignal<boolean | null>(null);
  const [noNewFeaturesReason, setNoNewFeaturesReason] = createSignal("");
  const [generatedFor, setGeneratedFor] = createSignal("");
  const [photos, setPhotos] = createSignal<DraftPhoto[]>([]);
  const [photoIndex, setPhotoIndex] = createSignal(0);
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

  const clearCharMeta = () => {
    setCharStandards([]);
    setNoNewFeatures(null);
    setNoNewFeaturesReason("");
  };

  const clearPhotos = () => {
    revokePhotos(photos());
    setPhotos([]);
    setPhotoIndex(0);
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

  onCleanup(() => revokePhotos(photos()));

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

  const applyKtru = async (code: string, locked: boolean) => {
    const [node, record] = await Promise.all([getKtruNode(code), getKtruRecord(code)]);
    if (!node || node.kind !== "ktru") return;
    const text = description();
    const fields: DraftCharacteristic[] = (record?.characteristics ?? []).map((item) => {
      const options = item.options ?? [];
      const ranges = item.ranges ?? [];
      const raw = guessFromText(item.name, text);
      const id = item.code || item.name;
      if (isRangeCharacteristic(item)) {
        const value = extractCharacteristicNumber(raw);
        const number = parseCharacteristicNumber(value);
        return {
          id,
          name: item.name,
          value,
          rangeOption: number == null ? "" : (matchNumberToRange(number, ranges) ?? ""),
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
        value: resolveCharacteristicValue(raw, options, !!item.multiple, text),
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
    batch(() => {
      setSelected(node);
      setKtruVote({ code: node.code, name: node.name, score: ktruVote()?.score ?? 1 });
      setKtruLocked(locked);
      setCharacteristics((current) => [
        ...fields,
        ...current.filter((item) => item.source === "spgz" || item.source === "custom" || item.custom),
      ]);
      setCharStandards(record?.standards ?? []);
      setNoNewFeatures(record ? record.noNewFeatures : null);
      setNoNewFeaturesReason(record?.noNewFeaturesReason ?? "");
      setGeneratedFor(`${node.code}\u0000${text}`);
      if (!nameTouched() || !name().trim()) {
        setName(nameFromDescription(text) || node.name);
        setNameTouched(false);
      }
      if (record?.okpd2) setOkpd2({ code: record.okpd2.code, name: record.okpd2.name || record.okpd2.code, score: okpd2()?.score ?? 1 });
    });
  };

  const applyKpgz = async (code: string, locked: boolean) => {
    const node = await getDirectoryNode("kpgz", code);
    if (!node || node.kind !== "kpgz") return;
    batch(() => {
      setKpgz({ code: node.code, name: node.name, score: kpgz()?.score ?? 1 });
      setSpgz(null);
      setSpgzLocked(locked);
      setCharacteristics((current) => current.filter((item) => item.source !== "spgz"));
    });
  };

  const applySpgz = async (code: string, locked: boolean) => {
    const [node, record] = await Promise.all([getDirectoryNode("spgz", code), getDirectoryRecord("spgz", code)]);
    if (!node || node.kind !== "spgz") return;
    const kpgzLink = record?.links.find((link) => link.tab === "kpgz");
    const fields = (record?.characteristics ?? []).map((item) => draftFromSpgzCharacteristic(item, description()));
    batch(() => {
      setSpgz({ code: node.code, name: node.name || record?.name || node.code, score: spgz()?.score ?? 1 });
      setSpgzLocked(locked);
      setCharacteristics((current) => [
        ...current.filter((item) => item.source !== "spgz"),
        ...fields,
      ]);
      if (kpgzLink) {
        const label = kpgzLink.label.replace(/^КПГЗ\s+/, "");
        const sep = label.indexOf(" — ");
        setKpgz({
          code: kpgzLink.code,
          name: sep >= 0 ? label.slice(sep + 3) : label,
          score: kpgz()?.score ?? 1,
        });
      }
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
      setSimilar([]); setSuggesting(false);
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
    setSuggesting(true);
    let alive = true;
    const timer = setTimeout(() => {
      void suggestFromNeighbors(query).then((draft) => {
        if (!alive) return;
        batch(() => {
          setSimilar(draft.neighbors);
          setSuggesting(false);
          if (!spgzLocked()) {
            setKpgz(draft.kpgz);
            setSpgz(draft.spgz);
          }
          const draftKtru = draft.characteristics.filter((item) => item.source === "ktru");
          const draftSpgz = draft.characteristics.filter((item) => item.source === "spgz");
          if (!ktruLocked() && !spgzLocked()) {
            setCharacteristics((current) => [
              ...draftKtru,
              ...draftSpgz,
              ...current.filter((item) => item.source === "custom" || item.custom),
            ]);
          } else if (!ktruLocked()) {
            setCharacteristics((current) => [
              ...draftKtru,
              ...current.filter((item) => item.source !== "ktru"),
            ]);
          } else if (!spgzLocked()) {
            setCharacteristics((current) => [
              ...current.filter((item) => item.source !== "spgz"),
              ...draftSpgz,
            ]);
          }
          if (!ktruLocked()) {
            setOkpd2(draft.okpd2);
            setKtruVote(draft.ktru);
            setCharStandards(draft.standards);
            setNoNewFeatures(draft.noNewFeatures);
            setNoNewFeaturesReason(draft.noNewFeaturesReason);
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
      }, () => { if (alive) setSuggesting(false); });
    }, 420);
    onCleanup(() => { alive = false; clearTimeout(timer); });
  }));

  const updateCharacteristic = (id: string, value: string) => {
    const ids = new Set(linkedIdsFor(id));
    setCharacteristics((items) => items.map((item) => {
      if (!ids.has(item.id)) return item;
      const next = item.options.length ? mapValueToOptions(value, item.options) : value;
      if (!isRangeCharacteristic(item)) return { ...item, value: next };
      const number = parseCharacteristicNumber(next);
      return {
        ...item,
        value: next,
        rangeOption: number == null ? "" : (matchNumberToRange(number, item.ranges) ?? mapValueToOptions(next, item.options)),
      };
    }));
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
          <div class="section-heading"><Tags size={18} strokeWidth={1.6} /><h2>Категории</h2></div>
          <Show when={!suggesting()} fallback={<p class="create-live-hint">Ищем ближайшие товары и голосуем за категории…</p>}>
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
          <Show when={!suggesting()} fallback={<p class="create-live-hint">Ищем похожие позиции в каталоге…</p>}>
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
          <CategoryPairs
            class="create-summary-votes"
            okpd2={okpd2()}
            ktru={ktruVote()}
            kpgz={kpgz()}
            spgz={spgz()}
            onEditKtru={openKtruPicker}
            onEditSpgz={openSpgzPicker}
          />
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
              onUpdate={updateCharacteristic}
              onUpdateName={updateCharacteristicName}
              onUpdateRange={updateCharacteristicRange}
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
