import { A } from "@solidjs/router";
import ClipboardList from "lucide-solid/icons/clipboard-list";
import { Show } from "solid-js";
import { directoryHref } from "~/components/catalog";
import type { VotedCode } from "~/lib/types";
import { Button } from "~/ui-kit";
import "~/routes/create-product.css";

function VoteRow(props: { label: string; tab: "okpd" | "ktru" | "kpgz" | "spgz"; vote: VotedCode | null | undefined }) {
  return <Show when={props.vote} fallback={
    <div>
      <dt>{props.label}</dt>
      <dd><span class="create-vote-name">—</span></dd>
    </div>
  }>{vote =>
    <div>
      <dt>{props.label}</dt>
      <dd>
        <A class="create-vote-code" href={directoryHref(props.tab, vote().code)}>{vote().code}</A>
        <span class="create-vote-name">
          {vote().name}
          <Show when={props.tab === "ktru" && vote().isTemplate}>{" "}(Шаблон)</Show>
        </span>
        <Show when={props.tab === "ktru" && vote().variantLabel}>
          <span class="create-vote-variant">{vote().variantLabel}</span>
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
        <VoteRow label="КТРУ" tab="ktru" vote={props.ktru} />
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
        <VoteRow label="СПГЗ" tab="spgz" vote={props.spgz} />
      </dl>
    </div>
  </div>;
}
