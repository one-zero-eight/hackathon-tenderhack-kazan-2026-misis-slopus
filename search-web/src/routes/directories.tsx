import { Meta, Title } from "@solidjs/meta";
import { A, createAsync, revalidate, useSearchParams } from "@solidjs/router";
import ChevronRight from "lucide-solid/icons/chevron-right";
import Search from "lucide-solid/icons/search";
import Sparkles from "lucide-solid/icons/sparkles";
import { createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import {
  getClassifierStats, getDirectoryChildren, getDirectoryFilterMeta, getDirectoryProducts, getDirectoryRaw, getDirectoryRecord, getDirectoryRevealPath,
  getKtruChildren, getKtruFilterMeta, getKtruRaw, getKtruRecord, getKtruRevealPath, getKtruStats,
} from "~/lib/rpc";
import type { DirChildrenPage, DirectoryTab, DirTreeNode, KtruChildrenPage, KtruTreeNode } from "~/lib/types";
import { formatNumber, formatShortDate, ktruEisHref } from "~/lib/utils";
import "./directories.css";

function writeDirectoryUrl(tab: DirectoryTab, code: string) {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  url.searchParams.set("tab", tab);
  url.searchParams.set("code", code);
  window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}`);
}

const TABS: { id: DirectoryTab; label: string; paragraphs: string[] }[] = [
  {
    id: "ktru",
    label: "ОКПД2 и КТРУ",
    paragraphs: [
      "ОКПД 2 — общероссийский классификатор продукции по видам экономической деятельности. Его код определяет группу товара, но не задаёт конкретное наименование, единицу измерения и характеристики для закупки.",
      "КТРУ — каталог конкретных позиций в ЕИС, привязанных к кодам ОКПД 2. Позиция содержит наименование, единицу измерения и обязательные и необязательные характеристики. Выбирайте подходящую позицию, а не только группу классификатора.",
      "Шаблон КТРУ объединяет дочерние позиции, различающиеся ключевой характеристикой, например типом товара. Для карточки выбирайте дочернюю позицию с нужным значением, а не сам шаблон.",
    ],
  },
  {
    id: "kpgz",
    label: "КПГЗ и СПГЗ",
    paragraphs: [
      "КПГЗ — классификатор предметов госзаказа Москвы: иерархия категорий товаров, работ и услуг. Код задаёт группу, а не конкретный предмет закупки.",
      "СПГЗ — справочник конкретных позиций внутри категорий КПГЗ. Позиция содержит наименование, характеристики и допустимые значения для описания предмета закупки на Портале поставщиков и в ЕАСУЗ.",
      "Сначала выберите категорию КПГЗ, затем подходящую позицию СПГЗ. Если готовой позиции нет, оставьте категорию и заполните характеристики вручную.",
    ],
  },
  {
    id: "okei",
    label: "ОКЕИ",
    paragraphs: [
      "ОКЕИ — общероссийский классификатор единиц измерения. Содержит коды и обозначения единиц, например штуки и килограмма. В позициях КТРУ и СПГЗ единица измерения обычно уже задана справочником.",
    ],
  },
];

function param(value: string | string[] | undefined) {
  return (Array.isArray(value) ? value[0] : value)?.trim() || "";
}

function normalizeTab(value: string): DirectoryTab {
  if (value === "spgz") return "kpgz";
  if (value === "okpd") return "ktru";
  return TABS.some(item => item.id === value) ? value as DirectoryTab : "ktru";
}

/** Tree/filter source: unified КПГЗ/СПГЗ tab uses SPGZ mode (KPGZ tree + SPGZ leaves + joint filter). */
function treeTab(tab: DirectoryTab): DirectoryTab {
  return tab === "kpgz" ? "spgz" : tab;
}

function directoryHref(tab: DirectoryTab, code?: string) {
  const resolved = tab === "spgz" ? "kpgz" : tab === "okpd" ? "ktru" : tab;
  const query = new URLSearchParams({ tab: resolved });
  if (code) query.set("code", code);
  return `/directories?${query}`;
}

function nodeKindLabel(kind: string): string {
  switch (kind) {
    case "okpd": return "ОКПД2";
    case "ktru": return "КТРУ";
    case "kpgz": return "КПГЗ";
    case "spgz": return "СПГЗ";
    case "okei": return "ОКЕИ";
    case "okei-group": return "группа ОКЕИ";
    case "okei-section": return "раздел ОКЕИ";
    default: return kind.toUpperCase();
  }
}

function nodeCodeLabel(node: { kind: string; code: string }): string {
  const kind = nodeKindLabel(node.kind);
  const code = node.code?.trim();
  if (!code || code === "—" || code.startsWith("s:") || code.startsWith("g:")) return kind;
  return `${kind} ${code}`;
}

function ktruStatusLabel(status: string | null): string {
  switch (status) {
    case "ACTIVE": return "Действует";
    case "TERMINATED": return "Исключена";
    case "DRAFT": return "Черновик";
    default: return status || "—";
  }
}

function statusBadge(status: string | null) {
  if (!status) return null;
  const kind = status === "ACTIVE" ? "active" : status === "TERMINATED" ? "terminated" : "";
  return <span class={`ktru-badge ${kind}`}>{ktruStatusLabel(status)}</span>;
}

function ktruVariantLabel(node: DirTreeNode | KtruTreeNode): string | null {
  if (!("status" in node) || node.kind !== "ktru") return null;
  return node.variantLabel?.trim() || null;
}

function nextPathId(pathIds: string[], parentId: string | null) {
  if (!pathIds.length) return null;
  if (parentId == null) return pathIds[0] ?? null;
  const index = pathIds.indexOf(parentId);
  return index >= 0 ? pathIds[index + 1] ?? null : null;
}

function TreeBranch(props: {
  tab: DirectoryTab;
  parentId: string | null;
  selectedId: string | null;
  filter: string;
  matchIds: Set<string>;
  expandIds: Set<string>;
  pathIds: string[];
  onSelect: (node: DirTreeNode | KtruTreeNode) => void;
}) {
  const [extra, setExtra] = createSignal<(DirTreeNode | KtruTreeNode)[]>([]);
  const [page, setPage] = createSignal<DirChildrenPage | KtruChildrenPage | undefined>();
  const [loading, setLoading] = createSignal(true);
  const [loadingMore, setLoadingMore] = createSignal(false);
  const [error, setError] = createSignal("");
  let generation = 0;
  onCleanup(() => { generation++; });
  const revealId = () => nextPathId(props.pathIds, props.parentId);
  createEffect(on(() => [props.tab, props.parentId, props.filter] as const, ([tab, parentId, filter]) => {
    let alive = true;
    generation++;
    setExtra([]);
    setPage(undefined);
    setError("");
    setLoadingMore(false);
    setLoading(true);
    const request = tab === "ktru" ? getKtruChildren(parentId, 0, filter) : getDirectoryChildren(tab, parentId, 0, filter);
    void request.then(data => {
      if (!alive) return;
      setPage(data);
      setLoading(false);
    }).catch((cause: unknown) => {
      if (alive) { setError(cause instanceof Error ? cause.message : "Не удалось загрузить дерево."); setLoading(false); }
    });
    onCleanup(() => { alive = false; });
  }));
  const nodes = createMemo(() => [...(page()?.nodes ?? []), ...extra()]);
  const loadMore = async () => {
    if (loading() || loadingMore()) return;
    const version = generation;
    setLoadingMore(true);
    setError("");
    try {
      const more = props.tab === "ktru"
        ? await getKtruChildren(props.parentId, nodes().length, props.filter)
        : await getDirectoryChildren(props.tab, props.parentId, nodes().length, props.filter);
      if (version !== generation) return;
      setExtra(current => [...current, ...more.nodes]);
    } catch (cause) {
      if (version === generation) setError(cause instanceof Error ? cause.message : "Не удалось загрузить следующие позиции.");
    } finally {
      if (version === generation) setLoadingMore(false);
    }
  };
  createEffect(on(() => [revealId(), nodes(), page()?.total, loading(), loadingMore()] as const, ([id]) => {
    if (!id || loading() || loadingMore() || error()) return;
    const total = page()?.total ?? 0;
    if (!total || nodes().some(node => node.id === id) || nodes().length >= total) return;
    void loadMore();
  }));
  return <Show when={page()} fallback={<div class="ktru-loading" role={error() ? "alert" : undefined}>{error() || "Загружаем…"}</div>}>{data =>
    <div classList={{ "ktru-tree-pending": loading() }}>
      <Show when={error()}><p class="dir-search-error" role="alert">{error()}</p></Show>
      <Show when={nodes().length} fallback={<div class="ktru-empty">{props.filter ? "Нет совпадений в этой ветке" : "Нет дочерних позиций"}</div>}>
        <ul class="ktru-tree-list">
          <For each={nodes()}>{node =>
            <TreeNode
              tab={props.tab}
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
  tab: DirectoryTab;
  node: DirTreeNode | KtruTreeNode;
  selectedId: string | null;
  filter: string;
  matchIds: Set<string>;
  expandIds: Set<string>;
  pathIds: string[];
  onSelect: (node: DirTreeNode | KtruTreeNode) => void;
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
    const frame = requestAnimationFrame(() => {
      document.querySelector(`[data-node-id="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    onCleanup(() => cancelAnimationFrame(frame));
  }));
  const openDetails = () => {
    props.onSelect(props.node);
    if (props.node.hasChildren) setManualOpen(true);
  };
  const isKtru = () => "status" in props.node;
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
        <span class="ktru-node-name">
          {props.node.name}
          <Show when={ktruVariantLabel(props.node)}>{label =>
            <span class="ktru-node-variant"> · {label()}</span>
          }</Show>
        </span>
        <span class="ktru-node-meta">
          <Show when={isKtru() && (props.node as KtruTreeNode).kind === "ktru" && (props.node as KtruTreeNode).status !== "ACTIVE"}>{statusBadge((props.node as KtruTreeNode).status)}</Show>
          <Show when={isKtru() && (props.node as KtruTreeNode).isTemplate}><span class="ktru-badge template">шаблон</span></Show>
        </span>
      </button>
      <Show when={props.node.childCount > 0}>
        <span class="ktru-count" aria-label={`${formatNumber(props.node.childCount)} дочерних`}>{formatNumber(props.node.childCount)}</span>
      </Show>
    </div>
    <Show when={open() && props.node.hasChildren}>
      <TreeBranch
        tab={props.tab}
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

function LazyRawJson(props: { load: () => Promise<Record<string, unknown> | null>; label: string }) {
  const [open, setOpen] = createSignal(false);
  const raw = createAsync(() => open() ? props.load() : Promise.resolve(null));
  return <details class="dir-raw" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{props.label}</summary>
    <Show when={open()}>
      <Suspense fallback={<div class="ktru-loading">Загружаем JSON…</div>}>
        <Show when={raw()} fallback={<div class="ktru-empty">Нет данных</div>}>
          {data => <pre class="ktru-json" aria-label={props.label}>{JSON.stringify(data(), null, 2)}</pre>}
        </Show>
      </Suspense>
    </Show>
  </details>;
}

function DirForwardLinks(props: { links: { tab: DirectoryTab; code: string; label: string }[] }) {
  return <Show when={props.links.length}>
    <div class="dir-links dir-links-top">
      <h3>Связанные справочники</h3>
      <div class="dir-links-list">
        <For each={props.links}>{link => <A href={directoryHref(link.tab, link.code)}>{link.label}</A>}</For>
      </div>
    </div>
  </Show>;
}

function DirBacklinks(props: {
  backlinks: { tab: DirectoryTab; code: string; label: string }[];
  backlinksMore: number;
}) {
  return <Show when={props.backlinks.length}>
    <div class="dir-links dir-links-bottom">
      <h3>Обратные ссылки</h3>
      <div class="dir-links-list">
        <For each={props.backlinks}>{link => <A href={directoryHref(link.tab, link.code)}>{link.label}</A>}</For>
      </div>
      <Show when={props.backlinksMore > 0}>
        <p class="dir-products-note">и ещё {formatNumber(props.backlinksMore)}</p>
      </Show>
    </div>
  </Show>;
}

function CharacteristicTable(props: {
  items: {
    name: string;
    value?: string;
    values?: string;
    required?: boolean;
    options?: string[];
    parts?: { text: string; okeiCode?: string; okeiName?: string }[];
  }[];
}) {
  return <div class="dir-characteristics">
    <h3>Характеристики <span>{formatNumber(props.items.length)}</span></h3>
    <Show when={props.items.length} fallback={<p class="ktru-empty">Характеристики не указаны.</p>}>
      <table class="dir-characteristics-table" aria-label="Характеристики и допустимые значения">
        <thead><tr><th scope="col">Характеристика</th><th scope="col">Варианты значений</th></tr></thead>
        <tbody>
          <For each={props.items}>{item => {
            const parts = item.parts?.length
              ? item.parts
              : (item.options?.length ? item.options.map(text => ({ text })) : [{ text: item.values || item.value || "—" }]);
            return <tr>
              <th scope="row">
                {item.name}
                <Show when={item.required}><span class="dir-char-required" aria-label="Обязательная характеристика"> *</span></Show>
              </th>
              <td>
                <ul class="dir-char-options">
                  <For each={parts}>{part => <li><span class="dir-char-option">{part.text}</span></li>}</For>
                </ul>
              </td>
            </tr>;
          }}</For>
        </tbody>
      </table>
    </Show>
  </div>;
}

function ListBlocks(props: { lists?: { title: string; collapsible?: boolean; inline?: boolean; items: { text: string; href?: string; meta?: string }[] }[] }) {
  return <Show when={props.lists?.length}>
    <For each={props.lists}>{block =>
      <Show when={block.inline} fallback={<ListBlock title={block.title} items={block.items} collapsible={block.collapsible} />}>
        <p class="dir-search-aliases">{block.title}: {block.items.map(item => item.text).join(", ")}</p>
      </Show>
    }</For>
  </Show>;
}

function ListBlock(props: {
  title: string;
  collapsible?: boolean;
  items: { text: string; href?: string; meta?: string }[];
}) {
  const [open, setOpen] = createSignal(false);
  const folded = () => !!props.collapsible && props.items.length > 0 && !open();
  const visibleItems = () => (folded() && props.items.length > 1 ? props.items.slice(0, 1) : props.items);
  return <div class="dir-lists" classList={{ "dir-lists-folded": folded() }}>
    <div class="dir-lists-head">
      <h3>{props.title}</h3>
      <Show when={props.collapsible && props.items.length}>
        <button
          type="button"
          class="dir-lists-toggle"
          classList={{ open: open() }}
          aria-expanded={open()}
          aria-label={open() ? "Свернуть" : "Развернуть"}
          onClick={() => setOpen(value => !value)}
        >
          <ChevronRight size={14} />
        </button>
      </Show>
    </div>
    <ul>
      <For each={visibleItems()}>{item =>
        <li>
          <Show when={item.href} fallback={<span class="dir-list-text">{item.text}</span>}>
            <a class="dir-list-text" href={item.href} target="_blank" rel="noreferrer">{item.text}</a>
          </Show>
          <Show when={item.meta && !folded()}><span class="dir-list-meta">{item.meta}</span></Show>
        </li>
      }</For>
    </ul>
    <Show when={folded() && props.items.length > 1}>
      <button
        type="button"
        class="dir-products-note dir-lists-more"
        aria-expanded={open()}
        onClick={() => setOpen(true)}
      >ещё {formatNumber(props.items.length - 1)}</button>
    </Show>
  </div>;
}

function KtruDetail(props: { code: string }) {
  const record = createAsync(() => getKtruRecord(props.code));
  return <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>{data => {
    const okpd = data().okpd2;
    const updateDate = data().dates.find(item => item.label === "Дата обновления");
    return <>
      <dl class="dir-fields">
        <div><dt>Код КТРУ</dt><dd>{data().code}</dd></div>
        <div><dt>Госкаталог ЕИС</dt><dd><a href={ktruEisHref(data().code)} target="_blank" rel="noopener noreferrer">Страница в госкаталоге ЕИС</a></dd></div>
        <div><dt>Название</dt><dd>{data().name}</dd></div>
        <Show when={data().officialName && data().officialName !== data().name}>
          <div><dt>Официальное название</dt><dd>{data().officialName}</dd></div>
        </Show>
        <div><dt>Статус</dt><dd>{ktruStatusLabel(data().status)}{data().isTemplate ? " · шаблон" : ""}{data().noNewFeatures ? " · без новых характеристик" : ""}</dd></div>
        <div><dt>Версия</dt><dd>{data().version || "—"}<Show when={updateDate}>{date => <> · обновлено <time dateTime={date().value}>{formatShortDate(date().value)}</time></>}</Show></dd></div>
        <Show when={okpd}><div><dt>ОКПД2</dt><dd><A href={directoryHref("okpd", okpd!.code)}>{okpd!.code}{okpd!.name ? ` — ${okpd!.name}` : ""}</A></dd></div></Show>
        <Show when={data().units.length}>
          <div><dt>Ед. измерения</dt><dd><For each={data().units}>{(unit, index) => <>{index() > 0 ? ", " : ""}<A href={directoryHref("okei", unit.code)}>{unit.name} ({unit.code})</A></>}</For></dd></div>
        </Show>
        <Show when={data().rubricators.length}>
          <div><dt>Рубрикатор</dt><dd>{data().rubricators.map(item => `${item.name || item.code}${item.code ? ` (${item.code})` : ""}`).join(", ")}</dd></div>
        </Show>
        <Show when={data().cancellation}>
          <div><dt>Отмена</dt><dd>{[data().cancellation!.date, data().cancellation!.reason].filter(Boolean).join(" — ") || "—"}</dd></div>
        </Show>
        <Show when={data().printFormUrl}>
          <div><dt>Печатная форма</dt><dd><a href={data().printFormUrl!} target="_blank" rel="noreferrer">Открыть на zakupki.gov.ru</a></dd></div>
        </Show>
      </dl>
      <Show when={data().description}><p class="dir-description">{data().description}</p></Show>
      <ListBlocks lists={data().standards.length ? [{ title: "Стандарты и нормы", items: data().standards.map(text => ({ text })) }] : []} />
      <CharacteristicTable items={data().characteristics} />
      <Show when={okpd}>
        <Suspense fallback={<div class="ktru-loading">Товары…</div>}>
          <KtruProducts code={props.code} okpdCode={okpd!.code} />
        </Suspense>
      </Show>
      <DirBacklinks backlinks={data().backlinks} backlinksMore={data().backlinksMore} />
      <LazyRawJson label="Исходный JSON" load={() => getKtruRaw(props.code)} />
    </>;
  }}</Show>;
}

function KtruProducts(props: { code: string; okpdCode: string }) {
  const products = createAsync(() => getDirectoryProducts("okpd", props.okpdCode));
  return <Show when={products()}>{list =>
    <div class="dir-products">
      <h3>Товары каталога</h3>
      <p class="dir-products-note">По ОКПД2 позиции КТРУ · {formatNumber(list().total)}</p>
      <Show when={list().items.length} fallback={<p class="ktru-empty">В загруженном каталоге нет товаров с ОКПД2 {props.okpdCode}.</p>}>
        <For each={list().items}>{item =>
          <A class="dir-product-link" href={`/products/${item.id}?from=${encodeURIComponent(directoryHref("ktru", props.code))}`}>{item.name}<span>СТЕ {item.id}{item.kpgzCode ? ` · КПГЗ ${item.kpgzCode}` : ""}</span></A>
        }</For>
      </Show>
    </div>
  }</Show>;
}

function DirectoryDetail(props: { tab: DirectoryTab; code: string }) {
  const record = createAsync(() => getDirectoryRecord(props.tab, props.code));
  const products = createAsync(() => getDirectoryProducts(props.tab, props.code));
  return <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>{data => <>
    <dl class="dir-fields"><For each={data().fields}>{field => <div><dt>{field.label}</dt><dd>{field.value}</dd></div>}</For></dl>
    <Show when={data().description}><p class="dir-description">{data().description}</p></Show>
    <DirForwardLinks links={data().links} />
    <ListBlocks lists={data().lists} />
    <Show when={data().characteristics.length}>
      <CharacteristicTable items={data().characteristics} />
    </Show>
    <Suspense fallback={<div class="ktru-loading">Товары…</div>}>
      <Show when={products()}>{list =>
        <div class="dir-products">
          <h3>Товары каталога</h3>
          <p class="dir-products-note">
            {props.tab === "spgz" ? "Товары с тем же ОКПД2" : props.tab === "okei" ? "Товары с этой единицей измерения" : "Прямое соответствие коду"} · {formatNumber(list().total)}
          </p>
          <Show when={list().items.length} fallback={<p class="ktru-empty">В загруженном каталоге СТЕ совпадений нет.</p>}>
            <For each={list().items}>{item =>
              <A class="dir-product-link" href={`/products/${item.id}?from=${encodeURIComponent(directoryHref(props.tab, props.code))}`}>{item.name}<span>СТЕ {item.id}{item.okpd2 ? ` · ОКПД2 ${item.okpd2}` : ""}{item.kpgzCode ? ` · КПГЗ ${item.kpgzCode}` : ""}</span></A>
            }</For>
          </Show>
        </div>
      }</Show>
    </Suspense>
    <DirBacklinks backlinks={data().backlinks} backlinksMore={data().backlinksMore} />
    <LazyRawJson label="Исходный JSON" load={() => getDirectoryRaw(props.tab, props.code)} />
  </>}</Show>;
}

function DetailPanel(props: { tab: DirectoryTab; selected: DirTreeNode | KtruTreeNode | null }) {
  const showKtru = () => props.tab === "ktru" && props.selected && (!("kind" in props.selected) || props.selected.kind === "ktru");
  const showDir = () => {
    if (!props.selected) return false;
    if (props.tab === "ktru") return props.selected.kind === "okpd";
    if (props.tab === "kpgz") return props.selected.kind === "spgz" || props.selected.kind === "kpgz";
    if (props.tab === "okei") return props.selected.kind === "okei" || props.selected.kind === "okei-group" || props.selected.kind === "okei-section";
    return props.selected.kind === props.tab;
  };
  const detailTab = (): DirectoryTab => {
    if (!props.selected) return props.tab;
    if (props.selected.kind === "spgz") return "spgz";
    if (props.selected.kind === "kpgz") return "kpgz";
    if (props.selected.kind === "okpd") return "okpd";
    if (props.selected.kind === "okei" || props.selected.kind === "okei-group" || props.selected.kind === "okei-section") return "okei";
    return props.tab;
  };
  return <section class="ktru-panel" aria-label="Карточка позиции справочника">
    <div class="ktru-panel-head">
      <h2>Карточка</h2>
      <Show when={props.selected} fallback={<span>Выберите позицию</span>}><span>{nodeCodeLabel(props.selected!)}</span></Show>
    </div>
    <div class="dir-detail-scroll">
      <Show when={props.selected} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию</strong>Параметры, связи и товары каталога появятся здесь.</div></div>}>
        <Show when={showKtru()}>
          <DataBoundary resetKey={props.selected!.code} retry={() => revalidate(getKtruRecord.key)}>
            <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}><KtruDetail code={props.selected!.code} /></Suspense>
          </DataBoundary>
        </Show>
        <Show when={showDir()}>
          <DataBoundary resetKey={`${detailTab()}:${props.selected!.code}`} retry={() => revalidate(getDirectoryRecord.key)}>
            <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}><DirectoryDetail tab={detailTab()} code={props.selected!.code} /></Suspense>
          </DataBoundary>
        </Show>
      </Show>
    </div>
  </section>;
}

export default function DirectoriesPage() {
  const [searchParams] = useSearchParams();
  const tab = (): DirectoryTab => normalizeTab(param(searchParams.tab));
  const sourceTab = () => treeTab(tab());
  const codeParam = () => param(searchParams.code);
  const [selected, setSelected] = createSignal<DirTreeNode | KtruTreeNode | null>(null);
  const [pathIds, setPathIds] = createSignal<string[]>([]);
  const [draft, setDraft] = createSignal("");
  const [filter, setFilter] = createSignal("");
  const [matchIds, setMatchIds] = createSignal(new Set<string>());
  const [matchCount, setMatchCount] = createSignal<number | null>(null);
  const [matchTruncated, setMatchTruncated] = createSignal(false);
  const [searchError, setSearchError] = createSignal("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  const ktruStats = createAsync(() => getKtruStats());
  const classifierStats = createAsync(() => getClassifierStats());
  const expandIds = createMemo(() => {
    const path = pathIds();
    return new Set(path.length > 1 ? path.slice(0, -1) : path);
  });

  createEffect(on(() => [tab(), codeParam()] as const, ([currentTab, code]) => {
    if (!code) {
      setPathIds([]);
      return;
    }
    let alive = true;
    const request = currentTab === "ktru" ? getKtruRevealPath(code) : getDirectoryRevealPath(currentTab, code);
    void request.then(result => {
      if (!alive) return;
      if (result.node) setSelected(result.node);
      setPathIds(result.pathIds);
    }).catch((cause: unknown) => {
      if (alive) setSearchError(cause instanceof Error ? cause.message : "Не удалось открыть позицию.");
    });
    onCleanup(() => { alive = false; });
  }));

  createEffect(on(() => tab(), () => {
    clearTimeout(timer);
    setSearchError("");
    setDraft("");
    setFilter("");
    setMatchIds(new Set<string>());
    setMatchCount(null);
    setMatchTruncated(false);
    setSelected(null);
    setPathIds([]);
  }, { defer: true }));

  createEffect(on(() => [sourceTab(), filter()] as const, ([currentTab, value]) => {
    let alive = true;
    onCleanup(() => { alive = false; });
    setSearchError("");
    if (value.length < 2) {
      setMatchIds(new Set<string>());
      setMatchCount(null);
      setMatchTruncated(false);
      return;
    }
    const request = currentTab === "ktru" ? getKtruFilterMeta(value) : getDirectoryFilterMeta(currentTab, value);
    void request.then(meta => {
      if (!alive) return;
      setMatchIds(new Set(meta.matchIds));
      setMatchCount(meta.matchCount);
      setMatchTruncated(meta.truncated);
    }).catch((cause: unknown) => {
      if (alive) setSearchError(cause instanceof Error ? cause.message : "Не удалось найти позиции в дереве.");
    });
  }));

  const selectNode = (node: DirTreeNode | KtruTreeNode) => {
    setSelected(node);
    writeDirectoryUrl(tab(), node.code);
  };

  const onFilterInput = (value: string) => {
    setDraft(value);
    clearTimeout(timer);
    timer = setTimeout(() => setFilter(value.trim()), 280);
  };
  onCleanup(() => clearTimeout(timer));

  const current = () => TABS.find(item => item.id === tab())!;

  return <main id="main-content" class="page-width ktru-main">
    <Title>Справочники — каталог</Title>
    <Meta name="description" content="Справочники ОКПД2, КТРУ, КПГЗ, СПГЗ и ОКЕИ со связями и товарами каталога." />
    <section class="ktru-hero">
      <h1>Изучите<br /><em>справочники</em><span class="hero-asterisk" aria-hidden="true"><Sparkles strokeWidth={1.1} /></span></h1>
      <div class="ktru-hero-description">
        <For each={current().paragraphs}>{paragraph => <p>{paragraph}</p>}</For>
      </div>
      <div class="ktru-stats">
        <Suspense fallback={<span>Загружаем счётчики…</span>}>
          <Show when={classifierStats()}>
            <span><strong>{formatNumber(classifierStats()!.okpd)}</strong> ОКПД2</span>
          </Show>
          <Show when={ktruStats()}><span><strong>{formatNumber(ktruStats()!.total)}</strong> КТРУ</span></Show>
          <Show when={classifierStats()}>
            <span><strong>{formatNumber(classifierStats()!.kpgz)}</strong> КПГЗ</span>
            <span><strong>{formatNumber(classifierStats()!.spgz)}</strong> СПГЗ</span>
            <span><strong>{formatNumber(classifierStats()!.okei)}</strong> ОКЕИ</span>
          </Show>
        </Suspense>
      </div>
    </section>
    <nav class="dir-tabs" aria-label="Справочники">
      <For each={TABS}>{item =>
        <A href={directoryHref(item.id, tab() === item.id ? (selected()?.code || codeParam() || undefined) : undefined)} classList={{ active: tab() === item.id }}>{item.label}</A>
      }</For>
    </nav>
    <div class="ktru-layout">
      <section class="ktru-panel" aria-label={`Дерево ${current().label}`}>
        <div class="ktru-panel-head">
          <h2>{current().label}</h2>
          <Show when={matchCount() !== null} fallback={<span>фильтр по коду или названию</span>}>
            <span>{formatNumber(matchCount()!)} совп.{matchTruncated() ? "+" : ""}</span>
          </Show>
        </div>
        <label class="ktru-filter"><Search size={15} /><input type="search" value={draft()} placeholder="Код, название или синоним…" autocomplete="off" onInput={event => onFilterInput(event.currentTarget.value)} /></label>
        <Show when={searchError()}><p class="dir-search-error" role="alert">{searchError()}</p></Show>
        <div class="ktru-tree" role="tree">
          <TreeBranch
            tab={sourceTab()}
            parentId={null}
            selectedId={selected()?.id ?? null}
            filter={filter()}
            matchIds={matchIds()}
            expandIds={expandIds()}
            pathIds={pathIds()}
            onSelect={selectNode}
          />
        </div>
      </section>
      <DetailPanel tab={tab()} selected={selected()} />
    </div>
  </main>;
}
