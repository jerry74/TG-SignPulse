let token = sessionStorage.getItem("tgsp-token") || "";

export function setToken(value: string) {
  token = value;
  sessionStorage.setItem("tgsp-token", value);
}

export function hasToken() {
  return Boolean(token);
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...(init.headers || {}),
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}
