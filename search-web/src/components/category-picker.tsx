import Check from "lucide-solid/icons/check";
import ChevronRight from "lucide-solid/icons/chevron-right";
import Search from "lucide-solid/icons/search";
import SlidersHorizontal from "lucide-solid/icons/sliders-horizontal";
import X from "lucide-solid/icons/x";
import { createEffect, createMemo, createSignal, For, on, onCleanup, Show } from "solid-js";
import type { CategoryTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "~/ui-kit";
import "./category-picker.css";

function matchesQuery(node: CategoryTreeNode, query: string) {
  if (!query) return true;
  return node.name.toLowerCase().includes(query);
}

function TreeNode(props: {
  node: CategoryTreeNode;
  selectedId: number | undefined;
  matchIds: { ids: Set<number>; parents: Set<number> } | null;
  expanded: Set<number>;
  childrenOf: (parentId: number) => CategoryTreeNode[];
  onToggle: (id: number) => void;
  onSelect: (id: number) => void;
}) {
  const open = () => props.matchIds != null || props.expanded.has(props.node.id);
  const children = () => props.childrenOf(props.node.id);
  const matched = () => props.matchIds?.ids.has(props.node.id) ?? false;
  const selected = () => props.selectedId === props.node.id;
  return (
    <li>
      <div
        title={props.node.nameResolved ? undefined : "Название отсутствует в справочнике; ID и положение восстановлены из карточек"}
        class="cat-node"
        classList={{ selected: selected(), match: matched() }}
        role="treeitem"
        aria-expanded={props.node.hasChildren ? open() : undefined}
        aria-selected={selected()}
      >
        <button
          type="button"
          class="twist"
          classList={{ open: open() }}
          disabled={!props.node.hasChildren}
          aria-label={open() ? "Свернуть" : "Развернуть"}
          onClick={() => props.onToggle(props.node.id)}
        >
          <ChevronRight size={15} />
        </button>
        <button
          type="button"
          class="cat-node-main"
          onClick={() => props.onSelect(props.node.id)}
        >
          <span class="cat-node-name">{props.node.name}</span>
          <span class="cat-count">{formatNumber(props.node.count)}</span>
        </button>
      </div>
      <Show when={open() && props.node.hasChildren}>
        <ul class="cat-tree-list" role="group">
          <For each={children()}>{child =>
            <TreeNode
              node={child}
              selectedId={props.selectedId}
              matchIds={props.matchIds}
              expanded={props.expanded}
              childrenOf={props.childrenOf}
              onToggle={props.onToggle}
              onSelect={props.onSelect}
            />
          }</For>
        </ul>
      </Show>
    </li>
  );
}

export function CategoryPicker(props: {
  nodes: CategoryTreeNode[];
  value: number | undefined;
  onChange: (value: number | undefined) => void;
}) {
  const [open, setOpen] = createSignal(false);
  const [draft, setDraft] = createSignal("");
  const [filter, setFilter] = createSignal("");
  const [selected, setSelected] = createSignal<number | undefined>(props.value);
  const [expanded, setExpanded] = createSignal<Set<number>>(new Set());
  let timer: ReturnType<typeof setTimeout> | undefined;
  onCleanup(() => clearTimeout(timer));

  const byParent = createMemo(() => {
    const map = new Map<number | null, CategoryTreeNode[]>();
    for (const node of props.nodes) {
      const key = node.parentId;
      const list = map.get(key);
      if (list) list.push(node);
      else map.set(key, [node]);
    }
    return map;
  });

  const byId = createMemo(() => new Map(props.nodes.map(node => [node.id, node])));
  const selectedNode = () => (props.value != null ? byId().get(props.value) : undefined);

  const matchIds = createMemo(() => {
    const query = filter();
    if (query.length < 1) return null;
    const ids = new Set<number>();
    const parents = new Set<number>();
    for (const node of props.nodes) {
      if (!matchesQuery(node, query)) continue;
      ids.add(node.id);
      let parent = node.parentId;
      while (parent != null) {
        parents.add(parent);
        parent = byId().get(parent)?.parentId ?? null;
      }
    }
    return { ids, parents };
  });

  const roots = createMemo(() => {
    const children = byParent().get(null) ?? [];
    const matches = matchIds();
    if (!matches) return children;
    return children.filter(node => matches.ids.has(node.id) || matches.parents.has(node.id));
  });

  const childrenOf = (parentId: number) => {
    const children = byParent().get(parentId) ?? [];
    const matches = matchIds();
    if (!matches) return children;
    return children.filter(node => matches.ids.has(node.id) || matches.parents.has(node.id));
  };

  const toggle = (id: number) => {
    setExpanded(current => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const selectNode = (id: number) => {
    setSelected(id);
    const node = byId().get(id);
    if (node?.hasChildren) {
      setExpanded(current => new Set(current).add(id));
    }
  };

  const expandAncestors = (id: number | undefined) => {
    const next = new Set<number>();
    // Open each real root; an unresolved group stays collapsed.
    for (const node of byParent().get(null) ?? []) {
      if (node.id > 0) next.add(node.id);
    }
    if (id != null) {
      let parent = byId().get(id)?.parentId ?? null;
      while (parent != null) {
        next.add(parent);
        parent = byId().get(parent)?.parentId ?? null;
      }
    }
    setExpanded(next);
  };

  createEffect(on(open, isOpen => {
    clearTimeout(timer);
    if (!isOpen) return;
    setSelected(props.value);
    setDraft("");
    setFilter("");
    expandAncestors(props.value);
  }));

  const onFilterInput = (value: string) => {
    setDraft(value);
    clearTimeout(timer);
    timer = setTimeout(() => setFilter(value.trim().toLowerCase()), 200);
  };

  const confirm = () => {
    props.onChange(selected());
    setOpen(false);
  };

  const clear = () => {
    props.onChange(undefined);
    setOpen(false);
  };

  return <>
    <button type="button" class="category-select category-trigger" aria-label="Категория КПГЗ" onClick={() => setOpen(true)}>
      <SlidersHorizontal size={16} />
      <span class="category-trigger-label">{selectedNode()?.name ?? "Все категории"}</span>
      <Show when={selectedNode()}><span class="category-trigger-count">{formatNumber(selectedNode()!.count)}</span></Show>
      <ChevronRight size={15} class="category-trigger-chevron" />
    </button>

    <Dialog open={open()} onOpenChange={setOpen}>
      <DialogContent class="category-dialog">
        <DialogHeader>
          <DialogTitle>Категория КПГЗ</DialogTitle>
          <DialogDescription>Дерево по классификатору КПГЗ. Можно выбрать любую ветку — покажем товары внутри неё.</DialogDescription>
        </DialogHeader>
        <div class="category-picker-body">
          <label class="cat-filter">
            <Search size={15} />
            <input
              type="search"
              value={draft()}
              placeholder="Поиск по названию категории…"
              autocomplete="off"
              onInput={event => onFilterInput(event.currentTarget.value)}
            />
          </label>
          <button
            type="button"
            class="cat-all"
            classList={{ selected: selected() == null }}
            onClick={() => setSelected(undefined)}
          >
            <span>Все категории</span>
            <Show when={selected() == null}><Check size={14} /></Show>
          </button>
          <div class="cat-tree" role="tree">
            <Show when={roots().length} fallback={<div class="cat-empty">{filter() ? "Нет совпадений" : "Нет категорий"}</div>}>
              <ul class="cat-tree-list">
                <For each={roots()}>{node =>
                  <TreeNode
                    node={node}
                    selectedId={selected()}
                    matchIds={matchIds()}
                    expanded={expanded()}
                    childrenOf={childrenOf}
                    onToggle={toggle}
                    onSelect={selectNode}
                  />
                }</For>
              </ul>
            </Show>
          </div>
        </div>
        <DialogFooter>
          <Show when={props.value != null}>
            <Button type="button" variant="outline" class="category-clear" onClick={clear}>
              <X size={14} />Сбросить
            </Button>
          </Show>
          <Button type="button" variant="outline" onClick={() => setOpen(false)}>Отмена</Button>
          <Button type="button" onClick={confirm}>
            <Check size={15} />Применить
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
