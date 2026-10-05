// 部署ごとの追加タグの一覧を管理する（管理者だけに出す）。1タグ1行の表で、説明・色・並び順・アーカイブを変える。
// 説明は、取り込み状況でタグにマウスを乗せたときと、エージェントがどのタグで絞るかを選ぶときに使う。
// 付いている文書があるタグを消すときは、文書から外して消すか、アーカイブ（付けた分は残す）かを選ぶ。
import { useCallback, useEffect, useState } from 'react';
import { api, type Me, type TagColor } from './api';

interface Tag {
  name: string;
  description: string;
  color: TagColor;
  order: number;
  archived: boolean;
  version: number;
  count: number;
  updatedBy: string;
  updatedAt: string;
}

interface TagList {
  tags: Tag[];
  unlisted: { name: string; count: number }[];
  maxActive: number;
}

const COLORS: { id: TagColor; label: string }[] = [
  { id: 'gray', label: 'グレー' }, { id: 'blue', label: '青' }, { id: 'green', label: '緑' },
  { id: 'orange', label: 'オレンジ' }, { id: 'red', label: '赤' }, { id: 'purple', label: '紫' },
];

type Draft = { description: string; color: TagColor };

export function TagAdmin({ me, onChanged }: { me: Me; onChanged: () => void }) {
  const [dept, setDept] = useState(me.allDepartments[0]?.id ?? '');
  const [list, setList] = useState<TagList>();
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [confirming, setConfirming] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const [added, setAdded] = useState<Draft & { name: string }>({ name: '', description: '', color: 'gray' });

  const load = useCallback(async () => {
    try {
      setList(await api<TagList>('GET', `/api/tags?department=${encodeURIComponent(dept)}`));
      setDrafts({});
      setConfirming('');
    } catch (e) {
      setError((e as Error).message);
    }
  }, [dept]);
  useEffect(() => { load(); }, [load]);

  const run = async (done: string, call: () => Promise<unknown>) => {
    setError('');
    setMessage('');
    try {
      await call();
      setMessage(done);
      onChanged();
    } catch (e) {
      setError((e as Error).message);
    }
    await load();
  };

  const draftOf = (t: Tag): Draft => drafts[t.name] ?? { description: t.description, color: t.color };
  const edit = (t: Tag, change: Partial<Draft>) => setDrafts((d) => ({ ...d, [t.name]: { ...draftOf(t), ...change } }));
  const dirty = (t: Tag) => !!drafts[t.name]
    && (drafts[t.name].description !== t.description || drafts[t.name].color !== t.color);

  const save = (t: Tag) => run(`「${t.name}」を保存しました`, () =>
    api('PUT', '/api/tags', { department: dept, name: t.name, version: t.version, ...draftOf(t) }));
  const archive = (t: Tag, archived: boolean) => run(
    archived ? `「${t.name}」をアーカイブしました（付いている${t.count}件はそのまま）` : `「${t.name}」を使えるように戻しました`,
    () => api('PUT', '/api/tags', { department: dept, name: t.name, version: t.version, archived }));
  const remove = (t: Tag, detach: boolean) => run(
    detach ? `「${t.name}」を${t.count}件の文書から外して消しました。ナレッジへの反映には1〜2分かかります` : `「${t.name}」を消しました`,
    () => api('DELETE', `/api/tags?department=${encodeURIComponent(dept)}&name=${encodeURIComponent(t.name)}`
      + `&version=${t.version}${detach ? '&detach=1' : ''}`));
  const move = (index: number, by: number) => {
    if (!list) return;
    const names = list.tags.map((t) => t.name);
    const [name] = names.splice(index, 1);
    names.splice(index + by, 0, name);
    run('並び順を変えました', () => api('PUT', '/api/tags/order', { department: dept, names }));
  };
  const create = () => run(`「${added.name.trim()}」を足しました`, async () => {
    await api('POST', '/api/tags', { department: dept, ...added, name: added.name.trim() });
    setAdded({ name: '', description: '', color: 'gray' });
  });

  const active = list?.tags.filter((t) => !t.archived).length ?? 0;

  return (
    <section className="panel">
      <div className="row">
        <h2 style={{ flex: 1, margin: 0 }}>タグの管理</h2>
        <select value={dept} onChange={(e) => { setDept(e.target.value); setMessage(''); setError(''); }} aria-label="部署">
          {me.allDepartments.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <button onClick={load}>更新</button>
      </div>
      <p className="muted">
        取り込み状況の画面で、選んだファイルに一括で足すタグの一覧です。フォルダ名は自動でタグになるので、
        ここにはフォルダをまたぐ分類（例: 2026年度・要確認）を登録します。説明はエージェントがどのタグで絞るかを選ぶときにも使います。
      </p>
      {message && <p className="muted" role="status">{message}</p>}
      {error && <p className="err" role="alert">{error}</p>}

      {list && (
        <div className="table-wrap">
          <table className="tags">
            <thead>
              <tr>
                <th>順</th><th>タグ</th><th>色</th><th>説明</th><th className="num">付いている</th><th className="ops">操作</th>
              </tr>
            </thead>
            <tbody>
              {list.tags.map((t, i) => {
                const d = draftOf(t);
                return (
                  <tr key={t.name} className={t.archived ? 'archived' : ''}>
                    <td className="ops">
                      <button className="link" onClick={() => move(i, -1)} disabled={i === 0} aria-label={`${t.name} を上へ`}>↑</button>
                      <button className="link" onClick={() => move(i, 1)} disabled={i === list.tags.length - 1} aria-label={`${t.name} を下へ`}>↓</button>
                    </td>
                    <td title={t.updatedBy ? `最後に変えた人: ${t.updatedBy}（${new Date(t.updatedAt).toLocaleString('ja-JP')}）` : undefined}>
                      <span className={`c-${d.color}`}><span className="swatch" /></span>
                      <strong>{t.name}</strong>
                      {t.archived && <span className="muted">（アーカイブ）</span>}
                    </td>
                    <td>
                      <select value={d.color} onChange={(e) => edit(t, { color: e.target.value as TagColor })}
                        aria-label={`${t.name} の色`} className={d.color !== t.color ? 'dirty' : ''}>
                        {COLORS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                      </select>
                    </td>
                    <td>
                      <input className={`desc ${d.description !== t.description ? 'dirty' : ''}`} value={d.description}
                        maxLength={200} placeholder="何に付けるタグか" aria-label={`${t.name} の説明`}
                        onChange={(e) => edit(t, { description: e.target.value })} />
                    </td>
                    <td className="num">{t.count}件</td>
                    <td className="ops">
                      {dirty(t) && <button className="primary" onClick={() => save(t)}>保存</button>}{' '}
                      {t.archived
                        ? <button onClick={() => archive(t, false)}>戻す</button>
                        : <button onClick={() => archive(t, true)} title="これから付ける候補に出さない。付いている分は残す">アーカイブ</button>}{' '}
                      <button onClick={() => setConfirming(confirming === t.name ? '' : t.name)}>消す</button>
                      {confirming === t.name && (
                        <div className="confirm">
                          {t.count > 0 ? (
                            <>
                              {t.count}件の文書に付いています。
                              <div className="row" style={{ marginTop: 6 }}>
                                <button className="primary" onClick={() => remove(t, true)}>{t.count}件から外して消す</button>
                                {!t.archived && <button onClick={() => archive(t, true)}>消さずにアーカイブ</button>}
                                <button onClick={() => setConfirming('')}>やめる</button>
                              </div>
                            </>
                          ) : (
                            <div className="row">
                              どの文書にも付いていません。
                              <button className="primary" onClick={() => remove(t, false)}>消す</button>
                              <button onClick={() => setConfirming('')}>やめる</button>
                            </div>
                          )}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
              <tr>
                <td />
                <td>
                  <input value={added.name} maxLength={20} placeholder="新しいタグ" aria-label="新しいタグの名前"
                    onChange={(e) => setAdded({ ...added, name: e.target.value })} style={{ width: '100%', minWidth: 100 }} />
                </td>
                <td>
                  <select value={added.color} onChange={(e) => setAdded({ ...added, color: e.target.value as TagColor })} aria-label="新しいタグの色">
                    {COLORS.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
                  </select>
                </td>
                <td>
                  <input className="desc" value={added.description} maxLength={200} placeholder="何に付けるタグか（任意）"
                    aria-label="新しいタグの説明" onChange={(e) => setAdded({ ...added, description: e.target.value })} />
                </td>
                <td />
                <td className="ops">
                  <button className="primary" onClick={create} disabled={!added.name.trim() || active >= list.maxActive}>足す</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      )}
      {list && (
        <p className="muted">
          使えるタグ {active}/{list.maxActive}個。名前は20文字まで。アーカイブしたタグは一括で足す候補に出ませんが、付いている分は残り、検索にも使われます。
        </p>
      )}
      {list && list.unlisted.length > 0 && (
        <p className="err">
          一覧に無いのに文書に付いているタグがあります:{' '}
          {list.unlisted.map((u) => `${u.name}（${u.count}件）`).join('・')}。同じ名前で足し直すと、ここで管理できます。
        </p>
      )}
    </section>
  );
}
