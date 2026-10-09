import { Meta, Title } from "@solidjs/meta";
import { createAsync, revalidate } from "@solidjs/router";
import { ChevronRight, Network, Search } from "lucide-solid";
import { createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import { getKtruChildren, getKtruFilterMeta, getKtruRecord, getKtruStats } from "~/lib/rpc";
import type { KtruChildrenPage, KtruTreeNode } from "~/lib/types";
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
  filter: string;
  matchIds: Set<string>;
  onSelect: (node: KtruTreeNode) => void;
}) {
  const [extra, setExtra] = createSignal<KtruTreeNode[]>([]);
  const [page, setPage] = createSignal<KtruChildrenPage | undefined>();
  const [loading, setLoading] = createSignal(true);
  createEffect(on(() => [props.parentId, props.filter] as const, ([parentId, filter]) => {
    let alive = true;
    setExtra([]);
    setLoading(true);
    void getKtruChildren(parentId, 0, filter).then(data => {
      if (!alive) return;
      setPage(data);
      setLoading(false);
    }, () => { if (alive) setLoading(false); });
    onCleanup(() => { alive = false; });
  }));
  const nodes = createMemo(() => [...(page()?.nodes ?? []), ...extra()]);
  const loadMore = async () => {
    const more = await getKtruChildren(props.parentId, nodes().length, props.filter);
    setExtra(current => [...current, ...more.nodes]);
  };
  return <Show when={page()} fallback={<div class="ktru-loading">Загружаем…</div>}>{data =>
    <div classList={{ "ktru-tree-pending": loading() }}>
      <Show when={nodes().length} fallback={<div class="ktru-empty">{props.filter ? "Нет совпадений в этой ветке" : "Нет дочерних позиций"}</div>}>
        <ul class="ktru-tree-list">
          <For each={nodes()}>{node =>
            <TreeNode node={node} selectedId={props.selectedId} filter={props.filter} matchIds={props.matchIds} onSelect={props.onSelect} />
          }</For>
          <Show when={nodes().length < data().total}>
            <li><button type="button" class="ktru-more" onClick={() => void loadMore()}>Показать ещё ({formatNumber(data().total - nodes().length)})</button></li>
          </Show>
        </ul>
      </Show>
    </div>
  }</Show>;
}

function TreeNode(props: {
  node: KtruTreeNode;
  selectedId: string | null;
  filter: string;
  matchIds: Set<string>;
  onSelect: (node: KtruTreeNode) => void;
}) {
  const filtering = () => props.filter.length >= 2;
  const [manualOpen, setManualOpen] = createSignal<boolean | null>(null);
  const open = () => manualOpen() ?? (filtering() && props.node.hasChildren);
  const selected = () => props.selectedId === props.node.id;
  const matched = () => props.matchIds.has(props.node.id);
  createEffect(on(() => props.filter, () => setManualOpen(null)));
  const openDetails = () => {
    props.onSelect(props.node);
    if (props.node.hasChildren) setManualOpen(true);
  };
  return <li>
    <div class="ktru-node" classList={{ selected: selected(), match: matched() }} role="treeitem" aria-expanded={props.node.hasChildren ? open() : undefined} aria-selected={selected()}>
      <button type="button" class="twist" classList={{ open: open() }} disabled={!props.node.hasChildren} aria-label={open() ? "Свернуть" : "Развернуть"} onClick={() => setManualOpen(!(open()))}>
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
      <TreeBranch parentId={props.node.id} selectedId={props.selectedId} filter={props.filter} matchIds={props.matchIds} onSelect={props.onSelect} />
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
  const [filter, setFilter] = createSignal("");
  const [matchIds, setMatchIds] = createSignal(new Set<string>());
  const [matchCount, setMatchCount] = createSignal<number | null>(null);
  const [matchTruncated, setMatchTruncated] = createSignal(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  createEffect(on(() => filter(), value => {
    let alive = true;
    if (value.length < 2) {
      setMatchIds(new Set<string>());
      setMatchCount(null);
      setMatchTruncated(false);
      return;
    }
    void getKtruFilterMeta(value).then(meta => {
      if (!alive) return;
      setMatchIds(new Set(meta.matchIds));
      setMatchCount(meta.matchCount);
      setMatchTruncated(meta.truncated);
    });
    onCleanup(() => { alive = false; });
  }));
  const onFilterInput = (value: string) => {
    setDraft(value);
    clearTimeout(timer);
    timer = setTimeout(() => setFilter(value.trim()), 280);
  };
  return <main id="main-content" class="page-width ktru-main">
    <Title>КТРУ — дерево категорий</Title>
    <Meta name="description" content="Дерево позиций каталога товаров, работ и услуг (КТРУ) с полными параметрами в JSON." />
    <section class="ktru-hero">
      <h1><Network size={34} strokeWidth={1.4} style={{ "vertical-align": "-6px", "margin-right": "10px", color: "var(--green)" }} />КТРУ</h1>
      <p>Иерархия по ОКПД2 и связям parent из <code>ktru-actual.parquet</code>. Для каждой позиции доступны все исходные параметры.</p>
      <DataBoundary retry={() => revalidate(getKtruStats.key)}><Suspense fallback={<div class="ktru-loading">Считаем позиции…</div>}><StatsLine /></Suspense></DataBoundary>
    </section>
    <div class="ktru-layout">
      <section class="ktru-panel" aria-label="Дерево КТРУ">
        <div class="ktru-panel-head">
          <h2>Дерево</h2>
          <Show when={matchCount() !== null} fallback={<span>ОКПД2 → позиции КТРУ</span>}>
            <span>{formatNumber(matchCount()!)} совп.{matchTruncated() ? "+" : ""}</span>
          </Show>
        </div>
        <label class="ktru-filter"><Search size={15} /><input type="search" value={draft()} placeholder="Фильтр по коду или названию…" autocomplete="off" onInput={event => onFilterInput(event.currentTarget.value)} /></label>
        <div class="ktru-tree" role="tree">
          <TreeBranch parentId={null} selectedId={selected()?.id ?? null} filter={filter()} matchIds={matchIds()} onSelect={setSelected} />
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
