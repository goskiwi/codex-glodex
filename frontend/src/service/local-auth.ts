const LOCAL_AUTH_PREFIX = '/api/v1/web-console/local-auth';

export const LOCAL_SESSION_TOKEN = 'glodex-local-session';

export interface LocalAuthStatus {
  schemaVersion: 'glodex.local-auth.v1';
  username: string;
  expiresAt: string;
}

async function parseStatus(response: Response): Promise<LocalAuthStatus> {
  if (!response.ok) {
    throw new Error(response.status === 401 ? 'LOCAL_AUTH_REQUIRED' : 'LOCAL_AUTH_FAILED');
  }
  const value: unknown = await response.json();
  if (
    typeof value !== 'object' ||
    value === null ||
    (value as Partial<LocalAuthStatus>).schemaVersion !== 'glodex.local-auth.v1' ||
    typeof (value as Partial<LocalAuthStatus>).username !== 'string' ||
    typeof (value as Partial<LocalAuthStatus>).expiresAt !== 'string'
  ) {
    throw new Error('LOCAL_AUTH_RESPONSE_INVALID');
  }
  return value as LocalAuthStatus;
}

async function credentialsRequest(path: '/login' | '/register', username: string, password: string) {
  const response = await fetch(`${LOCAL_AUTH_PREFIX}${path}`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ username, password })
  });
  return parseStatus(response);
}

export function loginLocalAccount(username: string, password: string) {
  return credentialsRequest('/login', username, password);
}

export function registerLocalAccount(username: string, password: string) {
  return credentialsRequest('/register', username, password);
}

export async function currentLocalAccount() {
  const response = await fetch(`${LOCAL_AUTH_PREFIX}/me`, { credentials: 'same-origin' });
  return parseStatus(response);
}

export async function logoutLocalAccount() {
  const response = await fetch(`${LOCAL_AUTH_PREFIX}/logout`, {
    method: 'POST',
    credentials: 'same-origin'
  });
  if (!response.ok && response.status !== 401) throw new Error('LOCAL_AUTH_LOGOUT_FAILED');
}
