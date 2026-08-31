/** The context gauge and the "waiting for the model" line.
 *
 * The gauge counts against the *window* — the number on the model's box —
 * because that is the number people recognize; "46.6k of what?" was the
 * confusion a budget-based denominator caused. The runner still trims and
 * compacts against the budget (the window less the reply room), so that
 * boundary is a notch on the bar and the colour warms against it: the gauge
 * must never look calm while the trimmer is working.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ContextGauge } from "@/components/ContextGauge";

const usage = (over = {}) => ({
  tokens: 12_000, window: 64_000, budget: 58_904, reserved: 4_096,
  percent: 20, estimated: false, ...over,
});

describe("the context gauge", () => {
  it("shows the share of the window and the counts behind it", () => {
    render(<ContextGauge context={usage()} />);
    expect(screen.getByText("19%")).toBeInTheDocument();      // 12k of 64k
    expect(screen.getByText("12.0k/64.0k")).toBeInTheDocument();
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
    // The reading is of the window (70%), the colour is of the budget (76%):
    // trimming starts at the notch, not at the end of the bar.
    const warm = render(<ContextGauge context={usage({ tokens: 45_000 })} />);
    expect(warm.getByText("70%")).toHaveClass("text-warn");
    warm.unmount();

    const hot = render(<ContextGauge context={usage({ tokens: 56_000 })} />);
    expect(hot.getByText("88%")).toHaveClass("text-err");
  });

  it("never reads over 100% when the budget is already blown", () => {
    render(<ContextGauge context={usage({ tokens: 90_000 })} />);
    expect(screen.getByText("100%")).toBeInTheDocument();
  });
});
