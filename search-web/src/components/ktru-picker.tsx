import { createAsync, revalidate } from "@solidjs/router";
import { Check, ChevronRight, Search } from "lucide-solid";
import { createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import { getKtruChildren, getKtruFilterMeta, getKtruRaw } from "~/lib/rpc";
import type { KtruChildrenPage, KtruTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import { Button } from "~/ui-kit";
import "~/routes/ktru.css";

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
    void getKtruChildren(parentId, 0, filter).then((data) => {
      if (!alive) return;
      setPage(data);
      setLoading(false);
    }, () => { if (alive) setLoading(false); });
    onCleanup(() => { alive = false; });
  }));
  const nodes = createMemo(() => [...(page()?.nodes ?? []), ...extra()]);
  const loadMore = async () => {
    const more = await getKtruChildren(props.parentId, nodes().length, props.filter);
    setExtra((current) => [...current, ...more.nodes]);
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

function DetailPanel(props: { node: KtruTreeNode | null }) {
  const record = createAsync(() => props.node?.kind === "ktru" ? getKtruRaw(props.node.code) : Promise.resolve(null));
  return <Show when={props.node} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию КТРУ</strong>Описание, ОКПД2, единицы и характеристики появятся здесь.</div></div>}>
    <Show when={props.node!.kind === "okpd"}>
      <div class="ktru-json-placeholder"><div><strong>{props.node!.code}</strong>{props.node!.name}<br /><br />Это группа ОКПД2. Выберите позицию КТРУ ниже по дереву.</div></div>
    </Show>
    <Show when={props.node!.kind === "ktru"}>
      <DataBoundary resetKey={props.node!.code} retry={() => revalidate(getKtruRaw.key)}>
        <Suspense fallback={<div class="ktru-loading">Загружаем параметры…</div>}>
          <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>
            {data => {
              const row = () => data() as Record<string, unknown>;
              const okpd = () => (row().okpd2 ?? null) as { code?: string; name?: string } | null;
              const units = () => (row().units ?? []) as Array<{ code?: string; name?: string }>;
              const chars = () => (row().characteristics ?? []) as Array<{ name?: string; required?: boolean; type?: string }>;
              return <div class="ktru-detail">
                <div class="ktru-detail-head">
                  <strong>{String(row().name ?? props.node!.name)}</strong>
                  <code>{props.node!.code}</code>
                  {statusBadge(props.node!.status)}
                  <Show when={props.node!.isTemplate}><span class="ktru-badge template">шаблон</span></Show>
                </div>
                <Show when={String(row().description ?? "").trim()}>
                  <p class="ktru-detail-description">{String(row().description)}</p>
                </Show>
                <dl class="ktru-detail-meta">
                  <Show when={okpd()?.code}><div><dt>ОКПД2</dt><dd>{okpd()!.code} — {okpd()!.name || "—"}</dd></div></Show>
                  <Show when={units().length}><div><dt>Единицы</dt><dd>{units().map(unit => unit.name || unit.code).filter(Boolean).join(", ")}</dd></div></Show>
                  <Show when={row().application_date_start}><div><dt>Применение с</dt><dd>{String(row().application_date_start)}</dd></div></Show>
                </dl>
                <Show when={chars().length}>
                  <div class="ktru-detail-chars">
                    <h3>Характеристики ({chars().length})</h3>
                    <ul>
                      <For each={chars().slice(0, 40)}>{item =>
                        <li><span>{item.name || "—"}</span><Show when={item.required}><em>обяз.</em></Show></li>
                      }</For>
                    </ul>
                    <Show when={chars().length > 40}><p class="ktru-empty">Показаны первые 40 из {chars().length}</p></Show>
                  </div>
                </Show>
              </div>;
            }}
          </Show>
        </Suspense>
      </DataBoundary>
    </Show>
  </Show>;
}

export function KtruPickerBody(props: {
  selected: KtruTreeNode | null;
  onSelect: (node: KtruTreeNode) => void;
  initialFilter?: string;
}) {
  const [draft, setDraft] = createSignal(props.initialFilter ?? "");
  const [filter, setFilter] = createSignal((props.initialFilter ?? "").trim());
  const [matchIds, setMatchIds] = createSignal(new Set<string>());
  const [matchCount, setMatchCount] = createSignal<number | null>(null);
  const [matchTruncated, setMatchTruncated] = createSignal(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  createEffect(on(() => props.initialFilter, (value) => {
    if (value === undefined) return;
    setDraft(value);
    setFilter(value.trim());
  }));
  createEffect(on(() => filter(), (value) => {
    let alive = true;
    if (value.length < 2) {
      setMatchIds(new Set<string>());
      setMatchCount(null);
      setMatchTruncated(false);
      return;
    }
    void getKtruFilterMeta(value).then((meta) => {
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
  return <div class="ktru-picker-body">
    <section class="ktru-panel" aria-label="Дерево КТРУ">
      <div class="ktru-panel-head">
        <h2>Дерево</h2>
        <Show when={matchCount() !== null} fallback={<span>ОКПД2 → КТРУ</span>}>
          <span>{formatNumber(matchCount()!)} совп.{matchTruncated() ? "+" : ""}</span>
        </Show>
      </div>
      <label class="ktru-filter"><Search size={15} /><input type="search" value={draft()} placeholder="Быстрый поиск по коду или названию…" autocomplete="off" onInput={(event) => onFilterInput(event.currentTarget.value)} /></label>
      <div class="ktru-tree" role="tree">
        <TreeBranch parentId={null} selectedId={props.selected?.id ?? null} filter={filter()} matchIds={matchIds()} onSelect={props.onSelect} />
      </div>
    </section>
    <section class="ktru-panel" aria-label="Параметры выбранной позиции">
      <div class="ktru-panel-head">
        <h2>Информация</h2>
        <Show when={props.selected?.kind === "ktru"} fallback={<span>Описание позиции</span>}>
          <span>{props.selected!.code}</span>
        </Show>
      </div>
      <DetailPanel node={props.selected} />
    </section>
  </div>;
}

export function KtruPickerActions(props: {
  selected: KtruTreeNode | null;
  onConfirm: (node: KtruTreeNode) => void;
  onCancel: () => void;
}) {
  const canConfirm = () => props.selected?.kind === "ktru";
  return <>
    <Button type="button" variant="outline" onClick={props.onCancel}>Отмена</Button>
    <Button type="button" disabled={!canConfirm()} onClick={() => props.selected && props.onConfirm(props.selected)}>
      <Check size={15} />Выбрать позицию
    </Button>
  </>;
}

/** Characteristic names from a KTRU parquet record for the create form. */
export function characteristicsFromKtruRecord(record: Record<string, unknown> | null | undefined): { name: string; value: string }[] {
  const list = (record?.characteristics ?? []) as Array<{ name?: string | null }>;
  const seen = new Set<string>();
  const out: { name: string; value: string }[] = [];
  for (const item of list) {
    const name = (item.name ?? "").trim();
    if (!name || seen.has(name)) continue;
    seen.add(name);
    out.push({ name, value: "" });
    if (out.length >= 40) break;
  }
  return out;
}
