// 画面から呼ぶ API。アクセストークンは Amplify のログインから取る（必要なら自動で更新される）。
import { fetchAuthSession } from 'aws-amplify/auth';

export interface AppConfig {
  region: string;
  userPoolId: string;
  userPoolClientId: string;
  apiUrl: string;
}

let config: AppConfig;

export async function loadConfig(): Promise<AppConfig> {
  config = await (await fetch('/config.json')).json();
  return config;
}

export async function api<T>(method: string, path: string, body?: unknown): Promise<T> {
  const { tokens } = await fetchAuthSession();
  const token = tokens?.accessToken?.toString();
  if (!token) throw new Error('ログインし直してください');
  // API は CloudFront（OAC）の後ろにある。Authorization は OAC の署名が使うので、トークンは x-app-token で送る。
  // 本文があるときは、その SHA-256 を x-amz-content-sha256 で添える（OAC が POST の本文を署名するのに要る）
  const text = body === undefined ? undefined : JSON.stringify(body);
  const headers: Record<string, string> = { 'x-app-token': token };
  if (text !== undefined) {
    headers['Content-Type'] = 'application/json';
    headers['x-amz-content-sha256'] = await sha256Hex(text);
  }
  const res = await fetch(config.apiUrl.replace(/\/$/, '') + path, { method, headers, body: text });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `失敗しました（${res.status}）`);
  return data as T;
}

export type TagColor = 'gray' | 'blue' | 'green' | 'orange' | 'red' | 'purple';

export interface TagDef {
  name: string;
  description: string;
  color: TagColor;
  archived: boolean;
}

export interface Department {
  id: string;
  name: string;
  tags: string[]; // これから付けられるタグ（アーカイブを除く）
  tagDefs: TagDef[]; // アーカイブも含めた、説明と色
}

export interface Me {
  email: string;
  admin: boolean;
  departments: Department[];
  allDepartments: Department[];
}

export interface FileStatus {
  key: string;
  fileName: string;
  folder: string;
  uploadedAt: string;
  status: 'converted' | 'converting' | 'failed' | 'stalled' | 'skipped' | 'blocked';
  reason: string;
  tags: string[];
  folderTags: string[];
  extraTags: string[];
}

export interface Source {
  key: string;
  fileName: string;
  folder: string;
  department: string;
  tags: string[];
  url: string;
  pages: number[];
  pageKind: 'page' | 'slide' | '';
}

/** 元ファイルを開く。PDF はページを指定すると、ブラウザの PDF の表示でそのページを開く。 */
export async function openOriginal(key: string, page?: number) {
  const win = window.open('', '_blank');
  try {
    const { url: signed } = await api<{ url: string }>('GET', `/api/download-url?key=${encodeURIComponent(key)}`);
    const url = page ? `${signed}#page=${page}` : signed;
    if (win) win.location.href = url; else window.location.href = url;
  } catch (e) {
    win?.close();
    alert((e as Error).message);
  }
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}
