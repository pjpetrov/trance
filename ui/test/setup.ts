import "@testing-library/jest-dom/vitest";

/** jsdom implements no layout, so anything that scrolls is missing. These are
 *  gaps in the test environment rather than in the code — a component that
 *  follows the tail of a live console is doing the right thing, and should not
 *  have to check whether the DOM it is running in supports scrolling. */
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

/** Nothing in jsdom has a size, so "is the console scrolled to the bottom?"
 *  answers 0 - 0 - 0 = 0 and reads as yes. That is the behaviour under test. */
