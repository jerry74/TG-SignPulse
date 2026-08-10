import { describe, expect, it } from "vitest";
import { toTaskPayload } from "./task";

describe("task payload", () => {
  it("keeps imported-style tasks disabled until canary", () => {
    const payload = toTaskPayload({
      id: "task-1",
      name: "Daily",
      accountNames: ["primary"],
      chatId: "10001",
      scheduleKind: "fixed",
      at: "08:00",
      start: "08:00",
      end: "19:00",
      successPattern: "签到成功",
      failurePattern: "签到失败",
      command: "/start",
      button: "签到",
      solveChallenge: true,
    });

    expect(payload.enabled).toBe(false);
    expect(payload.steps.map((step) => step.kind)).toEqual([
      "send_text",
      "click_button",
      "solve_caption_arithmetic",
    ]);
  });
});
