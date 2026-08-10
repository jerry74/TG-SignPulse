<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { api, hasToken, setToken } from "./api";
import { describeRunEvent, toTaskPayload, type TaskDraft } from "./task";

type View = "tasks" | "accounts" | "runs";
const authenticated = ref(hasToken());
const view = ref<View>("tasks");
const username = ref("admin");
const password = ref("");
const error = ref("");
const busy = ref(false);
const accounts = ref<any[]>([]);
const tasks = ref<any[]>([]);
const runs = ref<any[]>([]);
const navLabels: Record<View, string> = { tasks: "任務", accounts: "帳號", runs: "紀錄" };
const loginAccount = ref("");
const loginPhone = ref("");
const loginId = ref("");
const loginCode = ref("");
const loginPassword = ref("");
const loginStatus = ref("");
const draft = reactive<TaskDraft>({
  id: crypto.randomUUID(), name: "", accountNames: [], chatId: "", scheduleKind: "fixed",
  at: "08:00", start: "08:00", end: "19:00",
  successPattern: "签到成功|已签到", failurePattern: "签到失败|操作失败",
  command: "/start", button: "签到", solveChallenge: true,
});

async function login() {
  busy.value = true; error.value = "";
  try {
    const result = await api<{access_token: string}>("/auth/login", {
      method: "POST", body: JSON.stringify({ username: username.value, password: password.value }),
    });
    setToken(result.access_token); authenticated.value = true; await refresh();
  } catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function refresh() {
  if (!authenticated.value) return;
  try {
    [accounts.value, tasks.value, runs.value] = await Promise.all([
      api<any[]>("/accounts"), api<any[]>("/tasks"), api<any[]>("/runs"),
    ]);
    if (!draft.accountNames.length && accounts.value[0]) draft.accountNames = [accounts.value[0].name];
  } catch (e) { error.value = String(e); }
}

async function createTask() {
  busy.value = true; error.value = "";
  try {
    await api("/tasks", { method: "POST", body: JSON.stringify(toTaskPayload(draft)) });
    draft.id = crypto.randomUUID(); draft.name = ""; await refresh();
  } catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function runTask(task: any) {
  const account = task.account_names[0];
  if (!confirm(`立即使用 ${account} 執行 ${task.name}？`)) return;
  busy.value = true; error.value = "";
  try { await api(`/tasks/${task.id}/run?account_name=${encodeURIComponent(account)}`, { method: "POST" }); await refresh(); }
  catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function setTaskEnabled(task: any, enabled: boolean) {
  if (enabled && !confirm(`確定啟用 ${task.name} 的自動排程？`)) return;
  busy.value = true; error.value = "";
  try {
    await api(`/tasks/${task.id}`, { method: "PUT", body: JSON.stringify({ ...task, enabled }) });
    await refresh();
  } catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function startAccountLogin() {
  busy.value = true; error.value = "";
  try {
    const result = await api<{login_id:string;status:string}>("/accounts/login/start", {
      method: "POST", body: JSON.stringify({ account_name: loginAccount.value, phone_number: loginPhone.value }),
    });
    loginId.value = result.login_id; loginStatus.value = result.status;
  } catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function submitLogin(kind: "code" | "password") {
  busy.value = true; error.value = "";
  try {
    const body = kind === "code" ? { code: loginCode.value } : { password: loginPassword.value };
    const result = await api<{status:string}>(`/accounts/login/${loginId.value}/${kind}`, { method: "POST", body: JSON.stringify(body) });
    loginStatus.value = result.status;
    if (result.status === "complete") { loginId.value = ""; await refresh(); }
  } catch (e) { error.value = String(e); } finally { busy.value = false; }
}

async function probe(account: string) {
  if (!confirm("將在 Saved Messages 傳送並刪除一則 TGSP_TEST 訊息，確定執行？")) return;
  busy.value = true; error.value = "";
  try { await api(`/accounts/${encodeURIComponent(account)}/probe-saved-messages`, { method: "POST" }); alert("Saved Messages 往返成功"); }
  catch (e) { error.value = String(e); } finally { busy.value = false; }
}

onMounted(refresh);
</script>

<template>
  <main v-if="!authenticated" class="login-shell">
    <form class="panel login" @submit.prevent="login">
      <p class="eyebrow">TG-SignPlus v3</p><h1>可靠的每日簽到</h1>
      <label>管理員<input v-model="username" autocomplete="username" /></label>
      <label>密碼<input v-model="password" type="password" autocomplete="current-password" /></label>
      <p v-if="error" class="error">{{ error }}</p>
      <button :disabled="busy">登入</button>
    </form>
  </main>
  <main v-else class="shell">
    <header><div><p class="eyebrow">TG-SignPlus</p><h1>簽到控制台</h1></div><button class="ghost" @click="refresh">重新整理</button></header>
    <nav><button v-for="item in (['tasks','accounts','runs'] as View[])" :key="item" :class="{active:view===item}" @click="view=item">{{ navLabels[item] }}</button></nav>
    <p v-if="error" class="error">{{ error }}</p>
    <section v-if="view==='tasks'" class="grid">
      <article class="panel"><h2>新增停用任務</h2>
        <label>名稱<input v-model="draft.name" /></label><label>帳號<select v-model="draft.accountNames" multiple><option v-for="a in accounts" :value="a.name">{{ a.name }}</option></select></label>
        <label>Chat ID<input v-model="draft.chatId" inputmode="numeric" /></label>
        <label>排程<select v-model="draft.scheduleKind"><option value="fixed">固定時間</option><option value="window">隨機時段</option></select></label>
        <label v-if="draft.scheduleKind==='fixed'">時間<input v-model="draft.at" type="time" /></label>
        <template v-else><label>開始<input v-model="draft.start" type="time" /></label><label>結束（可跨午夜）<input v-model="draft.end" type="time" /></label></template>
        <label>指令<input v-model="draft.command" /></label><label>按鈕文字<input v-model="draft.button" /></label>
        <label>成功規則<input v-model="draft.successPattern" /></label><label>失敗規則<input v-model="draft.failurePattern" /></label>
        <label class="check"><input v-model="draft.solveChallenge" type="checkbox" />啟用 caption 算式挑戰</label>
        <button :disabled="busy" @click="createTask">建立任務</button>
      </article>
      <article class="panel"><h2>任務</h2><div v-if="!tasks.length" class="empty">尚無任務</div>
        <div v-for="task in tasks" :key="task.id" class="row"><div><strong>{{ task.name }}</strong><small>{{ task.account_names.join(', ') }} · {{ task.schedule.kind }} {{ task.schedule.at || `${task.schedule.start}-${task.schedule.end}` }} · {{ task.enabled ? '啟用' : '停用' }}</small></div><div class="actions"><button class="ghost" :disabled="busy" @click="setTaskEnabled(task,!task.enabled)">{{ task.enabled ? '停用' : '啟用' }}</button><button class="danger" :disabled="busy" @click="runTask(task)">立即執行</button></div></div>
      </article>
    </section>
    <section v-else-if="view==='accounts'" class="grid"><article class="panel"><h2>登入 Telegram 帳號</h2><label>帳號名稱<input v-model="loginAccount" /></label><label>手機號碼<input v-model="loginPhone" type="tel" /></label><button v-if="!loginId" :disabled="busy" @click="startAccountLogin">傳送驗證碼</button><template v-else-if="loginStatus==='code_required'"><label>驗證碼<input v-model="loginCode" inputmode="numeric" /></label><button :disabled="busy" @click="submitLogin('code')">驗證</button></template><template v-else-if="loginStatus==='password_required'"><label>2FA 密碼<input v-model="loginPassword" type="password" /></label><button :disabled="busy" @click="submitLogin('password')">完成登入</button></template></article><article class="panel"><h2>帳號</h2><div v-for="account in accounts" :key="account.name" class="row"><div><strong>{{ account.name }}</strong><small>{{ account.status }}</small></div><button :disabled="busy" @click="probe(account.name)">Saved Messages 測試</button></div></article></section>
    <section v-else class="panel"><h2>執行紀錄</h2><div v-for="run in runs" :key="run.id" class="row run-row"><div><strong>{{ run.state }} · {{ run.code || 'pending' }}</strong><small>{{ run.account_name }} · {{ run.scheduled_for }} · {{ run.trigger }}</small><ol v-if="run.events?.length" class="events"><li v-for="(event,index) in run.events" :key="index">{{ describeRunEvent(event) }}</li></ol></div></div></section>
  </main>
</template>
