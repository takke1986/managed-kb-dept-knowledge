import { StrictMode, useCallback, useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Amplify } from 'aws-amplify';
import { I18n } from 'aws-amplify/utils';
import { Authenticator, ThemeProvider, defaultDarkModeOverride, translations, type Theme } from '@aws-amplify/ui-react';
import '@aws-amplify/ui-react/styles.css';
import '@aws-amplify/ui-react-storage/styles.css';
import './styles.css';
import { api, loadConfig, type AppConfig, type Me } from './api';
import { clearStorageCredentials, makeStorageBrowser } from './storage';
import { Chat } from './Chat';
import { Status, type Browsing } from './Status';
import { STORAGE_TEXT_JA } from './storageText';
import { TagAdmin } from './TagAdmin';
import { Notifications } from './Notifications';

I18n.putVocabularies(translations);
I18n.setLanguage('ja');

// Amplify の部品（ログイン画面・Storage Browser）も、OS の設定に合わせて暗い配色にする
const theme: Theme = { name: 'mkb', overrides: [defaultDarkModeOverride] };

// メニュー。画面を切り替えても、質問のやり取りやファイル置き場の場所が消えないよう、どれも作ったまま隠す
const PAGES = [
  { id: 'chat', label: '質問する' },
  { id: 'files', label: 'ファイル置き場' },
  { id: 'status', label: '取り込み状況' },
  { id: 'tags', label: 'タグの管理', admin: true },
] as const;
type PageId = (typeof PAGES)[number]['id'];

function pageFromHash(): PageId {
  const id = location.hash.slice(1);
  return PAGES.some((p) => p.id === id) ? (id as PageId) : 'chat';
}

function Main({ config, signOut }: { config: AppConfig; signOut: () => void }) {
  const [me, setMe] = useState<Me>();
  const [error, setError] = useState('');
  const [browsing, setBrowsing] = useState<Browsing>();
  const StorageBrowser = useMemo(() => makeStorageBrowser(config.region), [config.region]);

  const loadMe = useCallback(() => {
    api<Me>('GET', '/api/me').then(setMe).catch((e) => setError(e.message));
  }, []);
  useEffect(loadMe, [loadMe]);

  const [page, setPage] = useState<PageId>(pageFromHash);
  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const logout = () => {
    clearStorageCredentials();
    signOut();
  };

  const pages = PAGES.filter((p) => !('admin' in p) || me?.admin);
  const current = pages.some((p) => p.id === page) ? page : 'chat';

  return (
    <>
      <header>
        <h1>部署別ナレッジ（試作）</h1>
        {me && <span className="muted">{me.email}（{me.departments.map((d) => d.name).join('・') || '部署なし'}）</span>}
        <Notifications />
        <button onClick={logout}>ログアウト</button>
      </header>
      {error && <p className="err" style={{ padding: '0 16px' }}>{error}</p>}
      {me && me.departments.length === 0 && (
        <p className="err" style={{ padding: '0 16px' }}>どの部署にも属していません。管理者に連絡してください</p>
      )}
      {me && me.departments.length > 0 && (
        <div className="layout">
          <nav className="menu" aria-label="メニュー">
            {pages.map((p) => (
              <a key={p.id} href={`#${p.id}`} aria-current={current === p.id ? 'page' : undefined}>{p.label}</a>
            ))}
          </nav>
          <main>
            <div hidden={current !== 'chat'}><Chat departments={me.departments} /></div>
            <div hidden={current !== 'files'}>
              <section className="panel">
                <p className="muted">部署のフォルダに置くと、生成AIが書き起こしてナレッジに入れます。取り込まれたかは「取り込み状況」で見られます。</p>
                <StorageBrowser
                  displayText={STORAGE_TEXT_JA}
                  onValueChange={({ location }) => setBrowsing(location
                    ? { department: location.prefix.split('/')[0], path: location.path ?? '' } : undefined)}
                />
              </section>
            </div>
            <div hidden={current !== 'status'}><Status me={me} browsing={browsing} /></div>
            {me.admin && <div hidden={current !== 'tags'}><TagAdmin me={me} onChanged={loadMe} /></div>}
          </main>
        </div>
      )}
    </>
  );
}

function App() {
  const [config, setConfig] = useState<AppConfig>();
  useEffect(() => {
    loadConfig().then((c) => {
      Amplify.configure({ Auth: { Cognito: { userPoolId: c.userPoolId, userPoolClientId: c.userPoolClientId } } });
      setConfig(c);
    });
  }, []);
  if (!config) return null;
  return (
    <ThemeProvider theme={theme} colorMode="system">
      <Authenticator hideSignUp loginMechanisms={['email']}>
        {({ signOut }) => <Main config={config} signOut={() => signOut?.()} />}
      </Authenticator>
    </ThemeProvider>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
