import type { VotedCode } from "./types";

export type CategorySource = "ktru" | "spgz";
export type CategorySelection = {
  okpd2: VotedCode | null;
  ktru: VotedCode | null;
  kpgz: VotedCode | null;
  spgz: VotedCode | null;
};

/** KPGZ can supply a family schema; a bare OKPD2 cannot. */
export function hasCharacteristicSource(selection: CategorySelection): boolean {
  return !!(selection.ktru || selection.spgz || selection.kpgz);
}

export function categorySourcePresentation(selection: CategorySelection): {
  primary: CategorySource;
  secondary: CategorySource;
  hasSource: boolean;
  secondarySelected: boolean;
} {
  const primary: CategorySource = selection.ktru ? "ktru"
    : selection.spgz || selection.kpgz ? "spgz" : "ktru";
  const secondary = primary === "ktru" ? "spgz" : "ktru";
  return {
    primary,
    secondary,
    hasSource: hasCharacteristicSource(selection),
    secondarySelected: secondary === "ktru" ? !!selection.ktru : !!(selection.spgz || selection.kpgz),
  };
}
