export type StepKind = "send_text" | "click_button" | "send_dice" | "solve_caption_arithmetic";

export interface TaskDraft {
  id: string;
  name: string;
  accountNames: string[];
  chatId: string;
  scheduleKind: "fixed" | "window";
  at: string;
  start: string;
  end: string;
  successPattern: string;
  failurePattern: string;
  command: string;
  button: string;
  solveChallenge: boolean;
}

export function toTaskPayload(draft: TaskDraft) {
  if (!draft.name.trim() || !draft.successPattern.trim()) {
    throw new Error("任務名稱與成功規則為必填");
  }
  if (!draft.accountNames.length || !Number.isSafeInteger(Number(draft.chatId))) {
    throw new Error("必須選擇帳號並提供有效 Chat ID");
  }
  const steps: Array<Record<string, unknown>> = [
    { kind: "send_text", value: draft.command || "/start", match_mode: "exact", timeout_seconds: 30 },
  ];
  if (draft.button.trim()) {
    steps.push({ kind: "click_button", value: draft.button, match_mode: "exact", timeout_seconds: 30 });
  }
  if (draft.solveChallenge) {
    steps.push({ kind: "solve_caption_arithmetic", value: "", match_mode: "exact", timeout_seconds: 30 });
  }
  return {
    id: draft.id,
    name: draft.name.trim(),
    account_names: draft.accountNames,
    chat_id: Number(draft.chatId),
    schedule: draft.scheduleKind === "fixed"
      ? { kind: "fixed", at: draft.at }
      : { kind: "window", start: draft.start, end: draft.end },
    steps,
    success_patterns: [draft.successPattern.trim()],
    failure_patterns: draft.failurePattern.trim() ? [draft.failurePattern.trim()] : [],
    enabled: false,
  };
}
