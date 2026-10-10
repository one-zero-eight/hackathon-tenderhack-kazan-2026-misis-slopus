import { createAsync, revalidate } from "@solidjs/router";
import Check from "lucide-solid/icons/check";
import ChevronRight from "lucide-solid/icons/chevron-right";
import Search from "lucide-solid/icons/search";
import { createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary, directoryHref } from "~/components/catalog";
import { getDirectoryRecord, getKtruChildren, getKtruFilterMeta, getKtruRecord } from "~/lib/rpc";
import type { KtruChildrenPage, KtruTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import { Button } from "~/ui-kit";
import "~/routes/directories.css";
import "~/routes/ktru.css";

function statusBadge(status: string | null) {
  if (!status) return null;
  const kind = status === "ACTIVE" ? "active" : status === "TERMINATED" ? "terminated" : "";
  return <span class={`ktru-badge ${kind}`}>{status === "ACTIVE" ? "активна" : status === "TERMINATED" ? "исключена" : status}</span>;
}

function nodeCodeLabel(node: { kind: string; code: string }): string {
  const kind = node.kind === "okpd" ? "ОКПД2" : node.kind === "ktru" ? "КТРУ" : node.kind.toUpperCase();
  const code = node.code?.trim();
  if (!code || code === "—") return kind;
  return `${kind} ${code}`;
}

function nextPathId(pathIds: string[], parentId: string | null) {
  if (!pathIds.length) return null;
  if (parentId == null) return pathIds[0] ?? null;
  const index = pathIds.indexOf(parentId);
  return index >= 0 ? pathIds[index + 1] ?? null : null;
}

function TreeBranch(props: {
  parentId: string | null;
  selectedId: string | null;
  filter: string;
  matchIds: Set<string>;
  expandIds: Set<string>;
  pathIds: string[];
  onSelect: (node: KtruTreeNode) => void;
}) {
  const [extra, setExtra] = createSignal<KtruTreeNode[]>([]);
  const [page, setPage] = createSignal<KtruChildrenPage | undefined>();
  const [loading, setLoading] = createSignal(true);
  const [loadingMore, setLoadingMore] = createSignal(false);
  const revealId = () => nextPathId(props.pathIds, props.parentId);
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
    if (loadingMore()) return;
    setLoadingMore(true);
    const more = await getKtruChildren(props.parentId, nodes().length, props.filter);
    setExtra((current) => [...current, ...more.nodes]);
    setLoadingMore(false);
  };
  createEffect(on(() => [revealId(), nodes(), page()?.total, loading(), loadingMore()] as const, ([id]) => {
    if (!id || loading() || loadingMore()) return;
    const total = page()?.total ?? 0;
    if (!total || nodes().some((node) => node.id === id) || nodes().length >= total) return;
    void loadMore();
  }));
  return <Show when={page()} fallback={<div class="ktru-loading">Загружаем…</div>}>{data =>
    <div classList={{ "ktru-tree-pending": loading() }}>
      <Show when={nodes().length} fallback={<div class="ktru-empty">{props.filter ? "Нет совпадений в этой ветке" : "Нет дочерних позиций"}</div>}>
        <ul class="ktru-tree-list">
          <For each={nodes()}>{node =>
            <TreeNode
              node={node}
              selectedId={props.selectedId}
              filter={props.filter}
              matchIds={props.matchIds}
              expandIds={props.expandIds}
              pathIds={props.pathIds}
              onSelect={props.onSelect}
            />
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
  expandIds: Set<string>;
  pathIds: string[];
  onSelect: (node: KtruTreeNode) => void;
}) {
  const filtering = () => props.filter.length >= 2;
  const [manualOpen, setManualOpen] = createSignal<boolean | null>(null);
  const open = () => {
    if (manualOpen() !== null) return manualOpen()!;
    return props.expandIds.has(props.node.id) || (filtering() && props.node.hasChildren);
  };
  const selected = () => props.selectedId === props.node.id;
  const matched = () => props.matchIds.has(props.node.id);
  createEffect(on(() => props.filter, () => setManualOpen(null)));
  createEffect(on(() => [selected(), props.node.id] as const, ([isSelected, id]) => {
    if (!isSelected || typeof document === "undefined") return;
    requestAnimationFrame(() => {
      document.querySelector(`[data-node-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
  }));
  const openDetails = () => {
    props.onSelect(props.node);
    if (props.node.hasChildren) setManualOpen(true);
  };
  return <li>
    <div
      class="ktru-node"
      classList={{ selected: selected(), match: matched() }}
      data-node-id={props.node.id}
      role="treeitem"
      aria-expanded={props.node.hasChildren ? open() : undefined}
      aria-selected={selected()}
    >
      <button type="button" class="twist" classList={{ open: open() }} disabled={!props.node.hasChildren} aria-label={open() ? "Свернуть" : "Развернуть"} onClick={() => setManualOpen(!open())}>
        <ChevronRight size={15} />
      </button>
      <button type="button" class="ktru-node-main" onClick={openDetails}>
        <span class="ktru-node-code">{nodeCodeLabel(props.node)}</span>
        <span class="ktru-node-name">{props.node.name}</span>
        <span class="ktru-node-meta">
          <Show when={props.node.kind === "ktru"}>{statusBadge(props.node.status)}</Show>
          <Show when={props.node.isTemplate}><span class="ktru-badge template">шаблон</span></Show>
        </span>
      </button>
      <Show when={props.node.childCount > 0}>
        <span class="ktru-count" aria-label={`${formatNumber(props.node.childCount)} дочерних`}>{formatNumber(props.node.childCount)}</span>
      </Show>
    </div>
    <Show when={open() && props.node.hasChildren}>
      <TreeBranch
        parentId={props.node.id}
        selectedId={props.selectedId}
        filter={props.filter}
        matchIds={props.matchIds}
        expandIds={props.expandIds}
        pathIds={props.pathIds}
        onSelect={props.onSelect}
      />
    </Show>
  </li>;
}

function CharacteristicTable(props: {
  items: {
    name: string;
    required?: boolean;
    badges?: string[];
    code?: string;
    values?: string;
    parts?: { text: string; okeiCode?: string; okeiName?: string }[];
  }[];
}) {
  return <div class="dir-characteristics">
    <h3>Характеристики <span>{formatNumber(props.items.length)}</span></h3>
    <Show when={props.items.length} fallback={<p class="ktru-empty">Характеристики не указаны.</p>}>
      <dl class="characteristics-table dir-characteristics-table">
        <For each={props.items}>{item => {
          const badges = [
            ...(item.required ? ["обязательная"] : []),
            ...(item.badges ?? []),
          ].filter((value, index, all) => all.indexOf(value) === index);
          const parts = item.parts?.length ? item.parts : [{ text: item.values || "—" }];
          return <div>
            <dt>
              <span class="dir-char-name">{item.name}</span>
              <Show when={badges.length}>
                <span class="dir-char-badges">
                  <For each={badges}>{badge => <span class="dir-char-required">{badge}</span>}</For>
                </span>
              </Show>
              <Show when={item.code}><span class="dir-char-code">{item.code}</span></Show>
            </dt>
            <dd>
              <ul class="dir-char-parts">
                <For each={parts}>{part =>
                  <li>
                    <span>{part.text}</span>
                    <Show when={part.okeiCode}>
                      <span class="dir-char-okei">ОКЕИ {part.okeiCode}{part.okeiName ? ` · ${part.okeiName}` : ""}</span>
                    </Show>
                  </li>
                }</For>
              </ul>
            </dd>
          </div>;
        }}</For>
      </dl>
    </Show>
  </div>;
}

function OkpdDetail(props: { code: string }) {
  const record = createAsync(() => getDirectoryRecord("okpd", props.code));
  return <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>{data =>
    <>
      <dl class="dir-fields"><For each={data().fields}>{field => <div><dt>{field.label}</dt><dd>{field.value}</dd></div>}</For></dl>
      <Show when={data().description}><p class="dir-description">{data().description}</p></Show>
      <Show when={data().lists?.length}>
        <For each={data().lists!}>{block =>
          <div class="dir-lists">
            <div class="dir-lists-head"><h3>{block.title}</h3></div>
            <ul><For each={block.items}>{item => <li><span class="dir-list-text">{item.text}</span></li>}</For></ul>
          </div>
        }</For>
      </Show>
    </>
  }</Show>;
}

function KtruRecordDetail(props: { code: string }) {
  const record = createAsync(() => getKtruRecord(props.code));
  return <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>{data => {
    const okpd = data().okpd2;
    return <>
      <dl class="dir-fields">
        <div><dt>Код КТРУ</dt><dd>{data().code}</dd></div>
        <div><dt>Название</dt><dd>{data().name}</dd></div>
        <div><dt>Статус</dt><dd>{data().status || "—"}{data().isTemplate ? " · шаблон" : ""}{data().noNewFeatures ? " · без новых характеристик" : ""}</dd></div>
        <div><dt>Версия</dt><dd>{data().version || "—"}{data().versionCode ? ` (${data().versionCode})` : ""}</dd></div>
        <Show when={okpd}><div><dt>ОКПД2</dt><dd>{okpd!.code}{okpd!.name ? ` — ${okpd!.name}` : ""}</dd></div></Show>
        <Show when={data().units.length}>
          <div><dt>Ед. измерения</dt><dd>{data().units.map(unit => `${unit.name} (${unit.code})`).join(", ")}</dd></div>
        </Show>
        <For each={data().dates}>{item => <div><dt>{item.label}</dt><dd>{item.value}</dd></div>}</For>
      </dl>
      <Show when={data().description}><p class="dir-description">{data().description}</p></Show>
      <Show when={okpd}>
        <div class="dir-links dir-links-top">
          <h3>Связанные справочники</h3>
          <div class="dir-links-list">
            <a href={directoryHref("okpd", okpd!.code)}>ОКПД2 {okpd!.code}{okpd!.name ? ` — ${okpd!.name}` : ""}</a>
          </div>
        </div>
      </Show>
      <CharacteristicTable items={data().characteristics} />
    </>;
  }}</Show>;
}

function DetailPanel(props: { node: KtruTreeNode | null }) {
  return <Show when={props.node} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию</strong>Параметры и характеристики появятся здесь.</div></div>}>
    <Show when={props.node!.kind === "okpd"}>
      <DataBoundary resetKey={`okpd:${props.node!.code}`} retry={() => revalidate(getDirectoryRecord.key)}>
        <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}>
          <OkpdDetail code={props.node!.code} />
        </Suspense>
      </DataBoundary>
    </Show>
    <Show when={props.node!.kind === "ktru"}>
      <DataBoundary resetKey={props.node!.code} retry={() => revalidate(getKtruRecord.key)}>
        <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}>
          <KtruRecordDetail code={props.node!.code} />
        </Suspense>
      </DataBoundary>
    </Show>
  </Show>;
}

export function KtruPickerBody(props: {
  selected: KtruTreeNode | null;
  onSelect: (node: KtruTreeNode) => void;
  pathIds?: string[];
}) {
  const [draft, setDraft] = createSignal("");
  const [filter, setFilter] = createSignal("");
  const [matchIds, setMatchIds] = createSignal(new Set<string>());
  const [matchCount, setMatchCount] = createSignal<number | null>(null);
  const [matchTruncated, setMatchTruncated] = createSignal(false);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const pathIds = () => props.pathIds ?? [];
  const expandIds = createMemo(() => {
    const path = pathIds();
    return new Set(path.length > 1 ? path.slice(0, -1) : path);
  });
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
        <TreeBranch
          parentId={null}
          selectedId={props.selected?.id ?? null}
          filter={filter()}
          matchIds={matchIds()}
          expandIds={expandIds()}
          pathIds={pathIds()}
          onSelect={props.onSelect}
        />
      </div>
    </section>
    <section class="ktru-panel ktru-picker-detail" aria-label="Параметры выбранной позиции">
      <div class="ktru-panel-head">
        <h2>Информация</h2>
        <Show when={props.selected} fallback={<span>Выберите позицию</span>}>
          <span>{nodeCodeLabel(props.selected!)}</span>
        </Show>
      </div>
      <div class="ktru-picker-detail-scroll">
        <DetailPanel node={props.selected} />
      </div>
    </section>
  </div>;
}

export function KtruPickerActions(props: {
  selected: KtruTreeNode | null;
  onConfirm: (node: KtruTreeNode) => void;
  onCancel: () => void;
}) {
  const canConfirm = () => props.selected?.kind === "ktru" || props.selected?.kind === "okpd";
  const label = () => {
    if (props.selected?.kind === "okpd") return "Выбрать ОКПД2";
    if (props.selected?.kind === "ktru") return "Выбрать ОКПД2 и КТРУ";
    return "Выбрать";
  };
  return <>
    <Button type="button" variant="outline" onClick={props.onCancel}>Отмена</Button>
    <Button type="button" disabled={!canConfirm()} onClick={() => props.selected && props.onConfirm(props.selected)}>
      <Check size={15} />{label()}
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
