import assert from "node:assert/strict";
import test from "node:test";
import { categorySourcePresentation, hasCharacteristicSource, type CategorySelection } from "../src/lib/category-source";
import type { VotedCode } from "../src/lib/types";

const vote: VotedCode = { code: "P", name: "Позиция", score: 1, source: "classifier" };
const empty: CategorySelection = { okpd2: null, ktru: null, kpgz: null, spgz: null };

test("one KTRU is enough; the unselected supplier catalog is optional", () => {
  assert.deepEqual(categorySourcePresentation({ ...empty, okpd2: vote, ktru: vote }), {
    primary: "ktru", secondary: "spgz", hasSource: true, secondarySelected: false,
  });
});

test("one SPGZ is enough and is shown before the unselected KTRU", () => {
  assert.deepEqual(categorySourcePresentation({ ...empty, spgz: vote, kpgz: vote }), {
    primary: "spgz", secondary: "ktru", hasSource: true, secondarySelected: false,
  });
});

test("KPGZ family supports characteristics without forcing an unrelated KTRU", () => {
  assert.equal(hasCharacteristicSource({ ...empty, kpgz: vote }), true);
  assert.equal(categorySourcePresentation({ ...empty, kpgz: vote }).primary, "spgz");
});

test("bare OKPD2 and empty selections do not claim a characteristic source", () => {
  for (const selection of [empty, { ...empty, okpd2: vote }]) {
    assert.equal(hasCharacteristicSource(selection), false);
    assert.equal(categorySourcePresentation(selection).hasSource, false);
  }
});

test("unresolved optional catalog does not turn the selected source into an incomplete step", () => {
  const selection = {
    ...empty, ktru: vote,
    kpgzSpgzDecision: { needsChoice: true, reason: "no_suitable_category", alternatives: [] },
  };
  assert.equal(hasCharacteristicSource(selection), true);
  assert.deepEqual(categorySourcePresentation(selection), {
    primary: "ktru", secondary: "spgz", hasSource: true, secondarySelected: false,
  });
});

test("both selected sources remain visible without requiring another choice", () => {
  assert.deepEqual(categorySourcePresentation({ okpd2: vote, ktru: vote, kpgz: vote, spgz: vote }), {
    primary: "ktru", secondary: "spgz", hasSource: true, secondarySelected: true,
  });
});
