import { A } from "@solidjs/router";
import ClipboardList from "lucide-solid/icons/clipboard-list";
import { Show } from "solid-js";
import { directoryHref } from "~/components/catalog";
import type { VotedCode } from "~/lib/types";
import { Button } from "~/ui-kit";
import "~/routes/create-product.css";

function VoteRow(props: { label: string; tab: "okpd" | "ktru" | "kpgz" | "spgz"; vote: VotedCode | null | undefined; highlighted?: boolean; previousVote?: VotedCode | null }) {
  return <Show when={props.vote} fallback={
    <div>
      <dt>{props.label}</dt>
      <dd><span class="create-vote-name">—</span></dd>
    </div>
  }>{vote =>
    <div classList={{ "create-vote-row-refined": props.highlighted }}>
      <dt class="create-vote-label">
        {props.label}
        <Show when={props.highlighted}><span class="create-vote-refined-badge">Уточнено</span></Show>
      </dt>
      <dd class="create-vote-value">
        <div class="create-vote-value-main">
          <A class="create-vote-code" href={directoryHref(props.tab, vote().code)}>{vote().code}</A>
          <span class="create-vote-name">
            {vote().name}
            <Show when={props.tab === "ktru" && vote().isTemplate}>{" "}(Шаблон)</Show>
          </span>
        </div>
        <Show when={props.tab === "ktru" && vote().variantLabel}>
          <div class="create-vote-value-meta">
            <Show when={props.tab === "ktru" && vote().variantLabel}>
              <span class="create-vote-variant">{vote().variantLabel}</span>
            </Show>
          </div>
        </Show>
        <Show when={props.highlighted}>
          <div class="create-vote-previous">
            <span>Было:</span>
            <Show when={props.previousVote} fallback={<span>не выбрано</span>}>{previous => <>
              <A href={directoryHref(props.tab, previous().code)}>{previous().code}</A>
              <span>{previous().name}{previous().isTemplate ? " (Шаблон)" : ""}</span>
              <Show when={previous().variantLabel}><span>{previous().variantLabel}</span></Show>
            </>}</Show>
          </div>
        </Show>
      </dd>
    </div>
  }</Show>;
}

export function CategoryPairs(props: {
  okpd2: VotedCode | null;
  ktru: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
  highlightedKtru?: boolean;
  highlightedSpgz?: boolean;
  previousKtru?: VotedCode | null;
  previousSpgz?: VotedCode | null;
  onEditKtru?: () => void;
  onEditSpgz?: () => void;
  class?: string;
}) {
  return <div class={`create-vote-card ${props.class ?? ""}`.trim()}>
    <div class="create-vote-group">
      <div class="create-vote-group-head">
        <h3>ОКПД2 и КТРУ</h3>
        <Show when={props.onEditKtru}>{(edit) =>
          <Button type="button" variant="outline" size="xs" onClick={edit()}><ClipboardList size={12} />Изменить</Button>
        }</Show>
      </div>
      <dl class="create-vote-rows">
        <VoteRow label="ОКПД2" tab="okpd" vote={props.okpd2} />
        <VoteRow label="КТРУ" tab="ktru" vote={props.ktru} highlighted={props.highlightedKtru} previousVote={props.previousKtru} />
      </dl>
    </div>
    <div class="create-vote-group">
      <div class="create-vote-group-head">
        <h3>КПГЗ и СПГЗ</h3>
        <Show when={props.onEditSpgz}>{(edit) =>
          <Button type="button" variant="outline" size="xs" onClick={edit()}><ClipboardList size={12} />Изменить</Button>
        }</Show>
      </div>
      <dl class="create-vote-rows">
        <VoteRow label="КПГЗ" tab="kpgz" vote={props.kpgz} />
        <VoteRow label="СПГЗ" tab="spgz" vote={props.spgz} highlighted={props.highlightedSpgz} previousVote={props.previousSpgz} />
      </dl>
    </div>
  </div>;
}
