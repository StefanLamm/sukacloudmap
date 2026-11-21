export type Account = {id: string; username: string; display_name: string; is_admin: boolean; active: boolean; revision: number; created_at: string; updated_at: string; last_login_at: string | null};
export type SharedRecord = {id: string; title: string; content: string; revision: number; task_date:string; status:'PENDING'|'COMPLETED'|'OVERDUE'; completed_at:string|null; completed_by:string|null; created_by: string; updated_by: string; created_at: string; updated_at: string};
export type AuditEvent = {id: string; actor: string; action: string; summary: string; occurred_at: string};
let csrfToken = '';
export function setCsrf(token: string) { csrfToken = token; }

export class ApiError extends Error {
  constructor(public status: number, message: string, public code: string, public requestId?: string, public details?: unknown) { super(message); }
}

export async function api<T>(path: string, method = 'GET', body?: unknown, signal?: AbortSignal): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = body instanceof Blob ? 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' : 'application/json';
  if (method !== 'GET') headers['X-CSRF-Token'] = csrfToken;
  let response: Response;
  try {
    response = await fetch('/api' + path, {method, headers, credentials: 'same-origin', body: body instanceof Blob ? body : body === undefined ? undefined : JSON.stringify(body), signal});
  } catch {
    throw new ApiError(0, '连接服务失败，请检查网络后重试。', 'NETWORK_ERROR');
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    if (response.status === 401 && path !== '/auth/login') window.dispatchEvent(new Event('suka-session-expired'));
    throw new ApiError(response.status, data.message || '操作未完成，请稍后重试。', data.code || 'REQUEST_FAILED', data.request_id, data.details);
  }
  return response.status === 204 ? undefined as T : response.json();
}

