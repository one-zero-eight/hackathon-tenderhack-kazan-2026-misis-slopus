import { revalidate } from "@solidjs/router";
import Check from "lucide-solid/icons/check";
import Search from "lucide-solid/icons/search";
import { batch, createEffect, createMemo, createSignal, For, on, onCleanup, Show, Suspense } from "solid-js";
import { DataBoundary } from "~/components/catalog";
import { getDirectoryFilterMeta, getDirectoryRecord, getDirectoryRevealPath, searchClassifiers } from "~/lib/rpc";
import type { ClassifierPairHit, DirTreeNode } from "~/lib/types";
import { formatNumber } from "~/lib/utils";
import { DirectoryDetail, DirectoryTree, nodeCodeLabel } from "~/routes/directories";
import { Button } from "~/ui-kit";
import "~/routes/directories.css";
import "~/routes/ktru.css";

function DetailPanel(props: { node: DirTreeNode | null }) {
  return <Show when={props.node} fallback={<div class="ktru-json-placeholder"><div><strong>Выберите позицию</strong>Параметры, связи и товары каталога появятся здесь.</div></div>}>
    <DataBoundary resetKey={`${props.node!.kind}:${props.node!.code}`} retry={() => revalidate(getDirectoryRecord.key)}>
      <Suspense fallback={<div class="ktru-loading">Загружаем…</div>}>
        <DirectoryDetail tab={props.node!.kind === "spgz" ? "spgz" : "kpgz"} code={props.node!.code} />
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
    void getDirectoryFilterMeta("spgz", value).then((meta) => {
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
        <h2>КПГЗ и СПГЗ</h2>
        <Show when={matchCount() !== null} fallback={<span>фильтр по коду или названию</span>}>
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
      <DirectoryTree
        tab="spgz"
        selectedId={props.selected?.id ?? null}
        filter={filter()}
        matchIds={matchIds()}
        expandIds={expandIds()}
        pathIds={pathIds()}
        onSelect={(node) => props.onSelect(node as DirTreeNode)}
      />
    </section>
    <section class="ktru-panel ktru-picker-detail" aria-label="Карточка позиции справочника">
      <div class="ktru-panel-head">
        <h2>Карточка</h2>
        <Show when={props.selected} fallback={<span>Выберите позицию</span>}>
          <span>{nodeCodeLabel(props.selected!)}</span>
        </Show>
      </div>
      <div class="dir-detail-scroll">
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
