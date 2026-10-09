import { Meta, Title } from "@solidjs/meta";
import { createAsync, revalidate } from "@solidjs/router";
import { ChevronRight, Network, Search } from "lucide-solid";
import { createEffect, createMemo, createSignal, For, on, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import { getKtruChildren, getKtruRecord, getKtruStats, searchKtru } from "~/lib/rpc";
import type { KtruTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import "./ktru.css";

function statusBadge(status: string | null) {
  if (!status) return null;
  const kind = status === "ACTIVE" ? "active" : status === "TERMINATED" ? "terminated" : "";
  return <span class={`ktru-badge ${kind}`}>{status === "ACTIVE" ? "активна" : status === "TERMINATED" ? "исключена" : status}</span>;
}

function TreeBranch(props: {
  parentId: string | null;
  selectedId: string | null;
  onSelect: (node: KtruTreeNode) => void;
}) {
  const [extra, setExtra] = createSignal<KtruTreeNode[]>([]);
  const page = createAsync(() => getKtruChildren(props.parentId, 0));
  createEffect(on(() => props.parentId, () => setExtra([]), { defer: true }));
  const nodes = createMemo(() => [...(page()?.nodes ?? []), ...extra()]);
  const loadMore = async () => {
    const more = await getKtruChildren(props.parentId, nodes().length);
    setExtra(current => [...current, ...more.nodes]);
  };
  return <Show when={page()} fallback={<div class="ktru-loading">Загружаем…</div>}>{data =>
    <Show when={nodes().length} fallback={<div class="ktru-empty">Нет дочерних позиций</div>}>
      <ul class="ktru-tree-list">
        <For each={nodes()}>{node => <TreeNode node={node} selectedId={props.selectedId} onSelect={props.onSelect} />}</For>
        <Show when={nodes().length < data().total}>
          <li><button type="button" class="ktru-more" onClick={() => void loadMore()}>Показать ещё ({formatNumber(data().total - nodes().length)})</button></li>
        </Show>
      </ul>
    </Show>
  }</Show>;
}

function TreeNode(props: {
  node: KtruTreeNode;
  selectedId: string | null;
  onSelect: (node: KtruTreeNode) => void;
}) {
  const [open, setOpen] = createSignal(false);
  const selected = () => props.selectedId === props.node.id;
  const openDetails = () => {
    props.onSelect(props.node);
    if (props.node.hasChildren) setOpen(true);
  };
  return <li>
    <div class="ktru-node" classList={{ selected: selected() }} role="treeitem" aria-expanded={props.node.hasChildren ? open() : undefined} aria-selected={selected()}>
      <button type="button" class="twist" classList={{ open: open() }} disabled={!props.node.hasChildren} aria-label={open() ? "Свернуть" : "Развернуть"} onClick={() => setOpen(value => !value)}>
        <ChevronRight size={15} />
      </button>
      <button type="button" class="ktru-node-main" onClick={openDetails}>
        <span class="ktru-node-code">{props.node.code}<Show when={props.node.childCount > 0}><span class="ktru-count"> · {formatNumber(props.node.childCount)}</span></Show></span>
        <span class="ktru-node-name">{props.node.name}</span>
        <span class="ktru-node-meta">
          <Show when={props.node.kind === "ktru"}>{statusBadge(props.node.status)}</Show>
          <Show when={props.node.isTemplate}><span class="ktru-badge template">шаблон</span></Show>
        </span>
      </button>
    </div>
    <Show when={open() && props.node.hasChildren}>
      <DataBoundary resetKey={props.node.id} retry={() => revalidate(getKtruChildren.key)}>
        <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}>
          <TreeBranch parentId={props.node.id} selectedId={props.selectedId} onSelect={props.onSelect} />
        </Suspense>
      </DataBoundary>
    </Show>
  </li>;
}

function JsonPanel(props: { code: string | null; title: string | null }) {
  const record = createAsync(() => props.code ? getKtruRecord(props.code) : Promise.resolve(null));
  return <Show when={props.code} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию КТРУ</strong>Параметры категории отобразятся здесь в виде JSON.</div></div>}>
    <DataBoundary resetKey={props.code ?? ""} retry={() => revalidate(getKtruRecord.key)}>
      <Suspense fallback={<div class="ktru-loading">Загружаем JSON…</div>}>
        <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>
          {data => <pre class="ktru-json" aria-label={props.title ? `JSON: ${props.title}` : "JSON позиции КТРУ"}>{JSON.stringify(data(), null, 2)}</pre>}
        </Show>
      </Suspense>
    </DataBoundary>
  </Show>;
}

function StatsLine() {
  const stats = createAsync(() => getKtruStats());
  return <Show when={stats()}><div class="ktru-stats"><span><strong>{formatNumber(stats()!.total)}</strong> позиций КТРУ</span><span><strong>{formatNumber(stats()!.roots)}</strong> корневых групп ОКПД2</span></div></Show>;
}

export default function KtruPage() {
  const [selected, setSelected] = createSignal<KtruTreeNode | null>(null);
  const [draft, setDraft] = createSignal("");
  const [query, setQuery] = createSignal("");
  const results = createAsync(() => query() ? searchKtru(query()) : Promise.resolve([]));
  let timer: ReturnType<typeof setTimeout> | undefined;
  const onSearchInput = (value: string) => {
    setDraft(value);
    clearTimeout(timer);
    timer = setTimeout(() => setQuery(value.trim()), 220);
  };
  return <main id="main-content" class="page-width ktru-main">
    <Title>КТРУ — дерево категорий</Title>
    <Meta name="description" content="Дерево позиций каталога товаров, работ и услуг (КТРУ) с полными параметрами в JSON." />
    <section class="ktru-hero">
      <h1><Network size={34} strokeWidth={1.4} style={{ "vertical-align": "-6px", "margin-right": "10px", color: "var(--green)" }} />КТРУ</h1>
      <p>Иерархия по ОКПД2 и связям parent из <code>ktru-actual.parquet</code>. Для каждой позиции доступны все исходные параметры.</p>
      <DataBoundary retry={() => revalidate(getKtruStats.key)}><Suspense fallback={<div class="ktru-loading">Считаем позиции…</div>}><StatsLine /></Suspense></DataBoundary>
    </section>
    <label class="ktru-search"><Search size={16} /><input type="search" value={draft()} placeholder="Поиск по коду или названию…" autocomplete="off" onInput={event => onSearchInput(event.currentTarget.value)} /></label>
    <Show when={query().length >= 2}>
      <DataBoundary resetKey={query()} retry={() => revalidate(searchKtru.key)}>
        <Suspense fallback={<div class="ktru-loading">Ищем…</div>}>
          <Show when={results()} keyed>
            {items => <Show when={items.length} fallback={<div class="ktru-empty">Ничего не найдено</div>}>
              <div class="ktru-search-results">
                <For each={items}>{node =>
                  <button type="button" classList={{ selected: selected()?.id === node.id }} onClick={() => setSelected(node)}>
                    <span class="ktru-node-code">{node.code}</span>
                    <span class="ktru-node-name">{node.name}</span>
                  </button>
                }</For>
              </div>
            </Show>}
          </Show>
        </Suspense>
      </DataBoundary>
    </Show>
    <div class="ktru-layout">
      <section class="ktru-panel" aria-label="Дерево КТРУ">
        <div class="ktru-panel-head"><h2>Дерево</h2><span>ОКПД2 → позиции КТРУ</span></div>
        <div class="ktru-tree" role="tree">
          <DataBoundary retry={() => revalidate(getKtruChildren.key)}>
            <Suspense fallback={<div class="ktru-loading">Строим дерево…</div>}>
              <TreeBranch parentId={null} selectedId={selected()?.id ?? null} onSelect={setSelected} />
            </Suspense>
          </DataBoundary>
        </div>
      </section>
      <section class="ktru-panel" aria-label="Параметры выбранной позиции">
        <div class="ktru-panel-head">
          <h2>Параметры</h2>
          <Show when={selected()?.kind === "ktru"} fallback={<span>JSON позиции</span>}>
            <span>{selected()!.code}</span>
          </Show>
        </div>
        <Show when={selected()?.kind === "okpd"}>
          <div class="ktru-json-placeholder"><div><strong>{selected()!.code}</strong>{selected()!.name}<br /><br />Это группа ОКПД2. Выберите позицию КТРУ ниже по дереву, чтобы увидеть JSON.</div></div>
        </Show>
        <Show when={selected()?.kind !== "okpd"}>
          <JsonPanel code={selected()?.kind === "ktru" ? selected()!.code : null} title={selected()?.name ?? null} />
        </Show>
      </section>
    </div>
  </main>;
}
