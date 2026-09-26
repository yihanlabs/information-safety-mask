const fragment = new URLSearchParams(window.location.hash.slice(1));
const incomingToken = fragment.get('token');
if (incomingToken) {
  sessionStorage.setItem('safety-mask-session', incomingToken);
  history.replaceState(null, '', window.location.pathname);
}
const token = incomingToken || sessionStorage.getItem('safety-mask-session') || '';
export async function request(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers);
  headers.set('X-Session-Token', token);
  const response = await fetch('/api/' + path, { ...init, headers, cache: 'no-store' });
  if (!response.ok) {
    const data = await response.json().catch(() => null);
    throw new Error(data?.detail || '本地服务暂时无法响应，请重试');
  }
  return response;
}
export async function json<T>(path: string, init: RequestInit = {}): Promise<T> {
  return (await request(path, init)).json();
}
export function body(value: unknown): RequestInit {
  return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) };
}
