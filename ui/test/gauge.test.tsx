/** The context gauge and the "waiting for the model" line.
 *
 * The gauge is measured against the *budget* — the window less the room
 * reserved for the reply — because that is what the runner trims against. A
 * gauge that disagreed with the trimmer would be worse than none.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ContextGauge } from "@/components/ContextGauge";

const usage = (over = {}) => ({
  tokens: 12_000, window: 64_000, budget: 58_904, reserved: 4_096,
  percent: 20, estimated: false, ...over,
});

describe("the context gauge", () => {
  it("shows the share of the budget and the counts behind it", () => {
    render(<ContextGauge context={usage()} />);
    expect(screen.getByText("20%")).toBeInTheDocument();
    expect(screen.getByText("12.0k/58.9k")).toBeInTheDocument();
  });

  it("marks an estimate as one", () => {
    // Whether the number is reported or guessed changes what to do about it.
    render(<ContextGauge context={usage({ estimated: true })} />);
    expect(screen.getByText(/~$/)).toBeInTheDocument();
    expect(screen.getByTitle(/estimated/)).toBeInTheDocument();
  });

  it("warns before the window is a problem, not after", () => {
    // A step that fills the window mid-way starts dropping what it already
    // read, so the colour has to change while there is still time to act.
    const warm = render(<ContextGauge context={usage({ tokens: 45_000 })} />);
    expect(warm.getByText("76%")).toHaveClass("text-warn");
    warm.unmount();

    const hot = render(<ContextGauge context={usage({ tokens: 56_000 })} />);
    expect(hot.getByText("95%")).toHaveClass("text-err");
  });

  it("never reads over 100% when the budget is already blown", () => {
    render(<ContextGauge context={usage({ tokens: 90_000 })} />);
    expect(screen.getByText("100%")).toBeInTheDocument();
  });
});
