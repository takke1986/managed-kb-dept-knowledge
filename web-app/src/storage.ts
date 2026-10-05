// Storage Browser を、この試作の Cognito と API につなぐ（独自の認証）。
//
// 見せるフォルダ（listLocations）と、そのフォルダ用の一時的な認証情報（getLocationCredentials）を
// API からもらう。API は利用者の部署を JWT から決め、部署のフォルダだけに絞った認証情報を返す。
// ほかの部署のフォルダは一覧に出ず、認証情報も発行されない。
import { createElement } from 'react';
import { createStorageBrowser } from '@aws-amplify/ui-react-storage/browser';
import { api } from './api';

interface LocationItem {
  id: string;
  bucket: string;
  prefix: string;
  type: 'PREFIX';
  permissions: ('get' | 'list' | 'write' | 'delete')[];
}

interface CredentialsResponse {
  scope: string;
  credentials: { accessKeyId: string; secretAccessKey: string; sessionToken: string; expiration: string };
}

let onAuthStateChange: (() => void) | undefined;
let bucketName = '';
const departmentNames: Record<string, string> = {};

/** 部署のフォルダ（sales/確認用/）を、部署名で表す（営業部 / 確認用）。 */
export function folderLabel(key: string): string {
  const [dept, ...rest] = key.split('/').filter(Boolean);
  return [departmentNames[dept] ?? dept, ...rest].filter(Boolean).join(' / ') || 'ファイル置き場';
}

interface NavItem { isCurrent?: boolean; name?: string; onNavigate?: () => void }

/** パンくずリスト。バケット名を出さず、部署のフォルダ名（sales）は部署名（営業部）で出す。 */
function Navigation({ items }: { items: NavItem[] }) {
  const label = (name = '') => {
    if (name === 'Home') return 'トップ';
    const rest = bucketName && name.startsWith(bucketName) ? name.slice(bucketName.length).replace(/^\//, '') : name;
    const [dept, ...sub] = rest.split('/');
    return [departmentNames[dept] ?? dept, ...sub].filter(Boolean).join(' / ') || 'ファイル置き場';
  };
  return createElement('nav', { className: 'sb-nav', 'aria-label': '場所' },
    items.map((item, i) => createElement('span', { key: i },
      i > 0 ? ' / ' : '',
      item.isCurrent
        ? createElement('strong', null, label(item.name))
        : createElement('a', { href: '#', onClick: (e: Event) => { e.preventDefault(); item.onNavigate?.(); } }, label(item.name)),
    )));
}

/** ログアウトしたときに呼ぶ。Storage Browser が持っている認証情報を捨てさせる。 */
export function clearStorageCredentials() {
  onAuthStateChange?.();
}

export function makeStorageBrowser(region: string) {
  return createStorageBrowser({
    components: { Navigation },
    config: {
      region,
      registerAuthListener: (onStateChange) => {
        onAuthStateChange = onStateChange;
      },
      listLocations: async () => {
        const { items } = await api<{ items: (LocationItem & { name: string })[] }>('GET', '/api/storage/locations');
        for (const item of items) {
          bucketName = item.bucket;
          departmentNames[item.id] = item.name;
        }
        return { items, nextToken: undefined };
      },
      getLocationCredentials: async ({ scope, permissions }) => {
        const r = await api<CredentialsResponse>('POST', '/api/storage/credentials', { scope, permissions });
        return {
          scope: r.scope,
          credentials: { ...r.credentials, expiration: new Date(r.credentials.expiration) },
        };
      },
    },
  }).StorageBrowser;
}
