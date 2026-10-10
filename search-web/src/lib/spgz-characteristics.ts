import { shortUnitName } from "./characteristic-options";
import type { DraftCharacteristic } from "./types";

/** Map directory SPGZ characteristic into an editable draft row (no merge with КТРУ). */
export function draftFromSpgzCharacteristic(
  item: {
    name: string;
    code?: string;
    required?: boolean;
    badges?: string[];
    options?: string[];
    parts?: { text: string; okeiName?: string }[];
  },
): DraftCharacteristic {
  const options = (item.options ?? []).map((option) => option.trim()).filter(Boolean);
  const unit = shortUnitName(item.parts?.find((part) => part.okeiName)?.okeiName);
  const required = !!item.required
    || (item.badges ?? []).some((badge) => /обязательн/i.test(badge));
  const badges = (item.badges ?? []).filter((badge) => !/обязательн/i.test(badge));
  return {
    id: `spgz:${item.code || item.name}`,
    name: item.name,
    value: "",
    rangeOption: "",
    required,
    badges,
    hint: "",
    options,
    optionLabels: {},
    ranges: [],
    unit,
    multiple: false,
    source: "spgz",
  };
}
