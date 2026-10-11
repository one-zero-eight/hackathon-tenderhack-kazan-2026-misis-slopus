import * as Combobox from "@kobalte/core/combobox";
import ChevronDown from "lucide-solid/icons/chevron-down";
import Check from "lucide-solid/icons/check";
import { createMemo, createSignal, type JSX, Show } from "solid-js";

/** Catalog observations are suggestions, never a closed set of allowed values. */
export function CatalogCharacteristicInput(props: {
  name: string;
  value: string;
  examples: string[];
  exampleCounts: Record<string, number>;
  count: number;
  unit: string;
  numeric?: boolean;
  onChange: (value: string) => void;
  children?: JSX.Element;
}) {
  const [search, setSearch] = createSignal("");
  const options = createMemo(() => {
    const suggestions = [...new Set(props.examples)];
    return props.value.trim() && !suggestions.includes(props.value)
      ? [...suggestions, props.value] : suggestions;
  });
  const custom = (value: string) => !props.examples.includes(value);
  const normalize = (value: string) => value.toLocaleLowerCase("ru").replaceAll("ё", "е").replace(/\s+/g, " ").trim();
  const update = (value: string) => {
    if (value !== props.value) props.onChange(value);
  };
  return (
    <Combobox.Root<string>
      class="create-char-combobox"
      options={options()}
      value={props.value || null}
      onChange={(value) => { if (value !== null) update(value); }}
      onInputChange={update}
      noResetInputOnBlur
      triggerMode="focus"
      onOpenChange={(open, mode) => { if (open && mode !== "input") setSearch(""); }}
      defaultFilter={(option) => custom(option) || normalize(option).includes(normalize(search()))}
      placeholder="Выберите или введите своё"
      sameWidth
      gutter={4}
      itemComponent={(itemProps) => (
        <Combobox.Item class="create-char-combobox-option" item={itemProps.item}>
          <Combobox.ItemLabel class="create-char-combobox-label">
            <Show when={custom(itemProps.item.rawValue)} fallback={itemProps.item.rawValue}>
              <span class="create-char-combobox-custom">Своё значение: </span>{itemProps.item.rawValue}
            </Show>
          </Combobox.ItemLabel>
          <Show when={!custom(itemProps.item.rawValue) && props.count > 0 && props.exampleCounts[itemProps.item.rawValue] != null}>
            <span
              class="create-char-combobox-popularity"
              title={`${props.exampleCounts[itemProps.item.rawValue]} из ${props.count} товаров с этой характеристикой`}
              aria-label={`Популярность: ${props.exampleCounts[itemProps.item.rawValue]} из ${props.count} товаров с этой характеристикой`}
            >
              Популярность {Math.round(props.exampleCounts[itemProps.item.rawValue] / props.count * 100)}%
            </span>
          </Show>
          <Combobox.ItemIndicator class="create-char-combobox-indicator"><Check size={14} /></Combobox.ItemIndicator>
        </Combobox.Item>
      )}
    >
      <Combobox.Control
        class="create-char-combobox-control"
        onClick={(event) => {
          if ((event.target as Element).closest("input, button, [role='button']")) return;
          event.currentTarget.querySelector("input")?.focus();
        }}
      >
        <Combobox.Input
          aria-label={props.name}
          inputmode={props.numeric ? "decimal" : "text"}
          onInput={(event) => setSearch(event.currentTarget.value)}
        />
        <Show when={props.unit}><span class="create-char-unit">{props.unit}</span></Show>
        {props.children}
        <Combobox.Trigger class="create-char-combobox-trigger" aria-label={`${props.name}: популярные значения`}>
          <ChevronDown size={14} />
        </Combobox.Trigger>
      </Combobox.Control>
      <Combobox.Portal>
        <Combobox.Content class="create-char-combobox-content">
          <div class="create-char-combobox-caption">Популярные значения из каталога</div>
          <Combobox.Listbox class="create-char-combobox-list" />
          <div class="create-char-combobox-caption create-char-combobox-help">Можно выбрать из списка или ввести своё</div>
        </Combobox.Content>
      </Combobox.Portal>
    </Combobox.Root>
  );
}
