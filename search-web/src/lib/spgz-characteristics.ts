import { shortUnitName } from "./characteristic-options";
import type { DraftCharacteristic } from "./types";

/** Map directory SPGZ characteristic into an editable draft row (no merge with КТРУ). */
export function draftFromSpgzCharacteristic(
  item: {
    name: string;
    code?: string;
    required?: boolean;
    value?: string;
    badges?: string[];
    options?: string[];
    unit?: string;
    okeiCode?: string;
    parts?: { text: string; unit?: string; okeiCode?: string; okeiName?: string }[];
  },
): DraftCharacteristic {
  const options = (item.options ?? []).map((option) => option.trim()).filter(Boolean);
  const unitPart = item.parts?.find((part) => part.okeiCode || part.unit || part.okeiName);
  const unit = item.unit ?? unitPart?.unit ?? shortUnitName(unitPart?.okeiName);
  const okeiCode = item.okeiCode ?? unitPart?.okeiCode;
  const required = !!item.required
    || (item.badges ?? []).some((badge) => /обязательн/i.test(badge));
  const badges = (item.badges ?? []).filter((badge) =>
    !/обязательн|качественн|количественн/i.test(badge));
  return {
    id: `spgz:${item.code || item.name}`,
    name: item.name,
    value: "",
    rangeOption: "",
    required,
    badges,
    hint: !options.length && item.value && item.value !== "—" ? `Ограничение СПГЗ: ${item.value}` : "",
    options,
    optionLabels: {},
    ranges: [],
    unit,
    okeiCode,
    numeric: (item.badges ?? []).some((badge) => /^количественная$/i.test(badge)),
    multiple: false,
    source: "spgz",
  };
}
