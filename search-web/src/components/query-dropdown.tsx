import * as DropdownMenu from "@kobalte/core/dropdown-menu";
import Check from "lucide-solid/icons/check";
import ChevronDown from "lucide-solid/icons/chevron-down";
import CircleAlert from "lucide-solid/icons/circle-alert";
import Clock from "lucide-solid/icons/clock";
import LoaderCircle from "lucide-solid/icons/loader-circle";
import { For, Show, type JSX } from "solid-js";
import type { QueryStatus } from "~/lib/query-queue";
import { buttonVariants } from "~/ui-kit/button";

export type QueryDropdownItem = { id: string; text: string; status: QueryStatus; detail?: string };
const statusLabels: Record<QueryStatus, string> = {
  pending: "В очереди", processing: "Обрабатывается", done: "Обработан", error: "Ошибка · повторить",
};

export function QueryDropdown(props: {
  label: JSX.Element;
  items: QueryDropdownItem[];
  selectedId?: string | null;
  onSelect: (id: string) => void;
  class?: string;
  button?: boolean;
  hidePendingStatus?: boolean;
  emptyLabel?: string;
}) {
  return <DropdownMenu.Root gutter={6}>
    <DropdownMenu.Trigger class={`${props.button ? buttonVariants({ variant: "outline", size: "sm" }) : "query-dropdown-trigger"} ${props.class ?? ""}`}>
      {props.label}<ChevronDown size={14} aria-hidden="true" />
    </DropdownMenu.Trigger>
    <DropdownMenu.Portal>
      <DropdownMenu.Content class="query-dropdown-content">
        <Show when={props.items.length} fallback={<p class="query-dropdown-empty">{props.emptyLabel ?? "Запросов пока нет"}</p>}>
          <For each={props.items}>{(item) => <DropdownMenu.Item
            class="query-dropdown-item"
            classList={{ "query-dropdown-selected": props.selectedId === item.id }}
            onSelect={() => props.onSelect(item.id)}
            textValue={props.hidePendingStatus && item.status === "pending" ? item.text : `${item.text} ${statusLabels[item.status]}`}
          >
            <span class="query-dropdown-text">{item.text}</span>
            <Show when={!props.hidePendingStatus || item.status !== "pending"}>
              <span class={`query-dropdown-status ${item.status}`}>
                <Show when={item.status === "processing"}><LoaderCircle size={13} class="create-autofill-spinner" /></Show>
                <Show when={item.status === "done"}><Check size={13} /></Show>
                <Show when={item.status === "pending"}><Clock size={13} /></Show>
                <Show when={item.status === "error"}><CircleAlert size={13} /></Show>
                {statusLabels[item.status]}
              </span>
            </Show>
            <Show when={item.detail}><span class="query-dropdown-detail">{item.detail}</span></Show>
          </DropdownMenu.Item>}</For>
        </Show>
      </DropdownMenu.Content>
    </DropdownMenu.Portal>
  </DropdownMenu.Root>;
}
