# Submission Notes

* **Time spent (hours):** 3–4 hours

* **AI assistants used:** ChatGPT / Codex

* **What I designed myself vs AI-assisted:**
  I used AI to understand the existing codebase and requirements and to generate parts of the implementation and tests. I manually reviewed and integrated the generated code, understood how the different parts worked together, ran and debugged the tests, and verified the required scenarios and MCP client/server communication end to end.

* **Representative prompts / requests to the assistant:**

  * Understand the existing codebase, important files, and assignment requirements before making changes.
  * Map the requirements to the existing `MiniEda` API and suggest a simple implementation.
  * Generate implementation and tests while keeping the original Orion design file safe.
  * Explain the generated code and help verify the complete solution end to end.

* **Surprises observed while running Orion:**
  While working through the required scenarios, I confirmed several important behaviors in the supplied design: `L7` references the missing ID `sram_l2`, `L_loop` is a self-loop, basic substring search for `r0` can also match `ddr0`, and `persist: false` changes the live in-memory model without changing the JSON file.

* **Known limits of my solution:**

  * Mainly designed for local use.
  * Performs basic SoC checks rather than advanced design analysis.
  * Implements the MCP features required for this assignment rather than the complete MCP feature set.
