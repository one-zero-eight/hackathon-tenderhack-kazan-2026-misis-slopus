import Plus from "lucide-solid/icons/plus";
import X from "lucide-solid/icons/x";
import { createMemo, createSignal, For, Index, Show } from "solid-js";
import {
  characteristicNameKey, characteristicSchemaProblem, characteristicValueProblem, draftFromCharacteristicSchema,
  inferCatalogSchema, popularCatalogCharacteristics, schemaFromCharacteristic,
  type CharacteristicKind, type CharacteristicSchema,
} from "~/lib/characteristic-editor";
import { catalogFrequencyLabel } from "~/lib/merge-characteristics";
import type { CatalogCharacteristic, CharacteristicRange, DraftCharacteristic } from "~/lib/types";
import {
  Button, DialogContent, DialogFooter, DialogTitle,
  TextField, TextFieldInput, TextFieldLabel, TextFieldTextarea,
} from "~/ui-kit";
import { CatalogCharacteristicInput } from "./catalog-characteristic-input";

const KIND_LABELS: Record<CharacteristicKind, string> = {
  text: "Качественная — свободный текст",
  number: "Количественная — число",
  choice: "Качественная — выбор из списка",
  boolean: "Логическая — Да / Нет",
  range: "Количественная — диапазоны",
};

export function CharacteristicEditor(props: {
  item?: DraftCharacteristic;
  statistics: CatalogCharacteristic[];
  existing: DraftCharacteristic[];
  onSave: (item: DraftCharacteristic) => void;
  onCancel: () => void;
}) {
  const [schema, setSchema] = createSignal<CharacteristicSchema>(props.item ? schemaFromCharacteristic(props.item) : {
    name: "", kind: "text", unit: "", options: [], ranges: [], multiple: false, required: false, hint: "",
  });
  const [value, setValue] = createSignal(props.item?.value ?? "");
  const [selectedStats, setSelectedStats] = createSignal(props.item?.catalogFrequency);
  const [submitted, setSubmitted] = createSignal(false);
  const popular = createMemo(() => popularCatalogCharacteristics(props.statistics));
  const available = createMemo(() => popular().filter((item) => !props.existing.some((field) =>
    field.id !== props.item?.id && characteristicNameKey(field.name) === characteristicNameKey(item.name))));
  const suggestions = createMemo(() => available().filter((item) =>
    characteristicNameKey(item.name).includes(characteristicNameKey(schema().name))).slice(0, 10));
  const units = createMemo(() => [...new Set(props.statistics.filter((item) =>
    characteristicNameKey(item.name) === characteristicNameKey(schema().name)).map((item) => item.unit).filter(Boolean))]);
  const problem = createMemo(() => characteristicSchemaProblem(schema()) ?? characteristicValueProblem(schema(), value()) ?? (props.existing.some((field) =>
    field.id !== props.item?.id && characteristicNameKey(field.name) === characteristicNameKey(schema().name))
    ? "Характеристика с таким названием уже добавлена." : null));
  const update = <K extends keyof CharacteristicSchema>(key: K, next: CharacteristicSchema[K]) =>
    setSchema((current) => ({ ...current, [key]: next }));
  const choose = (item: CatalogCharacteristic) => {
    setSchema(inferCatalogSchema(item));
    setSelectedStats(item);
    setValue("");
  };
  const changeName = (name: string) => {
    const match = popular().find((item) => characteristicNameKey(item.name) === characteristicNameKey(name));
    if (match && characteristicNameKey(schema().name) !== characteristicNameKey(name)) {
      setSchema({ ...inferCatalogSchema(match), name });
      setSelectedStats(match);
    } else {
      update("name", name);
      if (!match) setSelectedStats(undefined);
    }
  };
  const changeKind = (kind: CharacteristicKind) => {
    setSchema((current) => ({ ...current, kind,
      multiple: (kind === "text" || kind === "choice") && current.multiple,
      options: kind === "choice" && !current.options.length ? selectedStats()?.examples ?? [] : current.options,
      ranges: kind === "range" && !current.ranges.length ? [emptyRange()] : current.ranges,
    }));
  };
  const changeRange = (index: number, patch: Partial<CharacteristicRange>) =>
    update("ranges", schema().ranges.map((range, position) => position === index ? { ...range, ...patch } : range));
  const save = (event: SubmitEvent) => {
    event.preventDefault();
    setSubmitted(true);
    if (problem()) return;
    const id = props.item?.id ?? `custom-${crypto.randomUUID()}`;
    props.onSave(draftFromCharacteristicSchema(schema(), id, { ...props.item, value: value() }, selectedStats()));
  };

  return <DialogContent class="create-characteristic-dialog">
    <form class="create-characteristic-form" onSubmit={save}>
      <DialogTitle class="sr-only">{props.item ? "Схема характеристики" : "Новая характеристика"}</DialogTitle>
      <div class="create-characteristic-body">
        <TextField class="create-characteristic-field" value={schema().name} onChange={changeName}>
          <TextFieldLabel>Название характеристики</TextFieldLabel>
          <TextFieldInput placeholder="Например, масса или материал" autofocus />
        </TextField>
        <Show when={!props.item && suggestions().length}>
          <div class="create-characteristic-suggestions">
            <p class="create-characteristic-caption">Популярные характеристики каталога СТЕ</p>
            <div class="create-characteristic-suggestion-list">
              <For each={suggestions()}>{(item) => <button type="button" onClick={() => choose(item)} title={catalogFrequencyLabel(item)}>
                <span><strong>{item.name}</strong><small>{item.numeric ? "Число" : "Текст"}{item.unit ? ` · ${item.unit}` : ""}</small></span>
                <span class="create-char-combobox-popularity">{item.scope === "schema" ? "Схема КПГЗ" : `Популярность ${Math.round(item.frequency * 100)}%`}</span>
              </button>}</For>
            </div>
          </div>
        </Show>
        <Show when={selectedStats()}>{(stats) => <p class="create-characteristic-caption">{catalogFrequencyLabel(stats())} Подсказка, не ограничение.</p>}</Show>
        <div class="create-characteristic-grid">
          <label class="create-characteristic-label">Тип характеристики
            <select value={schema().kind} onChange={(event) => changeKind(event.currentTarget.value as CharacteristicKind)}>
              <For each={Object.entries(KIND_LABELS)}>{([kind, label]) => <option value={kind}>{label}</option>}</For>
            </select>
          </label>
          <TextField class="create-characteristic-field" value={schema().unit} onChange={(unit) => {
            setSchema((current) => ({ ...current, unit,
              okeiCode: unit.trim() === current.unit.trim() ? current.okeiCode : undefined }));
            const match = props.statistics.find((item) => characteristicNameKey(item.name) === characteristicNameKey(schema().name) && item.unit === unit);
            setSelectedStats(match);
            if (match) update("okeiCode", match.okeiCode);
          }}>
            <TextFieldLabel>Единица измерения</TextFieldLabel>
            <TextFieldInput placeholder="Без единицы, мм, кг…" list="characteristic-unit-suggestions" />
            <datalist id="characteristic-unit-suggestions"><For each={units()}>{(unit) => <option value={unit} />}</For></datalist>
          </TextField>
        </div>
        <Show when={schema().kind === "choice"}>
          <TextField class="create-characteristic-field" value={schema().options.join("\n")} onChange={(text) => update("options", text.split("\n"))}>
            <TextFieldLabel>Допустимые значения — каждое с новой строки</TextFieldLabel>
            <TextFieldTextarea rows={4} placeholder={"Сталь\nДерево\nПластик"} />
          </TextField>
        </Show>
        <Show when={schema().kind === "range"}>
          <div class="create-characteristic-ranges">
            <p class="create-characteristic-caption">Допустимые диапазоны. Пустая граница означает отсутствие ограничения.</p>
            <Index each={schema().ranges}>{(range, index) => <div class="create-characteristic-range">
              <label>Минимум<input type="text" inputmode="decimal" value={range().min ?? ""} onChange={(event) => changeRange(index, { min: boundary(event.currentTarget.value) })} /></label>
              <label>Максимум<input type="text" inputmode="decimal" value={range().max ?? ""} onChange={(event) => changeRange(index, { max: boundary(event.currentTarget.value) })} /></label>
              <button type="button" class="create-char-remove" aria-label="Удалить диапазон" onClick={() => update("ranges", schema().ranges.filter((_, position) => position !== index))}><X size={14} /></button>
              <label class="create-characteristic-check"><input type="checkbox" checked={range().minInclusive} onChange={(event) => changeRange(index, { minInclusive: event.currentTarget.checked })} />Включая минимум</label>
              <label class="create-characteristic-check"><input type="checkbox" checked={range().maxInclusive} onChange={(event) => changeRange(index, { maxInclusive: event.currentTarget.checked })} />Включая максимум</label>
            </div>}</Index>
            <Button type="button" variant="outline" size="sm" onClick={() => update("ranges", [...schema().ranges, emptyRange()])}><Plus size={14} />Добавить диапазон</Button>
          </div>
        </Show>
        <div class="create-characteristic-checks">
          <label class="create-characteristic-check"><input type="checkbox" checked={schema().required} onChange={(event) => update("required", event.currentTarget.checked)} />Обязательная</label>
          <Show when={schema().kind === "text" || schema().kind === "choice"}>
            <label class="create-characteristic-check"><input type="checkbox" checked={schema().multiple} onChange={(event) => update("multiple", event.currentTarget.checked)} />Несколько значений</label>
          </Show>
        </div>
        <TextField class="create-characteristic-field" value={schema().hint} onChange={(hint) => update("hint", hint)}>
          <TextFieldLabel>Пояснение или стандарт</TextFieldLabel>
          <TextFieldInput placeholder="Необязательно" />
        </TextField>
        <div class="create-characteristic-value">
          <p class="create-characteristic-label">Значение характеристики (необязательно)</p>
          <CatalogCharacteristicInput name="Значение новой характеристики" value={value()}
            examples={schema().kind === "boolean" ? ["Да", "Нет"] : schema().kind === "choice" ? schema().options.filter(Boolean) : selectedStats()?.examples ?? []}
            exampleCounts={selectedStats()?.exampleCounts ?? {}} count={selectedStats()?.count ?? 0}
            unit={schema().unit} numeric={schema().kind === "number" || schema().kind === "range"} onChange={setValue} />
        </div>
        <Show when={submitted() && problem()}><p class="create-autofill-error" role="alert">{problem()}</p></Show>
      </div>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={props.onCancel}>Отмена</Button>
        <Button type="submit">{props.item ? "Сохранить" : "Добавить характеристику"}</Button>
      </DialogFooter>
    </form>
  </DialogContent>;
}

function emptyRange(): CharacteristicRange {
  return { option: "", min: null, max: null, minInclusive: true, maxInclusive: false };
}

function boundary(value: string): number | null {
  return value.trim() ? Number(value.replace(",", ".")) : null;
}
