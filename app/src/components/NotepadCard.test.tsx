// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { NotepadCard } from "./NotepadCard";

/**
 * TK-251 AC3: the Steward's notepad has no data route (DEC-48(d) recorded
 * deferral) - it always renders the designed honest-empty state, never a
 * fabricated entry.
 */

afterEach(() => {
  cleanup();
});

describe("NotepadCard", () => {
  it("renders the designed honest-empty state", () => {
    render(<NotepadCard />);

    expect(screen.getByText("Steward's notepad")).toBeTruthy();
    expect(screen.getByText("Notepad is empty.")).toBeTruthy();
  });
});
