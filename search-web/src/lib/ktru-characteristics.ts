import { isRangeCharacteristic } from "./characteristic-options";
import { characteristicUnitsEquivalent, mapValueToOptions } from "./merge-characteristics";
import { normSpecName } from "./specialization";
import type { KtruCharacteristic } from "./server/ktru-tree";
import type { DraftCharacteristic } from "./types";

/** Preserve values only when the refined official field has the same unit identity. */
export function buildKtruDraftFields(
  record: { characteristics: KtruCharacteristic[] }, previous: DraftCharacteristic[], keepPrevious: boolean,
): DraftCharacteristic[] {
  return record.characteristics.map((item) => {
    const unit = item.unit ?? "";
    const prior = keepPrevious ? previous.find((field) =>
      (field.source === "ktru" || field.source === "both")
      && normSpecName(field.name) === normSpecName(item.name)
      && characteristicUnitsEquivalent(field, { unit, okeiCode: item.okeiCode })) : undefined;
    const options = item.options ?? [];
    const range = isRangeCharacteristic(item);
    return {
      id: item.code || item.name,
      name: item.name,
      value: prior?.value.trim()
        ? range || !options.length ? prior.value : mapValueToOptions(prior.value, options)
        : "",
      rangeOption: range && prior?.rangeOption.trim() ? mapValueToOptions(prior.rangeOption, options) : "",
      required: item.required,
      badges: item.badges.filter((badge) => !/^(обязательная|качественная|количественная)$/i.test(badge.trim())),
      hint: item.hint || "",
      options,
      optionLabels: item.optionLabels ?? {},
      ranges: range ? item.ranges : [],
      unit,
      okeiCode: item.okeiCode,
      numeric: range || /количественная/i.test(item.badges.join(" ")) || (!!unit && !options.length),
      multiple: !!item.multiple,
      source: "ktru",
      autofill: prior?.value.trim() ? prior.autofill : undefined,
      autofillDiagnostics: prior?.value.trim() ? prior.autofillDiagnostics : undefined,
    };
  });
}
