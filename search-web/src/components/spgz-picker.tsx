import { createAsync, revalidate } from "@solidjs/router";
import Check from "lucide-solid/icons/check";
import ChevronRight from "lucide-solid/icons/chevron-right";
import Search from "lucide-solid/icons/search";
import { batch, createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import { getDirectoryChildren, getDirectoryFilterMeta, getDirectoryRecord, getDirectoryRevealPath, searchClassifiers } from "~/lib/rpc";
import type { ClassifierPairHit, DirChildrenPage, DirTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import { Button } from "~/ui-kit";
import "~/routes/directories.css";
import "~/routes/ktru.css";

const TAB = "spgz" as const;

function nodeCodeLabel(node: { kind: string; code: string }): string {
  const kind = node.kind === "spgz" ? "СПГЗ" : node.kind === "kpgz" ? "КПГЗ" : node.kind.toUpperCase();
  const code = node.code?.trim();
  if (!code || code === "—" || code.startsWith("s:") || code.startsWith("g:")) return kind;
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
  onSelect: (node: DirTreeNode) => void;
}) {
  const [extra, setExtra] = createSignal<DirTreeNode[]>([]);
  const [page, setPage] = createSignal<DirChildrenPage | undefined>();
  const [loading, setLoading] = createSignal(true);
  const [loadingMore, setLoadingMore] = createSignal(false);
  const [error, setError] = createSignal("");
  let generation = 0;
  onCleanup(() => { generation++; });
  const revealId = () => nextPathId(props.pathIds, props.parentId);
  createEffect(on(() => [props.parentId, props.filter] as const, ([parentId, filter]) => {
    let alive = true;
    generation++;
    setExtra([]);
    setPage(undefined);
    setError("");
    setLoadingMore(false);
    setLoading(true);
    void getDirectoryChildren(TAB, parentId, 0, filter).then((data) => {
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
      const more = await getDirectoryChildren(TAB, props.parentId, nodes().length, props.filter);
      if (version !== generation) return;
      setExtra((current) => [...current, ...more.nodes]);
    } catch (cause) {
      if (version === generation) setError(cause instanceof Error ? cause.message : "Не удалось загрузить следующие позиции.");
    } finally {
      if (version === generation) setLoadingMore(false);
    }
  };
  createEffect(on(() => [revealId(), nodes(), page()?.total, loading(), loadingMore()] as const, ([id]) => {
    if (!id || loading() || loadingMore() || error()) return;
    const total = page()?.total ?? 0;
    if (!total || nodes().some((node) => node.id === id) || nodes().length >= total) return;
    void loadMore();
  }));
  return <Show when={page()} fallback={<div class="ktru-loading" role={error() ? "alert" : undefined}>{error() || "Загружаем…"}</div>}>{data =>
    <div classList={{ "ktru-tree-pending": loading() }}>
      <Show when={error()}><p class="dir-search-error" role="alert">{error()}</p></Show>
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
  node: DirTreeNode;
  selectedId: string | null;
  filter: string;
  matchIds: Set<string>;
  expandIds: Set<string>;
  pathIds: string[];
  onSelect: (node: DirTreeNode) => void;
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
      <table class="dir-characteristics-table" aria-label="Характеристики и допустимые значения">
        <thead><tr><th scope="col">Характеристика</th><th scope="col">Варианты значений</th></tr></thead>
        <tbody>
          <For each={props.items}>{item => {
            const parts = item.parts?.length ? item.parts : [{ text: item.values || "—" }];
            return <tr>
              <th scope="row">{item.name}</th>
              <td>
                <ul class="dir-char-options">
                  <For each={parts}>{part => <li>{part.text}</li>}</For>
                </ul>
              </td>
            </tr>;
          }}</For>
        </tbody>
      </table>
    </Show>
  </div>;
}

function DirectoryRecordDetail(props: { tab: "kpgz" | "spgz"; code: string }) {
  const record = createAsync(() => getDirectoryRecord(props.tab, props.code));
  return <Show when={record()} fallback={<div class="ktru-empty">Запись не найдена</div>}>{data =>
    <>
      <dl class="dir-fields"><For each={data().fields}>{field => <div><dt>{field.label}</dt><dd>{field.value}</dd></div>}</For></dl>
      <Show when={data().description}><p class="dir-description">{data().description}</p></Show>
      <Show when={data().links.length}>
        <div class="dir-links dir-links-top">
          <h3>Связанные справочники</h3>
          <div class="dir-links-list"><For each={data().links}>{link => <span>{link.label}</span>}</For></div>
        </div>
      </Show>
      <CharacteristicTable items={data().characteristics} />
    </>
  }</Show>;
}

function DetailPanel(props: { node: DirTreeNode | null }) {
  return <Show when={props.node} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию</strong>Параметры и характеристики появятся здесь.</div></div>}>
    <DataBoundary resetKey={`${props.node!.kind}:${props.node!.code}`} retry={() => revalidate(getDirectoryRecord.key)}>
      <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}>
        <DirectoryRecordDetail tab={props.node!.kind === "spgz" ? "spgz" : "kpgz"} code={props.node!.code} />
      </Suspense>
    </DataBoundary>
  </Show>;
}

export function SpgzPickerBody(props: {
  selected: DirTreeNode | null;
  onSelect: (node: DirTreeNode) => void;
  pathIds?: string[];
}) {
  const [draft, setDraft] = createSignal("");
  const [filter, setFilter] = createSignal("");
  const [matchIds, setMatchIds] = createSignal(new Set<string>());
  const [matchCount, setMatchCount] = createSignal<number | null>(null);
  const [matchTruncated, setMatchTruncated] = createSignal(false);
  const [ranked, setRanked] = createSignal<ClassifierPairHit[]>([]);
  const [searchError, setSearchError] = createSignal("");
  let searchVersion = 0;
  onCleanup(() => { searchVersion++; });
  const [revealPath, setRevealPath] = createSignal<string[]>(props.pathIds ?? []);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const pathIds = () => revealPath().length ? revealPath() : (props.pathIds ?? []);
  const expandIds = createMemo(() => {
    const path = pathIds();
    return new Set(path.length > 1 ? path.slice(0, -1) : path);
  });
  createEffect(on(() => filter(), (value) => {
    let alive = true;
    searchVersion++;
    onCleanup(() => { alive = false; });
    setSearchError("");
    setRanked([]);
    if (value.length < 2) {
      setMatchIds(new Set<string>());
      setMatchCount(null);
      setMatchTruncated(false);
      setRanked([]);
      return;
    }
    void getDirectoryFilterMeta(TAB, value).then((meta) => {
      if (!alive) return;
      setMatchIds(new Set(meta.matchIds));
      setMatchCount(meta.matchCount);
      setMatchTruncated(meta.truncated);
    }).catch((cause: unknown) => {
      if (alive) setSearchError(cause instanceof Error ? cause.message : "Не удалось найти позиции в дереве.");
    });
    void searchClassifiers(value, 8).then((result) => {
      if (alive) setRanked(result.kpgzSpgz);
    }).catch((cause: unknown) => {
      if (alive) setSearchError(cause instanceof Error ? cause.message : "Не удалось выполнить поиск по справочникам.");
    });
  }));
  const pickRanked = async (hit: NonNullable<ClassifierPairHit["spgz"] | ClassifierPairHit["kpgz"]>) => {
    const version = ++searchVersion;
    try {
      const revealed = await getDirectoryRevealPath(hit.kind === "spgz" ? "spgz" : "kpgz", hit.code);
      if (version !== searchVersion) return;
      if (!revealed.node) throw new Error("Позиция не найдена в дереве.");
      clearTimeout(timer);
      batch(() => {
        setDraft(""); setFilter(""); setSearchError("");
        setRevealPath(revealed.pathIds);
        props.onSelect(revealed.node!);
      });
    } catch (cause) {
      if (version === searchVersion) setSearchError(cause instanceof Error ? cause.message : "Не удалось открыть позицию.");
    }
  };
  const onFilterInput = (value: string) => {
    searchVersion++;
    setDraft(value);
    clearTimeout(timer);
    timer = setTimeout(() => setFilter(value.trim()), 280);
  };
  onCleanup(() => clearTimeout(timer));
  return <div class="ktru-picker-body">
    <section class="ktru-panel" aria-label="Дерево КПГЗ и СПГЗ">
      <div class="ktru-panel-head">
        <h2>Дерево</h2>
        <Show when={matchCount() !== null} fallback={<span>КПГЗ → СПГЗ</span>}>
          <span>{formatNumber(matchCount()!)} совп.{matchTruncated() ? "+" : ""}</span>
        </Show>
      </div>
      <label class="ktru-filter"><Search size={15} /><input type="search" value={draft()} placeholder="Код, название или синоним…" autocomplete="off" onInput={(event) => onFilterInput(event.currentTarget.value)} /></label>
      <Show when={searchError()}><p class="dir-search-error" role="alert">{searchError()}</p></Show>
      <Show when={ranked().length}>
        <div class="create-choice-list ktru-ranked-hits">
          <For each={ranked().slice(0, 5)}>{pair => {
            const hit = pair.spgz || pair.kpgz;
            if (!hit) return null;
            return <button
              type="button"
              class="create-choice-option"
              onClick={() => void pickRanked(hit)}
            >
              <span class="create-vote-code">{hit.code}</span>
              <span>{hit.name}</span>
            </button>;
          }}</For>
        </div>
      </Show>
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

export function SpgzPickerActions(props: {
  selected: DirTreeNode | null;
  onConfirm: (node: DirTreeNode) => void;
  onCancel: () => void;
}) {
  const canConfirm = () => props.selected?.kind === "spgz" || props.selected?.kind === "kpgz";
  const label = () => {
    if (props.selected?.kind === "kpgz") return "Выбрать КПГЗ";
    if (props.selected?.kind === "spgz") return "Выбрать КПГЗ и СПГЗ";
    return "Выбрать";
  };
  return <>
    <Button type="button" variant="outline" onClick={props.onCancel}>Отмена</Button>
    <Button type="button" disabled={!canConfirm()} onClick={() => props.selected && props.onConfirm(props.selected)}>
      <Check size={15} />{label()}
    </Button>
  </>;
}
